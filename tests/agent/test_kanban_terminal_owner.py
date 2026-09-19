"""A native terminal receipt ends worker authority, even when verify asks for more."""
from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import contextmanager

import pytest

from agent import kanban_stop, turn_stop_gates, tool_executor


@pytest.mark.parametrize("status", ["done", "blocked", "review", "superseded", "changes_requested"])
def test_terminal_owner_prevents_synthetic_work(monkeypatch, status):
    monkeypatch.setattr(kanban_stop, "native_worker_stop_status", lambda: status, raising=False)
    verify = Mock(return_value="Create another verifier and continue")
    monkeypatch.setattr(turn_stop_gates, "_verify_on_stop_nudge", verify)
    agent = Mock()
    agent._verification_stop_nudges = 0
    messages = []
    verdict = turn_stop_gates.apply_stop_gates(
        agent, {"role": "assistant", "content": "Native receipt recorded."},
        final_response="Native receipt recorded.", messages=messages,
        conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=False,
    )
    assert not verdict.continue_turn
    assert messages == []
    verify.assert_not_called()


def test_rejected_completion_keeps_verification(monkeypatch):
    monkeypatch.setattr(kanban_stop, "native_worker_stop_status", lambda: None, raising=False)
    monkeypatch.setattr(turn_stop_gates, "_verify_on_stop_nudge", lambda agent: "Repair failed test")
    agent = Mock(); agent._verification_stop_nudges = 0
    messages = [{"role": "tool", "name": "kanban_complete", "content": '{"ok": false}'}]
    verdict = turn_stop_gates.apply_stop_gates(
        agent, {"role": "assistant", "content": "Done"}, final_response="Done",
        messages=messages, conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=False,
    )
    assert verdict.continue_turn
    assert messages[-1]["content"] == "Repair failed test"


def test_terminal_owner_blocks_additional_dispatch(monkeypatch):
    monkeypatch.setattr(kanban_stop, "native_worker_stop_status", lambda: "done", raising=False)
    monkeypatch.setattr(tool_executor, "_emit_terminal_post_tool_call", Mock())
    monkeypatch.setattr(tool_executor, "_pre_tool_block", lambda a, r: (None, r.args))
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *args: None)
    monkeypatch.setattr(tool_executor, "_run_with_activity_heartbeat", lambda a, n, fn: fn())
    execute = Mock(return_value="unexpected write")
    agent = Mock()
    agent._tool_guardrails.before_call.return_value.allows_execution = True
    state = tool_executor._ManagedToolResult(None, {}, [], False, False)
    result = tool_executor._dispatch_authorized_once(
        agent, state, tool_executor._ToolCallRef("write_file", {}, "task", "call", []),
        execute=execute, scope_block=None, display_index=None, begin_execution=None,
        authorization_gate=None,
    )
    execute.assert_not_called()
    assert state.blocked
    assert "done" in result


def test_exact_native_run_receipt_survives_successor(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing(db_path=tmp_path / "board.db") as conn:
        task_id = kb.create_task(conn, title="isolated owner proof")
        claimed = kb.claim_task(conn, task_id, claimer="fixture")
        run_id = claimed.current_run_id
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        @contextmanager
        def existing_connection():
            yield conn
        monkeypatch.setattr(kbc, "connect_closing", existing_connection)
        assert kanban_stop.native_worker_stop_status() is None
        rejected = [{"role": "tool", "name": "kanban_complete", "content": '{"ok": false}'}]
        assert kanban_stop.build_kanban_stop_nudge(messages=rejected) is not None
        assert kb.complete_task(conn, task_id, expected_run_id=run_id, summary="fixture accepted", fire_lifecycle_hook=False)
        assert kanban_stop.native_worker_stop_status() == "done"
        # Simulate an independent successor without rewriting the old run receipt.
        conn.execute("UPDATE tasks SET status='running', current_run_id=? WHERE id=?", (run_id + 1, task_id))
        assert kanban_stop.native_worker_stop_status() == "done"
        conn.execute("UPDATE task_runs SET outcome='failed' WHERE id=?", (run_id,))
        assert kanban_stop.native_worker_stop_status() == "superseded"


@pytest.mark.parametrize("run_id", ["", "invalid", "0"])
def test_incomplete_identity_cannot_waive_verification(monkeypatch, run_id):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    assert kanban_stop.native_worker_stop_status() is None


def test_unavailable_database_cannot_certify_completion(monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    monkeypatch.setattr(kbc, "connect_closing", Mock(side_effect=OSError("unavailable")))
    assert kanban_stop.native_worker_stop_status() is None
