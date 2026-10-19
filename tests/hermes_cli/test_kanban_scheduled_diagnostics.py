"""Invalid wake markers must be visible without changing schedule gates."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


@pytest.mark.parametrize(
    ("reason", "invalid"),
    [
        ("SCHEDULED_UNTIL=2026-10-26; preserve backfill and indexing gates", True),
        ("SCHEDULED_UNTIL=2999-01-01T00:00:00", True),
        ("SCHEDULED_UNTIL=2999-02-30T00:00:00Z", True),
        ("SCHEDULED_UNTIL=not-a-time", True),
        ("SCHEDULED_UNTIL= ", True),
        ("scheduled_until = 2999-01-01", True),
        ("SCHEDULED_UNTIL=2999-01-01T00:00:00Z", False),
        ("SCHEDULED_UNTIL=2000-01-01T00:00:00-04:00", False),
        ("Waiting for the existing owner approval", False),
        ("NOT_SCHEDULED_UNTIL=not-a-time", False),
        (None, False),
    ],
)
def test_invalid_markers_report_an_error_without_mutating_input(reason, invalid):
    task = {"id": "t_schedule", "status": "scheduled"}
    events = [{"id": 3, "kind": "scheduled", "created_at": 100,
               "payload": {"reason": reason}}]
    original = deepcopy((task, events))
    diags = kd.compute_task_diagnostics(task, events, [], now=200)
    wake = [d for d in diags if d.kind == "scheduled_wake_invalid"]
    assert bool(wake) is invalid
    if invalid:
        assert wake[0].severity == "error"
        assert "timezone" in wake[0].detail.lower()
        assert "automatic resume" in wake[0].title.lower()
        assert wake[0].data["reason"] == reason
        assert wake[0].actions[0].kind == "cli_hint"
        assert wake[0].actions[0].payload["command"] == "hermes kanban show t_schedule"
    assert (task, events) == original


@pytest.mark.parametrize("status", ["ready", "running", "blocked", "done", "archived"])
def test_marker_diagnostic_clears_when_task_leaves_scheduled(status):
    events = [{"kind": "scheduled", "created_at": 100,
               "payload": {"reason": "SCHEDULED_UNTIL=not-a-time"}}]
    diags = kd.compute_task_diagnostics({"id": "t_schedule", "status": status}, events, [], now=200)
    assert not any(d.kind == "scheduled_wake_invalid" for d in diags)


def test_latest_event_identity_wins_over_clock_order_and_clears_invalid_marker():
    task = {"id": "t_schedule", "status": "scheduled"}
    events = [
        {"id": 2, "kind": "scheduled", "created_at": 100,
         "payload": json.dumps({"reason": "SCHEDULED_UNTIL=2999-01-01T00:00:00Z"})},
        {"id": 1, "kind": "scheduled", "created_at": 200,
         "payload": {"reason": "SCHEDULED_UNTIL=bad"}},
    ]
    assert not any(d.kind == "scheduled_wake_invalid"
                   for d in kd.compute_task_diagnostics(task, events, [], now=300))
    events.append({"id": 3, "kind": "scheduled", "created_at": 50,
                   "payload": json.dumps({"reason": "SCHEDULED_UNTIL=bad-again"})})
    diags = kd.compute_task_diagnostics(task, events, [], now=300)
    wake = [d for d in diags if d.kind == "scheduled_wake_invalid"]
    assert len(wake) == 1
    assert wake[0].data["schedule_event_id"] == 3


def test_database_schedule_gate_is_unchanged_by_structured_and_cli_diagnostics(tmp_path, monkeypatch, capsys):
    from hermes_cli.kanban import _print_diagnostics

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Retain the existing execution gates")
        reason = "SCHEDULED_UNTIL=2999-01-01; retain owner approval and existing gates"
        assert kb.schedule_task(conn, task_id, reason=reason)
        original = list(conn.iterdump())
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        diags = kd.compute_task_diagnostics(task, events, [], now=200)
        wake = [d for d in diags if d.kind == "scheduled_wake_invalid"]
        assert len(wake) == 1
        assert wake[0].to_dict()["data"]["reason"] == reason
        _print_diagnostics(wake, "", with_kind=True)
        printed = capsys.readouterr().out.lower()
        assert "scheduled_wake_invalid" in printed
        assert "automatic resume" in printed
        assert "timezone" in printed
        assert list(conn.iterdump()) == original
        assert kb.get_task(conn, task_id).status == "scheduled"
