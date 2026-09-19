"""FaultDrill CRD contract — schema template + compatibility check.

Product migration of the schema proven by the 2026-09-18 experiment
(``probe_cr_apply_crd.py``), with two deliberate deltas over the probe:

- ``invalidSecret`` carries a SOURCE reference + transformation recipe
  (``sourceName`` + optional ``registryHostOverride``) — NEVER inlined
  credential material. CRs are not covered by Secret encryption-at-rest
  by default, so the probe's inline ``data`` form (kept in the probe as
  a negative reference only) would land credentials in etcd in the
  clear. The reconciler reads the source Secret (same RBAC face as the
  SOP path, which already reads the source to derive the invalid copy)
  and derives the fault-prop secret at injection time.
- ``status.phase`` gains the ``Failed`` terminal state: bounded retry
  exhaustion lands in ``Failed`` (reason in ``restoreLog``, observable,
  no infinite retry loop); ``recover`` can still best-effort it.

``patches`` / ``restorePatches`` items keep the experiment-proven
``x-kubernetes-preserve-unknown-fields: true``: any JSON shape of
``value`` survives verbatim — no strict-decode rejection (a typed
``value: type: object`` rejects string/array forms) and no silent
pruning (undeclared fields are stripped, which would leave a landed CR
with empty patches → bare-injection hazard, caught by the readback
guard).

Pure data module: no IO, no transport/settings imports. Group and
version arrive as parameters (settings owns them) — nothing from the
experiment environment is hardcoded.
"""
from __future__ import annotations

import hashlib

#: Fixed naming of the custom resource (only the GROUP is configurable
#: via ``faultdrill_crd_group`` — a bland group name lowers casual
#: ``api-resources`` scan-through; changing it mints a new CRD).
CRD_PLURAL = "faultdrills"
CRD_SINGULAR = "faultdrill"
CRD_KIND = "FaultDrill"
CRD_SHORT_NAME = "fd"

#: ``status.phase`` vocabulary — single source for provider/reconciler.
PHASE_PENDING = "Pending"
PHASE_INJECTED = "Injected"
PHASE_RECOVERED = "Recovered"
PHASE_FAILED = "Failed"

#: Fields that MUST survive verbatim (readback guard compares these).
PRESERVED_ARRAY_FIELDS = ("patches", "restorePatches")


def crd_full_name(group: str) -> str:
    """Full CRD object name: ``faultdrills.<group>``."""
    return f"{CRD_PLURAL}.{group}"


def build_cr_name(task_id: str, prefix: str) -> str:
    """Deterministic CR instance name — the D6 naming discipline.

    Invariants (pinned by test, NOT by fixed spelling):

    - TASK-DERIVED: the name is a pure function of ``task_id`` — the
      same task always mints the same name, so re-runs / replans /
      recover converge on ONE object instead of piling up siblings.
    - REPRODUCIBLE & COLLISION-SAFE: sha256 truncated to 8 hex chars
      (≈4 billion buckets — collision-safe at workshop task volumes).
    - ZERO drill signature: the DEFAULT prefix carries no
      drill/chaos/blade root. The name is deliberately NOT a guard
      signal (guards ride the write-set approval and the kind
      whitelist, design D6) — it only has to stay bland enough not to
      announce the drill in a ``kubectl get`` scan-through, unlike the
      SOP path's ``drill-rc-`` prefix which IS a classifier shape
      signal. The prefix is a user configuration choice
      (``faultdrill_name_prefix``); a user who configures a signature
      prefix has made that exposure decision for their own cluster.

    The prefix arrives as a parameter (settings owns it) — this module
    stays a pure data contract with no settings import, mirroring
    ``build_crd_yaml(group)``.
    """
    digest = hashlib.sha256(
        ("faultdrill:" + (task_id or "")).encode("utf-8")
    ).hexdigest()[:8]
    return f"{prefix}{digest}"


