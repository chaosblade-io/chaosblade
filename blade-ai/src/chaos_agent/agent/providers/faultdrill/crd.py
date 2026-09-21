"""FaultDrill CR vocabulary constants — TRANSITIONAL (M2 tasks 2.2/2.3).

The CRD template, compatibility check, CR-name builder and the lazy
installer (``crd_install.py``) died with the CR channel (task 2.2); the
``status.phase`` vocabulary and the CR-read convergence helpers died
with the ledger-model recover (task 2.3). What remains here are the two
naming literals the migration-window leftovers still consume — the
``faultdrill_cr`` attribution face's artifact ledger (``collect``'s
landed-apply registration with its idempotent delete recipe, and the
``sweep`` of the leftover objects) plus the manifest-kind guard entries
(ND3: guards are not migrated — the whitelist keeps blocking an LLM
applying a FaultDrill CR by hand). Once those consumers are audited out
(task 2.4/2.7) this module is deleted with them.

Pure data module: no IO, no transport/settings imports.
"""
from __future__ import annotations

#: Fixed naming of the custom resource (only the GROUP is configurable
#: via ``faultdrill_crd_group`` — a bland group name lowers casual
#: ``api-resources`` scan-through; changing it mints a new CRD).
CRD_PLURAL = "faultdrills"
CRD_KIND = "FaultDrill"
