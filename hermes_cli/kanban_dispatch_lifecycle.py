"""Read-only scheduled eligibility and the dispatch lifecycle phases."""
from __future__ import annotations

from datetime import datetime
import re
import sqlite3
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch import DispatchResult


def run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch

    dispatch.reap_worker_zombies()
    result.reaped_terminal_workers = dispatch.reap_terminal_workers(conn)
    result.reclaimed = kb.release_stale_claims(conn, failure_limit=failure_limit)
    if reconcile_orphans:
        result.reconciled_orphans = dispatch.reconcile_orphaned_running(conn)
    result.stale = dispatch.detect_stale_running(conn, stale_timeout_seconds=stale_timeout_seconds)
    result.crashed = dispatch.detect_crashed_workers(conn, board=board)
    # Side-channel attributes (see dispatch.detect_crashed_workers); rate-limited tasks
    # went back to ``ready`` and the respawn guard defers them until quota clears.
    result.auto_blocked.extend(getattr(dispatch.detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(dispatch.detect_crashed_workers, "_last_rate_limited", []))
    result.timed_out = dispatch.enforce_max_runtime(conn)
    result.promoted = kb.recompute_ready(conn, failure_limit=failure_limit)


_SCHEDULED_UNTIL_RE = re.compile(
    r"(?<!\S)SCHEDULED_UNTIL\s*=\s*([^\s,;]+)",
    re.IGNORECASE,
)


def scheduled_until_epoch(reason: object) -> Optional[float]:
    """Return an aware ``SCHEDULED_UNTIL`` marker as epoch seconds.

    Scheduled cards without an explicit, timezone-aware marker remain parked;
    an absent or malformed marker must never become an implicit wake-all.
    """
    if not isinstance(reason, str):
        return None
    match = _SCHEDULED_UNTIL_RE.search(reason)
    if match is None:
        return None
    token = match.group(1).strip()
    try:
        instant = datetime.fromisoformat(token)
    except ValueError:
        return None
    if instant.tzinfo is None:
        return None
    return instant.timestamp()


def scheduled_until_due(conn: sqlite3.Connection, *, now: Optional[float] = None) -> list[str]:
    """Find scheduled tasks whose latest schedule event carries an elapsed marker."""
    from hermes_cli import kanban_db as kb

    current = time.time() if now is None else float(now)
    due: list[str] = []
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'scheduled' ORDER BY id"
    ).fetchall()
    for row in rows:
        event = kb._latest_event(conn, row["id"], "scheduled")
        if event is None:
            continue
        payload = kb._json_dict(event["payload"])
        wake_at = scheduled_until_epoch(payload.get("reason"))
        if wake_at is not None and wake_at <= current:
            due.append(row["id"])
    return due


def wake_due_scheduled(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    dry_run: bool,
) -> None:
    """Wake only explicit, elapsed ``SCHEDULED_UNTIL`` cards.

    ``kanban_schedule`` is also used for external gates, so a plain scheduled
    row is intentionally untouched. In dry-run mode the due set is reported
    without changing lifecycle state; normal ticks call the canonical unblock
    path so parent dependencies and run bookkeeping are re-gated identically
    to an operator unblock.
    """
    from hermes_cli import kanban_db as kb

    due = scheduled_until_due(conn)
    result.scheduled_due.extend(due)
    if dry_run:
        return
    for task_id in due:
        if kb.unblock_task(conn, task_id):
            result.scheduled_woken.append(task_id)
