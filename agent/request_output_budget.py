"""Reserve output headroom on the final chat request, including recovery boosts."""
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


def clamp_chat_output_budget(agent: Any, kwargs: dict, pressure_tokens: int | None = None) -> None:
    """Clamp request-local caps; preserve route defaults and the cached prompt.

    Pressure is the loop's usage-anchored full request estimate. A small window-relative
    margin covers template/tokenizer overhead; provider overflow recovery remains the
    authority when estimates differ. Native Responses/Anthropic have separate budgeting.
    """
    if agent.api_mode != "chat_completions":
        return
    context = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
        return
    if not isinstance(pressure_tokens, int) or isinstance(pressure_tokens, bool):
        from agent.model_metadata import estimate_request_tokens_rough
        pressure_tokens = estimate_request_tokens_rough(kwargs.get("messages", []), tools=kwargs.get("tools"))
    # A non-positive input budget needs compression, not an invented positive output
    # allowance. Leave that request to the existing provider-overflow recovery path.
    available = context - pressure_tokens - max(256, math.ceil(context * 0.02))
    if available < 1:
        return
    extra = kwargs.get("extra_body")
    for key in ("max_tokens", "max_completion_tokens"):
        # SDK extra_body wins over the named argument when serialized.
        cap = extra.get(key, kwargs.get(key)) if isinstance(extra, dict) else kwargs.get(key)
        if not isinstance(cap, int) or isinstance(cap, bool) or cap <= available:
            continue
        kwargs[key] = available
        if isinstance(extra, dict) and key in extra:
            extra = {**extra, key: available}
            kwargs["extra_body"] = extra
        logger.info("Reserved chat output headroom: context=%s input_estimate=%s %s=%s->%s",
                    context, pressure_tokens, key, cap, available)
