"""Create-time skill-pin validation seam for ``hermes_cli.kanban_db.create_task``.

Split out of ``hermes_cli.kanban_db`` so the facade keeps its line ratchet; the
resolver itself lives in ``hermes_cli.kanban_skills_validation``. Origin-resident
helpers are reached late-bound via ``_kb`` so monkeypatching ``kanban_db.<name>``
keeps working.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

import hermes_cli.kanban_db as _kb


def _normalize_task_skills(skills: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Strip/dedupe a skills list. Commas are refused (a comma-joined string must
    not land in one argv slot); toolset names are rejected all at once because
    agents that confuse the two usually pass several."""
    if skills is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    toolset_typos: list[str] = []
    for s in skills:
        if not s:
            continue
        name = str(s).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in _kb.KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(n) for n in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned


def _check_task_skills(
    conn: sqlite3.Connection, skills: Optional[Iterable[str]], assignee: Optional[str],
) -> Optional[list[str]]:
    """Normalize ``skills`` and refuse pins the ASSIGNEE profile cannot load.

    Returns the normalized list for the caller to persist. A pin that profile does
    not have would be created, dispatched and die at the worker's
    ``kanban_skills_unavailable`` guard before any model call, so it is refused
    here — the one seam the CLI ``--skill`` flag and the ``kanban_create`` tool share.
    """
    from hermes_cli.kanban_skills_validation import validate_skill_pins

    skills_list = _normalize_task_skills(skills)
    validate_skill_pins(skills_list, assignee)
    return skills_list
