"""openspec faultdrill-cluster-native-recovery M1: assembler pins.

The acceptance baseline is the recovery-carrier standard's legislation
(``references/carrier/recovery-carrier.md`` §1 two-step Role
construction, §2 verb×resource derivation table, §3 three-step
real-token verification, §4 arming template, §7 payload tiers) plus the
task 1.6 pin list: derivation-table rows locked one by one, the
union-broadcast counterexample, 403/SSAR fail-closed with the four-way
cleanup, armed-before-inject as a dispatch ORDER, countdown from ARM,
the four-object shared name, artifact schema equivalence with the LLM
path's screener registration, and no orphaned stack on failure.

This is NEW safety-critical code (task 1.2): the pins hold the assembler
to the LEGISLATED behaviour, not to incidental implementation choices —
each test names the clause it enforces.

Everything kubectl-shaped routes through ONE patched seam
(``provider._kubectl`` — the assembler's ``_run`` reaches it by module
attribute, so the single patch point covers every call, the same
discipline as the restore/provider tests).
"""

from __future__ import annotations

import base64
import json
import re
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from chaos_agent.tools.guard import CommandResult

from chaos_agent.agent.providers.faultdrill.assembler import (
    ASSEMBLER_TOOL_NAME,
    _AssemblyError,
    assemble_recovery_carrier,
    assert_carrier_shape,
    build_body_landing_scripts,
    build_carrier_artifact,
    build_probe_get_script,
    build_restore_script,
    build_role_append_patch,
    build_role_create_v_args,
    build_run_v_args,
    build_ssar_script,
    canonical_kind,
    carrier_name,
    derive_role_rules,
    faultdrill_assemble_carrier,
    parse_receipt,
    render_role_commands,
    rest_url_for,
    select_carrier_image,
    ssar_verdict,
    verb_sets_uniform,
    verify_patches_landed,
    verify_restore_baseline,
)
from chaos_agent.agent.providers.faultdrill.provider import (
    FaultDrillProvider,
    _assembler_receipt_artifacts,
)
from chaos_agent.config.settings import settings


# ---------------------------------------------------------------------------
# Recipe fixtures (Service selector case — the #55 shape)
# ---------------------------------------------------------------------------

_BASELINE = {
    "metadata": {"name": "svc-x", "namespace": "cms-demo"},
    "spec": {"selector": {"app": "accounting"}},
}
_PATCHES = [{"op": "replace", "path": "/spec/selector/app", "value": "wrong"}]
_RESTORE = [{"op": "replace", "path": "/spec/selector/app", "value": "accounting"}]
_PATCHED = {
    "metadata": {"name": "svc-x", "namespace": "cms-demo"},
    "spec": {"selector": {"app": "wrong"}},
}


# ---------------------------------------------------------------------------
# Canned kubectl router (the single-seam patch discipline, plus a
# wildcard route: carrier names are salt-random, so exec calls cannot be
# keyed on the pod name the test does not know yet)
# ---------------------------------------------------------------------------


def _R(exit_code: int, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


class _Router:
    """Canned kubectl router keyed by ``(subcommand, first v_arg)``.

    ``(sub, "*")`` is the wildcard first-arg route; an EXACT key wins.
    A callable result receives ``(sub, v_args, stdin_data)`` so a key
    hit several times can dispense different responses (target GET:
    baseline read, then landing readback) and inspect the dispatched
    argv (exec scripts). Unrouted calls return ``_R(0, "{}")``.
    """

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
    """A callable result dispensing one canned response per call; the
    last repeats — for keys hit several times with DIFFERENT expected
    responses (the target GET fires twice: baseline precheck, then the
    post-injection landing readback)."""
    state = {"i": 0}

    def _dispense(sub, v_args, stdin_data):
        result = results[min(state["i"], len(results) - 1)]
        state["i"] += 1
        return result

    return _dispense


def _exec_script(v_args: list[str]) -> str:
    """The ``sh -c`` payload of one exec call — the assembler dispatches
    scripts as ONE argv element after ``--`` (``[pod, -n, ns, --, sh,
    -c, script]``, never a re-tokenized string)."""
    if "--" not in v_args:
        return ""
    tail = v_args[v_args.index("--") + 1:]  # [sh, -c, script]
    return tail[2] if len(tail) >= 3 else ""


def _echoed_ssar(script: str, allowed: bool = True, verb_drift: str | None = None):
    """A well-formed SSAR receipt: the apiserver echoes the request's
    ``resourceAttributes`` back (echo self-attestation — §3 step 3's
    precondition), plus the ``allowed`` verdict under test — riding
    ``status.allowed`` (SubjectAccessReviewStatus, the real API shape;
    R2 review Bug#1 pinned the assembler to read exactly here).

    The echo replicates the apiserver's SERIALIZATION contract, not the
    request bytes: ``ResourceAttributes`` fields are Go ``omitempty``
    strings, so an empty-string attribute is OMITTED from the echoed
    JSON (a core-group ``group:""`` comes back with NO group key). The
    naive request-mirror mock masked that shape for every prior run —
    78 unit tests green while a real cluster fail-closed every
    core-group target (#55 M3 retest live-fire)."""
    match = re.search(r"-d '(\{.*\})'", script)
    request = json.loads(match.group(1)) if match else {}
    attrs = dict((request.get("spec") or {}).get("resourceAttributes") or {})
    if verb_drift is not None:
        attrs["verb"] = verb_drift
    attrs = {k: v for k, v in attrs.items() if v != ""}  # omitempty
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": attrs},
        "status": {"allowed": allowed},
    }


def _ok_exec_router(arm_clock: dict | None = None, *, probe_code: str = "200",
                    ssar_allowed: bool = True, ssar_verb_drift: str | None = None):
    """Route every carrier exec by its script SHAPE (§3 GET probe / §3
    SSAR / §7 landing writes / §4 arm). ``arm_clock`` records the moment
    the arming exec is dispatched (the countdown's origin)."""

    def _route(sub, v_args, stdin_data):
        script = _exec_script(v_args)
        if "http_code" in script:
            return _R(0, probe_code)
        if "selfsubjectaccessreviews" in script:
            return _R(0, json.dumps(_echoed_ssar(
                script, allowed=ssar_allowed, verb_drift=ssar_verb_drift,
            )))
        if "echo armed" in script:
            if arm_clock is not None:
                arm_clock["t"] = time.time()
            return _R(0, "armed")
        return _R(0, "")  # §7 b64 landing writes

    return _route


