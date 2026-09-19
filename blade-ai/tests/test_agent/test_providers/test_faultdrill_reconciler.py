"""faultdrill-cr-channel M2 task 2.2: session-side reconciler pins.

Design D4's product migration of the v2 experiment's ``reconcile_once``
(``probe_cr_full_experiment.py``): the CR is the single source of truth
(every pass re-reads it), the TTL verdict reads ``status.injectedAt``
(cluster state, not process memory), and the three idempotence guards
(pre-inject readback / pre-restore readback / CR-vanished cleanup) are
pinned against their v1-incident motivation. The failure cap lands
``phase=Failed`` with the reason in ``status.restoreLog`` (bounded retry
— a bad recipe terminates, never spins).

Everything kubectl-shaped routes through ONE patched seam
(``provider._kubectl`` — the reconciler accesses it by module attribute
so the single patch point covers both ``_read_cr_json`` and the direct
action calls).
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from chaos_agent.tools.guard import CommandResult

from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.faultdrill.reconciler import (
    MAX_INJECT_ATTEMPTS,
    RECONCILE_INTERVAL_SECONDS,
    _filter_already_applied,
    _filter_already_restored,
    _json_path_exists,
    active_reconciler_handles,
    arm_session_reconciler,
    disarm_session_reconciler,
    reconcile_once,
)
from chaos_agent.config.settings import settings


_GROUP = "drill.blade-ai.io"
_HANDLE = "cms-demo/fd-x"
_TARGET = {"kind": "Deployment", "name": "drill-dep", "namespace": "cms-demo"}
_CR_RESOURCE = f"faultdrills.{_GROUP}"


@pytest.fixture(autouse=True)
def _restore_dark_launch():
    """Every test here may flip the flag or spawn tasks; both must be
    swept — a leaked live reconciler would sleep forever on the shared
    event loop. The flag restores its PRE-TEST value (the default
    flipped True post dark-launch)."""
    _orig_flag = settings.faultdrill_enabled
    try:
        yield
    finally:
        for handle in active_reconciler_handles():
            disarm_session_reconciler(handle)
        settings.faultdrill_enabled = _orig_flag
        FaultProviderRegistry.register_builtins()


def _R(exit_code: int, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


class _Router:
    """Canned kubectl router keyed by ``(subcommand, first v_arg)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str]] = []
        self.routes: dict[tuple[str, str], object] = {}

    def on(self, sub: str, first: str, result: object) -> None:
        self.routes[(sub, first)] = result

    def calls_matching(self, sub: str) -> list[tuple[str, list[str], str]]:
        return [c for c in self.calls if c[0] == sub]

    async def __call__(
        self, sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0
    ):
        self.calls.append((sub, list(v_args), stdin_data))
        key = (sub, v_args[0] if v_args else "")
        result = self.routes.get(key)
        if callable(result):
            result = result(sub, list(v_args), stdin_data)
        return result if result is not None else _R(0, "{}")


@pytest.fixture
def _kube(monkeypatch):
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider

    router = _Router()
    monkeypatch.setattr(fd_provider, "_kubectl", router)
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    return router


def _cr(
    *,
    phase: str = "",
    injected_at: str = "",
    patches: list | None = None,
    restore: list | None = None,
    inv: dict | None = None,
    ttl: int = 600,
) -> dict:
    spec: dict = {
        "action": "specPatch",
        "targetRef": dict(_TARGET),
        "patches": patches if patches is not None else [
            {"op": "add", "path": "/spec/paused", "value": True},
        ],
        "restorePatches": restore if restore is not None else [
            {"op": "remove", "path": "/spec/paused"},
        ],
        "durationSeconds": ttl,
    }
    if inv:
        spec["invalidSecret"] = inv
    status: dict = {}
    if phase:
        status["phase"] = phase
    if injected_at:
        status["injectedAt"] = injected_at
    return {
        "apiVersion": f"{_GROUP}/v1alpha1",
        "kind": "FaultDrill",
        "metadata": {"name": "fd-x", "namespace": "cms-demo"},
        "spec": spec,
        "status": status,
    }


