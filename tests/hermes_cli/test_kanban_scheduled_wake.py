"""Tests for the kanban scheduled-wake fix (no-show defect, 2026-10-12).

Before the fix, a card parked via ``kanban schedule`` with a
``SCHEDULED_UNTIL=<ISO8601>`` marker in its reason stayed in ``scheduled``
forever: nothing in the dispatcher tick ever looked at the ``scheduled``
lane again, so a card waiting on time never woke on its own (reproduced
live 2026-10-12 on card ``t_8ddeebba`` — SCHEDULED_UNTIL passed ~15h
earlier and the card only moved after a manual ``hermes kanban unblock``).

The fix:

* ``wake_due_scheduled`` (kanban_db) promotes parked cards whose latest
  ``scheduled`` event reason carries a passed ``SCHEDULED_UNTIL`` —
  parent-gated to ``ready``/``todo``, with a ``scheduled_wake`` audit event.
* The dispatcher's reclaim phase calls it before ``recompute_ready``, so a
  woken card is dispatchable in the SAME tick.
* Cards WITHOUT a parseable marker stay parked; ``kanban unblock`` remains
  their manual escape hatch. A far-past marker (missed daemon downtime)
  still wakes on the next tick — back-compat for boards repaired in place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home: Path):
    c = kbc.connect()
    try:
        yield c
    finally:
        c.close()


def _schedule(conn, tid, reason):
    assert kb.schedule_task(conn, tid, reason=reason)


def _tick(conn, now=None):
    """One dispatcher tick with a stub spawner; no real workers spawned."""
    if now is not None:
        with mock.patch.object(kb.time, "time", return_value=now):
            return kbd._dispatch_once_locked(
                conn, spawn_fn=lambda *a, **k: None,
                reconcile_orphans=False, board=None,
            )
    return kbd._dispatch_once_locked(
        conn, spawn_fn=lambda *a, **k: None,
        reconcile_orphans=False, board=None,
    )


# --- marker parsing ---------------------------------------------------------

def test_parse_scheduled_until_accepts_z_and_offset_and_naive():
    base = datetime(2026, 10, 12, 0, 30, tzinfo=UTC)
    assert kb.parse_scheduled_until("SCHEDULED_UNTIL=2026-10-12T00:30:00Z") == int(base.timestamp())
    assert kb.parse_scheduled_until("SCHEDULED_UNTIL=2026-10-12T00:30:00+00:00") == int(base.timestamp())
    assert kb.parse_scheduled_until(
        "SCHEDULED_UNTIL=2026-10-08T20:30:00-04:00 waiting for reconnect"
    ) == int(datetime(2026, 10, 8, 20, 30, tzinfo=timezone(timedelta(hours=-4))).timestamp())
    # Naive → local time (just assert it parses to *some* epoch).
    naive = kb.parse_scheduled_until("SCHEDULED_UNTIL=2026-10-12T00:30:00")
    assert isinstance(naive, int)
    # Case-insensitive marker, extra prose around it.
    assert kb.parse_scheduled_until("resume at scheduled_until=2026-10-12T00:30:00Z please") == int(base.timestamp())


def test_parse_scheduled_until_garbage_is_none_not_crash():
    assert kb.parse_scheduled_until(None) is None
    assert kb.parse_scheduled_until("") is None
    assert kb.parse_scheduled_until("just waiting around") is None
    assert kb.parse_scheduled_until("SCHEDULED_UNTIL=tomorrow-ish") is None
    assert kb.parse_scheduled_until("SCHEDULED_UNTIL=2026-13-45T99:99:99Z") is None


# --- unit: wake_due_scheduled ----------------------------------------------

def test_wake_due_scheduled_wakes_past_marker(conn):
    past = datetime.now(UTC) - timedelta(hours=15)
    tid = kb.create_task(conn, title="wake me", assignee="coder")
    _schedule(conn, tid, f"SCHEDULED_UNTIL={past.isoformat()} waiting for reconnect")
    assert kb.get_task(conn, tid).status == "scheduled"

    woken = kb.wake_due_scheduled(conn)
    assert woken == [tid]
    assert kb.get_task(conn, tid).status == "ready"
    events = kb.list_events(conn, tid)
    assert any(e.kind == "scheduled_wake" for e in events)


def test_wake_due_scheduled_future_marker_stays_parked(conn):
    future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    tid = kb.create_task(conn, title="not yet", assignee="coder")
    _schedule(conn, tid, f"SCHEDULED_UNTIL={future}")

    assert kb.wake_due_scheduled(conn) == []
    assert kb.get_task(conn, tid).status == "scheduled"


def test_wake_due_scheduled_far_past_marker_wakes_on_next_tick(conn):
    """Back-compat: a card parked before the fix with a long-passed marker
    wakes on the first tick after upgrade (daemon restart repairs in place)."""
    ancient = datetime(2026, 10, 8, 20, 30, tzinfo=timezone(timedelta(hours=-4)))
    tid = kb.create_task(conn, title="stale", assignee="coder")
    _schedule(conn, tid, f"SCHEDULED_UNTIL={ancient.isoformat()}")
    assert kb.wake_due_scheduled(conn) == [tid]
    assert kb.get_task(conn, tid).status == "ready"


def test_wake_due_scheduled_without_marker_stays_parked(conn):
    tid = kb.create_task(conn, title="no timer", assignee="coder")
    _schedule(conn, tid, "waiting on an external crawl to finish")
    assert kb.wake_due_scheduled(conn) == []
    assert kb.get_task(conn, tid).status == "scheduled"
    # Manual escape hatch still works.
    assert kb.unblock_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "ready"


def test_wake_respects_parent_gating(conn):
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    parent = kb.create_task(conn, title="parent", assignee="coder")
    tid = kb.create_task(conn, title="child", assignee="coder", parents=[parent])
    _schedule(conn, tid, f"SCHEDULED_UNTIL={past}")

    assert kb.wake_due_scheduled(conn) == [tid]
    assert kb.get_task(conn, tid).status == "todo"
    # Once the parent finishes, the normal promotion path takes over.
    kb.complete_task(conn, parent, result="done")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "ready"


def test_wake_uses_latest_scheduled_event(conn):
    """Re-scheduling replaces the old marker: the latest event wins."""
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    tid = kb.create_task(conn, title="rescheduled", assignee="coder")
    _schedule(conn, tid, f"SCHEDULED_UNTIL={past}")
    # Re-scheduling requires leaving scheduled first (schedule_task only
    # accepts todo/ready/running/blocked sources).
    kb.unblock_task(conn, tid)
    _schedule(conn, tid, f"SCHEDULED_UNTIL={future}")
    assert kb.wake_due_scheduled(conn) == []
    assert kb.get_task(conn, tid).status == "scheduled"


# --- integration: dispatcher tick wakes in the same pass --------------------

def test_dispatch_tick_wakes_and_dispatches_scheduled_card(conn):
    """Card scheduled 1 minute ahead: stays parked across ticks until the
    time passes, then the very next tick promotes AND spawns it."""
    tid = kb.create_task(conn, title="timer wake", assignee="coder")
    due = int(datetime.now(UTC).timestamp())
    _schedule(conn, tid, f"SCHEDULED_UNTIL={datetime.fromtimestamp(due, UTC).isoformat()}")

    # Tick while not yet due → parked, nothing spawned.
    result = _tick(conn, now=due - 60)
    assert result.scheduled_wakes == []
    assert kb.get_task(conn, tid).status == "scheduled"

    # Tick after the wake time → promoted and dispatched in the same tick.
    result = _tick(conn, now=due + 60)
    assert tid in result.scheduled_wakes
    assert kb.get_task(conn, tid).status == "ready"


def test_dispatch_tick_wakes_far_past_card_on_start(conn):
    """Back-compat via the live tick path: the daemon's first tick after a
    fix/upgrade wakes a long-overdue card."""
    ancient = int(datetime(2026, 10, 11, 20, 30, tzinfo=UTC).timestamp())
    tid = kb.create_task(conn, title="overdue", assignee="coder")
    _schedule(conn, tid, f"SCHEDULED_UNTIL={datetime.fromtimestamp(ancient, UTC).isoformat()}")

    result = _tick(conn, now=ancient + 15 * 3600)
    assert tid in result.scheduled_wakes
    assert kb.get_task(conn, tid).status == "ready"