@pytest.fixture(autouse=True)
def _carrier_settings(monkeypatch):
    """Pin the carrier vocabulary to a fixed set: the defaults carry four
    images and an operator environment (or a PRECEDING test — settings
    is a shared singleton) may have flipped discovery or the prefix."""
    monkeypatch.setattr(
        settings, "recovery_carrier_allowed_images",
        "busybox:1.36,curlimages/curl:8.8.0",
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


def _wire_happy(router: _Router, *, exec_router=None, patch_result=None) -> dict:
    """Canned happy-path wiring: baseline read → patched readback, a
    Running skeleton, script-shaped exec responses, injectable patch."""
    arm_clock: dict = {}
    router.on("get", "service", _seq(
        _R(0, json.dumps(_BASELINE)),
        _R(0, json.dumps(_PATCHED)),
    ))
    router.on("get", "pod", _R(0, json.dumps({"status": {"phase": "Running"}})))
    router.on_any("exec", exec_router or _ok_exec_router(arm_clock))
    if patch_result is not None:
        router.on("patch", "service", patch_result)
    return arm_clock


async def _assemble(
    router=None, *, patches=_PATCHES, restore=_RESTORE, duration=600,
    kind="Service", name="svc-x", ns="cms-demo", carrier_image="",
):
    return await assemble_recovery_carrier(
        target_kind=kind, target_name=name, target_namespace=ns,
        patches=patches, restore_patches=restore,
        duration_seconds=duration, kubeconfig="/kc", task_id="task-1",
        carrier_image=carrier_image,
    )


def _stack_name(router: _Router) -> str:
    """The four-object shared name — exact-match linking
    (``_attach_recovery_carrier_rbac``) and the cleanup chain both
    depend on SA/Role/RoleBinding/Pod being ONE name."""
    names: set[str] = set()
    for sub, v_args, _ in router.calls:
        if sub == "create" and v_args[0] in ("serviceaccount", "role", "rolebinding"):
            names.add(v_args[1])
        elif sub == "run":
            names.add(v_args[0])
    assert names, "no stack objects were created"
    assert len(names) == 1, (
        f"stack objects must share ONE name, got {sorted(names)}"
    )
    return names.pop()


def _index_of_arm(router: _Router) -> int:
    for i, (sub, v_args, _) in enumerate(router.calls):
        if sub == "exec" and "echo armed" in _exec_script(v_args):
            return i
    raise AssertionError("no arming exec was dispatched")


def _index_of_inject(router: _Router, kind: str = "service") -> int:
    for i, (sub, v_args, _) in enumerate(router.calls):
        if sub == "patch" and v_args and v_args[0] == kind:
            return i
    raise AssertionError("no injection patch was dispatched")


# ---------------------------------------------------------------------------
# §2 derivation table — every row pinned (task 1.2: NEW safety-critical
# code; the table is the acceptance baseline, not the implementation)
# ---------------------------------------------------------------------------


def test_derivation_rest_verb_rows():
    # §2 rows: PATCH→patch, PUT→update, DELETE→delete, POST→create
    for method, verb in [
        ("PATCH", "patch"), ("PUT", "update"),
        ("DELETE", "delete"), ("POST", "create"),
    ]:
        rules = derive_role_rules([{"kind": "Service", "methods": [method]}])
        assert rules == [{
            "apiGroups": [""], "resources": ["services"],
            "verbs": sorted(["get", verb]),
        }], method


def test_derivation_probe_get_rides_the_restore_resource():
    # §2 row "验权探测 → 目标资源的 get": the real-token GET probe shares
    # the restore resource — every rule carries get WITHOUT a separate
    # probe entry.
    rules = derive_role_rules([{"kind": "Deployment", "methods": ["PATCH"]}])
    assert rules[0]["verbs"] == ["get", "patch"]


def test_derivation_group_and_plural_rows():
    # §2's kind → (group, plural) column, one by one
    for kind, (group, plural) in {
        "Deployment": ("apps", "deployments"),
        "StatefulSet": ("apps", "statefulsets"),
        "DaemonSet": ("apps", "daemonsets"),
        "ReplicaSet": ("apps", "replicasets"),
        "Service": ("", "services"),
        "ConfigMap": ("", "configmaps"),
        "Secret": ("", "secrets"),
        "PersistentVolumeClaim": ("", "persistentvolumeclaims"),
        "ResourceQuota": ("", "resourcequotas"),
        "Pod": ("", "pods"),
        "Ingress": ("networking.k8s.io", "ingresses"),
    }.items():
        rules = derive_role_rules([{"kind": kind, "methods": ["PATCH"]}])
        assert rules[0]["apiGroups"] == [group], kind
        assert rules[0]["resources"] == [plural], kind


def test_derivation_wildcard_free_by_construction():
    # §2 form-agnostic master rule: a Role rendered from the closed
    # vocabulary can never grow a wildcard.
    rules = derive_role_rules([
        {"kind": "Deployment", "methods": ["PATCH", "DELETE"]},
        {"kind": "ConfigMap", "methods": ["PUT"]},
    ])
    for rule in rules:
        assert "*" not in rule["verbs"]
        assert "*" not in rule["resources"]
        assert "*" not in rule["apiGroups"]


def test_derivation_one_rule_per_family_stable_order():
    rules = derive_role_rules([
        {"kind": "ConfigMap", "methods": ["PUT"]},
        {"kind": "Deployment", "methods": ["PATCH"]},
    ])
    # stable order BY PLURAL (alphabetical), regardless of input order
    assert [r["resources"] for r in rules] == [["configmaps"], ["deployments"]]


def test_derivation_same_family_verbs_union():
    # alias + canonical naming hit ONE family slot: verbs union there
    rules = derive_role_rules([
        {"kind": "Service", "methods": ["PATCH"]},
        {"kind": "svc", "methods": ["DELETE"]},
    ])
    assert rules == [{
        "apiGroups": [""], "resources": ["services"],
        "verbs": ["delete", "get", "patch"],
    }]


def test_derivation_fail_closed():
    with pytest.raises(ValueError, match="no REST mapping"):
        derive_role_rules([{"kind": "Widget", "methods": ["PATCH"]}])
    with pytest.raises(ValueError, match="no RBAC verb mapping"):
        derive_role_rules([{"kind": "Service", "methods": ["HEAD"]}])
    with pytest.raises(ValueError, match="no REST methods"):
        derive_role_rules([{"kind": "Service", "methods": []}])


# ---------------------------------------------------------------------------
# §1 two-step Role construction — the union-broadcast counterexample is
# the legislation's own motivation and MUST be pinned
# ---------------------------------------------------------------------------


def test_uniform_set_renders_one_create_command():
    rules = derive_role_rules([
        {"kind": "Deployment", "methods": ["PATCH"]},
        {"kind": "ConfigMap", "methods": ["PATCH"]},
    ])
    plan = render_role_commands("drill-rc-x", "ns1", rules)
    assert len(plan) == 1
    sub, v_args, stdin = plan[0]
    assert sub == "create" and stdin == ""
    assert v_args[0] == "role" and v_args[1] == "drill-rc-x"
    assert v_args[v_args.index("-n") + 1] == "ns1"
    # the shared verb set travels in ONE --verb flag
    assert v_args[v_args.index("--verb") + 1] == "get,patch"
    # per-group --resource lists (kubectl broadcasts the shared verbs
    # into each — exact, not over-granting)
    joined = " ".join(v_args)
    assert "apps/deployments" in joined and "configmaps" in joined


def test_non_uniform_two_step_union_broadcast_counterexample():
    # §1's measured counterexample: delete×resourcequotas +
    # patch×deployments. A SINGLE create role would broadcast the verb
    # UNION {delete,get,patch} into BOTH rules — delete on deployments
    # is an over-grant. Two-step: create the FIRST family (stable
    # plural order: deployments) ONLY, then json-patch append the
    # second as an INDEPENDENT rule.
    rules = derive_role_rules([
        {"kind": "ResourceQuota", "methods": ["DELETE"]},
        {"kind": "Deployment", "methods": ["PATCH"]},
    ])
    assert not verb_sets_uniform(rules)
    plan = render_role_commands("drill-rc-x", "ns1", rules)
    assert [p[0] for p in plan] == ["create", "patch"]

    sub1, v1, _ = plan[0]
    assert sub1 == "create"
    # the create step carries ONE family and its OWN verbs — NOT the
    # union (delete riding the deployments create would be the
    # over-grant the two-step form exists to prevent)
    assert v1[v1.index("--verb") + 1] == "get,patch"
    assert v1[v1.index("--resource") + 1] == "apps/deployments"

    sub2, v2, _ = plan[1]
    assert sub2 == "patch" and v2[0] == "role" and v2[1] == "drill-rc-x"
    assert v2[v2.index("-n") + 1] == "ns1"
    assert "--type=json" in v2
    ops = json.loads(v2[v2.index("-p") + 1])
    assert ops == [{
        "op": "add", "path": "/rules/-",
        "value": {"apiGroups": [""], "resources": ["resourcequotas"],
                  "verbs": ["delete", "get"]},
    }], "the appended family must be an INDEPENDENT rule"


def test_create_v_args_refuses_non_uniform():
    rules = derive_role_rules([
        {"kind": "ResourceQuota", "methods": ["DELETE"]},
        {"kind": "Deployment", "methods": ["PATCH"]},
    ])
    with pytest.raises(ValueError, match="two-step"):
        build_role_create_v_args("drill-rc-x", "ns1", rules)


def test_append_patch_skip_first_and_empty_guard():
    rules = derive_role_rules([
        {"kind": "ResourceQuota", "methods": ["DELETE"]},
        {"kind": "Deployment", "methods": ["PATCH"]},
    ])
    # stable plural order: [deployments, resourcequotas]; skipping the
    # family the create rendered leaves the quota family to append
    payload = build_role_append_patch(rules, skip_first=1)
    assert json.loads(payload) == [{
        "op": "add", "path": "/rules/-",
        "value": {"apiGroups": [""], "resources": ["resourcequotas"],
                  "verbs": ["delete", "get"]},
    }]
    with pytest.raises(ValueError, match="no rules left"):
        build_role_append_patch(rules, skip_first=2)


def test_verb_sets_uniform_boundaries():
    assert verb_sets_uniform([]) is True
    assert verb_sets_uniform([{"verbs": ["get", "patch"]}]) is True
    # order-insensitive signature comparison
    assert verb_sets_uniform([
        {"verbs": ["get", "patch"]}, {"verbs": ["patch", "get"]},
    ]) is True
    assert verb_sets_uniform([
        {"verbs": ["get", "delete"]}, {"verbs": ["get", "patch"]},
    ]) is False


# ---------------------------------------------------------------------------
# Kind map / REST URL shapes
# ---------------------------------------------------------------------------


def test_canonical_kind_alias_normalisation():
    assert canonical_kind("Deployment") == "deployment"
    assert canonical_kind("deployments") == "deployment"
    assert canonical_kind("svc") == "service"
    assert canonical_kind("ConfigMap") == "configmap"
    assert canonical_kind("PersistentVolumeClaim") == "persistentvolumeclaim"
    with pytest.raises(ValueError):
        canonical_kind("Widget")


def test_rest_url_shapes():
    assert rest_url_for("Service", "s1", "ns1") == (
        "https://kubernetes.default.svc/api/v1/namespaces/ns1/services/s1"
    )
    assert rest_url_for("Deployment", "d1", "ns1") == (
        "https://kubernetes.default.svc/apis/apps/v1"
        "/namespaces/ns1/deployments/d1"
    )
    assert rest_url_for("Ingress", "i1", "ns1") == (
        "https://kubernetes.default.svc/apis/networking.k8s.io/v1"
        "/namespaces/ns1/ingresses/i1"
    )
    # cluster-scoped kinds map for URL shape; the ASSEMBLER rejects them
    assert rest_url_for("Node", "n1", "ns1") == (
        "https://kubernetes.default.svc/api/v1/nodes/n1"
    )


# ---------------------------------------------------------------------------
# Carrier skeleton shape + the §1 four-object shared name
# ---------------------------------------------------------------------------


def test_carrier_name_shape_and_determinism():
    name = carrier_name("task-1", {"kind": "service", "name": "svc-x",
                                   "namespace": "cms-demo"})
    assert name.startswith("drill-rc-")
    assert len(name) == len("drill-rc-") + 8
    # same inputs → same name (salt-less determinism); the assembly
    # itself salts with time so a retry builds a FRESH stack
    assert carrier_name("task-1", {"kind": "service", "name": "svc-x",
                                   "namespace": "cms-demo"}) == name
    assert carrier_name(
        "task-1", {"kind": "service", "name": "svc-x",
                   "namespace": "cms-demo"}, salt="other",
    ) != name


def test_run_v_args_fixed_template_and_shared_name():
    v = build_run_v_args("drill-rc-x", "ns1", "busybox:1.36", 2400)
    assert v[0] == "drill-rc-x"
    assert v[v.index("-n") + 1] == "ns1"
    assert v[v.index("--image") + 1] == "busybox:1.36"
    assert v[v.index("--restart") + 1] == "Never"
    assert v[-2:] == ["sleep", "2400"]
    overrides = json.loads(v[v.index("--overrides") + 1])
    assert overrides["spec"] == {"serviceAccountName": "drill-rc-x"}


def test_run_v_args_tolerations_via_overrides_only():
    # §1: tolerations ride --overrides (one of the ONLY two keys the
    # shape admits there), never a bare flag the whitelist rejects
    v = build_run_v_args(
        "drill-rc-x", "ns1", "busybox:1.36", 100,
        tolerations=[{"key": "dedicated", "operator": "Equal",
                      "value": "drill", "effect": "NoSchedule"}],
    )
    overrides = json.loads(v[v.index("--overrides") + 1])
    assert overrides["spec"]["tolerations"][0]["key"] == "dedicated"


def test_assert_carrier_shape_self_check_passes():
    # task 1.3: the constructed run satisfies the SAME library predicate
    # the guard dispatches on (classifier._is_recovery_carrier_run) —
    # asserted BEFORE dispatch, a construction guarantee.
    v = build_run_v_args("drill-rc-x", "ns1", "busybox:1.36", 2400)
    assert_carrier_shape(v, "drill-rc-x")


def test_assert_carrier_shape_catches_regression():
    # a malformed construction (restart policy dropped) must FAIL the
    # self-check — the assembler refuses to dispatch it
    v = build_run_v_args("drill-rc-x", "ns1", "busybox:1.36", 2400)
    v.pop(v.index("--restart") + 1)
    v.pop(v.index("--restart"))
    with pytest.raises(ValueError, match="self-check failed"):
        assert_carrier_shape(v, "drill-rc-x")


# ---------------------------------------------------------------------------
# Image selection (configured ∪ discovered, same source as the classifier)
# ---------------------------------------------------------------------------


def test_image_curl_name_preferred(monkeypatch):
    # the arming knowledge hard-requires sh+sleep+curl — a curl-capable
    # image is preferred within each pool
    monkeypatch.setattr(settings, "recovery_carrier_allowed_images",
                        "busybox:1.36,curlimages/curl:8.8.0")
    monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")
    assert select_carrier_image() == "curlimages/curl:8.8.0"


def test_image_discovered_pool_first(monkeypatch):
    # discovered (node-cached, no pull) beats configured within a tier
    monkeypatch.setattr(settings, "recovery_carrier_allowed_images",
                        "curlimages/curl:8.8.0")
    monkeypatch.setattr(settings, "recovery_carrier_discovered_images",
                        "registry.local/curl:9,busybox:1.36")
    assert select_carrier_image() == "registry.local/curl:9"


def test_image_discovered_pool_is_local(monkeypatch):
    # #55 M3 live-run regression anchor: a curl-capable NAME in the
    # configured pool may not pull the choice out of a non-empty
    # discovered pool — the sole discovered image (node-cached,
    # empirically curl-capable despite a curl-less name) is the carrier;
    # the configured curlimages image is unpullable in the VPC cluster
    # and must never win over the discovery
    monkeypatch.setattr(settings, "recovery_carrier_allowed_images",
                        "busybox:1.36,curlimages/curl:8.8.0")
    monkeypatch.setattr(settings, "recovery_carrier_discovered_images",
                        "registry.local/terway:v1.12.1")
    assert select_carrier_image() == "registry.local/terway:v1.12.1"


def test_image_empty_union_refuses(monkeypatch):
    # fail-closed: no allowed image → no carrier
    monkeypatch.setattr(settings, "recovery_carrier_allowed_images", "")
    monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")
    with pytest.raises(ValueError, match="allowlist is empty"):
        select_carrier_image()


def _run_image(router) -> str:
    """The image the dispatched ``kubectl run`` actually carries."""
    for sub, v_args, _ in router.calls:
        if sub == "run" and "--image" in v_args:
            return v_args[v_args.index("--image") + 1]
    raise AssertionError("no kubectl run was dispatched")


async def test_carrier_image_override_honored(_kube, monkeypatch):
    # #55 M3 root-cause B pin: the planning-phase image decision MUST
    # have a channel into the assembler. The LLM had decided terway
    # (probe candidates ∩ evidence archive; node-cached 37/37) while
    # the standalone selector — even pool-local — would have picked the
    # alphabetically-first discovered image (an npd). The override
    # carries the decision the selector cannot see; the selector is a
    # fallback, not the decision-maker.
    monkeypatch.setattr(settings, "recovery_carrier_allowed_images",
                        "busybox:1.36,curlimages/curl:8.8.0")
    monkeypatch.setattr(settings, "recovery_carrier_discovered_images",
                        "ack-node-problem-detector:v2,registry.local/terway:v1.12.1")
    _wire_happy(_kube)
    receipt = await _assemble(carrier_image="registry.local/terway:v1.12.1")
    assert receipt["status"] == "success"
    assert _run_image(_kube) == "registry.local/terway:v1.12.1"


async def test_carrier_image_outside_union_fails_before_the_stack(
    _kube, monkeypatch,
):
    # The override is a CHOICE within the allowlist, never a way AROUND
    # it: an image outside configured ∪ discovered is fail-closed
    # BEFORE anything is built (same shape as the empty-allowlist pin).
    monkeypatch.setattr(settings, "recovery_carrier_allowed_images",
                        "busybox:1.36")
    monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")
    with pytest.raises(_AssemblyError, match="outside the recovery-carrier"):
        await _assemble(carrier_image="nginx:1.27")
    assert not any(s in ("create", "run", "patch") for s, _, _ in _kube.calls)


async def test_carrier_image_empty_falls_back_to_selector(_kube):
    # Omitted → the pool-local selector decides alone (the tool stays
    # callable in one-arg form; the override is additive, not mandatory).
    _wire_happy(_kube)
    receipt = await _assemble()
    assert receipt["status"] == "success"
    # _carrier_settings pins configured to busybox:1.36,curlimages/curl:8.8.0
    # with no discovery → the curl-named configured image wins the pool.
    assert _run_image(_kube) == "curlimages/curl:8.8.0"


# ---------------------------------------------------------------------------
# §3/§4/§7 payload script shapes
# ---------------------------------------------------------------------------


def test_probe_get_script_shape():
    s = build_probe_get_script(
        "https://kubernetes.default.svc/api/v1/namespaces/ns1/services/svc-x"
    )
    # §3: the real-token read-only GET probe — -w %{http_code} binary
    # verdict, bearer SA token, NO impersonated can-i
    assert '-w "%{http_code}"' in s
    assert "Authorization: Bearer $T" in s
    assert "can-i" not in s
    assert "--as" not in s
    assert "-X POST" not in s and "-X PATCH" not in s
    assert s.startswith(
        "T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)"
    )


def test_ssar_script_field_discipline():
    # §3 step 2 + the #51-R field discipline: ``group`` (NOT apiGroup),
    # ``spec`` an OBJECT wrapping resourceAttributes, per-verb POST
    s = build_ssar_script("patch", "", "services", "ns1")
    assert "apiGroup" not in s
    body = re.search(r"-d '(\{.*\})'", s).group(1)
    request = json.loads(body)
    assert request["spec"] == {"resourceAttributes": {
        "namespace": "ns1", "verb": "patch", "group": "", "resource": "services",
    }}
    assert (
        "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews" in s
    )
    assert "-X POST" in s
    assert "Authorization: Bearer $T" in s


def test_restore_script_compact_variable_form():
    # §4 approved compact form: C/T/U assigned INSIDE the payload,
    # json-patch content type, restore.log NEVER /dev/null, sh -c echo
    body = json.dumps([{"op": "remove", "path": "/spec/paused"}])
    s = build_restore_script(600, "https://u", body)
    assert "sleep 600" in s
    assert "C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt" in s
    assert "T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" in s
    assert "U=https://u" in s
    assert "-X PATCH" in s
    assert "application/json-patch+json" in s
    assert " >/tmp/restore.log 2>&1 & echo armed" in s
    assert "/dev/null" not in s


def test_restore_script_body_file_variant():
    s = build_restore_script(60, "https://u", "ignored",
                             body_file="/tmp/restore.json")
    assert "-d @/tmp/restore.json" in s


def test_body_landing_scripts_b64_chunks():
    # §7 oversized tier: b64 chunks (no quote or $ expansion points),
    # first write truncates (>) the rest append (>>), decode + cleanup
    body = json.dumps([{"op": "replace", "path": "/p", "value": "v" * 900}])
    scripts = build_body_landing_scripts(body, chunk_size=720)
    assert scripts[0].startswith("echo -n ") and " > /tmp/restore.b64" in scripts[0]
    assert all(" >> /tmp/restore.b64" in s for s in scripts[1:-1])
    assert scripts[-1] == (
        "base64 -d /tmp/restore.b64 > /tmp/restore.json && rm -f /tmp/restore.b64"
    )
    joined = "".join(
        re.search(r"echo -n (\S+)", s).group(1) for s in scripts[:-1]
    )
    assert joined == base64.b64encode(body.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# §3 step 3 — SSAR echo self-attestation, three states
# ---------------------------------------------------------------------------


def _ssar_ok(verb="patch", group="", resource="services", ns="ns1",
             allowed=True, **echo_overrides):
    # Echo shape = the apiserver's serialization contract: empty-string
    # attributes are OMITTED (Go omitempty), so the default core-group
    # ``group=""`` echoes back with no group key at all.
    attrs = {"namespace": ns, "verb": verb, "group": group,
             "resource": resource}
    attrs.update(echo_overrides)
    attrs = {k: v for k, v in attrs.items() if v != ""}  # omitempty
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": attrs},
        "status": {"allowed": allowed},
    }


def test_ssar_verdict_allowed():
    verdict, detail = ssar_verdict(_ssar_ok(), "patch", "", "services", "ns1")
    assert verdict == "allowed" and detail == ""


def test_ssar_verdict_denied():
    verdict, detail = ssar_verdict(
        _ssar_ok(allowed=False), "patch", "", "services", "ns1",
    )
    assert verdict == "denied"


def test_ssar_verdict_echo_drift_is_form_error_before_permission():
    # #51-R: the echo check PRECEDES the permission judgement — an
    # ``allowed:true`` over a drifted question is meaningless
    verdict, detail = ssar_verdict(
        _ssar_ok(verb="escalate"), "patch", "", "services", "ns1",
    )
    assert verdict == "form_error"
    assert "verb" in detail


def test_ssar_verdict_missing_echo_is_form_error():
    verdict, _ = ssar_verdict(
        {"status": {"allowed": True}}, "patch", "", "services", "ns1",
    )
    assert verdict == "form_error"


def test_ssar_verdict_allowed_absent_is_form_error():
    response = _ssar_ok()
    del response["status"]
    verdict, _ = ssar_verdict(response, "patch", "", "services", "ns1")
    assert verdict == "form_error"


def test_ssar_verdict_toplevel_allowed_is_form_error():
    # R2 review Bug#1 pin: the apiserver's verdict rides ``status.allowed``
    # (SubjectAccessReviewStatus — "Allowed is required"); a top-level
    # ``allowed`` (the pre-fix read site, which made every real-cluster
    # SSAR a form_error and the assembler 100% fail-closed) must NEVER
    # be read as a verdict.
    response = _ssar_ok()
    response["allowed"] = True
    del response["status"]
    verdict, detail = ssar_verdict(response, "patch", "", "services", "ns1")
    assert verdict == "form_error"
    assert "allowed field absent" in detail


def test_ssar_core_group_omitempty_omission_is_allowed():
    # #55 M3 retest live-fire regression anchor: the apiserver omits an
    # empty-string attribute from the echoed resourceAttributes (Go
    # ``omitempty``) — a core-group request (``"group": ""``) comes back
    # with NO group key, and that is the CONTRACT, not drift. The old
    # byte-exact comparison fail-closed every core-group target on a
    # real cluster while the request-mirror mocks kept unit tests green.
    ok, _ = ssar_verdict(
        {"status": {"allowed": True},
         "spec": {"resourceAttributes": {
             "namespace": "ns1", "verb": "patch", "resource": "services",
         }}},  # no group key — the real apiserver echo shape
        "patch", "", "services", "ns1",
    )
    assert ok == "allowed"


def test_ssar_null_group_echo_is_the_omitted_key_family():
    # JSON null and a missing key are one equivalence family under the
    # omitempty contract: the apiserver only ever emits the omission,
    # but a null normalizes to the same string zero-value.
    ok, _ = ssar_verdict(_ssar_ok(group=None), "patch", "", "services", "ns1")
    assert ok == "allowed"


def test_ssar_nonempty_group_omission_still_fails_closed():
    # The normalization is one-way: an omitted key normalizes to "", so
    # a NON-empty expectation ("apps") can never be satisfied by it — a
    # genuinely dropped non-empty field still fails closed.
    verdict, detail = ssar_verdict(
        {"status": {"allowed": True},
         "spec": {"resourceAttributes": {
             "namespace": "ns1", "verb": "patch",
             "resource": "deployments",
         }}},  # group key dropped, but "apps" was asked
        "patch", "apps", "deployments", "ns1",
    )
    assert verdict == "form_error"
    assert "group" in detail


# ---------------------------------------------------------------------------
# Recipe verification — baseline precheck (guard against a timer that
# would MUTATE the baseline) + landing readback
# ---------------------------------------------------------------------------


def test_baseline_precheck_replace_matching_baseline_is_clean():
    doc = {"spec": {"paused": False}}
    assert verify_restore_baseline(
        doc, [{"op": "replace", "path": "/spec/paused", "value": False}],
    ) == []


def test_baseline_precheck_stale_replace_value_violates():
    doc = {"spec": {"paused": False}}
    violations = verify_restore_baseline(
        doc, [{"op": "replace", "path": "/spec/paused", "value": True}],
    )
    assert violations and "mutate" in violations[0]


def test_baseline_precheck_remove_of_existing_path_violates():
    doc = {"spec": {"paused": False}}
    violations = verify_restore_baseline(
        doc, [{"op": "remove", "path": "/spec/paused"}],
    )
    assert violations and "baseline" in violations[0]


def test_baseline_precheck_add_colliding_path_violates():
    doc = {"spec": {"paused": False}}
    assert verify_restore_baseline(
        doc, [{"op": "add", "path": "/spec/paused", "value": True}],
    )


def test_baseline_precheck_unresolvable_path_conservative_keep():
    # a path absent from the live object cannot be checked — skipped,
    # the same orientation as the reconciler's readback guards
    assert verify_restore_baseline(
        {"spec": {}}, [{"op": "replace", "path": "/spec/absent", "value": 1}],
    ) == []


def test_baseline_precheck_array_index_resolution():
    doc = {"spec": {"containers": [{"name": "a"}, {"name": "b"}]}}
    assert verify_restore_baseline(doc, [
        {"op": "replace", "path": "/spec/containers/1/name", "value": "b"},
    ]) == []
    assert verify_restore_baseline(doc, [
        {"op": "replace", "path": "/spec/containers/1/name", "value": "zzz"},
    ])


def test_landing_readback_misses():
    doc = {"spec": {"paused": True}}
    assert verify_patches_landed(
        doc, [{"op": "add", "path": "/spec/paused", "value": True}],
    ) == []
    assert verify_patches_landed(
        doc, [{"op": "replace", "path": "/spec/paused", "value": False}],
    )
    # a remove is verified by absence
    assert verify_patches_landed(
        {"spec": {}}, [{"op": "remove", "path": "/spec/paused"}],
    ) == []
    assert verify_patches_landed(
        doc, [{"op": "remove", "path": "/spec/paused"}],
    )


# ---------------------------------------------------------------------------
# Artifact schema equivalence (task 1.5: the receipt-transported artifact
# is schema-equivalent to the LLM path's screener registration)
# ---------------------------------------------------------------------------


def test_artifact_schema_equivalent_to_screener_registration():
    art = build_carrier_artifact(
        name="drill-rc-x", namespace="ns1", task_id="t1",
        rules=[{"apiGroups": [""], "resources": ["services"],
                "verbs": ["get", "patch"]}],
        duration_seconds=600, deadline_epoch=1234.5,
        recovery_handle={"kind": "recovery_carrier"},
    )
    # the screener literal's contract fields (tool_screener L1395-1449),
    # plus the armed stamp (_mark_bounded_host_recovery semantics) the
    # LLM path adds at arming time — here pre-applied by construction
    for field in (
        "artifact_id", "type", "kind", "status", "task_id", "name",
        "namespace", "operation_family", "created_tool_call_id",
        "rbac_family", "cleanup", "recovery_timeout_seconds",
        "recovery_deadline_epoch", "host_exec_tool_call_id",
        "host_exec_seen_ids", "recovery_handle",
    ):
        assert field in art, field
    assert art["artifact_id"] == "recovery_carrier:ns1/drill-rc-x"
    assert art["type"] == "recovery_carrier"
    assert art["kind"] == "pod"
    assert art["status"] == "recovery_armed"
    assert art["operation_family"] == "recovery_carrier"
    assert art["recovery_timeout_seconds"] == 600
    assert art["recovery_deadline_epoch"] == 1234.5
    # id provenance is left for the receipt branch — only the ToolMessage
    # side knows the tool_call_id
    assert art["created_tool_call_id"] == ""
    assert art["host_exec_tool_call_id"] == ""
    assert art["host_exec_seen_ids"] == []
    # four-way delete, reverse-dependency order (pod → binding → role → sa)
    assert [c["v_args"].split()[0] for c in art["cleanup"]] == [
        "pod", "rolebinding", "role", "serviceaccount",
    ]
    assert all(
        c["tool"] == "kubectl" and c["subcommand"] == "delete"
        and "--ignore-not-found" in c["v_args"] and "-n ns1" in c["v_args"]
        for c in art["cleanup"]
    )
    # rbac_family members carry the shared name; the role carries the
    # derived verbs (the same member shape execution_artifacts attaches)
    fam = {m["kind"]: m for m in art["rbac_family"]}
    assert set(fam) == {"serviceaccount", "role", "rolebinding"}
    assert all(m["name"] == "drill-rc-x" and m["namespace"] == "ns1"
               for m in fam.values())
    assert fam["role"]["verbs"] == ["get", "patch"]


# ---------------------------------------------------------------------------
# Assembly core — the dispatch-sequence pins (task 1.6)
# ---------------------------------------------------------------------------


async def test_assemble_success_full_chain(_kube):
    arm_clock = _wire_happy(_kube)
    receipt = await _assemble()

    assert receipt["status"] == "success"
    assert receipt["error"] == ""
    carrier = receipt["carrier"]
    assert carrier["armed"] is True
    assert carrier["landing_verified"] is True
    assert carrier["recovery_timeout_seconds"] == 600
    assert carrier["name"].startswith("drill-rc-")

    # step trail: every legislated stage executed and ok
    labels = [s["step"] for s in receipt["steps"]]
    for expected in (
        "baseline_precheck", "create_serviceaccount", "role_create",
        "create_rolebinding", "run_carrier", "wait_running", "probe_get",
        "ssar_patch", "arm", "inject_patch", "landing_readback",
    ):
        assert expected in labels, labels

    # the four objects share ONE name
    name = _stack_name(_kube)
    assert name == carrier["name"]

    # RBAC was derived from the SAME recipe (patch on the service): the
    # create role carries get+patch on services
    create_calls = [v for s, v, _ in _kube.calls if s == "create"]
    role_create = next(v for v in create_calls if v[0] == "role")
    assert role_create[1] == name
    assert role_create[role_create.index("--verb") + 1] == "get,patch"
    assert "services" in role_create

    # the skeleton outlives the window (window + 1800s forensics buffer)
    run_v = next(v for s, v, _ in _kube.calls if s == "run")
    assert run_v[-2:] == ["sleep", str(600 + 1800)]

    # deadline counts from the ARM dispatch, not test start
    assert receipt["carrier"]["recovery_deadline_epoch"] == pytest.approx(
        arm_clock["t"] + 600, abs=5,
    )
    assert receipt["artifact"]["recovery_deadline_epoch"] == (
        receipt["carrier"]["recovery_deadline_epoch"]
    )

    # recovery handle carries the whole convergence recipe
    handle = receipt["recovery_handle"]
    assert handle["kind"] == "recovery_carrier"
    assert handle["value"] == f"cms-demo/{name}"
    assert handle["target_ref"] == {
        "kind": "service", "name": "svc-x", "namespace": "cms-demo",
    }
    assert handle["patches"] == _PATCHES
    assert handle["restore_patches"] == _RESTORE
    assert handle["duration_seconds"] == 600


async def test_armed_before_inject_hard_order(_kube):
    # spec "armed-before-inject 硬序与窗口完整性": the injection patch
    # may only land AFTER the arming exec echoed armed — pinned as a
    # dispatch-ORDER property, the strongest form available to a test
    _wire_happy(_kube)
    await _assemble()

    arm_idx = _index_of_arm(_kube)
    inject_idx = _index_of_inject(_kube)
    assert arm_idx < inject_idx
    # and NOTHING touched the target before arming
    for i, (sub, v_args, _) in enumerate(_kube.calls[:arm_idx]):
        assert not (sub == "patch" and v_args and v_args[0] == "service"), (
            f"a target patch was dispatched before arming (call #{i})"
        )
    # verification order inside the arming half: probe BEFORE SSAR
    # BEFORE arm (all indices in the SAME exec-scripts domain)
    scripts = [_exec_script(v) for s, v, _ in _kube.calls if s == "exec"]
    probe_idx = next(i for i, s in enumerate(scripts) if "http_code" in s)
    ssar_idx = next(
        i for i, s in enumerate(scripts) if "selfsubjectaccessreviews" in s
    )
    arm_in_scripts = next(i for i, s in enumerate(scripts) if "echo armed" in s)
    assert probe_idx < ssar_idx < arm_in_scripts


async def test_probe_403_fails_closed_and_cleans_the_stack(_kube):
    # spec "装配器 fail-closed 自检": verification 403 → abort, clean the
    # built stack, honest failure — and NEVER patch the target
    _wire_happy(_kube, exec_router=_ok_exec_router(probe_code="403"))
    receipt = await _assemble()

    assert receipt["status"] == "failed"
    assert "403" in receipt["error"]
    assert receipt["carrier"]["armed"] is False

    # the four-way delete, reverse-dependency order, shared name
    name = _stack_name(_kube)
    deletes = [v for s, v, _ in _kube.calls if s == "delete"]
    assert [v[0] for v in deletes] == [
        "pod", "rolebinding", "role", "serviceaccount",
    ]
    assert all(v[1] == name for v in deletes)
    assert all("--ignore-not-found" in v for v in deletes)
    # #55 M3 retest: the pod delete must be force+grace0 — a Pod's
    # default 30s termination grace outlives the 30s command timeout, so
    # a graceful delete reports a bogus timeout failure while the delete
    # was in fact accepted. RBAC members carry no grace concept.
    pod_delete = deletes[0]
    assert "--force" in pod_delete and "--grace-period=0" in pod_delete
    assert not any(
        "--force" in v for v in deletes[1:]
    )
    # nothing was injected, nothing armed
    assert not any(s == "patch" for s, _, _ in _kube.calls)
    assert not any(
        s == "exec" and "echo armed" in _exec_script(v)
        for s, v, _ in _kube.calls
    )


async def test_ssar_denied_fails_closed_and_cleans(_kube):
    # grant absent (allowed:false) on the write verb → abort before arming
    _wire_happy(_kube, exec_router=_ok_exec_router(ssar_allowed=False))
    receipt = await _assemble()

    assert receipt["status"] == "failed"
    assert "denied" in receipt["error"]
    assert receipt["carrier"]["armed"] is False
    deletes = [v for s, v, _ in _kube.calls if s == "delete"]
    assert [v[0] for v in deletes] == [
        "pod", "rolebinding", "role", "serviceaccount",
    ]
    assert not any(s == "patch" for s, _, _ in _kube.calls)


async def test_ssar_echo_drift_fails_closed(_kube):
    # #51-R field drift voids the receipt: a drifted echo is form_error
    # even when "allowed" — the question asked was not the question
    _wire_happy(_kube, exec_router=_ok_exec_router(ssar_verb_drift="escalate"))
    receipt = await _assemble()

    assert receipt["status"] == "failed"
    assert "form_error" in receipt["error"]
    assert not any(s == "patch" for s, _, _ in _kube.calls)


async def test_baseline_inconsistency_fails_before_building_anything(_kube):
    # a restore value that no longer matches the live baseline would
    # make the timer MUTATE the target — refuse BEFORE the stack is
    # built (an _AssemblyError raised pre-stack: nothing exists to
    # clean, the receipt layer converts it to a failed receipt)
    _wire_happy(_kube)
    with pytest.raises(_AssemblyError, match="baseline-consistent"):
        await _assemble(restore=[
            {"op": "replace", "path": "/spec/selector/app", "value": "stale"},
        ])

    subs = [s for s, _, _ in _kube.calls]
    assert set(subs) <= {"get"}, subs  # the baseline read only
    assert not any(s == "delete" for s, _, _ in _kube.calls)


async def test_terminal_pod_phase_fails_closed_and_cleans(_kube):
    _wire_happy(_kube)
    _kube.on("get", "pod", _R(0, json.dumps({"status": {"phase": "Failed"}})))
    receipt = await _assemble()

    assert receipt["status"] == "failed"
    assert "terminal phase" in receipt["error"]
    deletes = [v for s, v, _ in _kube.calls if s == "delete"]
    assert [v[0] for v in deletes] == [
        "pod", "rolebinding", "role", "serviceaccount",
    ]


async def test_post_arming_injection_failure_keeps_the_carrier(_kube):
    # §8/ND4: POST-arming the timer MUST survive — a timed-out patch may
    # have landed, and dropping the timer could strand the fault. The
    # receipt is `partial`, honest, and NO delete is dispatched.
    _wire_happy(_kube, patch_result=_R(1, "", "etcd unavailable"))
    receipt = await _assemble()

    assert receipt["status"] == "partial"
    assert "stays armed" in receipt["error"]
    assert receipt["carrier"]["armed"] is True
    assert receipt["carrier"]["landing_verified"] is False
    assert receipt["carrier"]["recovery_deadline_epoch"] > time.time()
    # the carrier stays: no cleanup deletes, but the artifact registers
    assert not any(s == "delete" for s, _, _ in _kube.calls)
    assert receipt["artifact"]["status"] == "recovery_armed"
    assert receipt["recovery_handle"]["restore_patches"] == _RESTORE


async def test_unconfirmed_landing_is_partial_not_success(_kube):
    # readback sees the pre-injection state (patch "landed" but did not
    # stick) → partial with an honest error; the carrier stays armed
    _wire_happy(_kube)
    _kube.on("get", "service", _seq(
        _R(0, json.dumps(_BASELINE)),
        _R(0, json.dumps(_BASELINE)),  # readback: fault NOT visible
    ))
    receipt = await _assemble()

    assert receipt["status"] == "partial"
    assert "readback" in receipt["error"]
    assert receipt["carrier"]["armed"] is True
    assert receipt["artifact"]["status"] == "recovery_armed"
    assert not any(s == "delete" for s, _, _ in _kube.calls)


async def test_duration_bounds_fail_closed_before_anything_is_built(_kube):
    for bad in (0, -5, 84601, 86399, 86400, 86401):
        with pytest.raises(_AssemblyError):
            await _assemble(duration=bad)
    # nothing dispatched: the window bound is a precondition, not a
    # mid-assembly abort
    assert _kube.calls == []


async def test_duration_bound_leaves_the_restore_buffer_whole(_kube):
    # R2 review Bug#2 pin: duration = max_sleep − 1800 (84600) is the
    # LAST legal window — skeleton = min(84600+1800, 86400) = 86400, so
    # the timer's post-fire margin is the full 1800s buffer. One second
    # more and the cap starts eating the buffer (86399 used to squeeze it
    # to 1s minus the run→arm delay — carrier death before restore).
    arm_clock = _wire_happy(_kube)
    receipt = await _assemble(duration=84600)
    assert receipt["status"] == "success"
    run_calls = [v for sub, v, _ in _kube.calls if sub == "run"]
    skeleton = int(run_calls[0][run_calls[0].index("--") + 2])
    assert skeleton == 86400
    # the restore timer sleeps the full window from ARM — the skeleton
    # outlives the fire by exactly the buffer
    arm_calls = [
        v for sub, v, _ in _kube.calls
        if sub == "exec" and "echo armed" in str(v)
    ]
    timer = re.search(r"sleep (\d+)", str(arm_calls[-1]))
    assert timer is not None and int(timer.group(1)) == 84600
    assert 86400 - 84600 == 1800
    assert arm_clock  # (the countdown origin marker the wiring records)


async def test_cluster_scoped_kind_refused(_kube):
    with pytest.raises(_AssemblyError, match="cluster-scoped"):
        await _assemble(kind="Node", name="node-1")
    with pytest.raises(_AssemblyError, match="cluster-scoped"):
        await _assemble(kind="Namespace", name="ns-x")
    assert _kube.calls == []


async def test_unknown_kind_refused(_kube):
    with pytest.raises(ValueError, match="no REST mapping"):
        await _assemble(kind="Widget")
    assert _kube.calls == []


async def test_empty_image_allowlist_fails_before_the_stack(_kube, monkeypatch):
    monkeypatch.setattr(settings, "recovery_carrier_allowed_images", "")
    monkeypatch.setattr(settings, "recovery_carrier_discovered_images", "")
    with pytest.raises(ValueError, match="allowlist is empty"):
        await _assemble()
    # nothing built, nothing armed, nothing to clean
    assert not any(s in ("create", "run", "patch") for s, _, _ in _kube.calls)


async def test_rbac_derivation_is_same_source_and_always_uniform(_kube):
    # The assembly derives RBAC from the SAME recipe that arms the
    # payload: restore json-patch ops on the target kind → PATCH method
    # → {get, patch} on that one family. Single family ⇒ ALWAYS uniform
    # ⇒ ALWAYS the one-command create (the two-step json-patch path is
    # the render API's legislated capability for multi-family recipes —
    # pinned in the unit tests above; the M1 assembly cannot reach it).
    baseline = {
        "metadata": {"name": "svc-x", "namespace": "cms-demo"},
        "spec": {"selector": {"app": "accounting"},
                 "progressDeadlineSeconds": 300},
    }
    patches = [
        {"op": "replace", "path": "/spec/selector/app", "value": "wrong"},
        {"op": "replace", "path": "/spec/progressDeadlineSeconds", "value": 1},
    ]
    restore = [
        {"op": "replace", "path": "/spec/selector/app", "value": "accounting"},
        {"op": "replace", "path": "/spec/progressDeadlineSeconds", "value": 300},
    ]
    _wire_happy(_kube)
    _kube.on("get", "service", _seq(
        _R(0, json.dumps(baseline)),
        _R(0, json.dumps({
            "spec": {"selector": {"app": "wrong"},
                     "progressDeadlineSeconds": 1},
        })),
    ))
    receipt = await _assemble(patches=patches, restore=restore)

    assert receipt["status"] == "success"
    role_calls = [
        (s, v) for s, v, _ in _kube.calls
        if v and v[0] == "role" and s in ("create", "patch")
    ]
    assert [s for s, _ in role_calls] == ["create"], (
        "a single-family derivation must render through ONE create "
        "command — a json-patch append here would mean the derivation "
        "grew a second family"
    )
    _, create_v = role_calls[0]
    assert create_v[create_v.index("--verb") + 1] == "get,patch"
    assert "services" in create_v
    # the artifact's rbac_family role mirrors the SAME derived verbs
    fam = {m["kind"]: m for m in receipt["artifact"]["rbac_family"]}
    assert fam["role"]["verbs"] == ["get", "patch"]


async def test_oversized_recipe_lands_body_before_arming(_kube):
    # §7 tier: an arm payload past the ~900B safety line lands the body
    # in-carrier as b64 chunks, and the armed curl reads ``-d @file``
    big = "x" * 1200
    baseline = {"spec": {"template": {"spec": {"containers": [
        {"image": big},
    ]}}}}
    patches = [{"op": "replace",
                "path": "/spec/template/spec/containers/0/image",
                "value": "registry.example.com/paused:1"}]
    restore = [{"op": "replace",
                "path": "/spec/template/spec/containers/0/image",
                "value": big}]
    _wire_happy(_kube)
    _kube.on("get", "service", _seq(
        _R(0, json.dumps(baseline)),
        _R(0, json.dumps({
            "spec": {"template": {"spec": {"containers": [
                {"image": "registry.example.com/paused:1"},
            ]}}},
        })),
    ))
    receipt = await _assemble(patches=patches, restore=restore)

    assert receipt["status"] == "success"
    scripts = [_exec_script(v) for s, v, _ in _kube.calls if s == "exec"]
    arm_idx = next(i for i, s in enumerate(scripts) if "echo armed" in s)
    landing = scripts[:arm_idx]
    assert any("/tmp/restore.b64" in s and "echo -n " in s for s in landing)
    assert any("base64 -d /tmp/restore.b64" in s for s in landing)
    assert "-d @/tmp/restore.json" in scripts[arm_idx]


# ---------------------------------------------------------------------------
# Tool layer (blade_create precedent: ONE call, deterministic chain)
# ---------------------------------------------------------------------------


async def test_tool_happy_path_returns_registerable_receipt(_kube):
    _wire_happy(_kube)
    out = await faultdrill_assemble_carrier.ainvoke({
        "target_kind": "Service", "target_name": "svc-x",
        "target_namespace": "cms-demo",
        "patches": json.dumps(_PATCHES),
        "restore_patches": json.dumps(_RESTORE),
        "duration_seconds": 600, "kubeconfig": "/kc", "task_id": "task-1",
    })
    receipt = json.loads(out)
    assert receipt["status"] == "success"
    # the handle travels INSIDE the artifact (pop at the top level: the
    # artifact ledger is the single registration, no duplicate handle)
    assert "recovery_handle" not in receipt
    assert receipt["artifact"]["recovery_handle"]["kind"] == "recovery_carrier"


async def test_tool_invalid_recipe_json_is_a_failed_receipt():
    out = await faultdrill_assemble_carrier.ainvoke({
        "target_kind": "Service", "target_name": "svc-x",
        "target_namespace": "cms-demo",
        "patches": "not json",
        "restore_patches": json.dumps(_RESTORE),
        "duration_seconds": 600,
    })
    receipt = json.loads(out)
    assert receipt["status"] == "failed"
    assert "patches is not valid JSON" in receipt["error"]
    assert receipt["carrier"]["armed"] is False


async def test_tool_empty_recipe_array_is_a_failed_receipt():
    out = await faultdrill_assemble_carrier.ainvoke({
        "target_kind": "Service", "target_name": "svc-x",
        "target_namespace": "cms-demo",
        "patches": json.dumps(_PATCHES),
        "restore_patches": "[]",
        "duration_seconds": 600,
    })
    receipt = json.loads(out)
    assert receipt["status"] == "failed"
    assert "non-empty" in receipt["error"]


# ---------------------------------------------------------------------------
# Receipt parsing + the receipt-transport branch (task 1.5)
# ---------------------------------------------------------------------------


def _registerable_receipt(status="success", name="drill-rc-x") -> dict:
    return {
        "status": status,
        "carrier": {"name": name, "namespace": "ns1", "armed": True},
        "artifact": build_carrier_artifact(
            name=name, namespace="ns1", task_id="",
            rules=[{"apiGroups": [""], "resources": ["services"],
                    "verbs": ["get", "patch"]}],
            duration_seconds=600, deadline_epoch=1.0,
            recovery_handle={"kind": "recovery_carrier",
                             "value": f"ns1/{name}"},
        ),
        "recovery_handle": {"kind": "recovery_carrier"},
        "steps": [],
    }


def _ai_call(tool_call_id: str, name: str, args: dict | None = None) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"id": tool_call_id, "name": name,
                                 "args": args or {}}],
    )


