"""Tests for ``chaos_agent.agent.target_guard.freeze``.

The freeze helpers translate AgentState's loose dict shape into the
canonical ``ApprovedTarget`` snapshot (and back). These tests pin the
field-extraction logic so refactors of AgentState don't silently
break the screener's view of "what the user approved".
"""

from __future__ import annotations

from chaos_agent.agent.target_guard import (
    ApprovedTarget,
    approved_from_dict,
    freeze_approved_target,
    freeze_approved_target_from_spec,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec


class TestFreezeApprovedTarget:
    def test_full_pod_inject_freezes_cleanly(self):
        d = freeze_approved_target(
            target={
                "namespace": "prod", "names": ["pod-a"],
                "labels": {}, "resource_type": "pod",
            },
            params={"scope": "pod", "target": "cpu", "action": "fullload"},
            fault_scope="pod",
            fault_target="cpu",
            fault_action="fullload",
        )
        assert d == {
            "scope": "pod", "namespace": "prod",
            "names": ["pod-a"], "labels": {},
            "is_namespace_wide": False,
            "fault_target": "cpu", "fault_action": "fullload",
            "lock_fault_type": True,
            "owner_names": [],
            "resolved_names": [],
            "pvc_claims": [],
            "secondary_scopes": ["pv", "persistentvolume", "configmap", "secret", "pod", "node", "resourcequota", "serviceaccount", "role", "rolebinding", "clusterrole", "clusterrolebinding"],
            "secondary_namespace": "prod",
            "host_name": "",
        }

    def test_container_scope_normalised_to_pod(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p1"]},
            params={"scope": "container"},
            fault_scope=None, fault_target="jvm", fault_action="oom",
        )
        assert d["scope"] == "pod"

    def test_node_scope_clears_namespace(self):
        d = freeze_approved_target(
            target={"namespace": "leftover", "names": ["node-1"]},
            params={"scope": "node"},
            fault_scope="node", fault_target="cpu", fault_action="fullload",
        )
        assert d["scope"] == "node"
        # Cluster-scoped → namespace nulled in the snapshot.
        assert d["namespace"] == ""

    def test_service_scope_grants_carrier_companion_surface(self):
        # W-55-1 red-green anchor: scope=service (#55 selector-tamper first
        # run died on REJECT_DRIFT for create sa/role) must carry the
        # carrier companion surface so the bounded self-recovery timer is
        # reachable for service-config faults.
        d = freeze_approved_target(
            target={"namespace": "default", "names": ["drill-svcselect-svc"], "resource_type": "service"},
            params={"scope": "service"},
            fault_scope="service", fault_target="service", fault_action="selector",
        )
        for s in ("pod", "serviceaccount", "role", "rolebinding"):
            assert s in d["secondary_scopes"]
        assert d["secondary_namespace"] == "default"

    def test_generic_namespaced_scope_grants_carrier_companion_surface(self):
        # W-55-1 方案 B 通用化锚：任意 namespaced scope（此处用 pvc 代表未来
        # 新 scope 类型）都自动获得载体伴生面 —— 堵死「新 scope = 伴生面缺口」
        # 模式性缺口（B28 家族三撞：#15 / #55）。
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p1"], "resource_type": "pvc"},
            params={"scope": "pvc"},
            fault_scope="pvc", fault_target="pvc", fault_action="fill",
        )
        for s in ("pod", "serviceaccount", "role", "rolebinding"):
            assert s in d["secondary_scopes"]

    def test_cluster_scoped_scope_excluded_from_carrier_grant(self):
        # 排除面：cluster-scoped（node）/host 走非 k8s 载体（systemd timer 等），
        # 不叠加载体 RBAC 面 —— 最小面原则。
        d = freeze_approved_target(
            target={"namespace": "leftover", "names": ["node-1"], "resource_type": "node"},
            params={"scope": "node"},
            fault_scope="node", fault_target="cpu", fault_action="fullload",
        )
        assert "serviceaccount" not in d["secondary_scopes"]

    def test_default_namespace_when_missing(self):
        d = freeze_approved_target(
            target={"names": ["p1"]},  # no namespace
            params={"scope": "pod"},
            fault_scope=None, fault_target=None, fault_action=None,
        )
        assert d["namespace"] == "default"

    def test_namespace_wide_when_no_names_or_labels(self):
        d = freeze_approved_target(
            target={"namespace": "ns"},
            params={"scope": "pod"},
            fault_scope=None, fault_target=None, fault_action=None,
        )
        assert d["is_namespace_wide"] is True

    def test_labels_only_is_not_namespace_wide(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "labels": {"app": "demo"}},
            params={"scope": "pod"},
            fault_scope=None, fault_target=None, fault_action=None,
        )
        assert d["is_namespace_wide"] is False

    def test_names_csv_string_normalised_to_list(self):
        # Back-compat path: some callers pass names as CSV.
        d = freeze_approved_target(
            target={"namespace": "ns", "names": "a,b,c"},
            params={"scope": "pod"},
            fault_scope=None, fault_target=None, fault_action=None,
        )
        assert d["names"] == ["a", "b", "c"]

    def test_explicit_blade_fields_win_over_params(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={"target": "mem", "action": "ram"},
            fault_scope="pod",
            fault_target="cpu",
            fault_action="fullload",
        )
        assert d["fault_target"] == "cpu"
        assert d["fault_action"] == "fullload"

    def test_falls_back_to_params_when_blade_fields_empty(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={"scope": "pod", "target": "mem", "action": "ram"},
            fault_scope=None, fault_target=None, fault_action=None,
        )
        assert d["fault_target"] == "mem"
        assert d["fault_action"] == "ram"

    def test_no_scope_returns_none(self):
        # No scope anywhere — caller should treat as "no approval"
        # and disable guarding for the turn.
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={},
            fault_scope=None, fault_target=None, fault_action=None,
        )
        assert d is None

    def test_none_target_and_params(self):
        d = freeze_approved_target(
            target=None, params=None,
            fault_scope=None, fault_target=None, fault_action=None,
        )
        assert d is None

    def test_lock_fault_type_default_true(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={"scope": "pod"},
            fault_scope=None, fault_target="cpu", fault_action=None,
        )
        assert d["lock_fault_type"] is True

    def test_lock_fault_type_can_be_overridden(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={"scope": "pod"},
            fault_scope=None, fault_target="cpu", fault_action=None,
            lock_fault_type=False,
        )
        assert d["lock_fault_type"] is False


