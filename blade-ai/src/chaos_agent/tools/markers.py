"""Machine-readable outcome markers embedded in tool return TEXT.

Tool wrappers live in the providers layer and never touch agent state —
for cross-layer contracts like "this create's outcome is UNKNOWN", the
return TEXT is the single channel of truth between the tool layer and
the execute-loop guards that scan it. These marker constants live here
(neutral ground: both the providers layer and the generic layer import
from ``tools/`` legally) so neither layer has to reach across the
carrier-import boundary for the other's vocabulary.

Wording discipline: markers that must be discriminated by a scanner are
deliberately distinct word forms with no substring containment (see the
create-reconcile gate's three-state scan).
"""

# Create-tool returns whose outcome is UNKNOWN (transport exception /
# no-UID transient error): the request may have reached the cluster even
# though its response did not reach us. Consumed by the execute-loop
# create-reconcile gate's three-state scan (agent/nodes/execute/
# _reconcile_gate.py); the guidance text around it is for the LLM.
# Deliberately distinct from the gate's own interception marker below:
# uncertain = a real execution whose result is unknown, gate-blocked = an
# interception that never executed.
UNCERTAIN_OUTCOME_MARKER = "[outcome:uncertain]"

# The create-reconcile gate's FABRICATED interception answer for a held
# create retry. The gate itself is a generic state machine
# (agent/nodes/execute/_reconcile_gate.py) while the feedback TEXT is
# composed carrier-side (providers/chaosblade/reconcile.py, naming that
# carrier's reconciliation tools) — neutral ground for the same reason as
# the uncertain marker: the generic scan and the carrier-side composer sit
# on opposite sides of the carrier-import boundary and both need this
# exact wording. The three-state scan keys on the difference from the
# uncertain marker: this heads a never-executed interception, never a real
# tool return.
GATE_RECONCILE_BLOCKED_MARKER = "[gate:reconcile-blocked]"