def test_parse_receipt_registerable_states():
    assert parse_receipt(json.dumps(_registerable_receipt())) is not None
    assert parse_receipt(json.dumps(_registerable_receipt("partial"))) is not None


def test_parse_receipt_fail_closed_faces():
    # failed receipts cleaned their stack → nothing registerable
    assert parse_receipt(json.dumps(_registerable_receipt("failed"))) is None
    # unparseable results (screener rejections render as free text)
    assert parse_receipt("[target_guard] REJECT_BANNED …") is None
    assert parse_receipt("Error: kubectl patch: boom") is None
    assert parse_receipt(json.dumps({"status": "success"})) is None  # no artifact
    no_name = _registerable_receipt()
    no_name["artifact"]["name"] = ""
    assert parse_receipt(json.dumps(no_name)) is None


def test_receipt_transport_registers_artifact_with_id_provenance():
    receipt = _registerable_receipt()
    messages = [
        _ai_call("call-1", ASSEMBLER_TOOL_NAME, {"target_kind": "Service"}),
        ToolMessage(content=json.dumps(receipt), tool_call_id="call-1"),
    ]
    artifacts = _assembler_receipt_artifacts(messages, task_id="t-9")
    assert len(artifacts) == 1
    art = artifacts[0]
    # the branch MOVES the receipt's armed facts and fills ONLY the id
    # provenance a ToolMessage can supply
    assert art["created_tool_call_id"] == "call-1"
    assert art["host_exec_tool_call_id"] == "call-1"
    assert art["host_exec_seen_ids"] == ["call-1"]
    assert art["task_id"] == "t-9"
    assert art["status"] == "recovery_armed"
    assert art["rbac_family"][1]["verbs"] == ["get", "patch"]


