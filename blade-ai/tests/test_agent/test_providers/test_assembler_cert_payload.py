"""openspec carrier-cert-payload-generation: computed cert-payload pins.

A TLS-expiry fault's payload is a COMPUTED artifact (an expired cert whose
notAfter is cryptographically signed into the past) — authorable neither by
the LLM nor on the operator host (``_run`` is the single kubectl-only seam).
The carrier Pod is the sole in-cluster managed compute venue, reached through
``_exec_in_carrier`` exactly like the SA-token GET probe / SSAR / arming
scripts, so cert generation adds NO new channel and touches NO guard.

These pins hold the assembler to that legislation (design D1..D6):
  4.1  sentinel parsing extracts cn + the crt/key op-index pair; NO sentinel
       leaves ``patches`` byte-identical (the regression guard);
  4.2  the openssl-ca capability probe fails closed PRE-ARMING (no inject);
  4.3  a resolved cert backfills BOTH ops in place (crt/key atomic);
  4.4  the receipt carries notAfter + crt base64 (white-box verdict evidence);
  4.5  the mini-CA script is built injection-safe (quoted heredoc, baked
       absolute ``$dir``, CN in ``-subj``, ``-enddate`` in the PAST).

Everything kubectl-shaped routes through ONE patched seam
(``provider._kubectl`` — the assembler's ``_run`` reaches it by module
attribute, the same single-patch-point discipline as the sibling assembler
tests).
"""

from __future__ import annotations

import copy
import json
import re
import time

import pytest

from chaos_agent.tools.guard import CommandResult

from chaos_agent.agent.providers.faultdrill.assembler import (
    _AssemblyError,
    _CERT_CRT_MARKER,
    _CERT_END_MARKER,
    _CERT_KEY_MARKER,
    _CERT_NOT_AFTER,
    _between_markers,
    _cert_sentinel_specs,
    _parse_cert_gen_output,
    assemble_recovery_carrier,
    build_cert_capability_probe_script,
    build_cert_gen_script,
)
from chaos_agent.config.settings import settings


# ---------------------------------------------------------------------------
# Secret recipe fixtures (the TLS-expiry target shape)
# ---------------------------------------------------------------------------

_CN = "drill-tls.example.com"
_CRT_SENTINEL = f"<faultdrill:expired-cert cn={_CN} part=crt>"
_KEY_SENTINEL = f"<faultdrill:expired-cert cn={_CN} part=key>"

# The in-carrier generation resolves the sentinels to these fixed base64
# blobs (the exec router below dispenses them; the readback mirrors them so
# the landing verification passes deterministically).
_GEN_CRT = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tRkFLRUNSVA=="
_GEN_KEY = "LS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0tRkFLRUtFWQ=="
_GEN_NOT_AFTER = "Jan  2 00:00:00 2020 GMT"

_CERT_GEN_OUT = (
    f"{_CERT_CRT_MARKER}\n{_GEN_CRT}\n"
    f"{_CERT_KEY_MARKER}\n{_GEN_KEY}\n"
    f"{_CERT_END_MARKER}\nnotAfter={_GEN_NOT_AFTER}\n"
)

_SECRET_BASELINE = {
    "metadata": {"name": "drill-tls-secret", "namespace": "default"},
    "data": {"tls.crt": "BASECRTB64", "tls.key": "BASEKEYB64"},
}
_SECRET_PATCHED = {
    "metadata": {"name": "drill-tls-secret", "namespace": "default"},
    "data": {"tls.crt": _GEN_CRT, "tls.key": _GEN_KEY},
}
_SECRET_PATCHES = [
    {"op": "replace", "path": "/data/tls.crt", "value": _CRT_SENTINEL},
    {"op": "replace", "path": "/data/tls.key", "value": _KEY_SENTINEL},
]
_SECRET_RESTORE = [
    {"op": "replace", "path": "/data/tls.crt", "value": "BASECRTB64"},
    {"op": "replace", "path": "/data/tls.key", "value": "BASEKEYB64"},
]


# ---------------------------------------------------------------------------
# Canned kubectl router (single-seam patch discipline; wildcard exec route —
# carrier names are salt-random so exec cannot be keyed on the pod name)
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