class TestFreezeApprovedTargetFromSpec:
    def test_matches_legacy_constructor_for_fault_spec(self):
        spec = FaultSpec(
            namespace="prod",
            scope="pod",
            names=("pod-a",),
            labels={"app": "demo"},
            fault_target="network",
            fault_action="loss",
            params={"percent": "100"},
        )

        direct = freeze_approved_target_from_spec(
            spec,
            owner_names=("deploy-a",),
        )
        legacy = freeze_approved_target(
            target={
                "namespace": "prod",
                "names": ["pod-a"],
                "labels": {"app": "demo"},
                "resource_type": "pod",
            },
            params={"percent": "100"},
            fault_scope="pod",
            fault_target="network",
            fault_action="loss",
            owner_names=("deploy-a",),
        )

        assert direct == legacy

    def test_accepts_fault_spec_dict(self):
        spec = FaultSpec(
            namespace="prod",
            scope="pod",
            names=("pod-a",),
            fault_target="cpu",
            fault_action="fullload",
        )

        d = freeze_approved_target_from_spec(spec.to_dict())

        assert d is not None
        assert d["scope"] == "pod"
        assert d["namespace"] == "prod"
        assert d["names"] == ["pod-a"]
        assert d["fault_target"] == "cpu"

    def test_none_or_malformed_spec_returns_none(self):
        assert freeze_approved_target_from_spec(None) is None
        assert freeze_approved_target_from_spec({"scope": []}) is None