def test_receipt_transport_failed_receipt_registers_nothing():
    messages = [
        _ai_call("call-1", ASSEMBLER_TOOL_NAME),
        ToolMessage(content=json.dumps(_registerable_receipt("failed")),
                    tool_call_id="call-1"),
    ]
    assert _assembler_receipt_artifacts(messages, task_id="t-9") == []


def test_receipt_transport_guard_rejection_registers_nothing():
    messages = [
        _ai_call("call-1", ASSEMBLER_TOOL_NAME),
        ToolMessage(
            content="[target_guard] REJECT_BANNED scope mismatch",
            tool_call_id="call-1",
        ),
    ]
    assert _assembler_receipt_artifacts(messages, task_id="t-9") == []


def test_receipt_transport_ignores_other_tools_and_unpaired_calls():
    messages = [
        _ai_call("call-1", "kubectl", {"subcommand": "patch"}),
        ToolMessage(content="patched", tool_call_id="call-1"),
        # an assembler call whose result never arrived (interrupted)
        _ai_call("call-2", ASSEMBLER_TOOL_NAME),
    ]
    assert _assembler_receipt_artifacts(messages, task_id="t-9") == []


def test_receipt_transport_object_shaped_tool_calls():
    # older SDK / custom wrappers emit tool_call OBJECTS inside an
    # AIMessage — the pairing walk reads both shapes (the same
    # discipline as _faultdrill_apply_events). New langchain versions
    # normalise constructor dicts, so the object shape is injected via
    # a spec'd mock whose isinstance passes.
    from unittest.mock import Mock

    receipt = _registerable_receipt()
    ai = Mock(spec=AIMessage)
    ai.tool_calls = [
        SimpleNamespace(id="call-7", name=ASSEMBLER_TOOL_NAME, args={}),
    ]
    messages = [
        ai,
        ToolMessage(content=json.dumps(receipt), tool_call_id="call-7"),
    ]
    artifacts = _assembler_receipt_artifacts(messages)
    assert len(artifacts) == 1
    assert artifacts[0]["created_tool_call_id"] == "call-7"


