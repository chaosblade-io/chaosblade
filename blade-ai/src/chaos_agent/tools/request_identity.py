"""Request identity contract for non-idempotent create tools.

Neutral ground for the create-reconcile gate's cross-layer identity
(blade-create-reconcile-before-retry D6): the generic gate
(``agent/nodes/execute/_reconcile_gate.py``) holds the flag and compares
identities, while the provider that owns the create tool builds them from
its own tool-call argument shapes — both layers import from ``tools/``
legally (same rationale as ``tools/markers.py``: neither layer reaches
across the carrier-import boundary for the other's vocabulary).

The four dimensions (namespace / labels / target_names /
scope-target-action) are the CURRENT contract shape, declared by the
carriers under the gate; a future carrier whose request identity has a
different shape extends or redeclares the contract HERE, not in any
carrier's or the generic gate's module. The conflict-query consumers
(``check_blade_conflicts`` and its pre-injection safety_check caller)
consume the same identity — ``as_query_kwargs`` projects the four
dimensions into their keyword shape.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RequestFingerprint:
    """Four-dimension request identity for a create tool call.

    Shared construction for every consumer: the safety_check
    pre-injection conflict query (someone else's conflict → warning/
    confirm) and the create-reconcile gate (my own create, possibly
    already in effect → reuse). Same four dimensions, two consumption
    semantics.

    Fields hold the RAW string forms the consumers take
    (``as_query_kwargs`` returns them unchanged) so every path stays
    byte-identical to the pre-extraction construction; order-insensitive
    comparison lives in ``matches`` only.
    """

    namespace: str = ""
    labels: str = ""
    target_names: str = ""
    scope_target_action: str = ""

    def as_query_kwargs(self) -> dict:
        """Keyword arguments for the conflict-query consumers (raw forms)."""
        return {
            "namespace": self.namespace,
            "labels": self.labels,
            "target_names": self.target_names,
            "request_scope_target_action": self.scope_target_action,
        }

    def normalized(self) -> tuple:
        """Order-insensitive identity for equality checks (gate side).

        Registration and retry both derive from LLM tool_call args; a
        reordered labels/names list is still the same target, so compare
        as sorted multisets. Whitespace-only differences likewise.
        """
        def _sorted_csv(value: str) -> tuple:
            return tuple(sorted(p.strip() for p in value.split(",") if p.strip()))

        return (
            self.namespace.strip(),
            _sorted_csv(self.labels),
            _sorted_csv(self.target_names),
            self.scope_target_action.strip(),
        )

    def matches(self, other: "RequestFingerprint") -> bool:
        return self.normalized() == other.normalized()


def build_request_fingerprint(
    namespace: str = "",
    labels: str = "",
    names: str = "",
    scope: str = "",
    target: str = "",
    action: str = "",
) -> RequestFingerprint:
    """Build the four-dimension request fingerprint from raw string args.

    scope/target/action join into "scope-target-action" only when ALL
    three are present (the safety_check pre-extraction contract; a
    partial triple yields "" rather than a malformed identity).
    """
    sta = f"{scope}-{target}-{action}" if scope and target and action else ""
    return RequestFingerprint(
        namespace=namespace or "",
        labels=labels or "",
        target_names=names or "",
        scope_target_action=sta,
    )
