"""Dispatcher-side skill-pin parking, split out of ``hermes_cli.kanban_db_dispatch``.

A ready/review card whose pinned ``skills`` do not resolve in its assignee's profile
would be claimed, spawned, and die at the worker's ``kanban_skills_unavailable`` guard
before any model call — a ready->crash->ready loop that burns the card's retry budget.
Parking it for a human instead is the dispatcher half of the create-time check.

Origin-resident helpers are reached late-bound via ``_kb`` so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import hermes_cli.kanban_db as _kb
    from hermes_cli.kanban_db import Task
    from hermes_cli.kanban_db_dispatch import DispatchResult


def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)


def _park_unresolvable_skills(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: DispatchResult,
    *,
    dry_run: bool,
) -> bool:
    """Park a ready/review row whose pinned skills its assignee cannot load.

    Returns True when the row was parked (or, under ``dry_run``, reported as
    would-be parked) and must not be claimed this tick. A row with no pins, an
    assignee whose profile home is absent, or a resolution that raised is left
    alone: unverifiable is not the same as unresolvable, and the runtime guard
    at ``gateway/session_kanban.py`` stays the backstop.
    """
    import hermes_cli.kanban_db as _kb
    from hermes_cli.kanban_skills_validation import missing_skills_for_assignee

    task = _kb.get_task(conn, row["id"])
    if task is None or not task.skills:
        return False
    missing = missing_skills_for_assignee(task.skills, assignee)
    if not missing:
        return False
    task_id = task.id
    if dry_run:
        result.parked_unresolvable_skills.append((task_id, missing))
        return True
    reason = (
        f"skills_unresolvable: assignee {assignee!r} cannot load "
        f"{', '.join(repr(n) for n in missing)} — install the skill(s) in that "
        "profile or repin the card; it was not dispatched"
    )
    if not _kb.block_task(conn, task_id, reason=reason, kind="needs_input"):
        return False
    result.parked_unresolvable_skills.append((task_id, missing))
    _kb._log.warning(
        "kanban dispatcher: parked %s blocked — assignee %r cannot load pinned skill(s) %s",
        task_id, assignee, ", ".join(missing),
    )
    return True
