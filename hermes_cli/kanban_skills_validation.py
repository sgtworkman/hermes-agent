"""Resolve a card's pinned ``skills`` against the ASSIGNEE profile's own skill tree.

A pin names a skill the worker is expected to preload; the worker loads it through
``skill_view`` under its own ``HERMES_HOME``. When a pin names something that profile
does not have, the card is created, dispatched, and dies at
``gateway/session_kanban.py``'s ``kanban_skills_unavailable`` guard before any model
call — a ready->crash->ready loop. Both the create surfaces (CLI ``--skill`` and the
``kanban_create`` tool) call :func:`validate_skill_pins` before the row is written,
and the dispatcher calls :func:`missing_skills_for_assignee` to park a pre-existing
card instead of spawning it.

Resolution deliberately goes through the same resolver the worker uses —
``skill_view`` with the assignee's home bound — so a name is accepted here exactly
when the worker could load it: the profile's own ``skills/`` dir, its configured
``skills.create_dir``/``external_dirs``, plugin-provided ``<plugin>:<skill>`` skills,
and the declared name of a skill whose directory name differs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable, Optional

# One process resolves several profiles (dispatcher, gateway, CLI); a validation that
# raised out of a resolver would refuse a valid card, so failures fail open here and
# the runtime guard at session_kanban.py stays the backstop.
_MAX_PINS = 32


def assignee_skill_home(assignee: Optional[str]) -> Optional[Path]:
    """The assignee profile's HERMES_HOME, or None when that profile has no live home.

    None means "cannot resolve" (isolated fixtures, control-plane lanes), never
    "this profile has no skills": callers treat it as unverifiable, not as a miss.

    The root is derived from the LIVE ``HERMES_HOME`` on every call rather than from
    ``get_profile_dir``/``resolve_profile_env``: both answer from a process-wide memo of
    the default root, so a long-lived process that re-homed (or a test that swapped
    HERMES_HOME) would be told about the wrong root's profiles. The profile dir must
    carry Hermes identity, the same predicate ``profile_exists`` uses, so a marker-less
    side-effect shell is not mistaken for a skill tree.
    """
    if not assignee:
        return None
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_root_for_env_home
        from hermes_constants import get_default_hermes_root, named_profile_is_live

        canon = normalize_profile_name(assignee)
        root = profile_root_for_env_home(os.environ.get("HERMES_HOME", ""), get_default_hermes_root())
        home = root if canon == "default" else root / "profiles" / canon
    except ValueError:
        return None  # not a valid profile id: unverifiable, not a miss
    except Exception:
        # A broken import here would otherwise read as "unverifiable" forever and
        # silently switch the whole check off.
        logging.getLogger(__name__).warning(
            "kanban skill-pin validation could not resolve profile %r", assignee, exc_info=True,
        )
        return None
    if canon != "default" and not named_profile_is_live(home):
        return None
    return home if home.is_dir() else None


def _skill_loads(name: str, home: Path) -> bool:
    """True when *name* resolves to a loadable skill under *home*.

    The home override is context-local (never ``os.environ``), so binding it cannot
    leak the assignee's scope into a concurrent turn on another profile.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(home))
    try:
        from agent.skill_utils import normalize_skill_lookup_name
        from tools.skills_tool import skill_view

        normalized = normalize_skill_lookup_name(name)
        if not normalized:
            return False
        payload = json.loads(skill_view(normalized, preprocess=False))
        return bool(payload.get("success"))
    except Exception:
        # Resolver failure means "cannot prove it loads", not "it does not exist":
        # report it so a broken skill tree is diagnosable, then fail open.
        logging.getLogger(__name__).warning(
            "kanban skill-pin validation could not resolve %r under %s", name, home, exc_info=True,
        )
        return False
    finally:
        reset_hermes_home_override(token)


def missing_skills_for_assignee(
    skills: Optional[Iterable[str]], assignee: Optional[str],
) -> Optional[list[str]]:
    """Pins that do NOT resolve in the assignee's tree.

    Returns ``None`` when the check is not answerable (no assignee, no profile home,
    or an empty pin list) — callers must not read that as "all good" evidence, only
    as "nothing to refuse".
    """
    pins = [str(s).strip() for s in (skills or []) if str(s or "").strip()]
    if not pins or not assignee:
        return None
    home = assignee_skill_home(assignee)
    if home is None:
        return None
    missing = [name for name in pins[:_MAX_PINS] if not _skill_loads(name, home)]
    if len(pins) > _MAX_PINS:
        missing.extend(name for name in pins[_MAX_PINS:] if not _skill_loads(name, home))
    return missing


def validate_skill_pins(skills: Optional[Iterable[str]], assignee: Optional[str]) -> None:
    """Raise ``ValueError`` naming every unresolvable pin and the assignee.

    A no-op when there is nothing to check; never refuses a pin it could not
    actually look up.
    """
    missing = missing_skills_for_assignee(skills, assignee)
    if not missing:
        return
    quoted = ", ".join(repr(name) for name in missing)
    noun = "skill" if len(missing) == 1 else "skills"
    raise ValueError(
        f"unknown {noun} for assignee {assignee!r}: {quoted} "
        f"(not found in the {assignee!r} profile's skill tree; "
        "pin a skill that profile can load, or install it there first)"
    )
