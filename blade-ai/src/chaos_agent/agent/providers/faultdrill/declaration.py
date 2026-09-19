"""FaultDrill CR carrier declaration — vocabulary surface.

Same dependency discipline as the sibling declarations (pinned by the
declaration guard in ``tests/test_agent/test_phase9_rename_guards.py``):
stdlib / typing ONLY — no provider implementation imports.

The vocabulary is intentionally EMPTY. Routing into the CR channel is
decided by the skill-case ``recovery_channel: apiserver-write`` metadata
+ planning decision + write-set validation (design D3 of openspec change
``faultdrill-cr-channel``), never by scope bridging — so
``INTENT_TARGETS`` / ``INTENT_ACTIONS`` stay byte-identical while the
channel exists (non-migration-domain zero-change guarantee).
"""
from __future__ import annotations

#: Carrier id of the FaultDrill CR backend (declarative apiserver-write
#: faults: apply one CR = injection, reconcile ``restorePatches`` =
#: recovery, ``status.injectedAt`` = the TTL clock).
CARRIER_ID = "faultdrill_cr"

#: Intent target types this carrier can execute — EMPTY by design (see
#: module docstring: routing is metadata-driven, not scope-bridged).
SUPPORTED_TARGETS: tuple[str, ...] = ()

#: Mutation verbs this carrier applies — EMPTY by design (same reason).
SUPPORTED_ACTIONS: tuple[str, ...] = ()