class TestApprovedFromDict:
    def test_round_trip(self):
        original = freeze_approved_target(
            target={"namespace": "prod", "names": ["a", "b"]},
            params={"scope": "pod"},
            fault_scope=None, fault_target="cpu", fault_action="fullload",
            owner_names=("deploy-x", "deploy-x-abc"),
        )
        approved = approved_from_dict(original)
        assert isinstance(approved, ApprovedTarget)
        assert approved.scope == "pod"
        assert approved.namespace == "prod"
        assert approved.names == ("a", "b")
        assert approved.labels == {}
        # The generation anchor (case #39) must survive the dict
        # round-trip: the screener hydrates the approval from the
        # state dict on EVERY guard decision, so a dropped field here
        # would silently disable the generation-successor exemption in
        # production while the direct-construction tests stay green.
        assert approved.owner_names == ("deploy-x", "deploy-x-abc")
        assert approved.fault_target == "cpu"
        assert approved.fault_action == "fullload"
        assert approved.lock_fault_type is True

    def test_none_returns_none(self):
        assert approved_from_dict(None) is None

    def test_empty_dict_returns_none(self):
        assert approved_from_dict({}) is None

    def test_missing_scope_returns_none(self):
        # Without scope we can't compare anything meaningfully.
        assert approved_from_dict({"namespace": "ns", "names": ["a"]}) is None

    def test_non_dict_returns_none(self):
        # Defensive — the state field is typed Optional[dict] but
        # checkpoint corruption could pass other shapes.
        assert approved_from_dict("not a dict") is None
        assert approved_from_dict([1, 2, 3]) is None

    def test_lock_fault_type_defaults_true_when_missing(self):
        # Old checkpoints that pre-date the field default to True
        # (safer: lock until operator explicitly relaxes).
        approved = approved_from_dict({
            "scope": "pod", "namespace": "ns",
            "names": ["a"], "fault_target": "cpu",
        })
        assert approved is not None
        assert approved.lock_fault_type is True


class TestDurationAnchorFreeze:
    """Contract duration frozen into the approval (twelfth-round E4).

    ``FaultSpec.duration_seconds`` is the user-approved bound on the
    fault's residence time; the guard's duration net compares the
    execution-side ``--timeout`` against it. These tests pin the freeze
    discipline: the key rides ONLY when the spec carried a duration
    (no-duration snapshots stay byte-identical to the pre-anchor
    output), and the dict round-trip hydrates it back.
    """

    def test_spec_duration_frozen(self):
        spec = FaultSpec(
            namespace="prod",
            scope="pod",
            names=("pod-a",),
            fault_target="cpu",
            fault_action="fullload",
            duration_seconds=300,
        )
        d = freeze_approved_target_from_spec(spec)
        assert d is not None
        assert d["duration_seconds"] == 300

    def test_spec_without_duration_omits_key(self):
        # Byte-identical snapshot discipline: no duration, no key.
        spec = FaultSpec(
            namespace="prod",
            scope="pod",
            names=("pod-a",),
            fault_target="cpu",
            fault_action="fullload",
        )
        d = freeze_approved_target_from_spec(spec)
        assert d is not None
        assert "duration_seconds" not in d

    def test_dict_duration_round_trip(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["a"]},
            params={"scope": "pod"},
            fault_scope=None, fault_target="cpu", fault_action="fullload",
            duration_seconds=300,
        )
        approved = approved_from_dict(d)
        assert approved is not None
        assert approved.duration_seconds == 300

    def test_legacy_dict_without_duration_hydrates_zero(self):
        # Checkpoints frozen before the anchor existed hydrate as 0 —
        # the guard's duration net stays silent for them.
        approved = approved_from_dict({
            "scope": "pod", "namespace": "ns",
            "names": ["a"], "fault_target": "cpu",
        })
        assert approved is not None
        assert approved.duration_seconds == 0

    def test_spec_dict_form_carries_duration(self):
        # The dict-shaped spec (checkpointer round-trip) carries the
        # field just like the dataclass form.
        spec = FaultSpec(
            namespace="prod",
            scope="pod",
            names=("pod-a",),
            fault_target="cpu",
            fault_action="fullload",
            duration_seconds=600,
        )
        d = freeze_approved_target_from_spec(spec.to_dict())
        assert d is not None
        assert d["duration_seconds"] == 600


