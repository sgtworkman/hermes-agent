#!/usr/bin/env python3
"""Run canonical reasoning regressions in a writable copy inside audit scratch.

The canonical runner writes duration/cache files. Copy Git-listed source into
TMPDIR so a read-only audit can run it without permitting repository writes.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    choices = {
        (): ["tests/hermes_cli/test_custom_reasoning_contract.py", "-q"],
        ("--negative",): ["tests/hermes_cli/test_custom_reasoning_contract.py", "-q", "-k", "malformed"],
        ("--kanban-contract",): ["tests/agent/transports/test_codex_app_server_runtime.py",
            "tests/agent/transports/test_hermes_tools_mcp_server.py",
            "tests/hermes_cli/test_codex_runtime_plugin_migration.py", "-q"],
        ("--kanban-negative",): ["tests/agent/transports/test_codex_app_server_runtime.py",
            "-q", "-k", "kanban_worker"],
    }
    selected = choices.get(tuple(sys.argv[1:]))
    if selected is None:
        raise ValueError("Unknown scoped regression selection")
    source = Path(__file__).resolve().parent.parent
    scratch = Path(tempfile.mkdtemp(prefix="hermes-reasoning-"))
    checkout = scratch / "source"
    checkout.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=source, env=env).decode().split("\0")
    hashes = {}
    for name in sorted(set(names)):
        if not name or Path(name).parts[0] in {"apps", ".github", "docs", "examples"}:
            continue
        path = source / name
        if not path.is_file() or path.is_symlink():
            continue
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        if hashlib.sha256(target.read_bytes()).hexdigest() != before:
            raise ValueError("Source copy hash mismatch")
        hashes[name] = before
    home = scratch / "home"
    home.mkdir()
    # Preserve the virtualenv invocation path: audit runners may resolve the
    # outer interpreter symlink to its base Python, which has no test extras.
    python = source / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("The repository test virtualenv is required")
    env.update(HOME=str(home), TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch),
               HERMES_PYTHON=str(python), HERMES_TEST_WORKERS="1")
    result = subprocess.run(["bash", str(checkout / "scripts/run_tests.sh"),
        *selected], cwd=checkout, env=env)
    changed = [name for name, expected in hashes.items()
               if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected]
    print(json.dumps({"source_files_bound": len(hashes), "source_unchanged": not changed,
                      "canonical_runner_exit": result.returncode}))
    return result.returncode if not changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