def _cert_exec_router(*, cap: str = "FD_OPENSSL_CA_OK",
                      gen_out: str = _CERT_GEN_OUT):
    """Route every carrier exec by script SHAPE. The capability probe
    (``command -v openssl``) and the generation script (``openssl ca
    -batch``) are the two new cert-payload shapes; the rest mirror the
    §3 probe / SSAR / §4 arm routing of the sibling assembler tests."""

    def _route(sub, v_args, stdin_data):
        script = _exec_script(v_args)
        if "command -v openssl" in script:
            return _R(0, cap)
        if "openssl ca -batch" in script:
            return _R(0, gen_out)
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
    """Pin the carrier vocabulary to a fixed set including an
    openssl-capable image name (settings is a shared singleton — a
    preceding test may have flipped discovery or the allowlist)."""
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


def _wire_secret(router: _Router, *, exec_router=None) -> None:
    """Canned happy-path wiring for the Secret target: baseline read →
    patched readback, a Running skeleton, script-shaped exec responses."""
    router.on("get", "secret", _seq(
        _R(0, json.dumps(_SECRET_BASELINE)),
        _R(0, json.dumps(_SECRET_PATCHED)),
    ))
    router.on("get", "pod", _R(0, json.dumps({"status": {"phase": "Running"}})))
    router.on_any("exec", exec_router or _cert_exec_router())


async def _assemble_secret(router, *, patches=None, restore=None,
                           exec_router=None, carrier_image="terway:v1.4.3"):
    if exec_router is not None:
        router.on_any("exec", exec_router)
    # DEEP copy: the assembler backfills the resolved base64 into the op
    # dicts IN PLACE (design D2), so a shallow copy would let one test's
    # backfill mutate the module-level fixture and leak into the next.
    return await assemble_recovery_carrier(
        target_kind="Secret", target_name="drill-tls-secret",
        target_namespace="default",
        patches=copy.deepcopy(
            patches if patches is not None else _SECRET_PATCHES
        ),
        restore_patches=copy.deepcopy(
            restore if restore is not None else _SECRET_RESTORE
        ),
        duration_seconds=600, kubeconfig="/kc", task_id="task-cert",
        carrier_image=carrier_image, readback_compare="exact",
    )


def _inject_patch_arg(router: _Router) -> list | None:
    """The ``-p`` JSON of the injection patch, or None if never fired."""
    for sub, v_args, _ in router.calls:
        if sub == "patch" and v_args and v_args[0] == "secret":
            return json.loads(v_args[v_args.index("-p") + 1])
    return None


# ---------------------------------------------------------------------------
# 4.1 / 4.3 — sentinel parsing (pure)
# ---------------------------------------------------------------------------


def test_sentinel_specs_extracts_cn_and_pair_indices():
    specs = _cert_sentinel_specs(list(_SECRET_PATCHES))
    assert specs == {_CN: {"crt": 0, "key": 1}}


def test_sentinel_specs_no_sentinel_returns_empty():
    # The regression guard: an ordinary recipe carries no sentinel, so the
    # whole cert-payload path must be inert ({} → step 4.5 is a no-op).
    plain = [{"op": "replace", "path": "/data/x", "value": "y"}]
    assert _cert_sentinel_specs(plain) == {}


def test_sentinel_specs_non_string_value_ignored():
    plain = [{"op": "replace", "path": "/data/x", "value": 42}]
    assert _cert_sentinel_specs(plain) == {}


def test_sentinel_specs_malformed_fails_closed():
    # A value carrying the namespace prefix but not a well-formed sentinel
    # must abort — never be injected as a literal string.
    bad = [{"op": "replace", "path": "/data/tls.crt",
            "value": "<faultdrill:bogus cn=x>"}]
    with pytest.raises(_AssemblyError, match="malformed computed-payload"):
        _cert_sentinel_specs(bad)


def test_sentinel_specs_missing_key_part_fails_closed():
    # crt without its key partner is not injectable — atomic pair.
    lone = [{"op": "replace", "path": "/data/tls.crt", "value": _CRT_SENTINEL}]
    with pytest.raises(_AssemblyError, match="needs BOTH"):
        _cert_sentinel_specs(lone)


def test_sentinel_specs_duplicate_part_fails_closed():
    dup = [
        {"op": "replace", "path": "/data/tls.crt", "value": _CRT_SENTINEL},
        {"op": "replace", "path": "/data/other.crt", "value": _CRT_SENTINEL},
        {"op": "replace", "path": "/data/tls.key", "value": _KEY_SENTINEL},
    ]
    with pytest.raises(_AssemblyError, match="duplicate cert-payload"):
        _cert_sentinel_specs(dup)


