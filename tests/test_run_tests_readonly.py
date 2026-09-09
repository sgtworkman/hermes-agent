"""Canonical test runs confine configuration discovery and cache writes."""
import importlib.util
from pathlib import Path


def _runner():
    path = Path(__file__).resolve().parents[1] / "scripts/run_tests_parallel.py"
    spec = importlib.util.spec_from_file_location("readonly_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parent_configuration_cannot_enter_a_project_test_run(tmp_path):
    runner = _runner()
    parent = tmp_path / "outside"
    project = parent / "project"
    project.mkdir(parents=True)
    (parent / "conftest.py").write_text("raise RuntimeError('outside-project configuration loaded')\n")
    test = project / "test_fixture.py"
    test.write_text("def test_fixture():\n    assert 2 + 3 == 5\n")
    _, code, output, counts, _ = runner._run_one_file_once(test, [], project, 30)
    assert code == 0, output
    assert counts.get("passed") == 1
    assert not (project / ".pytest_cache").exists()


def test_explicit_cache_root_preserves_checkout_and_separates_results(tmp_path, monkeypatch):
    runner = _runner()
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    runner._save_durations([(first / "test_fixture.py", 1.25)], first)
    assert runner._load_durations(first) == {"test_fixture.py": 1.25}
    assert runner._load_durations(second) == {}
    assert not (first / "test_durations.json").exists()
