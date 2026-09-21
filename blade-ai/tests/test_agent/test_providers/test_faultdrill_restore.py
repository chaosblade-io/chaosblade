"""faultdrill restore primitives — direct-drive pins (M2 task 2.1).

``restore.py`` is the surviving half of the old ``reconciler.py`` split:
the guarded patch replay, target readback and derived-invalid-secret
handling that ``provider._replay_restore_recipe`` (the ND7 ledger-model
replay, task 2.3) drives for BOTH faces. These pins drive the
primitives DIRECTLY; the recover-path wiring (the replay core routing
through ``_do_restore``) is pinned in ``test_faultdrill_provider.py``.

Everything kubectl-shaped routes through ONE patched seam
(``provider._kubectl`` — restore.py accesses it by module attribute so
the single patch point covers both provider's own dispatches and every
direct call here).
"""

from __future__ import annotations

import base64
import json

import pytest
from chaos_agent.tools.guard import CommandResult

import chaos_agent.agent.providers.faultdrill.provider as fd_provider
from chaos_agent.agent.providers.faultdrill.restore import (
    _apply_derived_invalid_secret,
    _delete_invalid_secret,
    _do_restore,
    _filter_already_applied,
    _filter_already_restored,
    _get_target_json,
    _json_path_exists,
    _patch_target,
    _target_namespace,
)
from chaos_agent.config.settings import settings


_GROUP = "drill.blade-ai.io"
_TARGET = {"kind": "Deployment", "name": "drill-dep", "namespace": "cms-demo"}


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
    router = _Router()
    monkeypatch.setattr(fd_provider, "_kubectl", router)
    monkeypatch.setattr(settings, "faultdrill_crd_group", _GROUP)
    return router


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
# _do_restore — guard-2 idempotent replay (direct drive)
# ---------------------------------------------------------------------------


def _faulted_state(*, inv: dict | None = None) -> dict:
    return {
        "target_ref": dict(_TARGET),
        "restore_patches": [{"op": "remove", "path": "/spec/paused"}],
        **({"invalid_secret": inv} if inv else {}),
    }


async def test_do_restore_replays_pending_ops_and_deletes_secret(_kube):
    """Faulted target: the remove op survives guard 2 and the patch really
    lands; the derived secret is deleted with --ignore-not-found."""
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json(paused=True))))
    _kube.on("patch", "deployment", _R(0, "ok"))
    _kube.on("delete", "secret", _R(0, "deleted"))

    assert await _do_restore(
        _faulted_state(inv={"name": "inv-cred", "sourceName": "src-cred"}),
        "kc",
    ) is True

    target_patch = _kube.calls_matching("patch")[0]
    assert target_patch[1] == [
        "deployment", "drill-dep", "-n", "cms-demo",
        "--type=json", "-p", json.dumps([{"op": "remove", "path": "/spec/paused"}]),
    ]
    delete = _kube.calls_matching("delete")[0]
    assert delete[1] == [
        "secret", "inv-cred", "-n", "cms-demo", "--ignore-not-found",
    ]


async def test_do_restore_guard2_baseline_skips_restore_patch(_kube):
    """Guard 2 (pre-restore readback): the remove-path is already absent
    → zero restore patches (json-remove on a missing path errors); the
    secret deletion still lands — partial convergence is honest work."""
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json())))
    _kube.on("delete", "secret", _R(0, "deleted"))

    assert await _do_restore(
        _faulted_state(inv={"name": "inv-cred", "sourceName": "src-cred"}),
        "kc",
    ) is True
    assert not _kube.calls_matching("patch")
    assert len(_kube.calls_matching("delete")) == 1


async def test_do_restore_patch_failure_returns_false(_kube):
    """A rejected patch is a failed restore — never a fabricated success."""
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json(paused=True))))
    _kube.on("patch", "deployment", _R(1, "", "patch rejected"))
    _kube.on("delete", "secret", _R(0, "deleted"))

    assert await _do_restore(
        _faulted_state(inv={"name": "inv-cred", "sourceName": "src-cred"}),
        "kc",
    ) is False


async def test_do_restore_no_invalid_secret_is_patch_only(_kube):
    """No invalidSecret in the recipe → no secret deletion at all."""
    _kube.on("get", "deployment", _R(0, json.dumps(_target_json(paused=True))))
    _kube.on("patch", "deployment", _R(0, "ok"))

    assert await _do_restore(_faulted_state(), "kc") is True
    assert not _kube.calls_matching("delete")


# ---------------------------------------------------------------------------
# _apply_derived_invalid_secret (D5: source reference + transformation)
# ---------------------------------------------------------------------------


