"""Tests for shared tool result classification helpers."""

import json

from agent.tool_result_classification import (
    file_mutation_result_landed,
)


def test_write_file_with_nested_lint_error_counts_as_landed():
    result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })

    assert file_mutation_result_landed("write_file", result) is True






def test_side_effect_classification_keeps_session_mutations():
    from agent.tool_result_classification import tool_may_have_side_effect

    assert tool_may_have_side_effect("todo") is True
    assert tool_may_have_side_effect("memory") is True
    assert tool_may_have_side_effect("write_file") is True
    assert tool_may_have_side_effect("mcp_unknown") is True
    assert tool_may_have_side_effect("read_file") is False
    assert tool_may_have_side_effect("web_search") is False


def test_outcome_classifier_covers_bounded_failure_lanes():
    from agent.tool_result_classification import classify_tool_outcome

    assert classify_tool_outcome({"ok": True}, tool_name="read_file")["status"] == "success"
    assert classify_tool_outcome("", tool_name="read_file")["status"] == "empty_result"
    assert classify_tool_outcome("", tool_name="read_file")["retryable"] is True
    assert classify_tool_outcome("", tool_name="write_file")["retryable"] is False
    assert classify_tool_outcome({"exit_code": 2}, tool_name="read_file")["status"] == "error"
    assert classify_tool_outcome("late", timed_out=True)["status"] == "timeout"
    assert classify_tool_outcome("late", tool_name="read_file", timed_out=True)["retryable"] is False
    assert classify_tool_outcome(
        "late", tool_name="read_file", error=TimeoutError("late")
    )["status"] == "timeout"
    assert classify_tool_outcome("unsafe", blocked_risk=True)["status"] == "blocked_risk"
    assert classify_tool_outcome("wrong", expected_shape=dict)["status"] == "malformed_result"


def test_outcome_classifier_detects_serialized_registry_error_without_changing_text():
    from agent.tool_result_classification import classify_tool_outcome

    assert classify_tool_outcome('{"error":{"message":"registry unavailable"}}', tool_name="web_search")["status"] == "error"
    assert classify_tool_outcome('{"ok":false,"message":"failed"}', tool_name="web_search")["status"] == "error"
    assert classify_tool_outcome('{"message":"ordinary JSON text"}', tool_name="web_search")["status"] == "success"
    assert classify_tool_outcome("ordinary successful text", tool_name="web_search")["status"] == "success"


def test_digest_is_deterministic_and_receipt_redacts_and_bounds(tmp_path):
    from agent.tool_result_classification import (
        classify_tool_outcome,
        result_digest,
        write_repair_receipt,
    )

    assert result_digest({"b": 2, "a": 1}) == result_digest({"a": 1, "b": 2})
    classification = classify_tool_outcome({"secret": "DO_NOT_STORE"}, tool_name="read_file")
    path = write_repair_receipt(
        mission_id="m1", turn_id=3, tool_call_id="tc1", classification=classification,
        repair_disposition="retry", profile_home=tmp_path, max_bytes=300,
        observed_result={"type": "dict", "digest": classification["result_digest"]},
    )
    text = path.read_text()
    assert len(path.read_bytes()) <= 300
    assert "DO_NOT_STORE" not in text
    assert "raw_args" not in text and "raw_result" not in text
    assert '"mission_id":"m1"' in text


def test_receipt_drops_untrusted_observation_payload(tmp_path):
    from agent.tool_result_classification import write_repair_receipt

    path = write_repair_receipt(
        mission_id="mission-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        classification={
            "status": "error",
            "retryable": True,
            "side_effect_risk": "none",
            "result_digest": "a" * 64,
            "observed_result": {"raw_result": "sk-test-DO_NOT_PERSIST"},
        },
        repair_disposition="retry",
        observed_result={
            "type": "str",
            "digest": "b" * 64,
            "size": 7,
            "raw_result": "sk-test-DO_NOT_PERSIST",
        },
        timestamp="2026-09-04T00:00:00Z",
        profile_home=tmp_path,
    )

    text = path.read_text()
    assert "raw_result" not in text
    assert "sk-test-DO_NOT_PERSIST" not in text
    assert '"timestamp_utc":"2026-09-04T00:00:00Z"' in text


def test_http_status_and_process_exit_have_distinct_semantics():
    import json
    from agent.tool_result_classification import classify_tool_outcome
    cases = [({"status_code": 200}, "success"), ({"status_code": 204}, "success"),
             ({"status_code": 302}, "success"), ({"status_code": 404}, "error"),
             ({"status_code": 503}, "error"), ({"exit_code": 1}, "error"),
             ({"return_code": 0}, "success"), ({"status_code": 200, "error": "bad"}, "error")]
    for value, expected in cases:
        for envelope in (value, json.dumps(value)):
            assert classify_tool_outcome(envelope, tool_name="web_search")["status"] == expected
