"""Kanban dispatch parks a card when its assignee cannot load any task skill.

Without this preflight, the worker exits with ``kanban_skills_unavailable`` after
the card has been claimed; the dispatcher then reclaims/requeues it and burns a
spawn attempt on every tick.  The card must be blocked once, with a capability
reason naming the missing skills.
"""
from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_dispatch_blocks_when_all_required_skills_are_unavailable(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(
        kbd,
        "_task_skill_preflight",
        lambda assignee, task_id, skills: ["rfd-deals-pipeline", "incident-learning-loop"],
    )
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="demo",
            assignee="coder",
            skills=["rfd-deals-pipeline", "incident-learning-loop"],
        )
        res = kbd.dispatch_once(conn, dry_run=False)
        task = kb.get_task(conn, tid)
        events = [(e.kind, e.payload) for e in kb.list_events(conn, tid)]

    assert res.blocked_unavailable_skills == [(tid, "rfd-deals-pipeline, incident-learning-loop")]
    assert task is not None and task.status == "blocked"
    assert task.block_kind == "capability"
    blocked = [payload for kind, payload in events if kind == "blocked"]
    assert blocked
    assert "cannot load any required skill" in str(blocked[-1].get("reason", ""))


def test_dispatch_dry_run_reports_skill_block_without_writing(tmp_path, monkeypatch):
    _isolated_home(tmp_path, monkeypatch)
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(kbd, "_task_skill_preflight", lambda *args: ["missing"])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="demo", assignee="coder", skills=["missing"])
        res = kbd.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, tid)
        kinds = [e.kind for e in kb.list_events(conn, tid)]

    assert res.blocked_unavailable_skills == [(tid, "missing")]
    assert task is not None and task.status == "ready"
    assert "blocked" not in kinds
