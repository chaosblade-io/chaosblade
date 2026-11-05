"""Plaintext→base64 sentinel pins (the Secret/ConfigMap credential cousin of
``test_assembler_cert_payload.py``).

A config-object content fault (Secret credential tamper) injects a value K8s
stores base64-encoded (``.data.<key>`` is base64 by contract). base64 is a
DETERMINISTIC fact, yet the main path used to make the LLM hand-encode it into
the inject op's ``value`` — slow (byte-by-byte derivation in reasoning) and
error-prone: cited incident inject-654ab665 (W-73R) wrote a WRONG constant into
the plan gate (``d3JvbmctcGFzcy0xMjM=`` / wrong-pass-123 vs the intent's
wrong-pass-999); only the verifier's independent hand-decode caught it, so a
less careful verifier would have false-negatived a healthy drill — and landing
readback only proves the patch LANDED, not that it matches intent, so a wrong
value still passes readback.

The ``<faultdrill:b64><plaintext>`` sentinel removes the LLM from that path:
the assembler base64-encodes it deterministically in PURE PYTHON (§0.5, before
anything is built — unlike the cert payload it needs no carrier/openssl) and
backfills ``value`` in place, so the inject patch, the landing readback, the
recovery_handle and the receipt all carry the program-encoded base64.

These pins hold that legislation:
  ·  resolve_b64_sentinels encodes the plaintext to the runtime base64
     (invariant, not a literal), leaves non-sentinel / value:null / non-string
     ops byte-identical (the regression guard), and fails closed on an
     empty/prefix-only sentinel;
  ·  the cross-scanner fail-closed invariant: a ``<faultdrill:``-prefixed value
     that is neither a well-formed b64 nor cert sentinel still aborts (in the
     cert scanner) — no malformed sentinel of either kind is injected literally;
  ·  end-to-end: the injection patch carries the RESOLVED base64 (never the
     sentinel), the receipt's recovery_handle.patches carries it too (the
     verify-phase fact surface), a malformed sentinel aborts with ZERO cluster
     calls (nothing built/read — stronger than the cert path's build-then-clean),
     and an ordinary recipe is injected byte-identical.

Everything kubectl-shaped routes through ONE patched seam (``provider._kubectl``
— the assembler's ``_run`` reaches it by module attribute, the same
single-patch-point discipline as the sibling assembler tests).
"""

from __future__ import annotations

import base64
import copy
import json
import re

import pytest

from chaos_agent.tools.guard import CommandResult

from chaos_agent.agent.providers.faultdrill.assembler import (
    _AssemblyError,
    _B64_SENTINEL_PREFIX,
    _cert_sentinel_specs,
    assemble_recovery_carrier,
    faultdrill_assemble_carrier,
    resolve_b64_sentinels,
)
from chaos_agent.config.settings import settings


# ---------------------------------------------------------------------------
# Secret recipe fixtures (the credential-tamper target shape)
# ---------------------------------------------------------------------------

_PLAINTEXT = "wrong-pass-999"
# The invariant anchor: the resolved value is ALWAYS the runtime base64 of the
# plaintext — never a hand-copied literal (779169a1: pin invariants not values).
_RESOLVED = base64.b64encode(_PLAINTEXT.encode("utf-8")).decode("ascii")
_BASELINE_PLAINTEXT = "baseline-pass-123"
_BASELINE_B64 = base64.b64encode(_BASELINE_PLAINTEXT.encode("utf-8")).decode()
_B64_SENTINEL = f"{_B64_SENTINEL_PREFIX}{_PLAINTEXT}"

_SECRET_BASELINE = {
    "metadata": {"name": "drill-secret-cred", "namespace": "default"},
    "data": {"DB_PASSWORD": _BASELINE_B64},
}
_SECRET_PATCHED = {
    "metadata": {"name": "drill-secret-cred", "namespace": "default"},
    "data": {"DB_PASSWORD": _RESOLVED},
}
# Inject authors the PLAINTEXT via the sentinel; restore live-captures the
# baseline via value:null (the two "deterministic bytes never round-trip
# through LLM tokens" mechanisms, side by side).
_SECRET_PATCHES = [
    {"op": "replace", "path": "/data/DB_PASSWORD", "value": _B64_SENTINEL},
]
_SECRET_RESTORE = [
    {"op": "replace", "path": "/data/DB_PASSWORD", "value": None},
]


# ---------------------------------------------------------------------------
# Canned kubectl router (single-seam patch discipline)
# ---------------------------------------------------------------------------