async def test_invalid_secret_derived_from_source_with_override(_kube):
    """The fault-prop secret is DERIVED at replay time: read the source,
    override the registry host when the recipe asks, rewrite the
    credentials to the invalid marker — zero credential material ever
    transits the recipe."""
    _kube.on("get", "secret", _R(0, json.dumps(_src_secret())))
    _kube.on("apply", "-f", _R(0, "secret created"))

    assert await _apply_derived_invalid_secret(
        {
            "name": "inv-cred",
            "sourceName": "src-cred",
            "registryHostOverride": "registry.override.example.com",
        },
        _TARGET,
        "kc",
    ) is True

    src_read = _kube.calls_matching("get")[0]
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
    dockerconfigjson host-matching law: a wrong host means the fault
    never engages)."""
    _kube.on("get", "secret", _R(0, json.dumps(
        _src_secret("registry.src.example.com"),
    )))
    _kube.on("apply", "-f", _R(0, "secret created"))

    assert await _apply_derived_invalid_secret(
        {"name": "inv-cred", "sourceName": "src-cred"},
        _TARGET,
        "kc",
    ) is True
    import yaml

    manifest = yaml.safe_load(_kube.calls_matching("apply")[0][2])
    cfg = json.loads(base64.b64decode(manifest["data"][".dockerconfigjson"]))
    assert list(cfg["auths"]) == ["registry.src.example.com"]


async def test_invalid_secret_source_unreadable_returns_false(_kube):
    """An unreadable source Secret (RBAC / not found) is an honest
    failure — bounded by the caller, never a fabricated secret."""
    _kube.on("get", "secret", _R(1, "", "forbidden"))

    assert await _apply_derived_invalid_secret(
        {"name": "inv-cred", "sourceName": "src-cred"},
        _TARGET,
        "kc",
    ) is False
    assert not _kube.calls_matching("apply")


async def test_invalid_secret_source_unparseable_returns_false(_kube):
    """A source whose .dockerconfigjson payload is garbage fails
    honestly — the derivation never guesses a host."""
    _kube.on("get", "secret", _R(0, json.dumps({
        "type": "kubernetes.io/dockerconfigjson",
        "data": {".dockerconfigjson": "bm90IGpzb24="},  # "not json"
    })))

    assert await _apply_derived_invalid_secret(
        {"name": "inv-cred", "sourceName": "src-cred"},
        _TARGET,
        "kc",
    ) is False


# ---------------------------------------------------------------------------
# Target I/O
# ---------------------------------------------------------------------------


async def test_patch_target_command_shape(_kube):
    _kube.on("patch", "deployment", _R(0, "ok"))
    assert await _patch_target(
        _TARGET, [{"op": "remove", "path": "/spec/paused"}], "kc",
    ) is True
    call = _kube.calls_matching("patch")[0]
    assert call[1] == [
        "deployment", "drill-dep", "-n", "cms-demo", "--type=json",
        "-p", json.dumps([{"op": "remove", "path": "/spec/paused"}]),
    ]


async def test_get_target_json_failure_modes(_kube):
    """Unreadable / unparseable / non-object / nameless targets all
    return ``(False, None, reason)`` — the guards then keep the full
    op set (conservative), never fabricate a readback."""
    _kube.on("get", "deployment", _R(1, "", "not found"))
    ok, doc, err = await _get_target_json(_TARGET, "kc")
    assert (ok, doc) == (False, None) and "not found" in err

    _kube.on("get", "deployment", _R(0, "not json"))
    ok, doc, err = await _get_target_json(_TARGET, "kc")
    assert (ok, doc) == (False, None) and "unparseable" in err

    _kube.on("get", "deployment", _R(0, "[1, 2]"))
    ok, doc, err = await _get_target_json(_TARGET, "kc")
    assert (ok, doc) == (False, None) and "non-object" in err

    ok, doc, err = await _get_target_json({"kind": "Deployment"}, "kc")
    assert (ok, doc) == (False, None) and "no name" in err


async def test_delete_invalid_secret_shape(_kube):
    _kube.on("delete", "secret", _R(0, "deleted"))
    assert await _delete_invalid_secret(
        {"name": "inv-cred"}, _TARGET, "kc",
    ) is True
    call = _kube.calls_matching("delete")[0]
    assert call[1] == [
        "secret", "inv-cred", "-n", "cms-demo", "--ignore-not-found",
    ]


def test_target_namespace_fallback():
    assert _target_namespace(_TARGET) == "cms-demo"
    assert _target_namespace({"kind": "Deployment", "name": "d"}) == "default"
    assert _target_namespace({}) == "default"
