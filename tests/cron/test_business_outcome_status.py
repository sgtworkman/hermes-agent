"""Regression coverage for scheduler-visible business outcome contracts."""

import hashlib
import json

from cron.jobs import _record_run_outcome


def _job(path, **contract_overrides):
    contract = {
        "path": str(path),
        "status_field": "business_state",
        "green_values": ["SENT"],
        "red_values": ["RED", "BLOCKED"],
        "skip_values": ["SKIPPED"],
        "green_requirements": [
            {"field": "provider_readback.delivery_verified", "values": ["true"]},
        ],
    }
    contract.update(contract_overrides)
    return {"id": "rfd", "business_outcome_contract": contract}


def _record(job, *, success=True, delivery_error=None):
    _record_run_outcome(job, success, None, delivery_error, None, "2026-09-05T00:00:00Z")
    return job


def test_process_ok_requires_hash_bound_green_business_state(tmp_path):
    terminal = tmp_path / "terminal.json"
    raw = json.dumps({
        "business_state": "SENT",
        "provider_readback": {"delivery_verified": True},
    }).encode()
    terminal.write_bytes(raw)

    job = _record(_job(terminal))

    assert job["agent_run_status"] == "ok"
    assert job["last_status"] == "ok"
    assert job["last_error"] is None
    assert job["business_outcome_readback"]["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert job["business_outcome_readback"]["reason"] == "business_outcome_green"


def test_process_ok_cannot_mask_red_business_state(tmp_path):
    terminal = tmp_path / "terminal.json"
    terminal.write_text(json.dumps({"business_state": "RED"}))

    job = _record(_job(terminal))

    assert job["agent_run_status"] == "ok"
    assert job["last_status"] == "business_red"
    assert "did not satisfy GREEN" in job["last_error"]
    assert job["failure_streak"] == 1


def test_green_label_with_failed_requirement_is_unknown(tmp_path):
    terminal = tmp_path / "terminal.json"
    terminal.write_text(json.dumps({
        "business_state": "SENT",
        "provider_readback": {"delivery_verified": False},
    }))

    job = _record(_job(terminal))

    assert job["last_status"] == "business_unknown"
    assert job["business_outcome_readback"]["reason"] == "green_requirements_not_satisfied"


def test_missing_or_malformed_terminal_fails_closed(tmp_path):
    missing = _record(_job(tmp_path / "missing.json"))
    assert missing["last_status"] == "business_unknown"
    assert missing["business_outcome_readback"]["reason"] == "FileNotFoundError"

    malformed_path = tmp_path / "bad.json"
    malformed_path.write_text("not json")
    malformed = _record(_job(malformed_path))
    assert malformed["last_status"] == "business_unknown"
    assert malformed["business_outcome_readback"]["reason"] == "JSONDecodeError"


def test_skip_is_visible_and_not_ok(tmp_path):
    terminal = tmp_path / "terminal.json"
    terminal.write_text(json.dumps({"business_state": "SKIPPED"}))

    job = _record(_job(terminal, green_requirements=[]))

    assert job["last_status"] == "business_skipped"
    assert job["business_outcome_readback"]["reason"] == "business_outcome_skipped"


def test_ordinary_jobs_and_delivery_failures_preserve_process_status(tmp_path):
    ordinary = _record({"id": "ordinary"})
    assert ordinary["last_status"] == "ok"
    assert ordinary["agent_run_status"] == "ok"

    terminal = tmp_path / "terminal.json"
    terminal.write_text(json.dumps({
        "business_state": "SENT",
        "provider_readback": {"delivery_verified": True},
    }))
    delivery_failed = _record(_job(terminal), delivery_error="notification failed")
    assert delivery_failed["last_status"] == "delivery_failed"
    assert delivery_failed["failure_streak"] == 0
