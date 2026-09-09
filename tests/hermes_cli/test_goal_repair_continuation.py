"""Standing mission failures are resumable work; explicit stops remain stops."""
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def goal_env(tmp_path, monkeypatch):
    from hermes_cli import goals
    home = tmp_path / 'hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    goals._DB_CACHE.clear()
    yield goals
    goals._DB_CACHE.clear()


@pytest.mark.parametrize('failure,repeats', [
    (('continue', 'next repair is known', False, None, False), 5),
    (('continue', 'judge parse failure', True, None, False), 4),
    (('continue', 'judge transport failure', False, None, True), 6),
    (('blocked', 'owned test fixture needs repair', False, None, False), 1),
])
def test_failure_checkpoint_survives_reload_and_reaches_outcome(goal_env, monkeypatch, failure, repeats):
    goals = goal_env
    sid = 'continuation-' + uuid.uuid4().hex
    mgr = goals.GoalManager(sid, default_max_turns=2)
    mgr.set('repair the fixture and verify the result')
    monkeypatch.setattr(goals, 'judge_goal', lambda *args, **kwargs: failure)
    for step in range(repeats):
        decision = mgr.evaluate_after_turn(f'Owned repair step {step}; outcome still pending.')
        assert decision['should_continue'] is True
        assert decision['status'] == 'active'
        mgr = goals.GoalManager(sid)
        assert mgr.state.goal == 'repair the fixture and verify the result'
        assert mgr.state.status == 'active'
    monkeypatch.setattr(goals, 'judge_goal', lambda *args, **kwargs: ('done', 'outcome proved', False, None, False))
    assert mgr.evaluate_after_turn('Outcome evidence confirms acceptance.')['status'] == 'done'


def test_user_pause_and_explicit_hard_budget_stop_automatic_dispatch(goal_env, monkeypatch):
    goals = goal_env
    monkeypatch.setattr(goals, 'judge_goal', lambda *args, **kwargs: ('continue', 'more work', False, None, False))
    mgr = goals.GoalManager('limits-' + uuid.uuid4().hex, default_max_turns=1)
    mgr.set('repair within the explicit limit', max_total_turns=2)
    assert mgr.evaluate_after_turn('first repair')['should_continue'] is True
    exhausted = mgr.evaluate_after_turn('second repair')
    assert exhausted['should_continue'] is False
    assert 'resource' in mgr.state.paused_reason
    mgr.resume()
    mgr.pause(reason='user requested stop')
    assert mgr.evaluate_after_turn('do not resume')['should_continue'] is False




def test_empty_turn_recovery_is_durable_bounded_and_never_judged_done(goal_env, monkeypatch):
    monkeypatch.setattr(goal_env, "workspace_fingerprint", lambda: "stable")
    monkeypatch.setattr(goal_env, "judge_goal", lambda *a, **kw: pytest.fail("empty turn reached judge"))
    mgr = goal_env.GoalManager("empty-" + uuid.uuid4().hex)
    mgr.set("repair the owned fixture")
    for _ in range(2):
        assert mgr.evaluate_after_turn("")["should_continue"]
        mgr = goal_env.GoalManager(mgr.session_id)
    assert not mgr.evaluate_after_turn("")["should_continue"]
    assert mgr.state.status == "paused"
    assert "NO_PROGRESS" in mgr.state.paused_reason


def test_gate_fingerprint_detects_second_edit_to_already_dirty_file(goal_env, tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init")
    target = repo / "source.txt"
    target.write_text("baseline")
    git("add", "source.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    target.write_text("broken implementation")
    before = goal_env.workspace_fingerprint(str(repo))
    target.write_text("repaired implementation")
    after = goal_env.workspace_fingerprint(str(repo))
    assert before and after and before != after


def test_explicit_per_goal_turn_limit_is_not_reinterpreted_as_batch_size(goal_env, monkeypatch):
    monkeypatch.setattr(goal_env, "judge_goal", lambda *a, **kw: ("continue", "more work", False, None, False))
    mgr = goal_env.GoalManager("explicit-" + uuid.uuid4().hex)
    mgr.set("repair within one turn", max_turns=1)
    assert not mgr.evaluate_after_turn("still needs work")["should_continue"]
    assert mgr.state.max_total_turns == 1


def test_task_workspace_survives_helper_directory_change_and_reload(goal_env, tmp_path, monkeypatch):
    task_dir = tmp_path / "mission"
    helper_dir = tmp_path / "helper"
    task_dir.mkdir()
    helper_dir.mkdir()
    from agent import runtime_cwd
    monkeypatch.setattr(runtime_cwd, "resolve_agent_cwd", lambda: task_dir)
    mgr = goal_env.GoalManager("scope-" + uuid.uuid4().hex)
    mgr.set("repair calculator.py")
    monkeypatch.chdir(helper_dir)
    reloaded = goal_env.GoalManager(mgr.session_id)
    assert reloaded.state.workspace == str(task_dir)
    assert str(task_dir) in reloaded.next_continuation_prompt()
    assert str(helper_dir) not in reloaded.workspace_instruction()