def test_provider_hook_merges_both_families(_kube):
    # collect_artifacts_from_messages = CR-apply ledger + assembler
    # receipts, one hook, one list — the registry seam stays untouched
    receipt = _registerable_receipt()
    messages = [
        _ai_call("call-1", ASSEMBLER_TOOL_NAME, {"target_kind": "Service"}),
        ToolMessage(content=json.dumps(receipt), tool_call_id="call-1"),
    ]
    artifacts = FaultDrillProvider().collect_artifacts_from_messages(
        messages, task_id="t-9",
    )
    carrier_arts = [a for a in artifacts if a.get("type") == "recovery_carrier"]
    assert len(carrier_arts) == 1
    assert carrier_arts[0]["created_tool_call_id"] == "call-1"


# ---------------------------------------------------------------------------
# Provider wiring (task 1.3/1.4: tools face + guard classification)
# ---------------------------------------------------------------------------


def test_tools_execute_face_only():
    # the factory unions ``provider.tools(EXECUTE)`` — the CONSTANT's
    # value is the lowercase "execute"; an uppercase literal here would
    # silently contribute nothing (the wiring bug this pin caught)
    from chaos_agent.agent.providers.base import (
        EXECUTE, PLAN, RECOVER_VERIFY, VERIFY,
    )

    provider = FaultDrillProvider()
    assert [t.name for t in provider.tools(EXECUTE)] == [
        "faultdrill_assemble_carrier",
    ]
    for phase in (PLAN, VERIFY, RECOVER_VERIFY):
        assert provider.tools(phase) == []