def _target_json(*, paused=None) -> dict:
    spec: dict = {}
    if paused is not None:
        spec["paused"] = paused
    return {"metadata": {"name": "drill-dep"}, "spec": spec}


def _src_secret(host: str = "registry.example.com") -> dict:
    cfg = {"auths": {host: {
        "username": "real-user", "password": "real-pass",
        "auth": base64.b64encode(b"real-user:real-pass").decode(),
    }}}
    return {
        "type": "kubernetes.io/dockerconfigjson",
        "data": {
            ".dockerconfigjson": base64.b64encode(
                json.dumps(cfg).encode()
            ).decode(),
        },
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_state() -> dict:
    return {"inject_attempts": 0, "did_inject": False}


def _status_payload(call) -> dict:
    """Parsed status-subresource merge payload of a CR status patch call
    (command shape: [resource, name, -n, ns, --subresource=status,
    --type=merge, -p, payload] — the payload is element 7)."""
    return json.loads(call[1][7])


# ---------------------------------------------------------------------------
# reconcile_once — the pass machine
# ---------------------------------------------------------------------------


async def test_pending_cr_injects_recipe_and_lands_injected(_kube):
    """Pending → apply the patches + set phase=Injected with injectedAt
    (the TTL verdict's anchor). Status patch is a merge on the status
    subresource."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr())))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json())))
    _kube.on("patch", "deployment", _R(0, "deployment patched"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    outcome = await reconcile_once(_HANDLE, "kc", _fresh_state())
    assert outcome == "injected"

    dep_patches = _kube.calls_matching("patch")
    target_patch = next(c for c in dep_patches if c[1][0] == "deployment")
    assert target_patch[1] == [
        "deployment", "drill-dep", "-n", "cms-demo",
        "--type=json", "-p", json.dumps([{"op": "add", "path": "/spec/paused", "value": True}]),
    ]
    status_patch = next(c for c in dep_patches if c[1][0] == _CR_RESOURCE)
    assert status_patch[1][:7] == [
        _CR_RESOURCE, "fd-x", "-n", "cms-demo",
        "--subresource=status", "--type=merge", "-p",
    ]
    payload = json.loads(status_patch[1][7])
    assert payload["status"]["phase"] == "Injected"
    assert payload["status"]["injectedAt"]


async def test_guard1_already_faulted_target_skips_repatch(_kube):
    """Guard 1 (pre-inject readback, the v1 double-injection incident):
    the target already carries the add-path → ZERO target patches, only
    the status backfill — reconciliation converges without re-applying."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr())))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json(paused=True))))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    state = _fresh_state()
    outcome = await reconcile_once(_HANDLE, "kc", state)
    assert outcome == "injected"
    assert state["did_inject"] is True
    # No deployment patch call at all — the recipe is already in effect.
    assert not _kube.calls_matching("patch") or all(
        c[1][0] == _CR_RESOURCE for c in _kube.calls_matching("patch")
    )


async def test_guard1_partial_application_applies_only_missing(_kube):
    """Crash-midway re-run: one of two add ops already landed → only the
    missing op is applied (the existing one would 422)."""
    patches = [
        {"op": "add", "path": "/spec/paused", "value": True},
        {"op": "add", "path": "/spec/other", "value": "x"},
    ]
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(patches=patches))))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json(paused=True))))
    _kube.on("patch", "deployment", _R(0, "ok"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    assert await reconcile_once(_HANDLE, "kc", _fresh_state()) == "injected"
    target_patch = next(
        c for c in _kube.calls_matching("patch") if c[1][0] == "deployment"
    )
    applied = json.loads(target_patch[1][6])
    assert applied == [{"op": "add", "path": "/spec/other", "value": "x"}]


async def test_inject_failure_below_cap_retries_next_pass(_kube):
    """A failed inject attempt counts but does NOT fail the CR — the next
    pass retries (bounded, not immediate-death)."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr())))
    _kube.on("get", "deployment", _R(1, "", "deployment not found"))
    _kube.on("patch", "deployment", _R(1, "", "no such deployment"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    state = _fresh_state()
    assert await reconcile_once(_HANDLE, "kc", state) == "retry-inject"
    assert state["inject_attempts"] == 1
    # No Failed status landed — the cap has not been reached.
    assert all(
        _status_payload(c)["status"]["phase"] != "Failed"
        for c in _kube.calls_matching("patch") if c[1][0] == _CR_RESOURCE
    )


async def test_inject_failure_cap_lands_failed_terminal(_kube):
    """D4 bounded retry: MAX_INJECT_ATTEMPTS consecutive failures land
    phase=Failed with the reason in restoreLog — observable, no infinite
    loop, recover can still best-effort it."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr())))
    _kube.on("get", "deployment", _R(1, "", "deployment not found"))
    _kube.on("patch", "deployment", _R(1, "", "no such deployment"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    state = _fresh_state()
    outcomes = [
        await reconcile_once(_HANDLE, "kc", state)
        for _ in range(MAX_INJECT_ATTEMPTS)
    ]
    assert outcomes == ["retry-inject"] * (MAX_INJECT_ATTEMPTS - 1) + [
        "abort-inject-failed",
    ]
    failed = [
        _status_payload(c) for c in _kube.calls_matching("patch")
        if c[1][0] == _CR_RESOURCE
        and _status_payload(c)["status"]["phase"] == "Failed"
    ]
    assert failed and "attempts" in failed[-1]["status"]["restoreLog"]

    # Terminal now: the next pass is a no-op exit.
    _kube.calls.clear()
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(phase="Failed"))))
    assert await reconcile_once(_HANDLE, "kc", state) == "terminal"
    assert not _kube.calls_matching("patch")


async def test_injected_ttl_not_expired_is_noop(_kube):
    """Fresh injectedAt + ttl 600 → noop: zero writes on the happy path."""
    _kube.on("get", _CR_RESOURCE, _R(
        0, json.dumps(_cr(phase="Injected", injected_at=_now_iso())),
    ))
    assert await reconcile_once(_HANDLE, "kc", _fresh_state()) == "noop"
    assert len(_kube.calls) == 1  # the CR read and nothing else


async def test_injected_missing_timestamp_selfheals(_kube):
    """phase=Injected but no injectedAt → rewrite the timestamp
    (controller self-heal — the TTL verdict depends on it)."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(phase="Injected"))))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))
    assert await reconcile_once(_HANDLE, "kc", _fresh_state()) == "noop"
    status_patch = _kube.calls_matching("patch")[0]
    payload = _status_payload(status_patch)
    assert payload["status"]["phase"] == "Injected"
    assert payload["status"]["injectedAt"]


async def test_ttl_expired_restores_and_lands_recovered(_kube):
    """TTL verdict from status.injectedAt (cluster state): age ≥ ttl →
    restore patches + delete the derived secret + phase=Recovered."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=99999)).isoformat()
    _kube.on("get", _CR_RESOURCE, _R(
        0, json.dumps(_cr(phase="Injected", injected_at=old, inv={
            "name": "inv-cred", "sourceName": "src-cred",
        })),
    ))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json(paused=True))))
    _kube.on("patch", "deployment", _R(0, "ok"))
    _kube.on("delete", "secret", _R(0, 'secret "inv-cred" deleted'))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    state = _fresh_state()
    state["did_inject"] = True
    assert await reconcile_once(_HANDLE, "kc", state) == "recovered"

    delete = _kube.calls_matching("delete")[0]
    assert delete[1] == [
        "secret", "inv-cred", "-n", "cms-demo", "--ignore-not-found",
    ]
    status_patch = next(
        c for c in _kube.calls_matching("patch") if c[1][0] == _CR_RESOURCE
    )
    payload = _status_payload(status_patch)
    assert payload["status"]["phase"] == "Recovered"
    assert payload["status"]["recoveredAt"]
    assert payload["status"]["restoreLog"]


