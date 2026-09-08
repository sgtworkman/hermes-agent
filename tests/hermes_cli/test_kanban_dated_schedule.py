"""A due time wakes the same card once, without bypassing dependencies or ownership."""
from datetime import datetime, timezone
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools.registry import registry


def test_dated_card_survives_reconnect_and_waits_for_dependencies(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    now = int(time.time())
    with kbc.connect_closing(db) as conn:
        upstream = kb.create_task(conn, title="existing source work")
        task = kb.create_task(conn, title="existing pilot")
        kb.link_tasks(conn, parent_id=upstream, child_id=task)
        legacy = kb.create_task(conn, title="manual parking")
        assert kb.schedule_task(conn, legacy, reason="manual unblock only")
        assert kb.schedule_task(conn, task, reason="wait for capacity", scheduled_for=now + 60)
        assert kb.get_task(conn, task).scheduled_for == now + 60
    with kbc.connect_closing(db) as conn:
        monkeypatch.setattr(kb.time, "time", lambda: now + 59)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, task).status == "scheduled"
        monkeypatch.setattr(kb.time, "time", lambda: now + 60)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, task).status == "todo"
        assert kb.get_task(conn, task).scheduled_for is None
        assert kb.get_task(conn, upstream).status == "ready"
        assert kb.get_task(conn, legacy).status == "scheduled"
        kb.complete_task(conn, upstream, result="source work accepted")
        assert kb.get_task(conn, task).status == "ready"
        kb.recompute_ready(conn)
        events = [e for e in kb.list_events(conn, task) if e.kind == "schedule_due"]
        assert len(events) == 1
        assert events[0].payload["scheduled_for"] == now + 60
        assert kb.get_task(conn, task).current_run_id is None


@pytest.mark.parametrize("goal_mode", [False, True])
def test_native_timed_handoff_preserves_run_guard_and_rejects_bad_dates(tmp_path, monkeypatch, goal_mode):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import kanban_tools  # Registers the real handler and ownership guards.
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="same pilot owner", goal_mode=goal_mode)
        foreign = kb.create_task(conn, title="other worker")
        kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    handler = lambda args: registry.dispatch("kanban_block", args)
    for date in ["tomorrow", "2026-09-12T19:17:00", "2000-01-01T00:00:00Z", "99999-01-01T00:00:00Z"]:
        assert not json.loads(handler({"reason": "waiting", "resume_at": date})).get("ok")
    due = datetime.fromtimestamp(int(time.time()) + 3600, timezone.utc).isoformat()
    assert not json.loads(handler({"task_id": foreign, "reason": "foreign", "resume_at": due})).get("ok")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run + 1))
    assert not json.loads(handler({"reason": "stale owner", "resume_at": due})).get("ok")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    result = json.loads(handler({"reason": "waiting for dated capacity", "resume_at": due}))
    assert result["ok"] and result["status"] == "scheduled"
    with kbc.connect_closing() as conn:
        scheduled = kb.get_task(conn, task)
        assert scheduled.scheduled_for == int(datetime.fromisoformat(due).timestamp())
        assert scheduled.current_run_id is None
        assert kb.latest_run(conn, task).outcome == "scheduled"
        assert kb.get_task(conn, foreign).status == "ready"
        assert not kb.schedule_task(conn, task, scheduled_for=scheduled.scheduled_for + 1,
                                    expected_run_id=run)

        assert scheduled.goal_mode == goal_mode
        assert kb.goal_run_status(conn, task, run) == "scheduled"
        from hermes_cli import goals
        from agent.kanban_stop import build_kanban_stop_nudge
        monkeypatch.setattr(goals, "judge_goal", lambda *a, **kw: pytest.fail("timed handoff is not completion"))
        loop = goals.run_kanban_goal_loop(
            task_id=task, goal_text="existing criteria", first_response="waiting",
            task_status_fn=lambda: kb.goal_run_status(conn, task, run),
            run_turn=lambda *a: pytest.fail("old run must stop"),
            block_fn=lambda *a: pytest.fail("wait is not blocked"))
        assert loop["outcome"] == "scheduled_by_worker"
        assert build_kanban_stop_nudge(worker_status="scheduled") is None
        monkeypatch.setattr(kb.time, "time", lambda: scheduled.scheduled_for)
        kb.recompute_ready(conn)
        kb.claim_task(conn, task)
        assert kb.get_task(conn, task).current_run_id != run
        assert kb.get_task(conn, task).goal_mode == goal_mode
        assert kb.goal_run_status(conn, task, run) == "scheduled"


def test_cli_date_readback_and_dispatcher_wake_same_card(tmp_path, monkeypatch, all_assignees_spawnable):
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db_dispatch as dispatcher
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = int(time.time())
    due = datetime.fromtimestamp(now + 60, timezone.utc).isoformat()
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="dated CLI task", assignee="alice")
    assert "Scheduled" in cli.run_slash(f"schedule {task} capacity --at {due}")
    readback = json.loads(cli.run_slash(f"show {task} --json"))["task"]
    assert readback["scheduled_for"] == now + 60
    with kbc.connect_closing() as conn:
        spawned = []
        def spawn(card, *a, **kw):
            spawned.append(card.id)
            return 4242
        monkeypatch.setattr(kb.time, "time", lambda: now + 59)
        dispatcher.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == []
        monkeypatch.setattr(kb.time, "time", lambda: now + 60)
        dispatcher.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == [task]
        assert kb.get_task(conn, task).scheduled_for is None
        parked = kb.create_task(conn, title="cancel date")
        assert kb.schedule_task(conn, parked, scheduled_for=now + 120)
        assert kb.unblock_task(conn, parked)
        assert kb.get_task(conn, parked).scheduled_for is None
