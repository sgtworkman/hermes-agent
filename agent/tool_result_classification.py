"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses atomic replace only.
    fcntl = None

from hermes_constants import get_hermes_home


OUTCOME_SUCCESS = "success"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
OUTCOME_EMPTY = "empty_result"
OUTCOME_MALFORMED = "malformed_result"
OUTCOME_BLOCKED_RISK = "blocked_risk"
REPAIR_RECEIPT_RELATIVE_PATH = Path("repair-receipts") / "tool-outcomes.jsonl"
REPAIR_RECEIPT_MAX_BYTES = 64 * 1024


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


def classify_tool_outcome(
    result: Any = None,
    *,
    tool_name: str = "",
    error: Any = None,
    timed_out: bool = False,
    blocked_risk: bool = False,
    expected_shape: type | tuple[type, ...] | None = None,
) -> dict[str, Any]:
    """Classify a tool result without retaining its payload.

    Precedence is safety-first: blocked risk, timeout, explicit error/nonzero,
    shape failure, empty result, then success. ``expected_shape`` is optional
    so existing callers can classify opaque tool payloads without guessing.
    """
    digest = result_digest(result)
    observed = _observed_result(result, digest)
    malformed = expected_shape is not None and not isinstance(result, expected_shape)
    side_effect_capable = tool_may_have_side_effect(tool_name)
    # A timeout exception is just as unsafe to replay as the explicit marker
    # synthesized by the executor: the callee may still own an in-flight
    # request when control returns to this layer.
    timed_out = bool(timed_out or isinstance(error, TimeoutError))
    if blocked_risk:
        status, retryable, risk = OUTCOME_BLOCKED_RISK, False, "high"
    elif timed_out:
        status, retryable, risk = OUTCOME_TIMEOUT, False, "unknown" if side_effect_capable else "none"
    elif error is not None or _nonzero_result(result):
        # Only a completed failure from a known no-effect tool is eligible for
        # the executor's single bounded retry. Unknown/plugin/MCP tools remain
        # effect-capable by default, even when their payload is serialized.
        status, retryable, risk = OUTCOME_ERROR, not side_effect_capable, "high" if side_effect_capable else "none"
    elif malformed:
        status, retryable, risk = OUTCOME_MALFORMED, False, "unknown"
    elif _is_empty(result):
        status, retryable, risk = OUTCOME_EMPTY, not side_effect_capable, "none" if not side_effect_capable else "unknown"
    else:
        status, retryable, risk = OUTCOME_SUCCESS, False, "none" if not side_effect_capable else "unknown"
    return {
        "status": status,
        "retryable": retryable,
        "side_effect_risk": risk,
        "result_digest": digest,
        "observed_result": observed,
    }


