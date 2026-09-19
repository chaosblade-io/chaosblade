"""FaultDrill CR carrier — declarative apiserver-write fault channel.

Full-workshop cases (recovery address = apiserver writes) route through a
FaultDrill CustomResource instead of the recovery-carrier SOP chain: apply
one CR = injection; controller-style reconciliation of ``restorePatches``
= recovery; ``status.injectedAt`` = the TTL clock. The channel is purely
additive — ``recovery_carrier`` SOP stays the fallback for clusters where
the CRD cannot be installed (degrade is a routing branch, not a failure
path). See openspec change ``faultdrill-cr-channel``.

Planned modules: ``crd`` (schema contract — installed lazily, this file),
``provider`` (FaultDrillProvider), ``reconciler`` (session-side level-
triggered reconciliation loop). External consumers import the concrete
modules directly — this package ``__init__`` intentionally re-exports
nothing.
"""
