"""Provider-proven context overflow must bypass automatic compression cooldown."""

from unittest.mock import ANY, MagicMock, patch

from run_agent import AIAgent
from tests.run_agent.test_run_agent import _make_tool_defs, _mock_response


def test_output_cap_recovery_forces_compression():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = True
    agent.save_trajectories = False
    agent.api_mode = "chat_completions"
    agent.provider = "openrouter"
    agent.model = "some/model"
    agent.max_tokens = 65_536
    agent.context_compressor.context_length = 200_000
    agent.context_compressor.should_compress = MagicMock(return_value=False)

    overflow = Exception(
        "max_tokens: 65536 > context_window: 200000 "
        "- input_tokens: 199000 = available_tokens: 1000"
    )
    overflow.status_code = 400
    overflow.code = 400
    agent.client.chat.completions.create.side_effect = [
        overflow,
        _mock_response(content="done", finish_reason="stop"),
    ]

    compressed = MagicMock(
        return_value=([{"role": "user", "content": "hello"}], "You are helpful.")
    )
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent.context_compressor, "update_model"),
        patch.object(agent, "_compress_context", compressed),
    ):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    compressed.assert_called_once()
    assert compressed.call_args.kwargs["force"] is True


def test_input_overflow_relaxes_protected_tail_before_forced_compression():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="http://127.0.0.1:8892/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = True
    agent.save_trajectories = False
    agent.api_mode = "chat_completions"
    agent.provider = "custom"
    agent.model = "qwen38-27b-unsloth-nvfp4-mtp1"
    agent.context_compressor.context_length = 65_536
    agent.context_compressor.should_compress = MagicMock(return_value=False)

    overflow = Exception("Prompt exceeds max length")
    overflow.status_code = 400
    overflow.code = 400
    agent.client.chat.completions.create.side_effect = [
        overflow,
        _mock_response(content="done", finish_reason="stop"),
    ]
    pressure_messages = [{"role": "user", "content": "bounded"}]
    pressure = MagicMock(return_value=(pressure_messages, 2, 1))
    compressed = MagicMock(return_value=(pressure_messages, "You are helpful."))

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_compress_context", compressed),
        patch(
            "agent.conversation_loop.anchored_context_tokens",
            return_value=77_500,
        ),
        patch(
            "agent.conversation_loop.relieve_forced_overflow_tail_pressure",
            pressure,
        ),
    ):
        result = agent.run_conversation(
            "same prompt",
            conversation_history=[
                {"role": "assistant", "content": "A" * 80_000},
                {"role": "user", "content": "same prompt"},
            ],
        )

    assert result["completed"] is True
    pressure.assert_called_once_with(
        ANY,
        current_tokens=77_500,
        context_length=65_536,
    )
    compressed.assert_called_once()
    assert compressed.call_args.args[0] is pressure_messages
    assert compressed.call_args.kwargs["force"] is True