def test_capability_gate_binds_assembler_to_k8s_profile():
    # the ownership index scans every provider phase's tools; the
    # assembler must surface as a FaultDrill-owned tool so the profile
    # gate refuses it on a host channel (matches_channel = K8s only)
    from chaos_agent.agent.capabilities.context import provider_tool_owners
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()
    owners = provider_tool_owners()
    assert ASSEMBLER_TOOL_NAME in owners
    assert any(isinstance(p, FaultDrillProvider) for p in owners[ASSEMBLER_TOOL_NAME])


def test_factory_union_binds_assembler_into_the_execute_surface():
    # end-to-end wiring: the phase tool surface the LLM actually sees
    # includes the assembler tool (blade_create precedent, factory.py
    # ``_append_provider_tools``)
    from chaos_agent.agent.factory import _append_provider_tools
    from chaos_agent.agent.providers import FaultProviderRegistry
    from chaos_agent.agent.providers.base import EXECUTE

    FaultProviderRegistry.register_builtins()
    surface = _append_provider_tools([], EXECUTE)
    names = [getattr(t, "name", "") for t in surface]
    assert ASSEMBLER_TOOL_NAME in names


def test_classify_claims_assembler_on_the_ordinary_net():
    provider = FaultDrillProvider()
    target = provider.classify_tool_target(
        ASSEMBLER_TOOL_NAME,
        {"target_kind": "Deployment", "target_name": "dep-x",
         "target_namespace": "cms-demo"},
        raw_command="faultdrill_assemble_carrier(...)",
    )
    assert target is not None
    assert target.scope == "deployment"
    assert target.namespace == "cms-demo"
    assert target.names == ("dep-x",)


