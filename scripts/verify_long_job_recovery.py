"""Read-only verification entrypoint; canonical tests execute in a scratch source copy."""
from pathlib import Path
import ast
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("run_agent.py", "agent/chat_completion_helpers.py", "agent/conversation_loop.py", "agent/turn_truncation.py",
           "agent/turn_final_response.py", "agent/request_output_budget.py", "agent/turn_api_request.py",
           "agent/turn_tool_round.py", "agent/transports/chat_completions.py")
TEST = "tests/agent/test_long_job_recovery.py"


def run_tests(mode):
    with tempfile.TemporaryDirectory(prefix="recovery-review-") as temporary:
        scratch = Path(temporary)
        checkout = scratch / "source"
        shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns(
            ".git", ".audit-runtime", "node_modules", "__pycache__", ".pytest_cache"))
        for name in (*SOURCES, TEST):
            assert hashlib.sha256((ROOT / name).read_bytes()).digest() == hashlib.sha256((checkout / name).read_bytes()).digest()
        env = dict(os.environ, HERMES_PYTHON=sys.executable, HOME=str(scratch), TMPDIR=str(scratch))
        argv = ["bash", "scripts/run_tests.sh", TEST]
        if mode == "adversarial":
            argv += ["tests/run_agent/test_continuation_ceiling_wedge.py",
                     "tests/run_agent/test_length_continuation_thinking_exhaustion.py",
                     "tests/test_output_cap_parsing.py"]
        completed = subprocess.run(argv, cwd=checkout, env=env, timeout=180, check=False)
        return completed.returncode


mode = sys.argv[1]
if mode == "syntax":
    for name in (*SOURCES, TEST, "scripts/verify_long_job_recovery.py"):
        ast.parse((ROOT / name).read_text(), filename=name)
    print("Parsed every changed source and verifier.")
elif mode == "static":
    for name in SOURCES:
        tree = ast.parse((ROOT / name).read_text())
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id in {"eval", "exec"} for n in ast.walk(tree))
    request = ast.parse((ROOT / "agent/turn_api_request.py").read_text())
    build = next(n for n in request.body if isinstance(n, ast.FunctionDef) and n.name == "build_api_request")
    assert "request_pressure_tokens" in [a.arg for a in build.args.kwonlyargs]
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "clamp_chat_output_budget" for n in ast.walk(build))
    print("Final request budget is wired; changed sources contain no dynamic evaluation.")
elif mode in {"unit", "adversarial"}:
    raise SystemExit(run_tests(mode))
else:
    raise ValueError("Unknown verification mode")
