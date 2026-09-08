"""Run dated-continuation checks in a disposable copy of the source checkout."""
from pathlib import Path
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ["hermes_cli/kanban.py", "hermes_cli/kanban_db.py", "hermes_cli/kanban_db_connect.py",
         "hermes_cli/kanban_db_schedule.py", "hermes_cli/kanban_parser.py", "hermes_cli/kanban_output.py",
         "tools/kanban_tools.py", "tools/kanban_tools_schemas.py", "hermes_cli/goals.py", "agent/kanban_stop.py"]
TESTS = ["tests/hermes_cli/test_kanban_dated_schedule.py", "tests/hermes_cli/test_kanban_db.py",
         "tests/hermes_cli/test_kanban_cli.py", "tests/hermes_cli/test_kanban_goal_mode.py",
         "tests/hermes_cli/test_kanban_db_init.py", "tests/tools/test_kanban_tools.py",
         "tests/agent/test_kanban_stop.py", "tests/hermes_cli/test_kanban_dispatch_tick_hook.py"]


def main(mode):
    if mode in {"syntax", "static"}:
        for name in FILES + TESTS + ["scripts/verify_kanban_dated_schedule.py"]:
            source = (ROOT / name).read_text()
            ast.parse(source, filename=name)
            compile(source, name, "exec")
        print(json.dumps({"mode": mode, "parsed_modules": len(FILES + TESTS) + 1}))
        return 0
    if mode not in {"unit", "adversarial"}:
        raise ValueError("unknown verification mode")
    with tempfile.TemporaryDirectory(prefix="dated-wake-check-") as scratch:
        repo = Path(scratch) / "repo"
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(
            ".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"))
        (repo / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
        probe = subprocess.run([str(repo / ".venv/bin/python"), "-c", "import pytest; print(pytest.__file__)"], capture_output=True, text=True)
        if probe.returncode:
            raise RuntimeError(probe.stderr)
        toolbin = Path(scratch) / "bin"
        toolbin.mkdir()
        git = toolbin / "git"
        git.write_text('#!/bin/sh\nGIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 exec /usr/bin/git "$@"\n')
        git.chmod(0o700)
        selected = TESTS if mode == "unit" else TESTS[:1]
        result = subprocess.run(["/bin/bash", "scripts/run_tests.sh", *selected,
                                 "-q", "--file-retries", "0", "-j", "4"], cwd=repo,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PATH": str(toolbin) + os.pathsep + os.defpath}, timeout=240)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