def _R(exit_code: int, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


class _Router:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str]] = []
        self.routes: dict[tuple[str, str], object] = {}

    def on(self, sub: str, first: str, result: object) -> None:
        self.routes[(sub, first)] = result

    def on_any(self, sub: str, result: object) -> None:
        self.routes[(sub, "*")] = result

    async def __call__(
        self, sub, v_args, kubeconfig, *, stdin_data="", timeout=30.0
    ):
        self.calls.append((sub, list(v_args), stdin_data))
        key = (sub, v_args[0] if v_args else "")
        result = self.routes.get(key)
        if result is None:
            result = self.routes.get((sub, "*"))
        if callable(result):
            result = result(sub, list(v_args), stdin_data)
        return result if result is not None else _R(0, "{}")


def _seq(*results):
    state = {"i": 0}

    def _dispense(sub, v_args, stdin_data):
        result = results[min(state["i"], len(results) - 1)]
        state["i"] += 1
        return result

    return _dispense


def _exec_script(v_args: list[str]) -> str:
    if "--" not in v_args:
        return ""
    tail = v_args[v_args.index("--") + 1:]  # [sh, -c, script]
    return tail[2] if len(tail) >= 3 else ""


def _echoed_ssar(script: str, allowed: bool = True):
    match = re.search(r"-d '(\{.*\})'", script)
    request = json.loads(match.group(1)) if match else {}
    attrs = dict((request.get("spec") or {}).get("resourceAttributes") or {})
    attrs = {k: v for k, v in attrs.items() if v != ""}  # omitempty
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": attrs},
        "status": {"allowed": allowed},
    }


def _b64_exec_router():
    """Route every carrier exec by script SHAPE. The b64 sentinel needs NO
    in-carrier compute (pure Python), so there are no openssl shapes here —
    only the §3 SA-token GET probe / SSAR / §4 arm routing."""

    def _route(sub, v_args, stdin_data):
        script = _exec_script(v_args)
        # The §4 arm script's UNIQUE `echo armed` marker is matched BEFORE
        # the generic %{http_code} probe branch: the self-termination tail
        # (§4 ownerRef GC cascade) captures the restore PATCH's http_code
        # to gate the fail-open self-delete, so the arm script now ALSO
        # contains `http_code` — without this ordering it mis-routes to the
        # probe's bare-code response and never echoes `armed`.
        if "echo armed" in script:
            return _R(0, "armed")
        if "http_code" in script:
            return _R(0, "200")
        if "selfsubjectaccessreviews" in script:
            return _R(0, json.dumps(_echoed_ssar(script)))
        return _R(0, "")

    return _route


@pytest.fixture(autouse=True)
def _carrier_settings(monkeypatch):
    """Pin the carrier vocabulary to a fixed set (settings is a shared
    singleton — a preceding test may have flipped discovery or the allowlist)."""
    monkeypatch.setattr(
        settings, "recovery_carrier_allowed_images",
        "busybox:1.36,curlimages/curl:8.8.0,terway:v1.4.3",
    )
    monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")
    monkeypatch.setattr(settings, "recovery_carrier_name_prefix", "drill-rc-")
    monkeypatch.setattr(settings, "recovery_carrier_max_sleep_seconds", 86400)


@pytest.fixture
def _kube(monkeypatch):
    import chaos_agent.agent.providers.faultdrill.provider as fd_provider

    router = _Router()
    monkeypatch.setattr(fd_provider, "_kubectl", router)
    return router


def _wire_secret(router: _Router, *, patched=None) -> None:
    """Canned happy-path wiring: baseline read → patched readback (mirrors the
    resolved base64 so landing verification passes), a Running skeleton,
    script-shaped exec responses."""
    router.on("get", "secret", _seq(
        _R(0, json.dumps(_SECRET_BASELINE)),
        _R(0, json.dumps(patched if patched is not None else _SECRET_PATCHED)),
    ))
    router.on("get", "pod", _R(0, json.dumps({"status": {"phase": "Running"}})))
    router.on_any("exec", _b64_exec_router())


async def _assemble_secret(router, *, patches=None, restore=None):
    # DEEP copy: the assembler backfills the resolved base64 into the op dicts
    # IN PLACE (§0.5), so a shallow copy would let one test's backfill mutate
    # the module-level fixture and leak into the next.
    return await assemble_recovery_carrier(
        target_kind="Secret", target_name="drill-secret-cred",
        target_namespace="default",
        patches=copy.deepcopy(
            patches if patches is not None else _SECRET_PATCHES
        ),
        restore_patches=copy.deepcopy(
            restore if restore is not None else _SECRET_RESTORE
        ),
        duration_seconds=600, kubeconfig="/kc", task_id="task-b64",
        carrier_image="terway:v1.4.3", readback_compare="exact",
    )