async def test_guard2_baseline_target_skips_restore_patch(_kube):
    """Guard 2 (pre-restore readback): the remove-path is already absent
    → zero restore patches (json-remove on a missing path errors); the
    secret deletion + Recovered status still land."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=99999)).isoformat()
    _kube.on("get", _CR_RESOURCE, _R(
        0, json.dumps(_cr(phase="Injected", injected_at=old, inv={
            "name": "inv-cred", "sourceName": "src-cred",
        })),
    ))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json())))
    _kube.on("delete", "secret", _R(0, "deleted"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    state = _fresh_state()
    state["did_inject"] = True
    assert await reconcile_once(_HANDLE, "kc", state) == "recovered"
    assert all(
        c[1][0] != "deployment" for c in _kube.calls_matching("patch")
    )


async def test_restore_failure_lands_failed(_kube):
    """A failing restore (patch rejected) lands phase=Failed — the abort
    label carries it out of the loop for recover to pick up."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=99999)).isoformat()
    _kube.on("get", _CR_RESOURCE, _R(
        0, json.dumps(_cr(phase="Injected", injected_at=old)),
    ))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json(paused=True))))
    _kube.on("patch", "deployment", _R(1, "", "patch rejected"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    state = _fresh_state()
    state["did_inject"] = True
    assert await reconcile_once(_HANDLE, "kc", state) == "abort-restore-failed"
    failed = [
        _status_payload(c) for c in _kube.calls_matching("patch")
        if c[1][0] == _CR_RESOURCE
        and _status_payload(c)["status"]["phase"] == "Failed"
    ]
    assert failed


async def test_guard3_cr_vanished_after_inject_cleans_up(_kube):
    """Guard 3 (finalizer semantics in miniature): the CR vanishing after
    THIS task injected triggers a best-effort restore from the CACHED
    recipe (the live CR is gone — the cache is all there is), then exits."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr())))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json())))
    _kube.on("patch", "deployment", _R(0, "ok"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))
    _kube.on("delete", "secret", _R(0, "deleted"))

    state = _fresh_state()
    assert await reconcile_once(_HANDLE, "kc", state) == "injected"
    _kube.calls.clear()

    # The CR is now gone; the cached recipe must still drive a restore.
    # The target reads back FAULTED (paused=True — what the first pass
    # applied), so guard 2 keeps the remove op and the patch really lands.
    _kube.on("get", _CR_RESOURCE, _R(1, "", "Error from server (NotFound)"))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json(paused=True))))
    assert await reconcile_once(_HANDLE, "kc", state) == "abort-cr-missing"
    target_patch = next(
        c for c in _kube.calls_matching("patch") if c[1][0] == "deployment"
    )
    assert json.loads(target_patch[1][6]) == [
        {"op": "remove", "path": "/spec/paused"},
    ]


async def test_cr_vanished_never_injected_is_quiet(_kube):
    """A vanished CR this task never injected is a plain abort — zero
    cluster writes (nothing of ours to clean)."""
    _kube.on("get", _CR_RESOURCE, _R(1, "", "not found"))
    assert await reconcile_once(_HANDLE, "kc", _fresh_state()) == "abort-cr-missing"
    assert len(_kube.calls) == 1


# ---------------------------------------------------------------------------
# invalidSecret derivation (D5: source reference + transformation)
# ---------------------------------------------------------------------------


async def test_invalid_secret_derived_from_source_with_override(_kube):
    """The fault-prop secret is DERIVED at injection time: read the source
    (same RBAC face as the SOP path), override the registry host when the
    recipe asks, rewrite the credentials to the invalid marker — zero
    credential material ever transited the CR."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(inv={
        "name": "inv-cred",
        "sourceName": "src-cred",
        "registryHostOverride": "registry.override.example.com",
    }))))
    _kube.on("get", "secret", _R(0, json.dumps(_src_secret())))
    _kube.on("apply", "-f", _R(0, "secret created"))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json())))
    _kube.on("patch", "deployment", _R(0, "ok"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    assert await reconcile_once(_HANDLE, "kc", _fresh_state()) == "injected"

    # get order is CR → target (guard 1) → source secret.
    get_calls = _kube.calls_matching("get")
    assert [c[1][0] for c in get_calls] == [_CR_RESOURCE, "deployment", "secret"]
    src_read = get_calls[2]
    assert src_read[1] == ["secret", "src-cred", "-n", "cms-demo", "-o", "json"]
    apply = _kube.calls_matching("apply")[0]
    assert apply[1] == ["-f", "-"]
    import yaml

    manifest = yaml.safe_load(apply[2])
    assert manifest["kind"] == "Secret"
    assert manifest["metadata"] == {"name": "inv-cred", "namespace": "cms-demo"}
    assert manifest["type"] == "kubernetes.io/dockerconfigjson"
    cfg = json.loads(
        base64.b64decode(manifest["data"][".dockerconfigjson"])
    )
    entry = cfg["auths"]["registry.override.example.com"]
    assert entry["username"] == "invalid-credential"
    assert entry["password"] == "invalid-credential"


async def test_invalid_secret_host_defaults_to_source_host(_kube):
    """No override → the derived copy targets the source's own host (the
    dockerconfigjson host-matching law the experiment's second pitfall
    taught: a wrong host means the fault never engages)."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(inv={
        "name": "inv-cred", "sourceName": "src-cred",
    }))))
    _kube.on("get", "secret", _R(0, json.dumps(_src_secret("registry.src.example.com"))))
    _kube.on("apply", "-f", _R(0, "secret created"))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json())))
    _kube.on("patch", "deployment", _R(0, "ok"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    assert await reconcile_once(_HANDLE, "kc", _fresh_state()) == "injected"
    import yaml

    manifest = yaml.safe_load(_kube.calls_matching("apply")[0][2])
    cfg = json.loads(base64.b64decode(manifest["data"][".dockerconfigjson"]))
    assert list(cfg["auths"]) == ["registry.src.example.com"]


async def test_invalid_secret_source_unreadable_counts_as_attempt(_kube):
    """A unreadable source Secret (RBAC / not found) is an inject attempt
    failure — bounded by the same cap, never an infinite loop."""
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(inv={
        "name": "inv-cred", "sourceName": "src-cred",
    }))))
    _kube.on("get", "secret", _R(1, "", "forbidden"))
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json())))
    _kube.on("patch", "deployment", _R(0, "ok"))
    _kube.on("patch", _CR_RESOURCE, _R(0, "cr patched"))

    state = _fresh_state()
    assert await reconcile_once(_HANDLE, "kc", state) == "retry-inject"
    assert state["inject_attempts"] == 1
    # The target patch DID land — but the phase stays unwritten until the
    # secret derivation succeeds too (the recipe is all-or-nothing).
    assert all(
        _status_payload(c)["status"]["phase"] != "Injected"
        for c in _kube.calls_matching("patch") if c[1][0] == _CR_RESOURCE
    )


# ---------------------------------------------------------------------------
# Guard helpers (pure functions)
# ---------------------------------------------------------------------------


def test_filter_already_applied_conservative_semantics():
    """Object paths resolve; array segments stay conservative (kept);
    replace ops are always kept (re-application is idempotent)."""
    target = {"spec": {"paused": True}}
    patches = [
        {"op": "add", "path": "/spec/paused", "value": True},      # exists → drop
        {"op": "add", "path": "/spec/other", "value": 1},          # missing → keep
        {"op": "replace", "path": "/spec/paused", "value": False},  # replace → keep
        {"op": "add", "path": "/spec/list/0", "value": "x"},        # array → keep
    ]
    assert _filter_already_applied(target, patches) == [
        {"op": "add", "path": "/spec/other", "value": 1},
        {"op": "replace", "path": "/spec/paused", "value": False},
        {"op": "add", "path": "/spec/list/0", "value": "x"},
    ]


def test_filter_already_restored_missing_paths_dropped():
    target = {"spec": {}}  # /spec/paused absent
    restore = [
        {"op": "remove", "path": "/spec/paused"},    # absent → drop
        {"op": "replace", "path": "/spec/other", "value": "y"},  # keep
    ]
    assert _filter_already_restored(target, restore) == [
        {"op": "replace", "path": "/spec/other", "value": "y"},
    ]


def test_json_path_exists_object_only():
    doc = {"spec": {"paused": True}}
    assert _json_path_exists(doc, "/spec/paused") is True
    assert _json_path_exists(doc, "/spec/missing") is False
    assert _json_path_exists(doc, "no-slash") is False
    assert _json_path_exists(doc, "/spec/paused/deeper") is False


# ---------------------------------------------------------------------------
# Arming lifecycle (the D4 session-side loop)
# ---------------------------------------------------------------------------


async def test_arm_is_idempotent_and_disarm_cancels(_kube, monkeypatch):
    """One loop per handle: a live task absorbs re-arms (it re-reads the
    CR every pass, so a re-apply under the same name needs no restart);
    disarm cancels and re-arming then spawns fresh."""
    monkeypatch.setattr(
        "chaos_agent.agent.providers.faultdrill.reconciler"
        ".RECONCILE_INTERVAL_SECONDS",
        0.01,
    )
    # CR already terminal → the loop exits after its first pass.
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(phase="Recovered"))))

    assert arm_session_reconciler(_HANDLE, "kc") is True
    assert _HANDLE in active_reconciler_handles()
    assert arm_session_reconciler(_HANDLE, "kc") is False  # idempotent
    assert arm_session_reconciler("", "kc") is False       # empty handle
    assert disarm_session_reconciler(_HANDLE) is True
    assert disarm_session_reconciler("no/such") is False

    await asyncio.sleep(0)  # let the done-callback flush


async def test_reconciler_loop_runs_to_terminal(_kube, monkeypatch):
    """End-to-end loop shape: armed → periodic passes → terminal phase
    exits the loop and retires the handle from the registry."""
    monkeypatch.setattr(
        "chaos_agent.agent.providers.faultdrill.reconciler"
        ".RECONCILE_INTERVAL_SECONDS",
        0.01,
    )
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(phase="Recovered"))))

    assert arm_session_reconciler(_HANDLE, "kc") is True
    for _ in range(200):  # bounded wait for the done-callback flush
        if _HANDLE not in active_reconciler_handles():
            break
        await asyncio.sleep(0.01)
    assert _HANDLE not in active_reconciler_handles()
    assert _kube.calls_matching("get")  # the loop did read the CR


async def test_reconciler_loop_survives_raising_pass(_kube, monkeypatch):
    """A raising pass never kills the loop (level-triggered: the next
    pass re-reads the CR and converges)."""
    monkeypatch.setattr(
        "chaos_agent.agent.providers.faultdrill.reconciler"
        ".RECONCILE_INTERVAL_SECONDS",
        0.01,
    )
    boom = {"armed": True}

    def flaky(_sub, _v_args, _stdin):
        if boom["armed"]:
            boom["armed"] = False
            raise RuntimeError("transport hiccup")
        return _R(0, json.dumps(_cr(phase="Recovered")))

    _kube.on("get", _CR_RESOURCE, flaky)

    assert arm_session_reconciler(_HANDLE, "kc") is True
    for _ in range(200):
        if _HANDLE not in active_reconciler_handles():
            break
        await asyncio.sleep(0.01)
    assert _HANDLE not in active_reconciler_handles()
    assert len(_kube.calls_matching("get")) >= 2  # raised once, then read


# ---------------------------------------------------------------------------
# Registry dispatch (dark-launch invariant)
# ---------------------------------------------------------------------------


async def test_registry_arms_only_when_channel_registered(_kube, monkeypatch):
    """Dark-launch invariant: the seam routes through registration —
    flag ON dispatches to the hook (a REAL spawn — swept by the autouse
    fixture); flag OFF is a structural no-op."""
    monkeypatch.setattr(
        "chaos_agent.agent.providers.faultdrill.reconciler"
        ".RECONCILE_INTERVAL_SECONDS",
        0.01,
    )
    _kube.on("get", _CR_RESOURCE, _R(0, json.dumps(_cr(phase="Recovered"))))

    settings.faultdrill_enabled = True
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.arm_session_reconciler(_HANDLE, "kc") is True
    assert _HANDLE in active_reconciler_handles()

    settings.faultdrill_enabled = False
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.arm_session_reconciler("other/ns-name", "kc") is False
    assert "other/ns-name" not in active_reconciler_handles()


def test_interval_and_cap_are_the_designed_constants():
    """Design D4 pins: 5s cadence, 3-attempt cap (bounded retry)."""
    assert RECONCILE_INTERVAL_SECONDS == 5.0
    assert MAX_INJECT_ATTEMPTS == 3