# ---------------------------------------------------------------------------
# 4.5 — script construction (string-level, no real openssl)
# ---------------------------------------------------------------------------


def test_capability_probe_script_shape():
    script = build_cert_capability_probe_script()
    # Verdict rides a stdout marker; always exits 0 (never aborts the exec).
    assert "command -v openssl" in script
    assert "-enddate" in script
    assert "FD_OPENSSL_CA_OK" in script
    assert "exit 0" in script


def test_gen_script_bakes_absolute_dir_and_literal_macro():
    script = build_cert_gen_script(_CN, workdir="/tmp/fd-cert-abc123")
    # The QUOTED heredoc delimiter keeps ``$dir`` literal for openssl (not
    # expanded by the outer shell), and the absolute workdir is baked in so
    # the ``dir =`` macro resolves without shell expansion.
    assert "<<'FD_CNF'" in script
    assert "dir = /tmp/fd-cert-abc123" in script
    assert "$dir/index.txt" in script  # literal macro survives the heredoc
    assert "D=/tmp/fd-cert-abc123" in script


def test_gen_script_carries_cn_and_past_enddate():
    script = build_cert_gen_script(_CN, workdir="/tmp/fd-cert-x")
    assert f'/CN={_CN}' in script
    assert f"-enddate {_CERT_NOT_AFTER}" in script
    # The fixed validity window is unambiguously in the PAST (2020).
    assert _CERT_NOT_AFTER.startswith("2020")
    # Output is framed by the markers the parser reads.
    assert _CERT_CRT_MARKER in script
    assert _CERT_KEY_MARKER in script
    assert _CERT_END_MARKER in script


def test_gen_script_custom_window_honoured():
    script = build_cert_gen_script(
        _CN, workdir="/tmp/wd",
        not_before="20190101000000Z", not_after="20190601000000Z",
    )
    assert "-startdate 20190101000000Z" in script
    assert "-enddate 20190601000000Z" in script


# ---------------------------------------------------------------------------
# 4.3 — generation-output parsing (pure)
# ---------------------------------------------------------------------------


def test_between_markers_slices_and_strips():
    text = "===A===\n  payload  \n===B===\n"
    assert _between_markers(text, "===A===", "===B===") == "payload"
    assert _between_markers(text, "===X===", "===B===") == ""


def test_parse_cert_gen_output_ok():
    crt, key, not_after = _parse_cert_gen_output(_CERT_GEN_OUT, _CN)
    assert crt == _GEN_CRT
    assert key == _GEN_KEY
    assert not_after == _GEN_NOT_AFTER


def test_parse_cert_gen_output_missing_key_fails_closed():
    half = f"{_CERT_CRT_MARKER}\n{_GEN_CRT}\n{_CERT_KEY_MARKER}\n{_CERT_END_MARKER}\nnotAfter={_GEN_NOT_AFTER}\n"
    with pytest.raises(_AssemblyError, match="did not yield both"):
        _parse_cert_gen_output(half, _CN)


def test_parse_cert_gen_output_missing_notafter_fails_closed():
    no_end = f"{_CERT_CRT_MARKER}\n{_GEN_CRT}\n{_CERT_KEY_MARKER}\n{_GEN_KEY}\n{_CERT_END_MARKER}\n"
    with pytest.raises(_AssemblyError, match="no notAfter"):
        _parse_cert_gen_output(no_end, _CN)


# ---------------------------------------------------------------------------
# 4.2 / 4.3 / 4.4 — end-to-end through assemble_recovery_carrier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_backfills_sentinel_and_reports_payload(_kube):
    _wire_secret(_kube)
    before = time.time()
    receipt = await _assemble_secret(_kube)

    assert receipt["status"] == "success", receipt
    # 4.3 — the injection patch carries the RESOLVED base64, never the
    # sentinel (backfill happens in place before arm→inject).
    injected = _inject_patch_arg(_kube)
    assert injected is not None, "no injection patch was dispatched"
    by_path = {op["path"]: op["value"] for op in injected}
    assert by_path["/data/tls.crt"] == _GEN_CRT
    assert by_path["/data/tls.key"] == _GEN_KEY
    assert "<faultdrill:" not in json.dumps(injected)

    # 4.4 — the receipt carries the white-box verdict evidence.
    payloads = receipt["cert_payloads"]
    assert len(payloads) == 1
    assert payloads[0]["cn"] == _CN
    assert payloads[0]["not_after"] == _GEN_NOT_AFTER
    assert payloads[0]["crt_b64"] == _GEN_CRT

    # The generation ran BEFORE the arming exec (pre-arming resolution).
    labels = [s["step"] for s in receipt["steps"]]
    assert "cert_gen" in labels
    assert labels.index("cert_gen") < labels.index("arm")
    assert before <= time.time()