def _inject_patch_arg(router: _Router) -> list | None:
    """The ``-p`` JSON of the injection patch, or None if never fired."""
    for sub, v_args, _ in router.calls:
        if sub == "patch" and v_args and v_args[0] == "secret":
            return json.loads(v_args[v_args.index("-p") + 1])
    return None


# ---------------------------------------------------------------------------
# resolve_b64_sentinels — pure
# ---------------------------------------------------------------------------


def test_resolve_encodes_plaintext_to_runtime_base64():
    patches = [{"op": "replace", "path": "/data/DB_PASSWORD",
                "value": _B64_SENTINEL}]
    n = resolve_b64_sentinels(patches)
    assert n == 1
    # Invariant: the resolved value IS the runtime base64 of the plaintext.
    assert patches[0]["value"] == base64.b64encode(
        _PLAINTEXT.encode("utf-8")
    ).decode("ascii")
    assert patches[0]["value"] == _RESOLVED


def test_resolve_no_sentinel_byte_identical():
    # The regression guard: an ordinary recipe value is untouched.
    patches = [{"op": "replace", "path": "/data/x", "value": "PLAINCRT"}]
    before = copy.deepcopy(patches)
    assert resolve_b64_sentinels(patches) == 0
    assert patches == before


def test_resolve_value_null_untouched():
    # value:null is the restore live-capture sentinel — a DIFFERENT mechanism;
    # the b64 resolver must leave it null so _capture_live_baselines still runs.
    patches = [{"op": "replace", "path": "/data/x", "value": None}]
    assert resolve_b64_sentinels(patches) == 0
    assert patches[0]["value"] is None


def test_resolve_non_string_value_ignored():
    patches = [{"op": "replace", "path": "/data/x", "value": 42}]
    assert resolve_b64_sentinels(patches) == 0
    assert patches[0]["value"] == 42


def test_resolve_empty_plaintext_fails_closed():
    # A prefix-only sentinel (empty plaintext) aborts — an empty credential
    # must never be silently injected.
    with pytest.raises(_AssemblyError, match="malformed base64 sentinel"):
        resolve_b64_sentinels(
            [{"op": "replace", "path": "/data/x", "value": _B64_SENTINEL_PREFIX}]
        )


def test_resolve_multiple_ops_all_encoded():
    patches = [
        {"op": "replace", "path": "/data/a", "value": "<faultdrill:b64>alpha"},
        {"op": "replace", "path": "/data/b", "value": "literal-untouched"},
        {"op": "replace", "path": "/data/c", "value": "<faultdrill:b64>beta"},
    ]
    assert resolve_b64_sentinels(patches) == 2
    assert patches[0]["value"] == base64.b64encode(b"alpha").decode()
    assert patches[1]["value"] == "literal-untouched"
    assert patches[2]["value"] == base64.b64encode(b"beta").decode()


def test_resolve_plaintext_special_chars_and_unicode():
    # The plaintext never reaches a shell (straight into b64encode), so it
    # needs no charset restriction — '>', spaces, newline, unicode all encode.
    for plain in ("a>b c", "line1\nline2", "pässwörd-✓", "===pad==="):
        patches = [{"op": "replace", "path": "/data/x",
                    "value": f"{_B64_SENTINEL_PREFIX}{plain}"}]
        resolve_b64_sentinels(patches)
        assert patches[0]["value"] == base64.b64encode(
            plain.encode("utf-8")
        ).decode("ascii")


# ---------------------------------------------------------------------------
# Cross-scanner fail-closed invariant (b64 ∪ cert cover the <faultdrill: space)
# ---------------------------------------------------------------------------


def test_b64_resolver_skips_non_b64_faultdrill_prefix():
    # A cert sentinel (or any other <faultdrill: form) is NOT the b64
    # resolver's business — it must leave it for the cert scanner.
    cert = [{"op": "replace", "path": "/data/tls.crt",
             "value": "<faultdrill:expired-cert cn=h part=crt>"}]
    assert resolve_b64_sentinels(cert) == 0
    assert cert[0]["value"].startswith("<faultdrill:")


def test_malformed_faultdrill_prefix_still_aborts_in_cert_scanner():
    # The invariant that keeps the namespace fail-closed across BOTH kinds:
    # a <faultdrill: value that is neither a well-formed b64 sentinel (handled
    # by resolve_b64_sentinels) nor a well-formed cert sentinel aborts in the
    # cert scanner — never injected as a literal string.
    bogus = [{"op": "replace", "path": "/data/x", "value": "<faultdrill:bogus>"}]
    assert resolve_b64_sentinels(bogus) == 0  # not a b64 sentinel → skipped
    with pytest.raises(_AssemblyError):
        _cert_sentinel_specs(bogus)  # ... and the cert scanner fails closed


