"""Long jobs must recover without overriding route defaults or losing completed work."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.run_agent.test_continuation_ceiling_wedge import loop_agent, _run, _stub
from tests.run_agent.test_run_agent import _mock_assistant_msg, _mock_tool_call


@pytest.mark.parametrize("profile_name", [None, "openrouter"])
def test_explicit_reasoning_off_wins_over_declared_template_without_mutation(profile_name):
    from agent.transports.chat_completions import ChatCompletionsTransport
    from providers import get_provider_profile

    overrides = {"extra_body": {"chat_template_kwargs": {
        "enable_thinking": True, "keep_me": "unchanged"}, "include_reasoning": False}}
    baseline = deepcopy(overrides)
    args = dict(model="local-reasoning-model", messages=[{"role": "user", "content": "Act"}],
                max_tokens=4096, request_overrides=overrides,
                reasoning_config={"enabled": False, "effort": "none"})
    if profile_name:
        args["provider_profile"] = get_provider_profile(profile_name)
    wire = ChatCompletionsTransport().build_kwargs(**args)
    assert wire["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert overrides == baseline
    args["reasoning_config"] = {"enabled": True, "effort": "high"}
    assert ChatCompletionsTransport().build_kwargs(**args)["extra_body"] == baseline["extra_body"]


@pytest.mark.parametrize("tool_succeeds", [True, False])
def test_productive_tool_rounds_do_not_share_a_lifetime_truncation_allowance(loop_agent, tool_succeeds):
    a = loop_agent
    a.valid_tool_names = {"read_file"}
    replies = []
    for i in range(5):
        replies.extend([
            _stub(f"Working on item {i}. "),
            SimpleNamespace(id=f"call-{i}", model="test/model", usage=None,
                choices=[SimpleNamespace(index=0, finish_reason="tool_calls",
                    message=_mock_assistant_msg(content=None, tool_calls=[
                        _mock_tool_call("read_file", '{"path":"file-' + str(i) + '"}', f"tool-{i}")]))]),
        ])
    replies.append(SimpleNamespace(id="final", model="test/model", usage=None,
        choices=[SimpleNamespace(index=0, finish_reason="stop",
            message=_mock_assistant_msg(content="All five items processed."))]))
    a.client.chat.completions.create.side_effect = replies
    tool_result = '{"content":"new evidence"}' if tool_succeeds else '{"error":"unavailable"}'
    history = [{"role": "user", "content": "Earlier request"},
               {"role": "assistant", "content": "Earlier fragment", "_length_continuation_fragment": True}]
    with patch("model_tools.handle_function_call", return_value=tool_result) as execute:
        result = _run(a, "Process all five files, one at a time.", history)
    assert execute.call_count == (5 if tool_succeeds else 3)
    assert result["completed"] is tool_succeeds
    if tool_succeeds:
        assert result["final_response"] == "All five items processed."
        assert any(m.get("_length_continuation_fragment") and m.get("content") == "Earlier fragment"
                   for m in result["messages"])


@pytest.mark.parametrize("cap_key", ["max_tokens", "max_completion_tokens"])
def test_final_wire_budget_reserves_margin_and_honors_smaller_caps(cap_key):
    from agent.request_output_budget import clamp_chat_output_budget
    a = SimpleNamespace(api_mode="chat_completions", context_compressor=SimpleNamespace(context_length=65536))
    messages = [{"role": "user", "content": "x"}]
    kwargs = {"messages": messages, cap_key: 32768, "extra_body": {cap_key: 32768}}
    clamp_chat_output_budget(a, kwargs, 52914)
    assert 4096 <= kwargs[cap_key] < 65536 - 52914
    assert kwargs["extra_body"][cap_key] == kwargs[cap_key]
    assert kwargs["messages"] is messages
    kwargs[cap_key] = kwargs["extra_body"][cap_key] = 1024
    clamp_chat_output_budget(a, kwargs, 52914)
    assert kwargs[cap_key] == 1024


def test_conversation_applies_headroom_to_actual_request_without_changing_prompt(loop_agent):
    a = loop_agent
    a.max_tokens = 32768
    a.context_compressor.context_length = 65536
    a._cached_system_prompt = "Reference data. " * 12000
    built_messages = []
    build = a._build_api_kwargs
    def capture_messages(*args, **kwargs):
        payload = build(*args, **kwargs)
        built_messages.extend(deepcopy(payload["messages"]))
        return payload
    a.client.chat.completions.create.return_value = SimpleNamespace(
        id="budgeted", model="test/model", usage=None,
        choices=[SimpleNamespace(index=0, finish_reason="stop",
                                 message=_mock_assistant_msg(content="Recorded."))])
    with patch.object(a, "_build_api_kwargs", side_effect=capture_messages):
        result = _run(a, "Acknowledge the reference data.")
    wire = a.client.chat.completions.create.call_args.kwargs
    cap = wire.get("max_completion_tokens", wire.get("max_tokens"))
    assert 0 < cap < a.max_tokens
    assert wire["messages"] == built_messages
    assert result["completed"] is True


def test_ordinary_tool_round_does_not_import_the_delegation_runtime():
    import sys
    from run_agent import AIAgent
    calls = [_mock_tool_call("read_file", '{"path":"fixture"}', "read-only")]
    with patch.dict(sys.modules, {"tools.delegate_tool": None}):
        assert AIAgent._cap_delegate_task_calls(calls) is calls


def test_reasoning_exhaustion_recovery_stays_off_until_episode_finishes(loop_agent):
    a = loop_agent
    a.reasoning_config = {"enabled": True, "effort": "high"}
    a.request_overrides = {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
    def reply(content, finish):
        return SimpleNamespace(id="provider-response", model="test/model", usage=None,
            choices=[SimpleNamespace(index=0, finish_reason=finish,
                                     message=_mock_assistant_msg(content=content))])
    a.client.chat.completions.create.side_effect = [
        reply(None, "length"), reply("Partial answer. ", "length"),
        reply("Completed answer.", "stop"), reply("Next answer.", "stop")]
    first = _run(a, "Answer the question.")
    assert first["completed"] is True
    assert _run(a, "A new question.", first["messages"])["completed"] is True
    switches = [call.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"]
                for call in a.client.chat.completions.create.call_args_list]
    assert switches == [True, False, False, True]


def test_rebuilding_request_does_not_lose_episode_reasoning_override():
    from agent.chat_completion_helpers import _reasoning_config_for_wire
    a = SimpleNamespace(reasoning_config={"enabled": True, "effort": "high"},
                        _ephemeral_reasoning_off=True, _length_reasoning_exhausted=True)
    for _ in range(3):
        assert _reasoning_config_for_wire(a)["enabled"] is False
    a._length_reasoning_exhausted = False
    assert _reasoning_config_for_wire(a)["enabled"] is True
    a._length_reasoning_exhausted = True
    a._reasoning_disable_rejected = True
    assert _reasoning_config_for_wire(a)["enabled"] is True
