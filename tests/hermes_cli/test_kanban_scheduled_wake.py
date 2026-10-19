"""Regression tests for time-marked Kanban scheduled wakes."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_dispatch_lifecycle as lifecycle


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as connection:
        yield connection


def test_elapsed_scheduled_until_is_woken_without_spawning(conn):
    task_id = kb.create_task(conn, title="scheduled wake fixture")
    assert kb.schedule_task(
        conn,
        task_id,
        reason="SCHEDULED_UNTIL=2000-01-01T00:00:00Z test fixture",
    )

    result = kbd.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: 1)

    assert task_id in result.scheduled_due
    assert task_id in result.scheduled_woken
    assert kb.get_task(conn, task_id).status == "ready"
    assert result.spawned == []


def test_unmarked_and_future_scheduled_tasks_remain_parked(conn):
    unmarked = kb.create_task(conn, title="external gate")
    future = kb.create_task(conn, title="future wake")
    assert kb.schedule_task(conn, unmarked, reason="waiting for an external gate")
    assert kb.schedule_task(
        conn,
        future,
        reason="SCHEDULED_UNTIL=2999-01-01T00:00:00Z not due",
    )

    result = kbd.dispatch_once(conn, dry_run=False)

    assert result.scheduled_due == []
    assert kb.get_task(conn, unmarked).status == "scheduled"
    assert kb.get_task(conn, future).status == "scheduled"


def test_malformed_or_naive_marker_fails_closed():
    assert lifecycle.scheduled_until_epoch("SCHEDULED_UNTIL=not-a-time") is None
    assert lifecycle.scheduled_until_epoch("SCHEDULED_UNTIL=2999-01-01T00:00:00") is None
    assert lifecycle.scheduled_until_epoch("waiting for gate") is None
