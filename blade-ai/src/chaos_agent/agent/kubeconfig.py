"""Graph-wide kubeconfig resolution — the ONE config contract the
injection chain and every settlement primitive share.

Historically the resolver lived in
``agent/nodes/execute/_kubeconfig_inject.py`` (the execute node's private
helper), so every non-execute consumer faced a layering inversion to reach
it and tended to read ``state["kubeconfig"]`` bare instead — the round-32
finding (K1): the same ``sweep_live_liabilities`` rode by four settlement
seams received one merged resolved value (round-31, verify-replan) and
three bare states, and the sweep's internal ``values.get("kubeconfig")
or ""`` silently re-homed every settlement destroy onto blade's own
default cluster whenever the state key was empty (the CLI entry seeds it
with ``""``). The fix moved resolution INSIDE the primitive — a caller
passing bare state is the CORRECT form, a new seam cannot reintroduce the
split by omission — which requires the resolver to live at a layer every
consumer can import without inversion. This module is that layer; the
execute-side private name ``_resolve_kubeconfig`` remains as a delegation
for its existing callers.
"""

from chaos_agent.config.settings import settings


def resolve_kubeconfig(state) -> str:
    """Resolve the kubeconfig a cluster-facing action must run under.

    Priority: state.kubeconfig > spec.params.kubeconfig > settings.kubeconfig_path

    This is the same three-level fallback the injection chain (execute
    loop's tool-call injection) has always run its creates under; the
    settlement primitives' destroys resolve through it too (round-32), so
    a destroy can never target a different cluster than the create that
    birthed the liability — the config domain of create and destroy is
    single-sourced.

    ``state`` is any mapping (graph state, langgraph values, plain dict);
    the spec read is deferred (lazy import) so this module stays importable
    from the provider layer without dragging the provider assembly.
    """
    values = state or {}
    kc = values.get("kubeconfig", "")
    if kc:
        return kc
    from chaos_agent.agent.spec.fault_spec import read_fault_spec

    spec = read_fault_spec(values)
    if spec:
        kc = spec.params.get("kubeconfig", "")
        if kc:
            return kc
    return settings.kubeconfig_path
