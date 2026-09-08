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
            "tests/agent/test_kanban_stop.py", "tests/hermes_cli/test_kanban_review_lifecycle_complete.py",
            "tests/hermes_cli/test_codex_runtime_plugin_migration.py", "-q"],
        ("--kanban-negative",): ["tests/agent/transports/test_codex_app_server_runtime.py",
            "-q", "-k", "kanban_worker"],
        ("--selector-ui",): ["src/lib/reasoning-effort.test.ts", "src/app/shell/model-edit-submenu.test.tsx"],
        ("--selector-ui-negative",): ["src/app/shell/model-edit-submenu.test.tsx", "-t", "renders only enforced endpoint levels"],
    }
    selected = choices.get(tuple(sys.argv[1:]))
    if selected is None:
        raise ValueError("Unknown scoped regression selection")
    ui = sys.argv[1:] in (["--selector-ui"], ["--selector-ui-negative"])
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
        excluded = {".github", "docs", "examples"} | (set() if ui else {"apps"})
        if not name or Path(name).parts[0] in excluded:
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
    if ui:
        # Each dependency remains read-only, while Vite's sibling cache directory
        # is created in scratch instead of inside the installed node_modules.
        for relative in ("node_modules", "apps/desktop/node_modules"):
            dependencies = source / relative
            destination = checkout / relative
            if not dependencies.is_dir():
                continue
            destination.mkdir(parents=True, exist_ok=True)
            for dependency in dependencies.iterdir():
                if dependency.name.startswith("."):
                    continue
                (destination / dependency.name).symlink_to(dependency.resolve(), target_is_directory=dependency.is_dir())
        node = shutil.which("node")
        if not node:
            raise ValueError("Node is required for selector UI verification")
        result = subprocess.run([node, str(source / "node_modules/vitest/vitest.mjs"),
            "run", *selected, "--cache=false", "--maxWorkers=1"],
            cwd=checkout / "apps/desktop", env=env)
    else:
        result = subprocess.run(["bash", str(checkout / "scripts/run_tests.sh"),
            *selected], cwd=checkout, env=env)
    changed = [name for name, expected in hashes.items()
               if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected]
    print(json.dumps({"source_files_bound": len(hashes), "source_unchanged": not changed,
                      "canonical_runner_exit": result.returncode}))
    return result.returncode if not changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
