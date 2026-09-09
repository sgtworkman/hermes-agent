"""Discriminating regressions for the command-side RFD catch-up guard."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path


# The guard is COMMAND-side (see the module docstring), so it lives under
# WorkmanAI/scripts — not ~/.hermes/scripts, which has never existed on any
# node. Pointing at the latter made all six tests here fail with
# FileNotFoundError, both inside and outside the sandbox.
#
# Two candidates, because ./pytest runs with HOME redirected to an adversarial
# sandbox: the HOME-relative path is right for a normal run, and the
# __file__-relative one still resolves when HOME has been redirected.
_GUARD_REL = "WorkmanAI/scripts/rfd_missed_cadence_catchup_guard.py"
_CANDIDATES = (
    Path.home() / _GUARD_REL,
    Path(__file__).resolve().parents[4] / _GUARD_REL,
)
SOURCE = next((p for p in _CANDIDATES if p.is_file()), _CANDIDATES[0])


def load_guard():
    spec = importlib.util.spec_from_file_location("rfd_missed_cadence_guard_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_live_et_date_schema_reaches_pending_gate_path():
    guard = load_guard()
    latest = {
        "et_date": "2026-08-26",
        "pending_approval": True,
        "draft_campaign_id": "campaign-123",
    }

    selected, reasons = guard.same_day_pending_payload(latest, "2026-08-26")

    assert selected == latest
    assert reasons == []


def test_lock_without_provider_readback_cannot_greenwash_missed_state(tmp_path, monkeypatch):
    guard = load_guard()
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(guard, "STATUS", status_path)
    monkeypatch.setattr(
        guard,
        "remote_lock_check",
        lambda: (0, "STATUS=already_sent\nVerification: pending\nCampaign ID: 123", ""),
    )

    rc = guard.main(["--force"])

    assert rc == guard.MISSED_OR_UNVERIFIED_EXIT
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "SENT_LOCK_PRESENT_UNVERIFIED"
    assert status["scheduler_visible_status"] == "RED_KEPT_OPEN"


def test_missed_slot_remains_nonzero_after_notification(tmp_path, monkeypatch):
    guard = load_guard()
    state_path = tmp_path / "state.json"
    status_path = tmp_path / "status.json"
    latest_path = tmp_path / "latest.json"
    state_path.write_text(
        json.dumps({"notified_missed_slots": [guard.local_today()]}) + "\n",
        encoding="utf-8",
    )
    latest_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(guard, "STATE", state_path)
    monkeypatch.setattr(guard, "STATUS", status_path)
    monkeypatch.setattr(guard, "LATEST", latest_path)
    monkeypatch.setattr(guard, "remote_lock_check", lambda: (0, "STATUS=missed_slot_no_send", ""))

    rc = guard.main(["--force"])

    assert rc == guard.MISSED_OR_UNVERIFIED_EXIT
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "MISSED_SLOT_NOTIFIED_NO_SEND"
    assert status["scheduler_visible_status"] == "RED_KEPT_OPEN"


def test_remote_blindness_is_scheduler_visible_failure(tmp_path, monkeypatch):
    guard = load_guard()
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(guard, "STATUS", status_path)
    monkeypatch.setattr(guard, "remote_lock_check", lambda: (255, "", "connection refused"))

    rc = guard.main(["--force"])

    assert rc == guard.MISSED_OR_UNVERIFIED_EXIT
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "YELLOW"
    assert status["reason"] == "mini2_readback_failed_no_send_attempted"


def test_prewindow_silence_requires_provider_verified_delivery(tmp_path, monkeypatch):
    guard = load_guard()
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(guard, "STATUS", status_path)
    monkeypatch.setattr(
        guard,
        "local_now",
        lambda: datetime(2026, 8, 28, 8, 0, tzinfo=guard.TZ),
    )
    monkeypatch.setattr(
        guard,
        "remote_lock_check",
        lambda: (
            0,
            "STATUS=already_sent\nVerification: verified\nProvider status: sent\nstats.sent: 54",
            "",
        ),
    )

    rc = guard.main([])

    assert rc == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["scheduler_visible_status"] == "GREEN"
    assert status["reason"] == "provider_send_verified"


def test_same_day_hash_bound_skip_is_not_misclassified_as_missed(tmp_path, monkeypatch):
    guard = load_guard()
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(guard, "STATUS", status_path)
    monkeypatch.setattr(
        guard,
        "local_now",
        lambda: datetime(2026, 8, 31, 8, 0, tzinfo=guard.TZ),
    )
    monkeypatch.setattr(
        guard,
        "remote_lock_check",
        lambda: (
            0,
            "RFD_LATEST_SHA256=" + "a" * 64
            + "\nRFD_TERMINAL=skipped-no-deal"
            + "\nRFD_SLOT=2026-08-31T06:00:00-04:00"
            + "\nRFD_REASON=curation_not_replacement_ready"
            + "\nSTATUS=missed_slot_no_send",
            "",
        ),
    )

    rc = guard.main([])

    assert rc == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["scheduler_visible_status"] == "BUSINESS_SKIPPED"
    assert status["business_state"] == "SKIPPED"