class TestDiscoverOwnerNamesNamesChannel:
    """Generation-anchor discovery (case #39): the names channel resolves
    each named pod's ownerReferences chain so a name-based pod approval
    also freezes the owner identity a recreated successor carries."""

    @staticmethod
    def _make_transport(monkeypatch, owner_refs: dict[str, str],
                        label_hits: str = ""):
        """Route fake answers by the object NAME inside the cmd tokens.

        ``owner_refs`` maps object name → ownerReferences jsonpath-range
        output (``Kind|name|true;`` records). ``label_hits`` is the stdout
        for the labels channel's ``-l`` queries (space-separated names).
        """
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            tokens = [str(t) for t in cmd]
            if any("ownerReferences" in t for t in tokens):
                for name, out in owner_refs.items():
                    if name in tokens:
                        return CommandResult(
                            exit_code=0, stdout=out, stderr="",
                        )
                return CommandResult(exit_code=0, stdout="", stderr="")
            if "-l" in tokens:
                return CommandResult(exit_code=0, stdout=label_hits, stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )

    async def test_names_channel_resolves_rs_to_deployment(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import discover_owner_names
        self._make_transport(monkeypatch, {
            "web-abc-111": "ReplicaSet|web-abc|true;",
            "web-abc": "Deployment|web|true;",
        })
        owners = await discover_owner_names(
            "pod", "default", {}, names=("web-abc-111",),
        )
        # Both chain hops frozen: the direct controller (RS) and the
        # top-level workload (Deployment) — the successor prefix check
        # consults either.
        assert owners == ("web", "web-abc")

    async def test_names_channel_daemonset_direct_owner(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import discover_owner_names
        self._make_transport(monkeypatch, {
            "agent-xyz": "DaemonSet|node-agent|true;",
        })
        owners = await discover_owner_names(
            "pod", "kube-system", {}, names=("agent-xyz",),
        )
        # DaemonSet owns its pods directly — single hop, no walk-up.
        assert owners == ("node-agent",)

    async def test_names_channel_bare_pod_yields_empty(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import discover_owner_names
        self._make_transport(monkeypatch, {"lonely-pod": ""})
        owners = await discover_owner_names(
            "pod", "default", {}, names=("lonely-pod",),
        )
        assert owners == ()

    async def test_names_channel_ignores_non_controller_refs(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import discover_owner_names
        self._make_transport(monkeypatch, {
            "web-abc-111": "Service|web-svc|false;ReplicaSet|web-abc|true;",
        })
        owners = await discover_owner_names(
            "pod", "default", {}, names=("web-abc-111",),
        )
        # Only the controller=true entry counts (API guarantees at most
        # one); the non-controller Service reference is not an owner.
        assert owners == ("web-abc",)

    async def test_names_channel_query_failure_fails_closed(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import discover_owner_names

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            raise RuntimeError("transport down")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        owners = await discover_owner_names(
            "pod", "default", {}, names=("web-abc-111",),
        )
        assert owners == ()

    async def test_container_scope_normalised_to_pod_channel(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import discover_owner_names
        self._make_transport(monkeypatch, {
            "c-pod-1": "DaemonSet|ds|true;",
        })
        owners = await discover_owner_names(
            "container", "default", {}, names=("c-pod-1",),
        )
        assert owners == ("ds",)

    async def test_non_pod_scope_skips_names_channel(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import discover_owner_names
        called = []

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            called.append(cmd)
            from chaos_agent.models.command_result import CommandResult
            return CommandResult(exit_code=0, stdout="", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        owners = await discover_owner_names(
            "node", "", {}, names=("node-1",),
        )
        assert owners == ()
        assert not called  # node scope: neither channel runs

    async def test_labels_and_names_channels_union(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import discover_owner_names
        self._make_transport(
            monkeypatch,
            {"web-abc-111": "ReplicaSet|web-abc|true;"},
            label_hits="other-workload",
        )
        owners = await discover_owner_names(
            "pod", "default", {"app": "web"},
            names=("web-abc-111",),
        )
        # labels channel (deployment matched by selector) + names channel
        # (ownerReferences chain) unioned, deduplicated, sorted.
        assert owners == ("other-workload", "web-abc")


class TestDiscoverWorkloadPvcClaims:
    """B79 (case #39-R): a workload-scope approval freezes the SAME PVC
    claim anchor a pod-scope approval of its pods would — the template
    claimName is the string every replica mounts."""

    @staticmethod
    def _make_transport(monkeypatch, answers: dict):
        """Route fake answers by jsonpath token inside the cmd tokens.

        ``answers`` maps a distinctive token fragment (e.g. the workload
        name or ``-l``) → stdout.
        """
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            tokens = [str(t) for t in cmd]
            for needle, out in answers.items():
                if needle in tokens:
                    return CommandResult(
                        exit_code=0, stdout=out, stderr="",
                    )
            return CommandResult(exit_code=0, stdout="", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )

    async def test_b79_replay_deployment_template_claim(self, monkeypatch):
        """The exact #39-R shape: deployment extraction returns the SAME
        claim string the pod channel froze in the first run."""
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )
        self._make_transport(
            monkeypatch, {"drill-mntopt-target": "drill-mntopt-pvc"},
        )
        claims = await discover_workload_pvc_claims(
            "deployment", "default", ("drill-mntopt-target",), {},
        )
        assert claims == ("drill-mntopt-pvc",)

    async def test_multi_claim_template_freezes_all(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )
        self._make_transport(
            monkeypatch, {"web": "data-pvc logs-pvc"},
        )
        claims = await discover_workload_pvc_claims(
            "deployment", "prod", ("web",), {},
        )
        assert claims == ("data-pvc", "logs-pvc")

    async def test_cronjob_uses_nested_template_path(self, monkeypatch):
        """CronJob nests the pod template inside jobTemplate — the jsonpath
        must carry the extra hop (pinned by asserting on the cmd tokens)."""
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )
        seen: list[list[str]] = []

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            from chaos_agent.models.command_result import CommandResult
            seen.append([str(t) for t in cmd])
            return CommandResult(
                exit_code=0, stdout="ci-cache-pvc", stderr="",
            )

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        claims = await discover_workload_pvc_claims(
            "cronjob", "prod", ("nightly",), {},
        )
        assert claims == ("ci-cache-pvc",)
        assert any(
            "jsonpath={.spec.jobTemplate.spec.template.spec.volumes[*]"
            ".persistentVolumeClaim.claimName}" in tokens
            for tokens in seen
        )

    async def test_b81_output_flag_always_carries_jsonpath_prefix(
        self, monkeypatch,
    ):
        """B81 teeth: a bare ``-o {....}`` is an unknown output format —
        kubectl exits 1 and the query silently freezes an empty claim
        set (the #39 third-retest live catch). Every ``-o`` token this
        channel emits MUST start with ``jsonpath=`` — pinned for BOTH
        the names form and the labels form."""
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )
        seen: list[list[str]] = []

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            from chaos_agent.models.command_result import CommandResult
            seen.append([str(t) for t in cmd])
            return CommandResult(
                exit_code=0, stdout="data-pvc", stderr="",
            )

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        await discover_workload_pvc_claims(
            "deployment", "prod", ("web",), {},
        )
        await discover_workload_pvc_claims(
            "deployment", "prod", (), {"app": "web"},
        )
        assert seen, "no queries emitted"
        for tokens in seen:
            for i, t in enumerate(tokens):
                if t == "-o":
                    out_flag = tokens[i + 1]
                    assert out_flag.startswith("jsonpath={"), tokens
                    assert out_flag.endswith("}"), tokens

    async def test_labels_form_uses_selector_variant(self, monkeypatch):
        """No names, labels only → one ``-l`` query whose jsonpath reads
        per-item template claims."""
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )
        seen: list[list[str]] = []

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            from chaos_agent.models.command_result import CommandResult
            tokens = [str(t) for t in cmd]
            seen.append(tokens)
            if "-l" in tokens:
                return CommandResult(
                    exit_code=0, stdout="shared-pvc", stderr="",
                )
            return CommandResult(exit_code=0, stdout="", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        claims = await discover_workload_pvc_claims(
            "deployment", "prod", (), {"app": "web"},
        )
        assert claims == ("shared-pvc",)
        assert any(
            "jsonpath={.items[*].spec.template.spec.volumes[*]"
            ".persistentVolumeClaim.claimName}" in tokens
            for tokens in seen
        )

    async def test_template_without_claims_yields_empty(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )
        self._make_transport(monkeypatch, {})
        claims = await discover_workload_pvc_claims(
            "deployment", "prod", ("web",), {},
        )
        assert claims == ()

    async def test_query_failure_fails_closed(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            raise RuntimeError("transport down")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        claims = await discover_workload_pvc_claims(
            "deployment", "prod", ("web",), {},
        )
        assert claims == ()

    async def test_non_template_scope_makes_no_query(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )
        called = []

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            called.append(cmd)
            from chaos_agent.models.command_result import CommandResult
            return CommandResult(exit_code=0, stdout="x", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        assert await discover_workload_pvc_claims(
            "node", "", ("node-1",), {},
        ) == ()
        assert await discover_workload_pvc_claims(
            "pod", "prod", ("p-1",), {},
        ) == ()
        assert not called  # neither channel runs for out-of-family scopes


class TestDiscoverStatefulsetPvcClaims:
    """B79 STS channel: claims are per-replica PVC instances resolved via
    live pods, NEVER the volumeClaimTemplates names (ghost entries)."""

    @staticmethod
    def _make_transport(monkeypatch, selector: str, pods: str,
                        pod_claims: str):
        """Three-query chain: selector → pod names → per-pod claims."""
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            tokens = [str(t) for t in cmd]
            if any("selector.matchLabels" in t for t in tokens):
                return CommandResult(
                    exit_code=0, stdout=selector, stderr="",
                )
            if any("items[*].metadata.name" in t for t in tokens):
                return CommandResult(
                    exit_code=0, stdout=pods, stderr="",
                )
            if any("persistentVolumeClaim.claimName" in t for t in tokens):
                return CommandResult(
                    exit_code=0, stdout=pod_claims, stderr="",
                )
            return CommandResult(exit_code=0, stdout="", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )

    async def test_claims_come_from_live_pod_mounts(self, monkeypatch):
        """web-0/web-1 mount data-web-0/data-web-1 — the per-replica PVC
        names, not the template name ``data``."""
        from chaos_agent.agent.target_guard.freeze import (
            discover_statefulset_pvc_claims,
        )
        self._make_transport(
            monkeypatch,
            selector="map[app:web]",
            pods="web-0 web-1",
            pod_claims="data-web-0 data-web-1",
        )
        claims = await discover_statefulset_pvc_claims(
            "prod", ("web",), {},
        )
        assert claims == ("data-web-0", "data-web-1")

    async def test_ghost_entry_blocked_by_construction(self, monkeypatch):
        """No query ever reads volumeClaimTemplates — a template name can
        structurally never enter the frozen whitelist."""
        from chaos_agent.agent.target_guard.freeze import (
            discover_statefulset_pvc_claims,
        )
        seen: list[list[str]] = []

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            from chaos_agent.models.command_result import CommandResult
            tokens = [str(t) for t in cmd]
            seen.append(tokens)
            if any("selector.matchLabels" in t for t in tokens):
                return CommandResult(
                    exit_code=0, stdout="map[app:web]", stderr="",
                )
            if any("items[*].metadata.name" in t for t in tokens):
                return CommandResult(
                    exit_code=0, stdout="web-0", stderr="",
                )
            return CommandResult(
                exit_code=0, stdout="data-web-0", stderr="",
            )

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        claims = await discover_statefulset_pvc_claims(
            "prod", ("web",), {},
        )
        assert claims == ("data-web-0",)
        assert not any(
            "volumeClaimTemplates" in t for tokens in seen for t in tokens
        )

    async def test_scaled_to_zero_fails_closed(self, monkeypatch):
        """No live pods → no ground truth → empty (occupant stays banned —
        honest when the target genuinely has no mounted storage)."""
        from chaos_agent.agent.target_guard.freeze import (
            discover_statefulset_pvc_claims,
        )
        self._make_transport(
            monkeypatch,
            selector="map[app:web]",
            pods="",
            pod_claims="",
        )
        claims = await discover_statefulset_pvc_claims(
            "prod", ("web",), {},
        )
        assert claims == ()

    async def test_selector_parse_failure_fails_closed(self, monkeypatch):
        from chaos_agent.agent.target_guard.freeze import (
            discover_statefulset_pvc_claims,
        )
        self._make_transport(
            monkeypatch,
            selector="not-a-map",
            pods="web-0",
            pod_claims="data-web-0",
        )
        claims = await discover_statefulset_pvc_claims(
            "prod", ("web",), {},
        )
        assert claims == ()

    async def test_labels_form_resolves_sts_names_first(self, monkeypatch):
        """labels-only approval resolves the STS names via ``-l`` before
        the selector→pods→claims chain."""
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            tokens = [str(t) for t in cmd]
            if tokens[:1] == ["statefulset"] and "-l" in tokens:
                return CommandResult(
                    exit_code=0, stdout="web", stderr="",
                )
            if any("selector.matchLabels" in t for t in tokens):
                return CommandResult(
                    exit_code=0, stdout="map[app:web]", stderr="",
                )
            if any("items[*].metadata.name" in t for t in tokens):
                return CommandResult(
                    exit_code=0, stdout="web-0", stderr="",
                )
            return CommandResult(
                exit_code=0, stdout="data-web-0", stderr="",
            )

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        from chaos_agent.agent.target_guard.freeze import (
            discover_statefulset_pvc_claims,
        )
        claims = await discover_statefulset_pvc_claims(
            "prod", (), {"app": "web"},
        )
        assert claims == ("data-web-0",)


class TestJsonpathRenderSingleSource:
    """Line-1/2 teeth: every guard jsonpath read renders through ONE function.

    The B81 lesson generalised: the ``jsonpath=`` prefix was an assembly-
    site convention (six copies) until one site dropped it and the failed
    query silently froze an empty claim set. Now the prefix is a
    construction-level invariant of :func:`_jsonpath_get_args`, and these
    tests pin it at the UNIT level — cheaper and more exhaustive than the
    transport-level B81 test, which stays as the integration backstop.
    """

    def test_prefix_invariant_all_shapes(self):
        from chaos_agent.agent.target_guard.freeze import _jsonpath_get_args
        cases = [
            _jsonpath_get_args("pod", "{.spec.x}", namespace="ns", name="p"),
            _jsonpath_get_args("pods", "{.items[*].metadata.name}",
                               namespace="ns", labels={"app": "web"}),
            _jsonpath_get_args("nodes", "{.items[*].metadata.name}",
                               labels={"zone": "z1"}),
            _jsonpath_get_args("statefulset", "{.spec.selector.matchLabels}",
                               namespace="ns", name="web"),
            _jsonpath_get_args("deployment", "{.spec.template.spec.x}",
                               namespace="ns", labels={"a": "1"}, items=True),
            _jsonpath_get_args("pods", "{.items[*].metadata.name}",
                               namespace="ns", label_selector="app=web"),
        ]
        for args in cases:
            assert "-o" in args, args
            flag = args[args.index("-o") + 1]
            assert flag.startswith("jsonpath={"), args
            assert flag.endswith("}"), args

    def test_items_rewrites_first_brace(self):
        from chaos_agent.agent.target_guard.freeze import _jsonpath_get_args
        args = _jsonpath_get_args(
            "deployment", "{.spec.template.spec.volumes[*].x}",
            namespace="ns", labels={"app": "web"}, items=True,
        )
        flag = args[args.index("-o") + 1]
        assert flag == (
            "jsonpath={.items[*].spec.template.spec.volumes[*].x}"
        )

    def test_name_and_labels_and_selector_mutually_clean(self):
        from chaos_agent.agent.target_guard.freeze import _jsonpath_get_args
        # labels wins over a bare label_selector string (dict is the
        # structured form); name only present when given.
        a = _jsonpath_get_args(
            "pods", "{.items[*].metadata.name}",
            namespace="ns", labels={"app": "web"}, label_selector="x=y",
        )
        assert a[:2] == ["pods", "-n"]
        assert a[a.index("-l") + 1] == "app=web"

    def test_malformed_path_fails_loud(self):
        """A path without the leading ``{`` is a PROGRAM error (typo in a
        code constant) — it must raise, not silently freeze an empty set."""
        from chaos_agent.agent.target_guard.freeze import _jsonpath_get_args
        import pytest
        with pytest.raises(ValueError):
            _jsonpath_get_args("pod", "spec.volumes[*].x", namespace="ns")

    async def test_query_failure_surfaces_as_not_ok(self, monkeypatch):
        """B81 teeth, tri-state form: a failed query is ``ok=False`` with a
        diagnosis — never a healthy-looking empty string."""
        from chaos_agent.agent.target_guard.freeze import (
            discover_workload_pvc_claims,
        )
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            return CommandResult(
                exit_code=1, stdout="", stderr="unknown output format",
            )

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        claims = await discover_workload_pvc_claims(
            "deployment", "prod", ("web",), {},
        )
        assert claims == ()  # fail-closed decision unchanged