@pytest.mark.asyncio
async def test_assemble_capability_probe_fail_closed_no_inject(_kube):
    # 4.2 — an openssl-less image fails closed PRE-ARMING: no cert, no
    # inject, and the built stack is cleaned (honest failure).
    _wire_secret(_kube, exec_router=_cert_exec_router(cap="FD_NO_OPENSSL"))
    receipt = await _assemble_secret(_kube)

    assert receipt["status"] == "failed"
    assert "openssl-ca" in receipt["error"]
    assert "carrier_image" in receipt["error"]  # retryable hint
    assert _inject_patch_arg(_kube) is None, "must NOT inject on fail-closed"
    assert receipt["carrier"]["armed"] is False
    # Cleanup ran (the four-object stack was torn down).
    assert any(sub == "delete" for sub, _, _ in _kube.calls)


@pytest.mark.asyncio
async def test_assemble_capability_probe_no_enddate_fail_closed(_kube):
    # An openssl present but WITHOUT ``ca -enddate`` (the expiry control)
    # is equally unusable — the probe must reject it too.
    _wire_secret(_kube, exec_router=_cert_exec_router(cap="FD_NO_CA_ENDDATE"))
    receipt = await _assemble_secret(_kube)
    assert receipt["status"] == "failed"
    assert "openssl-ca" in receipt["error"]
    assert _inject_patch_arg(_kube) is None


@pytest.mark.asyncio
async def test_assemble_missing_cert_half_fails_closed(_kube):
    # 4.3 atomicity end-to-end: a generation that yields a cert but no key
    # is not injectable — abort, never inject a half payload.
    half = (
        f"{_CERT_CRT_MARKER}\n{_GEN_CRT}\n"
        f"{_CERT_KEY_MARKER}\n{_CERT_END_MARKER}\nnotAfter={_GEN_NOT_AFTER}\n"
    )
    _wire_secret(_kube, exec_router=_cert_exec_router(gen_out=half))
    receipt = await _assemble_secret(_kube)
    assert receipt["status"] == "failed"
    assert _inject_patch_arg(_kube) is None


@pytest.mark.asyncio
async def test_assemble_no_sentinel_patches_byte_identical(_kube):
    # 4.1 regression guard end-to-end: an ordinary Secret recipe (no
    # sentinel) never triggers the cert path and its patches are injected
    # verbatim; the receipt carries an empty cert_payloads list.
    plain_patches = [
        {"op": "replace", "path": "/data/tls.crt", "value": "PLAINCRT"},
    ]
    plain_restore = [
        {"op": "replace", "path": "/data/tls.crt", "value": "BASECRTB64"},
    ]
    plain_readback = {
        "metadata": {"name": "drill-tls-secret", "namespace": "default"},
        "data": {"tls.crt": "PLAINCRT", "tls.key": "BASEKEYB64"},
    }
    _kube.on("get", "secret", _seq(
        _R(0, json.dumps(_SECRET_BASELINE)),
        _R(0, json.dumps(plain_readback)),
    ))
    _kube.on("get", "pod", _R(0, json.dumps({"status": {"phase": "Running"}})))
    _kube.on_any("exec", _cert_exec_router())

    receipt = await _assemble_secret(
        _kube, patches=plain_patches, restore=plain_restore,
    )
    assert receipt["status"] == "success", receipt
    assert receipt["cert_payloads"] == []
    injected = _inject_patch_arg(_kube)
    assert injected == plain_patches
    # No capability probe / generation exec was dispatched.
    for _sub, v_args, _ in _kube.calls:
        script = _exec_script(v_args)
        assert "command -v openssl" not in script
        assert "openssl ca -batch" not in script
    assert "cert_gen" not in [s["step"] for s in receipt["steps"]]
