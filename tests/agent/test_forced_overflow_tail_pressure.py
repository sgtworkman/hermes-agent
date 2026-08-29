"""Provider-overflow recovery for protected tails that cannot otherwise shrink."""

from agent.forced_overflow_pressure import (
    FORCED_OVERFLOW_DUPLICATE_MARKER,
    FORCED_OVERFLOW_EXCERPT_MARKER,
    FORCED_OVERFLOW_TARGET_RATIO,
    relieve_forced_overflow_tail_pressure,
)
from agent.model_metadata import estimate_messages_tokens_rough
from hermes_state import SessionDB
from tests.agent.test_compression_adoption_preserves_live_tail import (
    _build_agent_with_db,
)


def _incident_shape():
    prompt = "# Blog Factory External Control Plane v0.3\n" + ("P" * 14_000)
    return prompt, [
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY]\n" + ("S" * 11_000),
            "_compressed_summary": True,
        },
        {"role": "assistant", "content": "A" * 80_000},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "[todo] updated"},
        {
            "role": "user",
            "content": "active task list",
            "_todo_snapshot_synthetic": True,
        },
        {"role": "user", "content": prompt},
        {"role": "user", "content": prompt},
        {"role": "user", "content": prompt},
    ]


def test_forced_overflow_deduplicates_retries_and_bounds_completed_reply():
    prompt, messages = _incident_shape()
    before = estimate_messages_tokens_rough(messages)
    current_tokens = 77_600
    context_length = 65_536

    out, duplicate_users, bounded_assistants = (
        relieve_forced_overflow_tail_pressure(
            messages,
            current_tokens=current_tokens,
            context_length=context_length,
        )
    )

    assert duplicate_users == 2
    assert bounded_assistants == 1
    assert len(out) == len(messages)
    assert sum(m.get("content") == prompt for m in out) == 2
    assert sum(
        m.get("content") == FORCED_OVERFLOW_DUPLICATE_MARKER for m in out
    ) == 2
    bounded = next(
        m["content"] for m in out
        if m.get("role") == "assistant" and isinstance(m.get("content"), str)
    )
    assert bounded.startswith("A" * 100)
    assert bounded.endswith("A" * 100)
    assert FORCED_OVERFLOW_EXCERPT_MARKER in bounded
    assert all(
        m.get("content") == prompt
        for m in out
        if m.get("role") == "user" and m.get("content", "").startswith("# Blog")
    )

    reclaimed = before - estimate_messages_tokens_rough(out)
    assert current_tokens - reclaimed <= int(
        context_length * FORCED_OVERFLOW_TARGET_RATIO
    )


def test_below_provider_limit_is_identity_noop():
    _, messages = _incident_shape()

    out, duplicate_users, bounded_assistants = (
        relieve_forced_overflow_tail_pressure(
            messages,
            current_tokens=60_000,
            context_length=65_536,
        )
    )

    assert out is messages
    assert duplicate_users == 0
    assert bounded_assistants == 0


def test_distinct_or_separated_user_turns_are_never_collapsed():
    messages = [
        {"role": "user", "content": "same"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "same"},
        {"role": "user", "content": "different"},
    ]

    out, duplicate_users, _ = relieve_forced_overflow_tail_pressure(
        messages,
        current_tokens=70_000,
        context_length=65_536,
    )

    assert duplicate_users == 0
    assert [m["content"] for m in out if m["role"] == "user"] == [
        "same",
        "same",
        "different",
    ]


def test_provider_proof_overrides_low_estimate_and_reaches_assistant_anchor():
    prompt = "P" * 14_000
    messages = [
        {"role": "assistant", "content": "A" * 80_000},
        {"role": "user", "content": prompt},
    ]
    messages.extend(
        {"role": "tool", "content": f"result-{index}"}
        for index in range(9)
    )
    messages.extend(
        [
            {"role": "user", "content": prompt},
            {"role": "user", "content": prompt},
        ]
    )

    out, duplicate_users, bounded_assistants = (
        relieve_forced_overflow_tail_pressure(
            messages,
            current_tokens=40_000,
            context_length=65_536,
            overflow_proven=True,
        )
    )

    assert duplicate_users == 1
    assert bounded_assistants == 1
    assert len(out) == len(messages)
    bounded = out[0]["content"]
    assert FORCED_OVERFLOW_EXCERPT_MARKER in bounded
    assert len(bounded) <= 4_100
    assert sum(m.get("content") == prompt for m in out) == 2
    assert sum(
        m.get("content") == FORCED_OVERFLOW_DUPLICATE_MARKER for m in out
    ) == 1


def test_same_length_pressure_survives_legacy_persistence_boundary(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "PROVIDER_OVERFLOW_PERSISTENCE"
    prompt = "P" * 14_000
    db.create_session(session_id, source="desktop")
    db.append_message(session_id, "assistant", "A" * 80_000)
    db.append_message(session_id, "user", prompt)
    db.append_message(session_id, "user", prompt)
    db.append_message(session_id, "user", prompt)
    durable = db.get_messages_as_conversation(session_id)

    pressured, duplicate_users, bounded_assistants = (
        relieve_forced_overflow_tail_pressure(
            durable,
            current_tokens=40_000,
            context_length=65_536,
            overflow_proven=True,
        )
    )
    assert len(pressured) == len(durable)
    assert duplicate_users == 2
    assert bounded_assistants == 1

    agent = _build_agent_with_db(db, session_id)
    seen = []

    def _recording_compress(messages, **_kwargs):
        seen.append(messages)
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]

    agent.context_compressor.compress.side_effect = _recording_compress
    agent._persist_user_message_idx = len(pressured)
    agent._compress_context(pressured, "sys", approx_tokens=77_500, force=True)

    assert len(seen) == 1
    compress_input = seen[0]
    assert len(compress_input) == len(durable)
    assert FORCED_OVERFLOW_EXCERPT_MARKER in compress_input[0]["content"]
    assert sum(
        row.get("content") == FORCED_OVERFLOW_DUPLICATE_MARKER
        for row in compress_input
    ) == 2
    archived_parent = db.get_messages_as_conversation(
        session_id, include_inactive=True
    )
    assert len(archived_parent[0]["content"]) == 80_000
    assert sum(row.get("content") == prompt for row in archived_parent) == 3
