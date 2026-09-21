"""FaultDrill carrier — apiserver-write fault channel (M2: programmatic).

The injection and recovery ride the PROGRAMMATIC recovery-carrier
assembler (``assembler.py``, design ND2) — one tool call
deterministically assembles + verifies + arms + injects the one-shot
cluster-native carrier, and recovery replays the restore recipe from
the task ledger (design ND7, ``restore.py`` primitives). The legacy CR
channel (CustomResource apply + session-side reconciler) is REMOVED;
the ``faultdrill_cr`` attribution face in ``provider.py`` still
recognises CR applies from migration-window sessions so their faults
stay recoverable, and the manifest-kind guard entries keep blocking an
LLM applying a FaultDrill CR by hand (ND3). See openspec change
``faultdrill-cluster-native-recovery``.

Modules: ``crd`` (transitional naming constants for the
migration-window face), ``provider`` (FaultDrillProvider),
``assembler`` (the programmatic carrier tool), ``restore`` (the shared
restore primitives). External consumers import the concrete modules
directly — this package ``__init__`` intentionally re-exports nothing.
"""
