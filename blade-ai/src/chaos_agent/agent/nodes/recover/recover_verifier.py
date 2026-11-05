"""Recover verifier node: backward-compatible public API.

The implementation lives in _recover_verifier_loop.py.
This module re-exports the public entry points so existing imports
(e.g., from chaos_agent.agent.nodes.recover.recover_verifier import make_recover_verifier)
continue to work without modification.
"""

from chaos_agent.agent.nodes.recover._recover_layer1 import (  # noqa: F401 — backward compat re-exports
    RecoverLayer1Result,
    _RECOVER_BASELINE_TOOL_CALL_ID,
    _RECOVER_SYNTHETIC_TOOL_CALL_IDS,
    _RECOVER_CONTEXT_KWARGS_KEY,
    _build_recover_baseline_tool_messages,
    _build_layer1_recovery_prompt,
    _parse_layer1_recovery_result,
)
from chaos_agent.agent.nodes.recover._recover_layer2_parse import (  # noqa: F401 — backward compat re-exports
    _build_recover_verifier_prompt,
    _RECOVERY_CHECKLIST_PATTERNS,
    _parse_recovery_checklist_items,
    _has_recovery_checklist,
    _count_recovery_steps_in_skill_case,
    _extract_recovery_verification_section,
    _detect_recovery_checklist_inconsistency,
    _detect_recovery_contradiction,
    _parse_recovery_verification_result,
)
from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
    recover_verifier,
    make_recover_verifier,
)
# Phase-9: the ``_chaosblade_recover`` backward-compat re-exports
# (``DESTROYED_STATES`` / ``parse_blade_destroy_output`` /
# ``parse_blade_status_destroyed`` / ``run_layer1_destroy``) were removed —
# zero consumers repo-wide, and the re-export made this generic-layer
# facade import a concrete carrier module (direction violation). Import
# from ``providers.chaosblade.recover`` directly if the need ever returns.

__all__ = ["recover_verifier", "make_recover_verifier"]