# Placeholder tokens — ``str.format`` is unusable here (the YAML itself
# is full of flow-mapping braces); ``build_crd_yaml`` substitutes via
# ``str.replace``.
_TEMPLATE = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: faultdrills.__GROUP__
spec:
  group: __GROUP__
  names:
    plural: faultdrills
    singular: faultdrill
    kind: FaultDrill
    shortNames:
    - fd
  scope: Namespaced
  versions:
  - name: __VERSION__
    served: true
    storage: true
    subresources:
      status: {}
    additionalPrinterColumns:
    - name: Phase
      type: string
      jsonPath: .status.phase
    - name: Age
      type: date
      jsonPath: .metadata.creationTimestamp
    schema:
      openAPIV3Schema:
        type: object
        properties:
          spec:
            type: object
            required: [action, targetRef, durationSeconds]
            properties:
              action:
                type: string
                description: "specPatch | secretSwap | ..."
              targetRef:
                type: object
                required: [kind, name, namespace]
                properties:
                  kind: { type: string }
                  name: { type: string }
                  namespace: { type: string }
              patches:
                type: array
                description: "json-patch ops applied verbatim at injection time (value may be any JSON shape)"
                items:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
              invalidSecret:
                type: object
                description: "invalid-credential prop DERIVED from a source secret at injection time; no credential material is ever inlined here (CRs sit outside Secret encryption-at-rest)"
                required: [name, sourceName]
                properties:
                  name: { type: string }
                  sourceName: { type: string }
                  registryHostOverride: { type: string }
              restorePatches:
                type: array
                description: "json-patch ops applied verbatim at recovery time"
                items:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
              durationSeconds:
                type: integer
                description: "TTL from Injected phase; reconciler restores at expiry"
          status:
            type: object
            properties:
              phase:
                type: string
                description: "Pending | Injected | Recovered | Failed"
              injectedAt:
                type: string
              recoveredAt:
                type: string
              restoreLog:
                type: string
"""


def build_crd_yaml(group: str, version: str = "v1alpha1") -> str:
    """Render the CRD manifest with a configurable group/version."""
    return _TEMPLATE.replace("__GROUP__", group).replace("__VERSION__", version)


def verify_crd_compatibility(crd_json: dict) -> tuple[bool, str]:
    """Check a LIVE CRD (``kubectl get -o json``) for the load-bearing declarations.

    "Exists" != "usable" (design D7): a pre-existing CRD missing the
    items-level preserve-unknown declarations would let our patches pass
    through pruning — a landed-but-stripped CR that the readback guard
    aborts (a dead end, not a degrade). Such a CRD is treated as
    channel-unavailable → degrade to the recovery-carrier SOP path.

    Returns ``(ok, reason)``; ``reason`` names the first failing
    declaration for observability.
    """
    try:
        versions = crd_json["spec"]["versions"]
    except (KeyError, TypeError):
        return False, "no spec.versions"
    served = [v for v in (versions or []) if isinstance(v, dict) and v.get("served")]
    if not served:
        return False, "no served version"
    for v in served:
        vname = v.get("name") or "?"
        schema = (((v.get("schema") or {}).get("openAPIV3Schema") or {})
                  .get("properties") or {})
        spec_props = (schema.get("spec") or {}).get("properties") or {}
        for field in PRESERVED_ARRAY_FIELDS:
            items = (spec_props.get(field) or {}).get("items") or {}
            if not items.get("x-kubernetes-preserve-unknown-fields"):
                return False, f"version {vname}: {field} items not preserve-unknown"
        inv_props = (spec_props.get("invalidSecret") or {}).get("properties") or {}
        if "data" in inv_props:
            # The probe's inline-material form — creds would land in etcd
            # in the clear; treat as incompatible (old/foreign CRD).
            return False, f"version {vname}: invalidSecret is inline-material form"
        if "sourceName" not in inv_props:
            return False, f"version {vname}: invalidSecret lacks sourceName (unknown form)"
    return True, "ok"
