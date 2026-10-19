"""Gateway service launch environment keeps the install-bound Hermes launcher."""

import json
import plistlib
from pathlib import Path


def test_launchd_and_systemd_publish_hermes_bin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import gateway

    project_root = tmp_path / "hermes-agent"
    monkeypatch.setattr(gateway, "PROJECT_ROOT", project_root)
    store = tmp_path / "store"
    interpreter = store / "python-fixture" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    (interpreter.parent.parent / "python.exe").touch()
    (store / "facts.json").write_text(json.dumps({
        "packages": {"python": {"entry": "python-fixture"}},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))

    launchd = plistlib.loads(gateway.generate_launchd_plist().encode("utf-8"))
    expected = str(project_root / ".hermes/bin/hermes")
    assert launchd["EnvironmentVariables"]["HERMES_BIN"] == expected

    systemd = gateway.generate_systemd_unit()
    assert f'Environment="HERMES_BIN={expected}"' in systemd
