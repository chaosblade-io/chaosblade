"""Restore primitives shared by every faultdrill recovery path (M2 ND1).

Split out of ``reconciler.py`` (task 2.1): the restore machinery — guarded
patch replay, target readback, derived-invalid-secret handling — is the
half of the old CR reconciler that SURVIVES the CR-channel removal. The
CR-loop half (``arm_session_reconciler`` / ``reconcile_once`` / the
process-level task registry) died with the channel's wiring (task 2.3 —
its last dependency, the CR-read convergence, is gone); these primitives
stay because ``provider._replay_restore_recipe`` (the ND7 ledger-model
replay) drives every recover path through them.

Two idempotence guards (experiment-proven — by its own v1 incident):

1. **Pre-inject readback** — an ``add`` op whose path already exists on
   the target is skipped (a crashed-midway re-run would otherwise 422 on
   the already-applied op and mis-count as an inject failure). All ops
   already applied → zero patches, status backfill only.
2. **Pre-restore readback** — a ``remove`` op whose path is already
   absent is skipped (json-remove on a missing path errors; the target
   may have converged by other means).

Credential law (D5): the fault-prop secret is DERIVED from its SOURCE
(reference + transformation) — zero credential material ever transits a
handle, a task ledger, or a carrier payload; only the invalid marker
does.

Kubectl access goes through the provider seam by module attribute (NOT
``from .provider import _kubectl``) so tests can patch ONE point
(``provider._kubectl``) and every caller — here and in provider.py's own
dispatches — follows the patched seam.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Optional

from . import provider as _provider_mod

logger = logging.getLogger(__name__)

#: Credential material written into the derived invalid copy. Structurally
#: a valid dockerconfigjson entry, never a real credential — the fault is
#: the 401, and nothing sensitive ever transits a handle or ledger (D5).
_INVALID_CREDENTIAL = "invalid-credential"


# ---------------------------------------------------------------------------
# Restore action (guard-2 idempotent replay)
# ---------------------------------------------------------------------------


async def _do_restore(task_state: dict, kubeconfig: str) -> bool:
    """Revert the target to baseline. Guard 2 (pre-restore readback):
    ``remove`` ops whose path is already absent are dropped (json-remove
    on a missing path errors; the experiment's "已回基线 → 跳过").
    """
    target = task_state.get("target_ref") or {}
    restore = list(task_state.get("restore_patches") or [])
    ok_read, target_json, _ = await _get_target_json(target, kubeconfig)
    if ok_read and isinstance(target_json, dict):
        pending = _filter_already_restored(target_json, restore)
    else:
        pending = restore

    patch_ok = True
    if pending:
        patch_ok = await _patch_target(target, pending, kubeconfig)

    inv = dict(task_state.get("invalid_secret") or {})
    del_ok = True
    if inv.get("name"):
        del_ok = await _delete_invalid_secret(inv, target, kubeconfig)
    return patch_ok and del_ok


async def _apply_derived_invalid_secret(
    inv: dict, target: dict, kubeconfig: str
) -> bool:
    """Derive the fault-prop secret from its SOURCE (D5: source reference
    + transformation, zero credential material in the recipe).

    Reads the source Secret through the same RBAC face the SOP path uses
    (the SOP flow also reads the source to derive its invalid copy),
    overrides the registry host if the recipe asks, and rewrites the
    credentials to the invalid marker. The derived copy lands in the
    target's namespace, named by ``invalidSecret.name``.
    """
    import yaml

    namespace = _target_namespace(target)
    source_name = str(inv.get("sourceName") or "")
    r = await _provider_mod._kubectl(
        "get", ["secret", source_name, "-n", namespace, "-o", "json"],
        kubeconfig,
    )
    if r.exit_code != 0:
        logger.warning(
            "invalid-secret derivation: source %s/%s unreadable (rc=%s)",
            namespace, source_name, r.exit_code,
        )
        return False
    try:
        src = json.loads(r.stdout)
        src_data = src.get("data") if isinstance(src.get("data"), dict) else {}
        cfg_text = base64.b64decode(
            str(src_data.get(".dockerconfigjson") or "")
        ).decode("utf-8", "replace")
        cfg = json.loads(cfg_text) if cfg_text.strip() else {}
        auths = cfg.get("auths") if isinstance(cfg.get("auths"), dict) else {}
    except (ValueError, TypeError):
        logger.warning(
            "invalid-secret derivation: source %s unparseable", source_name,
        )
        return False
    secret_type = str(
        src.get("type") or "kubernetes.io/dockerconfigjson"
    )
    hosts = [str(k) for k in auths.keys()]
    host = str(inv.get("registryHostOverride") or "") or (
        hosts[0] if hosts else ""
    )
    derived_cfg = {
        "auths": {
            host: {
                "username": _INVALID_CREDENTIAL,
                "password": _INVALID_CREDENTIAL,
                "auth": base64.b64encode(
                    f"{_INVALID_CREDENTIAL}:{_INVALID_CREDENTIAL}".encode()
                ).decode(),
            },
        },
    }
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": str(inv.get("name") or ""),
            "namespace": namespace,
        },
        "type": secret_type,
        "data": {
            ".dockerconfigjson": base64.b64encode(
                json.dumps(derived_cfg).encode()
            ).decode(),
        },
    }
    r = await _provider_mod._kubectl(
        "apply", ["-f", "-"], kubeconfig,
        stdin_data=yaml.safe_dump(manifest, default_flow_style=False),
    )
    return r.exit_code == 0


async def _delete_invalid_secret(
    inv: dict, target: dict, kubeconfig: str
) -> bool:
    namespace = _target_namespace(target)
    r = await _provider_mod._kubectl(
        "delete",
        ["secret", str(inv.get("name") or ""), "-n", namespace,
         "--ignore-not-found"],
        kubeconfig,
    )
    return r.exit_code == 0


# ---------------------------------------------------------------------------
# Target I/O (guarded patches)
# ---------------------------------------------------------------------------


def _target_namespace(target: dict) -> str:
    """Namespace of the restore target (targetRef.namespace → ``default``;
    the fault props land beside their target)."""
    ns = str((target or {}).get("namespace") or "")
    if ns:
        return ns
    return "default"


async def _get_target_json(
    target: dict, kubeconfig: str
) -> tuple[bool, Optional[dict], str]:
    kind = str((target or {}).get("kind") or "Deployment").lower()
    name = str((target or {}).get("name") or "")
    if not name:
        return False, None, "targetRef carries no name"
    r = await _provider_mod._kubectl(
        "get", [kind, name, "-n", _target_namespace(target), "-o", "json"],
        kubeconfig,
    )
    if r.exit_code != 0:
        return False, None, r.stderr or ""
    try:
        parsed = json.loads(r.stdout)
    except (ValueError, TypeError):
        return False, None, "target get returned unparseable JSON"
    if not isinstance(parsed, dict):
        return False, None, "target get returned non-object JSON"
    return True, parsed, ""


async def _patch_target(
    target: dict, patches: list, kubeconfig: str
) -> bool:
    kind = str((target or {}).get("kind") or "Deployment").lower()
    name = str((target or {}).get("name") or "")
    r = await _provider_mod._kubectl(
        "patch",
        [kind, name, "-n", _target_namespace(target), "--type=json",
         "-p", json.dumps(patches)],
        kubeconfig,
    )
    return r.exit_code == 0


# ---------------------------------------------------------------------------
# Guard helpers (pure — patch-op vs live-target comparison)
# ---------------------------------------------------------------------------


def _filter_already_applied(target_json: dict, patches: list) -> list:
    """Guard 1: drop ``add`` ops whose path already exists on the target."""
    out = []
    for op in patches or []:
        if not isinstance(op, dict):
            out.append(op)
            continue
        if str(op.get("op") or "") == "add" and _json_path_exists(
            target_json, str(op.get("path") or "")
        ):
            continue
        out.append(op)
    return out


def _filter_already_restored(target_json: dict, restore_patches: list) -> list:
    """Guard 2: drop ``remove`` ops whose path is already absent."""
    out = []
    for op in restore_patches or []:
        if not isinstance(op, dict):
            out.append(op)
            continue
        if str(op.get("op") or "") == "remove" and not _json_path_exists(
            target_json, str(op.get("path") or "")
        ):
            continue
        out.append(op)
    return out


def _json_path_exists(doc: object, path: str) -> bool:
    """Conservative existence probe for a JSON-pointer-style path.

    Only pure OBJECT segments are resolved; an array index (or any
    unresolvable segment) yields ``False`` — the caller then keeps the op
    (conservative: ``replace`` re-application is idempotent, and an
    ``add``/``remove`` on an array path that errors is a visible attempt
    failure, never a silently-skipped guard).
    """
    if not path.startswith("/"):
        return False
    cur = doc
    for raw_seg in path.strip("/").split("/"):
        seg = raw_seg.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return False
    return True
