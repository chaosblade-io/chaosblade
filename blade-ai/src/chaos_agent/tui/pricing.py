"""Approximate per-1k-token pricing table for the cost footer.

Used by ``SessionState.add_cost`` so the footer can render a running
USD figure. Numbers are deliberately rough — the goal is "is this turn
cheap or expensive" not billing-accurate accounting. Anyone running on
discounted contracts can override per-model rates via env in a future
PR; for now the defaults match published list rates as of late 2025
(Anthropic / OpenAI public docs).
"""

from __future__ import annotations

# (input_per_1k_USD, output_per_1k_USD)
_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic Claude
    "claude-opus-4-7":          (0.015, 0.075),
    "claude-opus-4-6":          (0.015, 0.075),
    "claude-sonnet-4-6":        (0.003, 0.015),
    "claude-haiku-4-5":         (0.00080, 0.0040),
    # OpenAI compat — common names.
    "gpt-5":                    (0.005, 0.020),
    "gpt-4.1":                  (0.003, 0.012),
    "gpt-4o":                   (0.0025, 0.010),
    "gpt-4o-mini":              (0.00015, 0.00060),
    # DeepSeek / Qwen / others on the chinese-cloud route this agent
    # often uses — rates are nominal.
    "deepseek-chat":            (0.00027, 0.00110),
    "deepseek-reasoner":        (0.00055, 0.00220),
    "qwen-max":                 (0.0024, 0.0096),
}

# Mid-tier fallback so the cost number still moves on unknown models —
# better than silently displaying $0 when in fact we're spending.
_DEFAULT = (0.003, 0.015)


def resolve_pricing(model: str) -> tuple[float, float]:
    """Return (input_per_1k_USD, output_per_1k_USD) for a model id.

    Match strategy:
      1. Exact key in the table.
      2. Prefix match — many providers append a date suffix
         (e.g. ``claude-opus-4-7-20250101``); strip after the first
         numeric-version segment and retry.
      3. Default fallback.
    """
    if not model:
        return _DEFAULT
    if model in _PRICING:
        return _PRICING[model]
    # Trim trailing ``-YYYY...`` date or ``-vN`` revision so date-stamped
    # variants resolve to the base model rate.
    parts = model.split("-")
    while len(parts) > 1:
        parts.pop()
        candidate = "-".join(parts)
        if candidate in _PRICING:
            return _PRICING[candidate]
    return _DEFAULT
