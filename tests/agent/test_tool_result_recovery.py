"""Focused regressions for bounded self-healing tool retries."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import agent.tool_executor as tool_executor


def _agent() -> SimpleNamespace:
    return SimpleNamespace(session_id="session-1", _current_turn_id="turn-1")


def test_serialized_read_only_error_retries_once_and_reclassifies(monkeypatch):
    # The recovery re-dispatch is gated default-off (tools.recovery_retry) so it
    # cannot break upstream's exactly-once dispatch contract; opt in explicitly here.
    monkeypatch.setattr(tool_executor, "_recovery_retry_enabled", lambda: True)
    calls = []
    receipts = []
    results = [
        '{"error":{"message":"transient registry failure"}}',
        "recovered read content",
    ]

    def execute(args):
        calls.append(args)
        return results.pop(0)

    monkeypatch.setattr(tool_executor.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        tool_executor,
        "write_repair_receipt",
        lambda **kwargs: receipts.append(kwargs),
    )

    result = tool_executor._classify_and_recover_tool_result(
        _agent(),
        function_name="read_file",
        function_args={"path": "README.md"},
        effective_task_id="task-1",
        tool_call_id="call-1",
        result=results.pop(0),
        execute=execute,
    )

    assert result == "recovered read content"
    assert calls == [{"path": "README.md"}]
    assert len(receipts) == 2
    assert receipts[0]["classification"]["status"] == "error"
    assert receipts[0]["classification"]["retryable"] is True
    assert receipts[1]["classification"]["status"] == "success"


def test_serialized_errors_never_replay_effect_capable_or_blocked_tools(monkeypatch):
    # The recovery re-dispatch is gated default-off (tools.recovery_retry) so it
    # cannot break upstream's exactly-once dispatch contract; opt in explicitly here.
    monkeypatch.setattr(tool_executor, "_recovery_retry_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(tool_executor.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(tool_executor, "write_repair_receipt", lambda **kwargs: None)

    def execute(args):
        calls.append(args)
        return "should not run"

    serialized_error = '{"error":{"message":"failed"}}'
    effect_result = tool_executor._classify_and_recover_tool_result(
        _agent(),
        function_name="write_file",
        function_args={"path": "x", "content": "y"},
        effective_task_id="task-1",
        tool_call_id="call-effect",
        result=serialized_error,
        execute=execute,
    )
    blocked_result = tool_executor._classify_and_recover_tool_result(
        _agent(),
        function_name="read_file",
        function_args={"path": "x"},
        effective_task_id="task-1",
        tool_call_id="call-blocked",
        result=serialized_error,
        execute=execute,
        blocked=True,
    )

    assert calls == []
    assert "should not run" not in effect_result
    assert blocked_result == serialized_error


def test_relay_exception_reaches_the_recovery_classifier(monkeypatch):
    # The recovery re-dispatch is gated default-off (tools.recovery_retry) so it
    # cannot break upstream's exactly-once dispatch contract; opt in explicitly here.
    monkeypatch.setattr(tool_executor, "_recovery_retry_enabled", lambda: True)
    """Failures before the callback still become bounded model-visible output."""
    import agent.relay_tools as relay_tools

    receipts = []
    retry = MagicMock(side_effect=RuntimeError("retry failed"))

    def relay_failure(*_args, **_kwargs):
        raise RuntimeError("relay failed")

    monkeypatch.setattr(relay_tools, "execute", relay_failure)
    monkeypatch.setattr(
        tool_executor,
        "write_repair_receipt",
        lambda **kwargs: receipts.append(kwargs),
    )
    monkeypatch.setattr(tool_executor.time, "sleep", lambda seconds: None)

    managed = tool_executor._run_agent_tool_execution_middleware(
        _agent(),
        function_name="read_file",
        function_args={"path": "README.md"},
        effective_task_id="task-1",
        tool_call_id="call-relay-error",
        execute=retry,
    )

    assert '"status":"error"' in managed.result
    assert "retry failed" in managed.result
    retry.assert_called_once_with({"path": "README.md"})
    assert [item["classification"]["status"] for item in receipts] == [
        "error",
        "error",
    ]
