"""Session-side reconciler for landed FaultDrill CRs (M2 task 2.2, design D4).

Product migration of the v2 experiment's ``reconcile_once`` loop
(``probe_cr_full_experiment.py``): the CR is the SINGLE source of truth —
every pass re-reads the live CR (patches / restorePatches /
invalidSecret / durationSeconds all come from the cluster, zero
hardcoding), and the TTL verdict reads ``status.injectedAt`` (cluster
state, not process memory — recovery survives Agent death, the property
the controller-death experiment proved: delayed, never lost).

The loop is armed by the execute loop's post-landing seam right after
the readback guard verifies the recipe survived (task 2.1): a stripped
landing hard-aborts BEFORE any reconciliation, so this module never runs
on an empty recipe (the bare-injection hazard, v1 incident law).

Three idempotence guards (experiment-proven — by its own v1 incident):

1. **Pre-inject readback** — an ``add`` op whose path already exists on
   the target is skipped (a crashed-midway re-run would otherwise 422 on
   the already-applied op and mis-count as an inject failure). All ops
   already applied → zero patches, status backfill only.
2. **Pre-restore readback** — a ``remove`` op whose path is already
   absent is skipped (json-remove on a missing path errors; the target
   may have converged by other means).
3. **CR-vanished cleanup** — the CR disappearing while THIS task did
   inject triggers a best-effort restore from the task's cached recipe
   (finalizer semantics in miniature), then the loop exits.

Failure bounding (D4): inject attempts are counted per task; reaching
``MAX_INJECT_ATTEMPTS`` consecutive failures lands ``phase=Failed`` with
the reason in ``status.restoreLog`` — observable, no infinite retry. A
fresh session (or ``blade-ai recover``) restarts the count at zero, so a
transient apiserver outage cannot wedge a CR forever.

Recovery single-source law (D4): this reconciler is the ONLY recovery
executor for the CR channel — it arms no systemd/sleep carrier timer,
and the channel's plans stack none (planning-side legislation), so no
double-recovery race exists.

The active-task registry is PROCESS state (an ``asyncio.Task`` is not
LangGraph-serialisable): same-handle re-arms are idempotent no-ops, and
because every pass re-reads the CR, a replanned re-apply under the same
name is reconciled by the ALREADY-RUNNING task with the new recipe —
the replan seam deliberately does NOT disarm.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .crd import (
    CRD_PLURAL,
    PHASE_FAILED,
    PHASE_INJECTED,
    PHASE_RECOVERED,
)
# Module-attribute access (NOT ``from .provider import _kubectl``): the
# seam must be patchable at ONE point — tests monkeypatch
# ``provider._kubectl`` and both ``_read_cr_json`` (module-global lookup
# inside provider) and every direct call here follow the patched seam.
from . import provider as _provider_mod

logger = logging.getLogger(__name__)

#: Poll cadence (design D4: 5s — matches the experiment's loop rhythm).
RECONCILE_INTERVAL_SECONDS = 5.0

#: Consecutive inject failures before ``phase=Failed`` (design D4:
#: bounded retry — a bad recipe must terminate, not spin forever).
MAX_INJECT_ATTEMPTS = 3

#: Credential material written into the derived invalid copy. Structurally
#: a valid dockerconfigjson entry, never a real credential — the fault is
#: the 401, and nothing sensitive ever transits the CR (D5).
_INVALID_CREDENTIAL = "invalid-credential"

#: Process-level registry of live reconciler tasks, keyed by handle value
#: (``ns/name``). ``asyncio.Task`` objects cannot live in LangGraph state.
_ACTIVE_RECONCILERS: dict[str, "asyncio.Task"] = {}


# ---------------------------------------------------------------------------
# Public arming surface
# ---------------------------------------------------------------------------


def arm_session_reconciler(handle_value: str, kubeconfig: str = "") -> bool:
    """Spawn the background reconcile loop for a verified landing.

    Idempotent: a handle with a live task is a no-op (``False``) — the
    running loop re-reads the CR every pass, so it picks up any re-apply
    under the same name without a restart. Must be called from inside a
    running event loop (``asyncio.create_task``); the execute loop's seam
    is exactly that context (design D4 bans ``asyncio.run``).
    """
    if not handle_value or handle_value in _ACTIVE_RECONCILERS:
        return False
    task = asyncio.create_task(_reconciler_loop(handle_value, kubeconfig))
    _ACTIVE_RECONCILERS[handle_value] = task

    def _retire(done: "asyncio.Task") -> None:
        # Pop only if this very task still owns the slot (a re-arm after
        # a manual disarm could have replaced it).
        if _ACTIVE_RECONCILERS.get(handle_value) is done:
            _ACTIVE_RECONCILERS.pop(handle_value, None)
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:  # pragma: no cover — defensive, logged loudly
            logger.error(
                "faultdrill reconciler for %s crashed: %s", handle_value, exc,
            )

    task.add_done_callback(_retire)
    logger.info(
        "faultdrill reconciler armed for %s (interval=%ss, max_attempts=%s)",
        handle_value, RECONCILE_INTERVAL_SECONDS, MAX_INJECT_ATTEMPTS,
    )
    return True


def disarm_session_reconciler(handle_value: str) -> bool:
    """Cancel a live reconciler task (test teardown / explicit stop)."""
    task = _ACTIVE_RECONCILERS.pop(handle_value, None)
    if task is None:
        return False
    task.cancel()
    return True


def active_reconciler_handles() -> tuple[str, ...]:
    """Handles with a live session reconciler (observability / tests)."""
    return tuple(_ACTIVE_RECONCILERS)


async def _reconciler_loop(handle_value: str, kubeconfig: str) -> None:
    """Periodic reconcile until a terminal or abort outcome.

    A single pass raising never kills the loop (the next pass re-reads
    the CR and converges — level-triggered); only terminal phases,
    failure-bounded aborts, or the CR vanishing end it.
    """
    task_state: dict = {"inject_attempts": 0, "did_inject": False}
    while True:
        try:
            outcome = await reconcile_once(handle_value, kubeconfig, task_state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — level-triggered resilience
            logger.warning(
                "faultdrill reconcile pass for %s raised (retrying next "
                "tick)", handle_value, exc_info=True,
            )
            outcome = "noop"
        if outcome == "terminal" or outcome.startswith("abort-"):
            logger.info(
                "faultdrill reconciler for %s finished: %s",
                handle_value, outcome,
            )
            return
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Level-triggered reconcile pass (the experiment's reconcile_once)
# ---------------------------------------------------------------------------


async def reconcile_once(
    handle_value: str, kubeconfig: str, task_state: dict
) -> str:
    """One stateless reconcile pass. Returns the outcome label.

    ``injected`` / ``recovered`` / ``terminal`` / ``noop`` /
    ``retry-inject`` (attempts below the cap, next pass retries) /
    ``abort-cr-missing`` / ``abort-inject-failed`` (cap reached, CR now
    ``Failed``) / ``abort-restore-failed`` (CR now ``Failed``).
    """
    ok, cr, stderr = await _provider_mod._read_cr_json(handle_value, kubeconfig)
    if not ok:
        if task_state.get("did_inject"):
            # Guard 3: CR vanished after WE injected → finalizer-shaped
            # best-effort cleanup from the cached recipe, then exit.
            logger.warning(
                "faultdrill CR %s vanished after injection — best-effort "
                "restore from cached recipe (finalizer semantics)",
                handle_value,
            )
            await _do_restore(task_state, kubeconfig)
        return "abort-cr-missing"
    spec = cr.get("spec") if isinstance(cr.get("spec"), dict) else {}
    task_state.update(
        patches=list(spec.get("patches") or []),
        restore_patches=list(spec.get("restorePatches") or []),
        invalid_secret=dict(spec.get("invalidSecret") or {}),
        target_ref=dict(spec.get("targetRef") or {}),
        ttl=int(spec.get("durationSeconds") or 600),
    )
    status = cr.get("status") if isinstance(cr.get("status"), dict) else {}
    phase = str(status.get("phase") or "")

    if phase in (PHASE_RECOVERED, PHASE_FAILED):
        return "terminal"
    if phase != PHASE_INJECTED:
        ok_inject = await _do_inject(handle_value, task_state, kubeconfig)
        if ok_inject:
            return "injected"
        if task_state.get("inject_attempts", 0) >= MAX_INJECT_ATTEMPTS:
            return "abort-inject-failed"
        return "retry-inject"

    injected_at = _injected_at_of(cr)
    if injected_at is None:
        # phase=Injected but the timestamp is missing → rewrite it
        # (controller self-heal; the TTL verdict depends on it).
        await _set_phase(
            handle_value, kubeconfig, PHASE_INJECTED,
            {"injectedAt": _utc_now_iso()},
        )
        return "noop"
    if time.time() - injected_at >= task_state["ttl"]:
        ok_restore = await _do_restore(task_state, kubeconfig)
        if ok_restore:
            await _set_phase(
                handle_value, kubeconfig, PHASE_RECOVERED,
                {
                    "recoveredAt": _utc_now_iso(),
                    "restoreLog": "restore_patches+delete_invalid_secret ok",
                },
            )
            return "recovered"
        await _set_phase(
            handle_value, kubeconfig, PHASE_FAILED,
            {"restoreLog": "restore failed: patches or secret deletion errored"},
        )
        return "abort-restore-failed"
    return "noop"


# ---------------------------------------------------------------------------
# Reconcile actions
# ---------------------------------------------------------------------------


async def _do_inject(
    handle_value: str, task_state: dict, kubeconfig: str
) -> bool:
    """Apply the CR's recipe to the target. True = Injected landed.

    Guard 1 (pre-inject readback): ``add`` ops whose path already exists
    on the target are dropped — a crashed-midway re-run applies only what
    is still missing (all-applied → zero patches, status backfill only,
    exactly the experiment's "已带故障字段 → 跳过重复注入").
    """
    target = task_state.get("target_ref") or {}
    patches = list(task_state.get("patches") or [])
    ok_read, target_json, _ = await _get_target_json(target, kubeconfig)
    if ok_read and isinstance(target_json, dict):
        pending = _filter_already_applied(target_json, patches)
    else:
        # Target unreadable → apply the full recipe and let the patch's
        # own exit code decide (a wrong target name fails visibly).
        pending = patches

    secret_ok = True
    inv = dict(task_state.get("invalid_secret") or {})
    if inv.get("name") and inv.get("sourceName"):
        secret_ok = await _apply_derived_invalid_secret(
            inv, target, kubeconfig,
        )
    patch_ok = True
    if pending:
        patch_ok = await _patch_target(target, pending, kubeconfig)

    if secret_ok and patch_ok:
        await _set_phase(
            handle_value, kubeconfig, PHASE_INJECTED,
            {"injectedAt": _utc_now_iso()},
        )
        task_state["did_inject"] = True
        task_state["inject_attempts"] = 0
        return True

    attempts = int(task_state.get("inject_attempts") or 0) + 1
    task_state["inject_attempts"] = attempts
    if attempts >= MAX_INJECT_ATTEMPTS:
        await _set_phase(
            handle_value, kubeconfig, PHASE_FAILED,
            {
                "restoreLog": (
                    f"inject failed after {attempts} consecutive attempts "
                    f"(secret_ok={secret_ok}, patches_left={len(pending)}); "
                    "bounded retry exhausted — fix the recipe or recover"
                ),
            },
        )
    return False


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
    + transformation, zero credential material in the CR).

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
# Target I/O (guarded patches + status)
# ---------------------------------------------------------------------------


def _target_namespace(target: dict) -> str:
    """Namespace of the reconcile target (targetRef.namespace → CR's ns
    → ``default``; the fault props land beside their target)."""
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


async def _set_phase(
    handle_value: str, kubeconfig: str, phase: str, extra: dict
) -> bool:
    """Merge-patch the CR's status subresource (phase + timestamps/log)."""
    from chaos_agent.config.settings import settings

    namespace, _, name = handle_value.partition("/")
    payload = {"status": {"phase": phase, **extra}}
    r = await _provider_mod._kubectl(
        "patch",
        [f"{CRD_PLURAL}.{settings.faultdrill_crd_group}", name,
         "-n", namespace, "--subresource=status", "--type=merge",
         "-p", json.dumps(payload)],
        kubeconfig,
    )
    if r.exit_code != 0:
        logger.warning(
            "faultdrill set_phase(%s) for %s failed rc=%s: %s",
            phase, handle_value, r.exit_code, (r.stderr or "")[:120],
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


def _injected_at_of(cr: dict) -> Optional[float]:
    """``status.injectedAt`` as a unix timestamp (None when absent or
    unparseable — the experiment's two-format parser)."""
    s = str(((cr.get("status") or {}).get("injectedAt")) or "")
    if not s:
        return None
    s2 = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s2).timestamp()
    except ValueError:
        try:
            return datetime.strptime(s2, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
        except ValueError:
            return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
