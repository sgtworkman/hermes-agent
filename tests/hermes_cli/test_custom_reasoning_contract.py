"""An owned endpoint's picker and both request paths obey the same vocabulary."""
import json

import pytest

from agent.transports.chat_completions import ChatCompletionsTransport
from hermes_cli.config_providers import get_custom_provider_reasoning_efforts


def test_route_contract_survives_reload_and_bounds_both_transports(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    model, route = "owned-model", "http://127.0.0.1:19991/v1"
    levels = ["low", "medium", "xhigh"]
    cfg = {"providers": {"owned": {"base_url": route, "model": model,
           "models": {model: {"reasoning_efforts": levels}}}}}
    (home / "config.yaml").write_text(json.dumps(cfg))
    assert get_custom_provider_reasoning_efforts(model, route + "/") == levels
    assert get_custom_provider_reasoning_efforts(model, route.replace("19991", "19992")) is None
    assert get_custom_provider_reasoning_efforts("another-model", route) is None

    import model_tools  # noqa: F401 -- real plugin registration
    from providers import get_provider_profile
    from hermes_cli.inventory import _apply_capabilities
    from hermes_cli.model_switch_providers import list_authenticated_providers
    rows = list_authenticated_providers(current_provider="owned", current_model=model,
        current_base_url=route, user_providers=cfg["providers"], custom_providers=[],
        probe_custom_providers=False, probe_current_custom_provider=False)
    rows = [row for row in rows if row["slug"] == "owned"]
    assert len(rows) == 1
    _apply_capabilities(rows)
    caps = rows[0]["capabilities"][model]
    assert caps["reasoning_efforts"] == levels
    assert caps["can_disable_reasoning"] is False
    for profile in [None, get_provider_profile("custom")]:
        for effort, expected in [("max", "xhigh"), ("ultra", "xhigh"), ("high", "medium"),
                                 ("minimal", "low"), ("medium", "medium"), ("none", "low")]:
            request = ChatCompletionsTransport().build_kwargs(
                model, [{"role": "user", "content": "hello"}], base_url=route,
                provider_profile=profile, reasoning_config={"enabled": True, "effort": effort},
                request_overrides={"reasoning_effort": effort, "extra_body": {"reasoning_effort": effort}},
            )
            assert request["reasoning_effort"] == expected
            assert request["extra_body"]["reasoning_effort"] == expected
    assert cfg["providers"]["owned"]["models"][model]["reasoning_efforts"] == levels


def test_malformed_declared_contract_is_not_silently_ignored():
    for bad in [[], "low", ["typo"], [None], ["low", 1]]:
        cfg = {"providers": {"owned": {"base_url": "http://localhost:19991/v1",
               "models": {"m": {"reasoning_efforts": bad}}}}}
        with pytest.raises(ValueError, match="reasoning_efforts"):
            get_custom_provider_reasoning_efforts("m", "http://localhost:19991/v1", config=cfg)