def test_classify_defaults_namespace_and_maps_aliases():
    target = FaultDrillProvider().classify_tool_target(
        ASSEMBLER_TOOL_NAME,
        {"target_kind": "svc", "target_name": "svc-x"},
        raw_command="",
    )
    assert target.scope == "service"
    assert target.namespace == "default"


def test_classify_fail_closed_faces():
    from chaos_agent.agent.target_guard.types import SCOPE_UNKNOWN

    provider = FaultDrillProvider()
    unmappable = provider.classify_tool_target(
        ASSEMBLER_TOOL_NAME, {"target_kind": "Widget"}, raw_command="",
    )
    assert unmappable is not None
    assert unmappable.scope == SCOPE_UNKNOWN
    assert unmappable.reject_detail

    nameless = provider.classify_tool_target(
        ASSEMBLER_TOOL_NAME,
        {"target_kind": "Service", "target_name": " "},
        raw_command="",
    )
    assert nameless is not None
    assert nameless.reject_detail


def test_classify_leaves_kubectl_to_k8s_native():
    # single owner per tool: the CR apply's classification (and every
    # other kubectl call) stays with the k8s_native domain classifier
    assert FaultDrillProvider().classify_tool_target(
        "kubectl", {"subcommand": "patch"}, raw_command="kubectl patch",
    ) is None


def test_registry_dispatch_reaches_the_assembler_claim():
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()
    target = FaultProviderRegistry.classify_tool_target(
        ASSEMBLER_TOOL_NAME,
        {"target_kind": "Service", "target_name": "svc-x",
         "target_namespace": "cms-demo"},
        raw_command="",
    )
    assert target is not None and target.scope == "service"
