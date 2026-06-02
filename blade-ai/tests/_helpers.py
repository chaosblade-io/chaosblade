"""Shared test helpers (importable from any test module).

``conftest.py`` is auto-discovered by pytest but not a regular Python
module, so its symbols can't be ``import``ed. Helpers that tests need
to call go here.
"""

from __future__ import annotations


def intent_dict_from_result(result: dict) -> dict:
    """Project a node's returned ``fault_spec`` back to the legacy
    ``fault_intent`` dict shape that older tests assert against.

    The state-side source of truth is ``fault_spec`` after the
    refactor; this helper lets tests continue to read fault_intent
    fields without rewriting every assertion. Returns ``{}`` when no
    spec is present (rather than ``None``) so ``result["fault_intent"]
    ["scope"]`` style accesses translate to ``result_intent(result)
    .get("scope")`` cleanly.
    """
    from chaos_agent.agent.fault_spec import FaultSpec
    spec = FaultSpec.from_dict(result.get("fault_spec"))
    return spec.to_intent_dict() if spec else {}


def replace_fault_spec(state: dict, **field_updates) -> None:
    """Test helper: update specific fields of state.fault_spec in place.

    Tests that used to mutate ``state['target']`` / ``state['blade_scope']``
    / etc. should call this helper instead — the FaultSpec refactor
    consolidated those scattered fields into a single immutable spec.

    Example::

        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="kube-system", names=("coredns",))
        replace_fault_spec(state, scope="node", blade_target="cpu")
    """
    from chaos_agent.agent.fault_spec import FaultSpec
    existing = FaultSpec.from_dict(state.get("fault_spec")) or FaultSpec()
    state["fault_spec"] = existing.replace(**field_updates).to_dict()
