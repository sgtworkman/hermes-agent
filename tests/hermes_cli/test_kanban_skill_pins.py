"""Pinned ``skills`` resolve against the ASSIGNEE profile's own skill tree.

A card whose pin its assignee cannot load used to be created, dispatched and die at
``gateway/session_kanban.py``'s ``kanban_skills_unavailable`` guard before any model
call — a ready->crash->ready loop. These tests pin the three behaviours that close it:

* create-time refusal on both surfaces (CLI ``--skill`` and the ``kanban_create`` tool)
  through the one seam they share, ``kanban_db.create_task``;
* the dispatcher parking (not spawning) a pre-existing card with an unresolvable pin;
* no false positives: a pin the profile really has still creates and dispatches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def profile_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with a named ``coder`` profile that owns one real skill."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    profile = home / "profiles" / "coder"
    # A named profile is only a profile when it carries identity (same predicate
    # profile_exists uses); without it the tree is unverifiable, not empty.
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    skills = profile / "skills"
    skill_dir = skills / "devops" / "present-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: present-skill\ndescription: A skill this profile really has.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    kb.init_db()
    return home


def test_create_task_refuses_pin_the_assignee_cannot_load(profile_home: Path) -> None:
    """The message names the missing skill and the assignee, and no row is written."""
    with kbc.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(conn, title="bogus pin", assignee="coder", skills=["absent-skill"])
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    message = str(excinfo.value)
    assert "absent-skill" in message
    assert "coder" in message
    assert after == before


def test_create_task_names_every_missing_pin_and_keeps_the_valid_one_out(
    profile_home: Path,
) -> None:
    with kbc.connect() as conn:
        with pytest.raises(ValueError) as excinfo:
            kb.create_task(
                conn, title="mixed pins", assignee="coder",
                skills=["present-skill", "absent-one", "absent-two"],
            )
    message = str(excinfo.value)
    assert "absent-one" in message and "absent-two" in message
    assert "present-skill" not in message  # only the unresolvable pins are named


def test_create_task_accepts_a_pin_the_assignee_owns(profile_home: Path) -> None:
    """No false positives: a real skill still creates, and the pin is stored."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="valid pin", assignee="coder", skills=["present-skill"])
        task = kb.get_task(conn, task_id)
    assert task.skills == ["present-skill"]


def test_create_task_resolves_the_assignee_not_the_creator(profile_home: Path) -> None:
    """A skill under the CREATOR's tree is not a licence to pin it on another profile."""
    other = profile_home / "profiles" / "other"
    skill_dir = other / "skills" / "only-here"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: only-here\ndescription: Belongs to another profile.\n---\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="cross-profile pin", assignee="coder", skills=["only-here"])


def test_unverifiable_assignee_does_not_refuse(profile_home: Path) -> None:
    """A profile with no home on disk (isolated fixtures, control-plane lanes) cannot be
    checked, so creation proceeds — the runtime guard stays the backstop."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="unknown profile", assignee="ghost", skills=["absent-skill"])
        assert kb.get_task(conn, task_id) is not None


def test_dispatcher_parks_a_preexisting_unresolvable_pin(profile_home: Path) -> None:
    """A row written before validation existed is parked blocked/needs_input, not spawned."""
    spawned: list[str] = []

    def spawn(task, workspace, board=None):
        spawned.append(task.id)

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="pre-existing", assignee="coder")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET skills = ? WHERE id = ?",
                (json.dumps(["absent-skill"]), task_id),
            )
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
        row = conn.execute(
            "SELECT status, block_kind, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (task_id,),
        ).fetchall()

    assert task_id not in spawned
    assert result.spawned == []
    assert result.parked_unresolvable_skills == [(task_id, ["absent-skill"])]
    assert row["status"] == "blocked"
    assert row["block_kind"] == "needs_input"
    assert row["current_run_id"] is None
    blocked = [e for e in events if e["kind"] == "blocked"]
    assert blocked and "absent-skill" in json.loads(blocked[-1]["payload"])["reason"]


def test_dispatcher_still_spawns_a_resolvable_pin(profile_home: Path) -> None:
    """The park step must not withhold a card whose pin the assignee owns."""
    spawned: list[str] = []

    def spawn(task, workspace, board=None):
        spawned.append(task.id)

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="valid", assignee="coder", skills=["present-skill"])
        result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert spawned == [task_id]
    assert [t[0] for t in result.spawned] == [task_id]
    assert result.parked_unresolvable_skills == []
