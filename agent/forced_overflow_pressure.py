"""Deterministic request shrinking after a provider proves context overflow."""

from __future__ import annotations

from typing import Any, Dict, List

from agent.model_metadata import estimate_messages_tokens_rough
from agent.turn_context import drop_stale_api_content


_CHARS_PER_TOKEN = 4
FORCED_OVERFLOW_TARGET_RATIO = 0.85
_MIN_ASSISTANT_EXCERPT_CHARS = 4_000
FORCED_OVERFLOW_EXCERPT_MARKER = (
    "\n\n...[completed assistant reply excerpted during provider-overflow "
    "recovery; full text remains in session history]...\n\n"
)
FORCED_OVERFLOW_DUPLICATE_MARKER = (
    "[Duplicate retry prompt represented once during provider-overflow "
    "recovery; the identical full prompt follows.]"
)


def _is_plain_user_retry(message: Any) -> bool:
    return bool(
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and not message.get("_compressed_summary")
        and not message.get("_todo_snapshot_synthetic")
        and not message.get("_provider_overflow_duplicate_retry")
    )


def relieve_forced_overflow_tail_pressure(
    messages: List[Dict[str, Any]],
    *,
    current_tokens: int,
    context_length: int,
    overflow_proven: bool = False,
) -> tuple[List[Dict[str, Any]], int, int]:
    """Reclaim request budget that protected-tail compaction cannot.

    This is gated by a provider-proven overflow.  It represents consecutive
    byte-identical real user retries once, then excerpts oversized completed
    assistant anchor until the request has about 15% response headroom.  When
    the provider proves overflow but the local estimate is below the window,
    the newest oversized completed assistant anchor is reduced to the minimum
    excerpt because the estimate is demonstrably non-authoritative.
    User-authored text is never truncated.  The normal
    compaction commit archives the original full rows before these active
    replacements are persisted.

    Returns ``(messages, duplicate_user_rows_removed, assistant_rows_bounded)``.
    """
    if (
        not messages
        or context_length <= 0
        or (current_tokens < context_length and not overflow_proven)
    ):
        return messages, 0, 0

    out: List[Dict[str, Any]] = [
        original.copy() if isinstance(original, dict) else original
        for original in messages
    ]
    duplicate_user_rows_removed = 0
    run_start = 0
    while run_start < len(out):
        first = out[run_start]
        if not _is_plain_user_retry(first):
            run_start += 1
            continue
        run_end = run_start + 1
        while (
            run_end < len(out)
            and _is_plain_user_retry(out[run_end])
            and out[run_end].get("content") == first.get("content")
        ):
            run_end += 1
        for idx in range(run_start, run_end - 1):
            # Keep list length and the newest full user row intact. Legacy
            # rotation compression reloads the durable transcript whenever a
            # candidate list becomes shorter; same-length markers therefore
            # survive that persistence boundary while the append-only original
            # rows remain archived and recoverable.
            out[idx]["content"] = FORCED_OVERFLOW_DUPLICATE_MARKER
            out[idx]["_provider_overflow_duplicate_retry"] = True
            drop_stale_api_content(out[idx])
            duplicate_user_rows_removed += 1
        run_start = run_end

    before_tokens = estimate_messages_tokens_rough(messages)
    after_dedupe_tokens = estimate_messages_tokens_rough(out)
    target_tokens = int(context_length * FORCED_OVERFLOW_TARGET_RATIO)
    still_needed = max(
        0,
        current_tokens
        - target_tokens
        - max(0, before_tokens - after_dedupe_tokens),
    )
    if still_needed <= 0:
        if not overflow_proven:
            return out, duplicate_user_rows_removed, 0

    bounded_assistant_rows = 0
    estimate_disproven = overflow_proven and current_tokens < context_length
    for idx in range(len(out) - 1, -1, -1):
        msg = out[idx]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        if msg.get("_compressed_summary") or msg.get("tool_calls"):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= _MIN_ASSISTANT_EXCERPT_CHARS:
            continue

        # Two-token margin covers estimator rounding and prevents an exact
        # boundary from provoking one more provider retry.
        if estimate_disproven:
            desired_chars = _MIN_ASSISTANT_EXCERPT_CHARS
        else:
            reclaim_chars = (still_needed + 2) * _CHARS_PER_TOKEN
            desired_chars = max(
                _MIN_ASSISTANT_EXCERPT_CHARS,
                len(content) - reclaim_chars,
            )
        if desired_chars >= len(content):
            continue
        payload_chars = max(
            2,
            desired_chars - len(FORCED_OVERFLOW_EXCERPT_MARKER),
        )
        head_chars = max(1, int(payload_chars * 0.7))
        tail_chars = max(1, payload_chars - head_chars)
        msg["content"] = (
            content[:head_chars]
            + FORCED_OVERFLOW_EXCERPT_MARKER
            + content[-tail_chars:]
        )
        drop_stale_api_content(msg)
        bounded_assistant_rows += 1
        reclaimed = max(
            0,
            before_tokens - estimate_messages_tokens_rough(out),
        )
        still_needed = max(0, current_tokens - target_tokens - reclaimed)
        if estimate_disproven or still_needed <= 0:
            break

    if duplicate_user_rows_removed or bounded_assistant_rows:
        return out, duplicate_user_rows_removed, bounded_assistant_rows
    return messages, 0, 0
