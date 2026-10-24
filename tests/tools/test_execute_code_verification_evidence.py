"""The execute_code tool records ad-hoc verification evidence like a terminal run.

A foreground cell that names a managed ``hermes-verify-`` script is evidence; a cell that
names none records nothing. The handler must never turn a ledger failure into a tool error.
"""

import json
import tempfile
from pathlib import Path

import pytest

from agent.verification_evidence import verification_status


@pytest.fixture(autouse=True)
def _ledger_on(monkeypatch):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")


def _project(tmp_path: Path, monkeypatch, *, scratch_name: str = "hermes-verify-cell.py"):
    home = tmp_path / ".hermes" / "profiles" / "workman-executive"
    scratch = home / "cache" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("TMPDIR", "TMP", "TEMP", "HERMES_SCRATCH_DIR"):
        monkeypatch.delenv(key, raising=False)
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", None, raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(os_temp))
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text("{}", encoding="utf-8")
    script = scratch / scratch_name
    script.write_text("print('ok')\n", encoding="utf-8")
    return project, script


def _handler(monkeypatch, project, *, result):
    from tools import code_execution_tool

    monkeypatch.setattr(code_execution_tool, "execute_code", lambda **kwargs: json.dumps(result))
    monkeypatch.setattr(code_execution_tool, "_get_execution_mode", lambda: "project")
    monkeypatch.setattr(
        "tools.code_execution_env._resolve_child_cwd", lambda *args, **kwargs: str(project)
    )
    return code_execution_tool._execute_code_handler


def test_handler_records_a_passing_cell_that_names_a_managed_script(tmp_path, monkeypatch):
    project, script = _project(tmp_path, monkeypatch)
    handler = _handler(monkeypatch, project, result={"output": "ok", "exit_code": 0})

    result = handler(
        {"code": f"subprocess.run(['/usr/bin/python3', '{script}'], check=True)"},
        task_id="", session_id="s-cell",
    )

    assert json.loads(result)["exit_code"] == 0
    status = verification_status(session_id="s-cell", cwd=project)
    assert status["status"] == "passed"
    assert status["evidence"]["canonical_command"] == "ad-hoc verification script"


def test_handler_records_a_failing_cell_as_failed(tmp_path, monkeypatch):
    project, script = _project(tmp_path, monkeypatch)
    handler = _handler(monkeypatch, project, result={"output": "boom", "exit_code": 1})

    handler(
        {"code": f"subprocess.run(['/usr/bin/python3', '{script}'], check=True)"},
        task_id="", session_id="s-cell",
    )

    assert verification_status(session_id="s-cell", cwd=project)["status"] == "failed"


def test_handler_records_nothing_for_ordinary_python(tmp_path, monkeypatch):
    project, _ = _project(tmp_path, monkeypatch)
    handler = _handler(monkeypatch, project, result={"output": "hello", "exit_code": 0})

    handler({"code": "print('hello')"}, task_id="", session_id="s-cell")

    assert verification_status(session_id="s-cell", cwd=project)["status"] == "unverified"


def test_handler_result_survives_a_ledger_failure(tmp_path, monkeypatch):
    project, script = _project(tmp_path, monkeypatch)
    handler = _handler(monkeypatch, project, result={"output": "ok", "exit_code": 0})
    monkeypatch.setattr(
        "agent.verification_evidence.record_code_result",
        lambda **kwargs: (_ for _ in ()).throw(sqlite3_error()),
    )

    result = handler(
        {"code": f"subprocess.run(['/usr/bin/python3', '{script}'], check=True)"},
        task_id="", session_id="s-cell",
    )

    assert json.loads(result)["output"] == "ok"


def sqlite3_error():
    import sqlite3

    return sqlite3.OperationalError("ledger unavailable")
