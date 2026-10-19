"""Service children use this installation's declared launcher or own interpreter."""

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import gateway
from hermes_cli._launchers import installation_command
from hermes_cli.kanban_db_dispatch import _resolve_hermes_argv
from tests.installation_launcher_fixture import publish_fixture_launcher

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def installation(tmp_path, monkeypatch):
    user = tmp_path / "user"
    home = user / "hermes-home"
    root = tmp_path / "installation" / "tree"
    home.mkdir(parents=True)
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: user)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(gateway, "PROJECT_ROOT", root)
    return root, home


def _store(directory):
    python = directory / "python-fixture" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys._base_executable)
    (directory / "facts.json").write_text(json.dumps({
        "packages": {"python": {"entry": "python-fixture"}},
    }), encoding="utf-8")


def _publish(root):
    return publish_fixture_launcher(root, """
import json, sys
from pathlib import Path
def main():
    print(json.dumps({'root': str(Path(__file__).resolve().parents[1]), 'args': sys.argv[1:]}))
""")


def _service_environment():
    plist = plistlib.loads(gateway.generate_launchd_plist().encode("utf-8"))
    environment = plist["EnvironmentVariables"]
    unit = gateway.generate_systemd_unit()
    assert environment["HERMES_SUPERVISED_CHILD"] == "1"
    assert 'Environment="HERMES_SUPERVISED_CHILD=1"' in unit
    return environment, unit


def _assert_bound_child(root, launcher, environment, monkeypatch):
    monkeypatch.setenv("HERMES_BIN", environment["HERMES_BIN"])
    assert _resolve_hermes_argv() == [str(launcher)]
    command = installation_command(root, ["--version"])
    assert command == [str(launcher), "--version"]
    result = subprocess.run(command, env={**os.environ, **environment},
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"root": str(root), "args": ["--version"]}


def test_external_runtime_keeps_its_interpreter_instead_of_missing_source_shim(installation):
    root, _ = installation
    environment, unit = _service_environment()
    assert "HERMES_BIN" not in environment
    assert 'Environment="HERMES_BIN=' not in unit
    assert not (root / ".hermes" / "bin" / "hermes").exists()
    assert _resolve_hermes_argv() == [sys.executable, "-m", "hermes_cli.main"]


def test_managed_source_service_preserves_the_published_child(installation, monkeypatch):
    root, home = installation
    launcher = _publish(root)
    _store(home / "tools")
    environment, unit = _service_environment()
    assert environment["HERMES_BIN"] == str(launcher)
    assert f'Environment="HERMES_BIN={launcher}"' in unit
    _assert_bound_child(root, launcher, environment, monkeypatch)


@pytest.mark.parametrize("name", ["hermes channel & 50%$", "hermes-channel.exe"])
def test_payload_uses_exact_manifest_command_and_literal_environment(installation, monkeypatch, name):
    root, _ = installation
    source_launcher = _publish(root)
    launcher = root.parent / "bin" / name
    launcher.parent.mkdir()
    source_launcher.rename(launcher)
    _store(root.parent / "store")
    (root.parent / "manifest.json").write_text(json.dumps({
        "repo": root.name,
        "store": "store",
        "runtime": {"commands": {"hermes": str(launcher.relative_to(root.parent))}},
    }), encoding="utf-8")
    environment, unit = _service_environment()
    assert environment["HERMES_BIN"] == str(launcher)
    assignment = json.dumps("HERMES_BIN=" + str(launcher).replace("%", "%%"))
    assert "Environment=" + assignment in unit
    _assert_bound_child(root, launcher, environment, monkeypatch)


def test_payload_cannot_direct_service_children_outside_its_installation(installation):
    root, _ = installation
    (root.parent / "manifest.json").write_text(json.dumps({
        "repo": root.name,
        "runtime": {"commands": {"hermes": "../foreign/hermes"}},
    }), encoding="utf-8")
    for generate in (gateway.generate_launchd_plist, gateway.generate_systemd_unit):
        with pytest.raises(RuntimeError, match="escapes its root"):
            generate()
