"""Due-time parsing and atomic same-card wakeups for the normal dispatcher."""
from __future__ import annotations

from datetime import datetime, timezone
import math


def parse_resume_at(value: str) -> int:
    """Require an unambiguous ISO time; never wake before a fractional deadline."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("resume_at must be an ISO-8601 timestamp with a timezone")
    try:
        due = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("resume_at must be an ISO-8601 timestamp with a timezone") from None
    if due.tzinfo is None:
        raise ValueError("resume_at requires an explicit timezone")
    return math.ceil(due.astimezone(timezone.utc).timestamp())


def validate_due_time(value, *, now: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= now:
        raise ValueError("scheduled_for must be a future Unix timestamp in seconds")
    try:
        datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ValueError("scheduled_for is outside the supported date range") from None
    return value


def wake_due_tasks(conn, *, now: int) -> int:
    """Called inside recompute_ready's IMMEDIATE transaction; never claims work.

    Due rows return to dependency gating. Their timestamp is consumed atomically
    with the event, so repeated or competing dispatcher ticks cannot wake twice.
    Undated legacy parking and all active worker claims remain untouched.
    """
    from hermes_cli.kanban_db import _append_event

    due = conn.execute(
        "SELECT id, scheduled_for FROM tasks WHERE status = 'scheduled' "
        "AND scheduled_for IS NOT NULL AND scheduled_for <= ? "
        "AND current_run_id IS NULL AND claim_lock IS NULL AND worker_pid IS NULL",
        (now,),
    ).fetchall()
    count = 0
    for row in due:
        changed = conn.execute(
            "UPDATE tasks SET status = 'todo', scheduled_for = NULL "
            "WHERE id = ? AND status = 'scheduled' AND scheduled_for = ? "
            "AND current_run_id IS NULL AND claim_lock IS NULL AND worker_pid IS NULL",
            (row["id"], row["scheduled_for"]),
        )
        if changed.rowcount == 1:
            _append_event(conn, row["id"], "schedule_due", {
                "scheduled_for": row["scheduled_for"], "observed_at": now,
                "resume_status": "ready", "next_gate": "dependencies",
            })
            count += 1
    return count
