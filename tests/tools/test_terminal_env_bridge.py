"""Behavioral regressions for the terminal config → env bridge.

``terminal_tool._get_env_config()`` reads TERMINAL_* variables.  The bridge
must let explicitly configured terminal keys override stale launcher/.env
values while preserving environment values for terminal keys omitted from
config.yaml.
"""

import os

import pytest

import tools.terminal_tool as terminal_tool
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _reset_bridge_state(monkeypatch):
    """Each test starts with an un-attempted bridge and clean mapped env."""
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", False)
    for name in (
        "TERMINAL_ENV",
        "TERMINAL_CWD",
        "TERMINAL_DOCKER_IMAGE",
        "TERMINAL_SSH_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def _write_config(text: str) -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text)


def test_unset_terminal_env_backfills_backend_from_config():
    _write_config(
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: custom/image:1\n"
    )

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "custom/image:1"
    assert os.environ["TERMINAL_ENV"] == "docker"


def test_explicit_config_backend_overrides_stale_env(monkeypatch):
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert os.environ["TERMINAL_ENV"] == "docker"


def test_partial_terminal_config_preserves_unrelated_env_values(monkeypatch):
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "env/image:2")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "env/image:2"
    assert os.environ["TERMINAL_DOCKER_IMAGE"] == "env/image:2"


def test_explicit_config_key_overrides_matching_env_value(monkeypatch):
    _write_config(
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: config/image:1\n"
    )
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "env/image:2")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "config/image:1"


def test_ssh_config_preserves_remote_tilde_cwd(monkeypatch):
    """SSH ``~`` belongs to the remote user, not the Hermes host/container."""
    _write_config("terminal:\n  backend: ssh\n  cwd: '~'\n")
    monkeypatch.setenv("HOME", "/opt/data/home")
    monkeypatch.setenv("USERPROFILE", r"C:\opt\data\home")

    config = terminal_tool._get_env_config()

    assert os.environ["TERMINAL_CWD"] == "~"
    assert config["cwd"] == "~"


def test_env_is_preserved_when_config_has_no_terminal_section(monkeypatch):
    _write_config("agent:\n  max_turns: 100\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "example.test")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "example.test"


def test_defaults_backfill_when_neither_config_nor_env_selects_backend():
    _write_config("{}\n")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    assert os.environ["TERMINAL_ENV"] == "local"


def test_bridge_only_attempted_once(monkeypatch):
    calls = []

    import hermes_cli.config as config_mod

    real = config_mod.apply_terminal_config_to_env

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(config_mod, "apply_terminal_config_to_env", _counting)
    _write_config("{}\n")

    terminal_tool._get_env_config()
    terminal_tool._get_env_config()

    assert len(calls) == 1


def test_bridge_config_failure_does_not_crash(monkeypatch):
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "read_raw_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config read failed")),
    )
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "example.test")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "example.test"


def test_cli_workspace_is_not_overwritten_by_later_fallback_bridge(tmp_path, monkeypatch):
    task = tmp_path / "task"
    configured = tmp_path / "profile-default"
    task.mkdir()
    configured.mkdir()
    _write_config("terminal:\n  backend: local\n  cwd: " + str(configured) + "\n")
    monkeypatch.chdir(task)
    from types import SimpleNamespace
    from hermes_cli.main import _apply_in_dir
    _apply_in_dir(SimpleNamespace(in_dir=str(task)))
    import cli
    cli.load_cli_config()
    assert os.environ["TERMINAL_CWD"] == str(task)
    from hermes_cli.env_loader import _reapply_terminal_config_bridge
    _reapply_terminal_config_bridge(get_hermes_home())
    assert os.environ["TERMINAL_CWD"] == str(task)
    actual = terminal_tool._get_env_config()
    assert actual["cwd"] == str(task)
    assert os.environ["TERMINAL_CWD"] == str(task)


@pytest.mark.parametrize("backend,expected", [("local", "task"), ("docker", "/container-task")])
def test_runtime_workspace_preserves_backend_and_survives_partial_config(tmp_path, monkeypatch, backend, expected):
    task = tmp_path / "task"
    task.mkdir()
    _write_config("terminal:\n  backend: " + backend + "\n" +
                  ("  cwd: /container-task\n" if backend == "docker" else ""))
    from hermes_cli.config import apply_terminal_config_to_env, set_terminal_runtime_cwd
    set_terminal_runtime_cwd(str(task))
    monkeypatch.setenv("TERMINAL_CWD", "/stale-env-directory")
    apply_terminal_config_to_env()
    assert os.environ["TERMINAL_ENV"] == backend
    assert os.environ["TERMINAL_CWD"] == (str(task) if expected == "task" else expected)