def test_resolved_value_no_longer_carries_namespace_prefix():
    # After resolution the value is plain base64 (no <faultdrill: prefix), so
    # the downstream cert scanner sees an ordinary recipe and stays inert.
    patches = [{"op": "replace", "path": "/data/x", "value": _B64_SENTINEL}]
    resolve_b64_sentinels(patches)
    assert not patches[0]["value"].startswith("<faultdrill:")
    assert _cert_sentinel_specs(patches) == {}


# ---------------------------------------------------------------------------
# End-to-end through assemble_recovery_carrier / the tool wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_injects_resolved_base64_not_sentinel(_kube):
    _wire_secret(_kube)
    receipt = await _assemble_secret(_kube)

    assert receipt["status"] == "success", receipt
    # The injection patch carries the RESOLVED base64, never the sentinel
    # (backfill happens in place at §0.5, before arm→inject).
    injected = _inject_patch_arg(_kube)
    assert injected is not None, "no injection patch was dispatched"
    by_path = {op["path"]: op["value"] for op in injected}
    assert by_path["/data/DB_PASSWORD"] == _RESOLVED
    assert "<faultdrill:" not in json.dumps(injected)

    # §0.5 resolved BEFORE anything was armed/injected.
    labels = [s["step"] for s in receipt["steps"]]
    assert "b64_resolve" in labels
    assert labels.index("b64_resolve") < labels.index("arm")


@pytest.mark.asyncio
async def test_receipt_recovery_handle_carries_resolved_value(_kube):
    # The verify-phase fact surface: recovery_handle.patches (which rides into
    # state and the receipt) carries the program-derived base64, so the
    # verifier compares live data against an authoritative constant instead of
    # hand-decoding.
    _wire_secret(_kube)
    receipt = await _assemble_secret(_kube)
    assert receipt["status"] == "success", receipt
    handle_patches = receipt["artifact"]["recovery_handle"]["patches"]
    assert handle_patches[0]["value"] == _RESOLVED
    assert "<faultdrill:" not in json.dumps(handle_patches)


@pytest.mark.asyncio
async def test_assemble_malformed_sentinel_raises_before_any_call(_kube):
    # §0.5 fires before §2's target read, so a malformed sentinel aborts with
    # ZERO cluster calls — nothing read, nothing built (stronger than the cert
    # path, which builds then cleans).
    bad = [{"op": "replace", "path": "/data/DB_PASSWORD",
            "value": _B64_SENTINEL_PREFIX}]  # empty plaintext
    with pytest.raises(_AssemblyError, match="malformed base64 sentinel"):
        await _assemble_secret(_kube, patches=bad)
    assert _kube.calls == [], "must touch nothing before the fail-closed abort"


@pytest.mark.asyncio
async def test_wrapper_malformed_sentinel_is_a_failed_receipt(_kube):
    # The tool wrapper catches the §0.5 _AssemblyError into an honest failed
    # receipt (nothing armed, nothing injected).
    out = await faultdrill_assemble_carrier.ainvoke({
        "target_kind": "Secret", "target_name": "drill-secret-cred",
        "target_namespace": "default",
        "patches": json.dumps(
            [{"op": "replace", "path": "/data/DB_PASSWORD",
              "value": _B64_SENTINEL_PREFIX}]
        ),
        "restore_patches": json.dumps(_SECRET_RESTORE),
        "duration_seconds": 600, "kubeconfig": "/kc", "task_id": "task-b64",
    })
    receipt = json.loads(out)
    assert receipt["status"] == "failed"
    assert "malformed base64 sentinel" in receipt["error"]
    assert receipt["carrier"]["armed"] is False
    assert _inject_patch_arg(_kube) is None
    assert not any(sub == "create" for sub, _, _ in _kube.calls)


@pytest.mark.asyncio
async def test_assemble_no_sentinel_patches_byte_identical(_kube):
    # Regression guard end-to-end: an ordinary Secret recipe (no sentinel) is
    # injected verbatim and records no b64_resolve step.
    plain_value = base64.b64encode(b"already-encoded").decode()
    plain_patches = [
        {"op": "replace", "path": "/data/DB_PASSWORD", "value": plain_value},
    ]
    plain_restore = [
        {"op": "replace", "path": "/data/DB_PASSWORD", "value": _BASELINE_B64},
    ]
    plain_readback = {
        "metadata": {"name": "drill-secret-cred", "namespace": "default"},
        "data": {"DB_PASSWORD": plain_value},
    }
    _wire_secret(_kube, patched=plain_readback)

    receipt = await _assemble_secret(
        _kube, patches=plain_patches, restore=plain_restore,
    )
    assert receipt["status"] == "success", receipt
    injected = _inject_patch_arg(_kube)
    assert injected == plain_patches
    assert "b64_resolve" not in [s["step"] for s in receipt["steps"]]
