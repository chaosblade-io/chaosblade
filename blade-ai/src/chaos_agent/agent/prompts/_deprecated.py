"""Deprecated backward-compatible prompt constants.

New code should use builder functions from chaos_agent.agent.prompts.builders
instead of these pre-computed constants.
"""

import warnings

from chaos_agent.agent.prompts.builders import (
    build_inject_system_prompt,
    build_verifier_prompt,
)


def _deprecated_format_prompt(*args, **kwargs):
    """Emit deprecation warning when INJECT_SYSTEM_PROMPT.format() is called."""
    warnings.warn(
        "INJECT_SYSTEM_PROMPT is deprecated. Use build_inject_system_prompt() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Rebuild with empty skill_catalog as the deprecated constant had {skill_catalog} placeholder
    return build_inject_system_prompt(skill_catalog=kwargs.get("skill_catalog", ""))


# Pre-computed constants for backward compatibility
# These are evaluated at import time, so they use the default (empty) state.
INJECT_SYSTEM_PROMPT = build_inject_system_prompt(skill_catalog="{skill_catalog}")
VERIFIER_PROMPT = build_verifier_prompt()