def classify_tool_result(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility name for the pure outcome classifier."""
    return classify_tool_outcome(*args, **kwargs)


def result_digest(result: Any) -> str:
    """Return a deterministic SHA-256 digest, never the raw result."""
    try:
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    except Exception:
        encoded = repr(result).encode("utf-8", "backslashreplace")
    return hashlib.sha256(encoded).hexdigest()


def _safe_digest(value: Any) -> str:
    """Accept only a canonical digest string; hash every other value."""
    if isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value.lower()
    ):
        return value.lower()
    return result_digest(value)


def _is_empty(result: Any) -> bool:
    return result is None or result == "" or result == b"" or result == [] or result == {}


def _nonzero_result(result: Any) -> bool:
    """Recognize structured failures in native mappings and JSON envelopes."""
    if isinstance(result, str):
        try:
            decoded = json.loads(result)
        except (TypeError, ValueError):
            return False
        # Registry tools commonly serialize their response envelope before it
        # reaches the executor. Inspect only a decoded mapping; ordinary text
        # (including JSON arrays/scalars) remains a successful opaque payload.
        result = decoded
    if not isinstance(result, Mapping):
        return False
    if result.get("error") or result.get("ok") is False:
        return True
    for key in ("return_code", "exit_code"):
        value = result.get(key)
        if type(value) is int and value != 0:
            return True
    status_code = result.get("status_code")
    return type(status_code) is int and status_code >= 400


def _observed_result(result: Any, digest: str) -> dict[str, Any]:
    descriptor: dict[str, Any] = {"type": type(result).__name__, "digest": digest}
    try:
        descriptor["size"] = len(result)  # type: ignore[arg-type]
    except (TypeError, AttributeError):
        descriptor["size"] = None
    if isinstance(result, Mapping):
        descriptor["keys"] = sorted(str(key) for key in result.keys())[:32]
    return descriptor


def _receipt_text(value: Any, *, limit: int = 256) -> str:
    """Return a bounded, always-redacted receipt string."""
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(str(value), force=True)
    except Exception:
        # Receipts are a safety boundary. If the general redactor is
        # unavailable during an import failure, do not fall back to emitting
        # an untrusted identifier or message verbatim.
        text = "[REDACTED]"
    return text[:limit]


def _safe_receipt_classification(classification: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only bounded, non-payload fields in a persisted classification."""
    status = classification.get("status")
    risk = classification.get("side_effect_risk")
    digest = classification.get("result_digest")
    if not isinstance(status, str) or status not in {
        OUTCOME_SUCCESS,
        OUTCOME_TIMEOUT,
        OUTCOME_ERROR,
        OUTCOME_EMPTY,
        OUTCOME_MALFORMED,
        OUTCOME_BLOCKED_RISK,
    }:
        status = "unknown"
    if not isinstance(risk, str) or risk not in {"none", "unknown", "high"}:
        risk = "unknown"
    digest = _safe_digest(digest)
    return {
        "status": status,
        "retryable": classification.get("retryable") is True,
        "side_effect_risk": risk,
        "result_digest": digest,
    }


def _safe_observed_result(observed_result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Persist only the non-sensitive observation projection, never payloads."""
    if not isinstance(observed_result, Mapping):
        return {}
    safe: dict[str, Any] = {}
    if "type" in observed_result:
        safe["type"] = _receipt_text(observed_result["type"], limit=64)
    if "digest" in observed_result:
        safe["digest"] = _safe_digest(observed_result["digest"])
    if "size" in observed_result:
        size = observed_result["size"]
        safe["size"] = size if isinstance(size, int) and not isinstance(size, bool) else None
    keys = observed_result.get("keys")
    if isinstance(keys, (list, tuple)):
        safe["keys"] = [_receipt_text(key, limit=64) for key in list(keys)[:32]]
    return safe


def write_repair_receipt(
    *,
    mission_id: str,
    turn_id: str | int,
    tool_call_id: str,
    classification: Mapping[str, Any],
    repair_disposition: str,
    observed_result: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
    profile_home: str | os.PathLike[str] | None = None,
    max_bytes: int = REPAIR_RECEIPT_MAX_BYTES,
) -> Path:
    """Append a redacted, bounded receipt under one profile home atomically."""
    root = Path(profile_home).expanduser() if profile_home else get_hermes_home()
    path = root / REPAIR_RECEIPT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "mission_id": _receipt_text(mission_id),
        "turn_id": _receipt_text(turn_id),
        "tool_call_id": _receipt_text(tool_call_id),
        "failure_classification": _safe_receipt_classification(classification),
        "repair_disposition": _receipt_text(repair_disposition),
        "observed_result": _safe_observed_result(
            observed_result
            if observed_result is not None
            else classification.get("observed_result")
        ),
        "timestamp_utc": _receipt_text(
            timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            limit=64,
        ),
    }
    line = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    # A single oversized observation must not make the bounded file empty.
    # The digest remains in failure_classification; the observed projection can
    # safely lose its duplicate digest under a tight caller-supplied cap.
    if len(line) > max(1, int(max_bytes)):
        receipt["observed_result"] = {}
        receipt["failure_classification"]["result_digest"] = str(
            receipt["failure_classification"].get("result_digest", "")
        )[:32]
        line = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(line) > max(1, int(max_bytes)):
        raise ValueError("max_bytes is too small for a repair receipt")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = path.read_bytes() if path.exists() else b""
        data = (existing + line)[-max(1, int(max_bytes)):]
        # Keep complete JSONL records after byte trimming.
        if existing + line and len(data) and data[:1] != b"{":
            data = data[data.find(b"\n") + 1:]
        # TemporaryDirectory owns the transient inode lifecycle, including
        # cleanup when writing or replacement fails.  The directory remains
        # valid until after the atomic replacement, and its context manager
        # tolerates the successfully replaced file already being absent.
        with tempfile.TemporaryDirectory(prefix=f".{path.name}.", dir=path.parent) as temp_dir:
            temp_path = Path(temp_dir) / path.name
            with temp_path.open("wb") as temp:
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_path, path)
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return path


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
