"""Tests for ``chaos_agent.agent.target_guard.classifier``.

The classifier turns raw tool_call args into an ``EffectiveTarget``.
The test layout mirrors the major branches in ``classifier.py``:

  - kubectl subcommand dispatch (read-only / banned / destructive)
  - kubectl exec recursion (inner blade, inner kubectl, escape attempts)
  - blade_create dict-arg parsing
  - kind canonicalisation
  - namespace + label parsing (all 5 flag positions)
  - file/stdin ban
  - sentinel scope sentinels (READONLY / BANNED / UNKNOWN)
  - skill_script default ban
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.target_guard.classifier import (
    BLADE_TARGET_TO_SCOPE,
    SCOPE_BANNED,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    canonicalise_kind,
    infer_effective_target,
    parse_labels,
    parse_namespace,
)
from chaos_agent.agent.target_guard.types import ConfidenceLevel


# ---------------------------------------------------------------------------
# Kind canonicalisation
# ---------------------------------------------------------------------------


class TestCanonicaliseKind:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("pod", "pod"), ("pods", "pod"), ("po", "pod"), ("POD", "pod"),
            ("deploy", "deployment"), ("deployment", "deployment"),
            ("deployments", "deployment"), ("deployment.apps", "deployment"),
            ("deployment.v1.apps", "deployment"),
            ("svc", "service"), ("service", "service"), ("services", "service"),
            ("ns", "namespace"), ("namespace", "namespace"),
            ("ds", "daemonset"), ("sts", "statefulset"), ("rs", "replicaset"),
            ("hpa", "hpa"), ("cronjob", "cronjob"), ("cj", "cronjob"),
            ("cm", "configmap"), ("secret", "secret"),
            ("pvc", "pvc"), ("persistentvolumeclaim", "pvc"),
            ("storageclass", "storageclass"), ("sc", "storageclass"),
            ("no", "node"), ("nodes", "node"),
        ],
    )
    def test_canonical_form(self, raw, expected):
        assert canonicalise_kind(raw) == expected

    def test_empty_string(self):
        assert canonicalise_kind("") == ""

    def test_unknown_kind_returns_lowercased(self):
        # Unknown CRDs come through verbatim (lowercased, suffix
        # stripped) so the guard can still compare them character-wise.
        assert canonicalise_kind("MyCustomResource") == "mycustomresource"
        assert canonicalise_kind("Widget.example.com") == "widget"


# ---------------------------------------------------------------------------
# Namespace parsing — all 5 flag positions
# ---------------------------------------------------------------------------


class TestParseNamespace:
    def test_short_spaced(self):
        assert parse_namespace(["-n", "prod"]) == "prod"

    def test_short_equals(self):
        assert parse_namespace(["-n=prod"]) == "prod"

    def test_long_spaced(self):
        assert parse_namespace(["--namespace", "prod"]) == "prod"

    def test_long_equals(self):
        assert parse_namespace(["--namespace=prod"]) == "prod"

    def test_flag_before_subcommand(self):
        # parse_namespace scans the whole arg list, so it picks up
        # global-position flags too.
        assert parse_namespace(["--namespace", "prod", "scale", "deploy/x"]) == "prod"

    def test_flag_after_subcommand(self):
        assert parse_namespace(["scale", "deploy/x", "-n", "prod"]) == "prod"

    def test_missing_uses_default(self):
        assert parse_namespace(["get", "pods"]) == "default"
        assert parse_namespace([], default="default") == "default"

    def test_custom_default_for_cluster_scoped(self):
        # Caller passes default="" for cordon/node ops so missing ns
        # doesn't get auto-promoted to "default".
        assert parse_namespace(["cordon", "node1"], default="") == ""

    def test_malformed_short_flag_no_value(self):
        # `-n` with no following token → falls back to default
        assert parse_namespace(["-n"]) == "default"


# ---------------------------------------------------------------------------
# Label parsing
# ---------------------------------------------------------------------------


class TestParseLabels:
    def test_short_spaced(self):
        assert parse_labels(["-l", "app=demo"]) == {"app": "demo"}

    def test_long_spaced(self):
        assert parse_labels(["--selector", "app=demo"]) == {"app": "demo"}

    def test_equals_form(self):
        assert parse_labels(["-l=app=demo"]) == {"app": "demo"}
        assert parse_labels(["--selector=app=demo"]) == {"app": "demo"}

    def test_multi_pair(self):
        assert parse_labels(["-l", "app=demo,env=prod"]) == {
            "app": "demo", "env": "prod",
        }

    def test_operator_selector_preserved_verbatim(self):
        # `key!=val` and `key in (a,b)` aren't decomposed — guard
        # treats any non-equality difference as drift anyway.
        out = parse_labels(["-l", "app!=demo"])
        assert "app!=demo" in out

    def test_empty(self):
        assert parse_labels([]) == {}
        assert parse_labels(["get", "pods"]) == {}


# ---------------------------------------------------------------------------
# Top-level dispatch — read-only tools / banned tools / unknown
# ---------------------------------------------------------------------------


class TestKnownReadOnlyTools:
    @pytest.mark.parametrize(
        "tool",
        [
            "blade_status", "blade_query_k8s",
            "read_knowledge_resource", "read_skill_resource",
            "activate_skill", "submit_fault_intent",
            "kubectl_ro", "read_file", "save_fault_plan",
            "finish_planning",
        ],
    )
    def test_known_readonly_tools(self, tool):
        et = infer_effective_target(tool, {})
        assert et.scope == SCOPE_READONLY
        # raw_command is always populated for audit logs
        assert et.raw_command


class TestSkillScriptDefaultBan:
    def test_default_ban_no_opt_in(self):
        et = infer_effective_target("_execute_skill_script", {"path": "/foo"})
        assert et.scope == SCOPE_BANNED
        assert et.confidence == ConfidenceLevel.HIGH

    def test_opt_in_pass_through_as_readonly(self):
        # Opt-in flips the verdict from BANNED to READONLY so the guard
        # treats the call as pass-through. The script's k8s effect is
        # opaque to the classifier; the operator's flag is the trust
        # signal. Previously this returned UNKNOWN which still got
        # rejected — making the opt-in flag a no-op.
        et = infer_effective_target(
            "_execute_skill_script", {"path": "/foo"},
            skill_script_allowed=True,
        )
        assert et.scope == SCOPE_READONLY
        assert et.confidence == ConfidenceLevel.HIGH

    def test_legacy_name_also_banned(self):
        et = infer_effective_target("execute_skill_script", {"path": "/foo"})
        assert et.scope == SCOPE_BANNED


class TestUnknownTool:
    def test_unknown_tool_defaults_to_unknown(self):
        et = infer_effective_target("brand_new_mcp_tool", {"arg": 1})
        assert et.scope == SCOPE_UNKNOWN
        assert et.confidence == ConfidenceLevel.UNKNOWN


# ---------------------------------------------------------------------------
# blade_create dict-arg parsing
# ---------------------------------------------------------------------------


class TestContainerScopeMapsToPod:
    """Bug fix: ChaosBlade's ``scope=container`` must canonicalise to
    ``pod`` so the guard's pod-approval matches.

    Previously canonicalise_kind returned "container" verbatim, and
    _classify_blade_create rejected non-{pod,node} scopes by falling
    back to BLADE_TARGET_TO_SCOPE[target]. For ``container-cpu``
    that mapped to "node" (host CPU), causing a false-positive
    scope-drift rejection against any pod approval.
    """

    def test_canonicalise_container_to_pod(self):
        assert canonicalise_kind("container") == "pod"
        assert canonicalise_kind("containers") == "pod"
        assert canonicalise_kind("CONTAINER") == "pod"

    def test_blade_create_container_scope_resolves_to_pod(self):
        et = infer_effective_target("blade_create", {
            "scope": "container", "target": "cpu", "action": "fullload",
            "namespace": "ns", "names": ["p1"],
        })
        assert et.scope == "pod"
        assert et.namespace == "ns"

    def test_inline_blade_container_subtype_resolves_to_pod(self):
        # `kubectl exec POD -- blade create k8s container-cpu fullload`
        et = infer_effective_target("kubectl", [
            "exec", "p1", "-n", "ns", "--",
            "blade", "create", "k8s", "container-cpu", "fullload",
        ])
        assert et.scope == "pod"
        assert et.names == ("p1",)


class TestBladeCreate:
    def test_pod_cpu(self):
        et = infer_effective_target("blade_create", {
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "prod", "names": ["pod-a"],
        })
        assert et.scope == "pod"
        assert et.namespace == "prod"
        assert et.names == ("pod-a",)
        assert et.blade_target == "cpu"
        assert et.blade_action == "fullload"
        assert et.confidence == ConfidenceLevel.HIGH

    def test_node_cpu_host_mode(self):
        # blade target=cpu without explicit scope → BLADE_TARGET_TO_SCOPE
        # picks "node" (host blade burns CPU on the node).
        et = infer_effective_target("blade_create", {
            "target": "cpu", "action": "fullload",
        })
        assert et.scope == "node"
        # Cluster-scoped → namespace stays empty
        assert et.namespace == ""

    def test_namespace_defaulted(self):
        # Missing namespace on a namespace-scoped scope normalises
        # to "default" so guard comparison doesn't false-positive.
        et = infer_effective_target("blade_create", {
            "scope": "pod", "target": "jvm", "names": ["p1"],
        })
        assert et.namespace == "default"

    def test_names_as_csv_string(self):
        # Some callers pass names as a CSV string instead of a list.
        et = infer_effective_target("blade_create", {
            "scope": "pod", "target": "cpu", "names": "a,b,c",
            "namespace": "ns",
        })
        assert et.names == ("a", "b", "c")

    def test_labels_dict(self):
        et = infer_effective_target("blade_create", {
            "scope": "pod", "target": "cpu",
            "labels": {"app": "demo", "env": "prod"},
            "namespace": "ns",
        })
        assert et.labels == {"app": "demo", "env": "prod"}

    def test_labels_string(self):
        et = infer_effective_target("blade_create", {
            "scope": "pod", "target": "cpu",
            "labels": "app=demo,env=prod",
            "namespace": "ns",
        })
        assert et.labels == {"app": "demo", "env": "prod"}

    def test_no_target_unknown_scope(self):
        # No blade target, no scope → can't classify
        et = infer_effective_target("blade_create", {"action": "fullload"})
        assert et.scope == SCOPE_UNKNOWN

    def test_low_confidence_when_no_names_no_labels(self):
        # blade_create with scope/target but no names or labels can't
        # pin down a specific resource — LOW confidence.
        et = infer_effective_target("blade_create", {
            "scope": "pod", "target": "cpu", "namespace": "ns",
        })
        assert et.confidence == ConfidenceLevel.LOW


# ---------------------------------------------------------------------------
# kubectl read-only subcommands
# ---------------------------------------------------------------------------


class TestKubectlReadOnly:
    @pytest.mark.parametrize(
        "args",
        [
            ["get", "pods"],
            ["describe", "deploy/x"],
            ["top", "node"],
            ["logs", "pod-a"],
            ["events"],
            ["version"],
            ["api-resources"],
            ["explain", "pod"],
            ["wait", "--for=condition=Ready", "pod/x"],
            ["diff", "-f", "x.yaml"],
        ],
    )
    def test_known_readonly_subs(self, args):
        et = infer_effective_target("kubectl", args)
        assert et.scope == SCOPE_READONLY

    def test_rollout_status_is_readonly(self):
        et = infer_effective_target("kubectl", ["rollout", "status", "deploy/x"])
        assert et.scope == SCOPE_READONLY

    def test_rollout_history_is_readonly(self):
        et = infer_effective_target("kubectl", ["rollout", "history", "deploy/x"])
        assert et.scope == SCOPE_READONLY

    def test_config_view_is_readonly(self):
        et = infer_effective_target("kubectl", ["config", "view"])
        assert et.scope == SCOPE_READONLY


# ---------------------------------------------------------------------------
# kubectl banned subcommands
# ---------------------------------------------------------------------------


class TestKubectlBanned:
    def test_apply_always_banned(self):
        # apply without -f is rare; ban anyway because targets depend
        # on YAML content not in args.
        et = infer_effective_target("kubectl", ["apply", "-f", "x.yaml"])
        assert et.scope == SCOPE_BANNED

    def test_apply_no_args_unknown(self):
        # apply without -f or resource args → can't classify
        et = infer_effective_target("kubectl", ["apply"])
        assert et.scope == SCOPE_UNKNOWN

    def test_certificate_banned(self):
        et = infer_effective_target("kubectl", ["certificate", "approve", "csr-1"])
        assert et.scope == SCOPE_BANNED

    def test_config_write_banned(self):
        # set-context / set-credentials / use-context all mutate
        # kubeconfig itself — banned.
        et = infer_effective_target("kubectl", ["config", "set-context", "ctx"])
        assert et.scope == SCOPE_BANNED

    def test_proxy_banned(self):
        et = infer_effective_target("kubectl", ["proxy"])
        assert et.scope == SCOPE_BANNED

    def test_file_input_banned_for_destructive(self):
        # create/replace/patch/set/delete/edit + -f → banned because
        # we can't see the YAML content.
        for sub in ("create", "replace", "patch", "set", "delete", "edit"):
            et = infer_effective_target("kubectl", [sub, "-f", "x.yaml"])
            assert et.scope == SCOPE_BANNED, f"{sub} -f should be banned"

    def test_file_input_long_form_banned(self):
        et = infer_effective_target("kubectl", ["delete", "--filename", "x.yaml"])
        assert et.scope == SCOPE_BANNED


# ---------------------------------------------------------------------------
# kubectl destructive subcommands
# ---------------------------------------------------------------------------


class TestKubectlScale:
    def test_scale_with_slash_form(self):
        et = infer_effective_target("kubectl", [
            "scale", "deploy/myapp", "--replicas=0", "-n", "prod",
        ])
        assert et.scope == "deployment"
        assert et.namespace == "prod"
        assert et.names == ("myapp",)

    def test_scale_with_separate_form(self):
        et = infer_effective_target("kubectl", [
            "scale", "deployment", "myapp", "--replicas=3", "-n", "prod",
        ])
        assert et.scope == "deployment"
        assert et.names == ("myapp",)
        assert et.namespace == "prod"

    def test_scale_default_namespace(self):
        et = infer_effective_target("kubectl", ["scale", "deploy/x", "--replicas=0"])
        assert et.namespace == "default"


class TestKubectlNodeOps:
    @pytest.mark.parametrize("sub", ["cordon", "uncordon", "drain"])
    def test_node_ops(self, sub):
        et = infer_effective_target("kubectl", [sub, "node-1"])
        assert et.scope == "node"
        assert et.names == ("node-1",)
        assert et.namespace == ""  # cluster-scoped

    def test_taint(self):
        et = infer_effective_target("kubectl", [
            "taint", "nodes", "node-1", "key=value:NoSchedule",
        ])
        assert et.scope == "node"
        assert et.names == ("node-1",)

    def test_taint_with_short_kind(self):
        et = infer_effective_target("kubectl", [
            "taint", "no", "node-1", "key=value:NoSchedule",
        ])
        assert et.scope == "node"


class TestKubectlPatchSetDelete:
    @pytest.mark.parametrize("sub", ["patch", "set", "delete", "edit", "label", "annotate"])
    def test_basic_resource_op(self, sub):
        et = infer_effective_target("kubectl", [
            sub, "deploy/x", "-n", "prod",
        ])
        assert et.scope == "deployment"
        assert et.namespace == "prod"
        assert et.names == ("x",)

    def test_delete_by_label_selector(self):
        # delete pod -l app=x → labels-based selection, names empty
        et = infer_effective_target("kubectl", [
            "delete", "pod", "-l", "app=x", "-n", "prod",
        ])
        assert et.scope == "pod"
        assert et.labels == {"app": "x"}


class TestKubectlRun:
    def test_run_creates_pod(self):
        et = infer_effective_target("kubectl", [
            "run", "tester", "--image=busybox", "-n", "ns",
        ])
        assert et.scope == "pod"
        assert et.names == ("tester",)
        assert et.namespace == "ns"


class TestKubectlRollout:
    def test_rollout_restart_destructive(self):
        et = infer_effective_target("kubectl", [
            "rollout", "restart", "deploy/myapp", "-n", "prod",
        ])
        assert et.scope == "deployment"
        assert et.names == ("myapp",)


class TestKubectlCp:
    def test_cp_with_namespace_in_path(self):
        et = infer_effective_target("kubectl", [
            "cp", "prod/pod-a:/src", "/local",
        ])
        assert et.scope == "pod"
        assert et.namespace == "prod"
        assert et.names == ("pod-a",)

    def test_cp_with_flag_namespace(self):
        et = infer_effective_target("kubectl", [
            "cp", "pod-a:/src", "/local", "-n", "prod",
        ])
        assert et.scope == "pod"
        assert et.namespace == "prod"
        assert et.names == ("pod-a",)


class TestKubectlDebug:
    def test_debug_node(self):
        et = infer_effective_target("kubectl", [
            "debug", "node/node-1", "--image=busybox",
        ])
        assert et.scope == "node"
        assert et.names == ("node-1",)

    def test_debug_pod(self):
        et = infer_effective_target("kubectl", [
            "debug", "pod-a", "-n", "ns",
        ])
        assert et.scope == "pod"
        assert et.names == ("pod-a",)


# ---------------------------------------------------------------------------
# kubectl exec — recursive into inner command
# ---------------------------------------------------------------------------


class TestKubectlExec:
    def test_exec_plain_shell_acts_on_pod(self):
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "-n", "ns", "--", "ls", "-la",
        ])
        assert et.scope == "pod"
        assert et.names == ("pod-a",)
        assert et.namespace == "ns"

    def test_exec_no_inner_cmd_still_pod(self):
        # Pure stdio attach (no `-- cmd`) still acts on the pod
        et = infer_effective_target("kubectl", ["exec", "pod-a", "-n", "ns"])
        assert et.scope == "pod"
        assert et.names == ("pod-a",)

    def test_exec_blade_pod_cpu_recurses(self):
        # The most important case: kubectl exec POD -- blade create k8s pod-cpu
        # should RECURSE into the inner blade and report the inner
        # target. With no --names in inner, it inherits the host pod.
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "-n", "ns", "--",
            "blade", "create", "k8s", "pod-cpu", "fullload",
        ])
        assert et.scope == "pod"
        assert et.names == ("pod-a",)
        assert et.blade_target == "cpu"
        assert et.blade_action == "fullload"

    def test_exec_blade_NODE_cpu_escapes_to_node(self):
        # THE critical drift case: exec into approved pod-a but use
        # blade to act on a NODE. Classifier MUST detect this and
        # report scope=node so the guard can catch the escape.
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "-n", "ns", "--",
            "blade", "create", "k8s", "node-cpu", "fullload",
            "--node", "node-7",
        ])
        assert et.scope == "node"
        assert et.names == ("node-7",)
        assert et.namespace == ""  # node is cluster-scoped

    def test_exec_blade_with_explicit_names_flag(self):
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "-n", "ns", "--",
            "blade", "create", "k8s", "pod-cpu", "fullload",
            "--names", "pod-b,pod-c", "-n", "other-ns",
        ])
        assert et.scope == "pod"
        # Inner --names override the outer pod
        assert et.names == ("pod-b", "pod-c")
        assert et.namespace == "other-ns"

    def test_exec_nested_kubectl_inherits_outer_ns(self):
        # kubectl-inside-pod usually inherits the pod's ambient ns
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "-n", "ns", "--",
            "kubectl", "scale", "deploy/x", "--replicas=0",
        ])
        assert et.scope == "deployment"
        assert et.namespace == "ns"
        assert et.names == ("x",)
        # Nested → LOW confidence
        assert et.confidence == ConfidenceLevel.LOW

    @pytest.mark.parametrize("escape_cmd", ["nsenter", "chroot", "unshare"])
    def test_exec_escape_attempts_are_unknown(self, escape_cmd):
        # nsenter / chroot / unshare break out of container — we can't
        # tell what host or path they'd land on. Default-deny.
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "--", escape_cmd, "-t", "1", "-m", "bash",
        ])
        assert et.scope == SCOPE_UNKNOWN
        assert et.confidence == ConfidenceLevel.UNKNOWN

    def test_exec_no_pod_name_unknown(self):
        et = infer_effective_target("kubectl", ["exec", "--", "ls"])
        assert et.scope == SCOPE_UNKNOWN

    def test_exec_tier1_tool_pod_no_namespace_in_blade(self):
        """Tier 1: kubectl exec into chaosblade tool pod, inner blade
        omits --namespace (blade v1.8.0 rejects it for pod-network).
        Classifier must set is_tier1_exec=True and NOT default ns to
        'default'."""
        et = infer_effective_target("kubectl", [
            "exec", "otel-c-tool-5pmkc", "-n", "chaosblade", "--",
            "blade", "create", "k8s", "pod-network", "drop",
            "--names", "accounting-6fbdb464c7-qn2vr",
            "--percent", "100", "--interface", "eth0",
        ])
        assert et.scope == "pod"
        assert et.names == ("accounting-6fbdb464c7-qn2vr",)
        assert et.is_tier1_exec is True
        assert et.namespace == ""  # NOT "default"

    def test_exec_tier1_tool_pod_with_namespace_in_blade(self):
        """When inner blade DOES specify --namespace, tier1 is False
        (namespace is explicitly provided, normal comparison applies)."""
        et = infer_effective_target("kubectl", [
            "exec", "otel-c-tool-5pmkc", "-n", "chaosblade", "--",
            "blade", "create", "k8s", "pod-cpu", "fullload",
            "--names", "accounting-6fbdb464c7-qn2vr",
            "--namespace", "cms-demo",
        ])
        assert et.scope == "pod"
        assert et.namespace == "cms-demo"
        assert et.is_tier1_exec is False

    def test_exec_non_tool_ns_no_namespace_defaults_normally(self):
        """When outer exec is NOT into chaosblade ns, standard defaulting
        applies (ns defaults to 'default' when --namespace absent)."""
        et = infer_effective_target("kubectl", [
            "exec", "some-pod", "-n", "app-ns", "--",
            "blade", "create", "k8s", "pod-cpu", "fullload",
        ])
        assert et.scope == "pod"
        assert et.namespace == "default"
        assert et.is_tier1_exec is False


# ---------------------------------------------------------------------------
# Help flag → READONLY
# ---------------------------------------------------------------------------


class TestHelpFlagReadonly:
    """Commands with -h/--help should classify as READONLY."""

    def test_exec_inner_blade_create_drop_help(self):
        et = infer_effective_target("kubectl", [
            "exec", "otel-c-tool-5pmkc", "-n", "chaosblade", "--",
            "blade", "create", "k8s", "pod-network", "drop", "-h",
        ])
        assert et.scope == SCOPE_READONLY

    def test_exec_inner_blade_short_help(self):
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "-n", "ns", "--",
            "blade", "-h",
        ])
        assert et.scope == SCOPE_READONLY

    def test_exec_inner_blade_long_help(self):
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "-n", "ns", "--",
            "blade", "create", "--help",
        ])
        assert et.scope == SCOPE_READONLY

    def test_exec_inner_blade_subtype_help(self):
        et = infer_effective_target("kubectl", [
            "exec", "pod-a", "-n", "ns", "--",
            "blade", "create", "k8s", "pod-network", "-h",
        ])
        assert et.scope == SCOPE_READONLY

    def test_kubectl_exec_help(self):
        et = infer_effective_target("kubectl", ["exec", "-h"])
        assert et.scope == SCOPE_READONLY

    def test_kubectl_delete_help(self):
        et = infer_effective_target("kubectl", ["delete", "--help"])
        assert et.scope == SCOPE_READONLY

    def test_kubectl_scale_help(self):
        et = infer_effective_target("kubectl", ["scale", "-h"])
        assert et.scope == SCOPE_READONLY

    def test_blade_help_tool_readonly(self):
        et = infer_effective_target("blade_help", {"subcommand": "create k8s pod-network drop"})
        assert et.scope == SCOPE_READONLY

    def test_blade_help_tool_empty_subcommand(self):
        et = infer_effective_target("blade_help", {"subcommand": ""})
        assert et.scope == SCOPE_READONLY


# ---------------------------------------------------------------------------
# Non-create blade inside exec → READONLY
# ---------------------------------------------------------------------------


class TestBladeNonCreateReadonly:
    """blade status/destroy/query inside kubectl exec should be READONLY."""

    def test_blade_status_inside_exec_is_readonly(self):
        et = infer_effective_target("kubectl", [
            "exec", "otel-c-tool-5pmkc", "-n", "chaosblade", "--",
            "blade", "status", "98f70a1b2c3d4e5f",
        ])
        assert et.scope == SCOPE_READONLY

    def test_blade_destroy_inside_exec_is_readonly(self):
        et = infer_effective_target("kubectl", [
            "exec", "otel-c-tool-5pmkc", "-n", "chaosblade", "--",
            "blade", "destroy", "98f70a1b2c3d4e5f",
        ])
        assert et.scope == SCOPE_READONLY

    def test_blade_create_with_labels_no_fallback_pod_name(self):
        """When --labels is specified, effective_names should be empty
        (not the tool pod name)."""
        et = infer_effective_target("kubectl", [
            "exec", "otel-c-tool-5pmkc", "-n", "chaosblade", "--",
            "blade", "create", "k8s", "pod-network", "drop",
            "--labels", "app.kubernetes.io/name=accounting",
            "--namespace", "cms-demo",
        ])
        assert et.scope == "pod"
        assert et.namespace == "cms-demo"
        assert et.names == ()
        assert et.labels == {"app.kubernetes.io/name": "accounting"}


# ---------------------------------------------------------------------------
# blade target → scope mapping
# ---------------------------------------------------------------------------


class TestBladeTargetMapping:
    def test_pod_attached_targets_resolve_to_pod(self):
        # container/jvm/mysql/redis/kafka/etc. all live inside a pod
        for t in ("container", "jvm", "mysql", "redis", "kafka", "nginx", "rocketmq"):
            assert BLADE_TARGET_TO_SCOPE[t] == "pod"

    def test_host_targets_resolve_to_node(self):
        # cpu/mem/disk/network without k8s prefix → host = node
        for t in ("cpu", "mem", "memory", "disk", "network", "process",
                  "file", "script", "time", "kernel"):
            assert BLADE_TARGET_TO_SCOPE[t] == "node"


# ---------------------------------------------------------------------------
# Global-flag skipping in kubectl arg parsing
# ---------------------------------------------------------------------------


class TestGlobalFlagSkipping:
    def test_global_kubeconfig_flag_skipped(self):
        et = infer_effective_target("kubectl", [
            "--kubeconfig", "/tmp/kc", "scale", "deploy/x",
            "--replicas=0", "-n", "prod",
        ])
        assert et.scope == "deployment"
        assert et.names == ("x",)

    def test_global_context_flag_skipped(self):
        et = infer_effective_target("kubectl", [
            "--context", "prod", "get", "pods",
        ])
        assert et.scope == SCOPE_READONLY

    def test_global_namespace_picked_up(self):
        # `--namespace=foo scale deploy/x` — global ns is still
        # recognised by parse_namespace (it scans whole arg list).
        et = infer_effective_target("kubectl", [
            "--namespace=foo", "scale", "deploy/x", "--replicas=0",
        ])
        assert et.namespace == "foo"


# ---------------------------------------------------------------------------
# Args coercion — dict vs list vs string inputs
# ---------------------------------------------------------------------------


class TestParseStopsAtDoubleDash:
    """Bug fix: parse_namespace / parse_labels must stop at ``--`` so
    inner ``kubectl exec`` program flags don't leak into the outer
    kubectl's parse."""

    def test_parse_namespace_stops_at_double_dash(self):
        # Outer kubectl has no -n; inner program has -n inner-ns.
        # Without the stop, parse_namespace would return "inner-ns".
        ns = parse_namespace(["POD", "--", "prog", "-n", "inner-ns"])
        assert ns == "default"

    def test_parse_namespace_picks_outer_before_double_dash(self):
        # Outer DOES have -n; inner also has -n. Outer wins.
        ns = parse_namespace([
            "POD", "-n", "outer", "--", "prog", "-n", "inner",
        ])
        assert ns == "outer"

    def test_parse_labels_stops_at_double_dash(self):
        # Outer kubectl has no -l; inner program has -l x=y.
        # parse_labels must not pick up the inner flag.
        labels = parse_labels(["POD", "--", "prog", "-l", "x=y"])
        assert labels == {}


class TestExecGlobalNamespaceNotLeaked:
    """Bug fix: global --namespace before ``kubectl exec`` no longer
    gets shadowed by an inner program's ``-n`` flag."""

    def test_global_ns_with_inner_n_flag(self):
        # Global --namespace=foo BEFORE the subcommand, exec with
        # inner program that has its own -n. Previously the inner -n
        # leaked into parse_namespace and produced ns=inner.
        et = infer_effective_target("kubectl", [
            "--namespace=foo", "exec", "pod-a", "--",
            "prog", "-n", "inner-ns",
        ])
        assert et.scope == "pod"
        assert et.namespace == "foo"
        assert et.names == ("pod-a",)


class TestFirstPositionalSkipsBooleanFlags:
    """Bug fix: ``_first_positional`` and ``_find_subcommand_index``
    must recognise kubectl boolean flags (``--all`` / ``-A`` /
    ``--force`` / ...) so they don't consume the next positional."""

    def test_delete_with_all_flag(self):
        # `kubectl delete --all pod -n ns` — without the boolean-flag
        # set, --all would consume "pod" and the classifier would miss
        # the kind.
        et = infer_effective_target("kubectl", [
            "delete", "--all", "pod", "-n", "ns",
        ])
        assert et.scope == "pod"
        assert et.namespace == "ns"

    def test_delete_with_force_flag(self):
        et = infer_effective_target("kubectl", [
            "delete", "--force", "pod/my-pod", "-n", "ns",
        ])
        assert et.scope == "pod"
        assert et.names == ("my-pod",)

    def test_scale_with_recursive_flag(self):
        et = infer_effective_target("kubectl", [
            "scale", "-R", "deploy/myapp", "--replicas=0", "-n", "ns",
        ])
        assert et.scope == "deployment"
        assert et.names == ("myapp",)

    def test_global_short_A_flag_skipped(self):
        # `-A` (--all-namespaces) at global position. Without boolean
        # recognition, -A would consume "get" as its value and the
        # subcommand search would fail.
        et = infer_effective_target("kubectl", ["-A", "get", "pods"])
        assert et.scope == SCOPE_READONLY


class TestKindNameDisambiguation:
    """Bug fix: when ``default_kind`` is given and there's exactly one
    positional that happens to be a known kind keyword, prefer the
    name interpretation. Without this rule, a pod literally named
    "pod" (or any kind keyword) would be misclassified as kind=pod
    name=""."""

    def test_attach_with_pod_named_pod(self):
        # `kubectl attach pod -n ns` where "pod" is the pod's name.
        # default_kind="pod" + len(positionals)==1 → kind="pod",
        # name="pod". Previously the classifier saw "pod" as a kind
        # keyword and returned name="".
        et = infer_effective_target("kubectl", ["attach", "pod", "-n", "ns"])
        assert et.scope == "pod"
        assert et.names == ("pod",)

    def test_attach_with_uppercase_kind_collision(self):
        et = infer_effective_target("kubectl", ["attach", "POD", "-n", "ns"])
        assert et.scope == "pod"
        assert et.names == ("POD",)

    def test_attach_pod_with_explicit_kind_keeps_two_positional_form(self):
        # When the user DOES write "attach pod my-name" explicitly,
        # we still treat it as kind="pod", name="my-name".
        et = infer_effective_target("kubectl", [
            "attach", "pod", "my-name", "-n", "ns",
        ])
        assert et.scope == "pod"
        assert et.names == ("my-name",)

    def test_scale_deployment_separate_form_still_works(self):
        # Regression check: scale doesn't use default_kind, so the
        # KIND NAME form still works.
        et = infer_effective_target("kubectl", [
            "scale", "deployment", "myapp", "--replicas=0", "-n", "ns",
        ])
        assert et.scope == "deployment"
        assert et.names == ("myapp",)


class TestArgsCoercion:
    def test_kubectl_args_as_list(self):
        et = infer_effective_target("kubectl", ["get", "pods"])
        assert et.scope == SCOPE_READONLY

    def test_kubectl_args_as_dict_command_key(self):
        et = infer_effective_target("kubectl", {"command": ["get", "pods"]})
        assert et.scope == SCOPE_READONLY

    def test_kubectl_args_as_dict_args_key(self):
        et = infer_effective_target("kubectl", {"args": ["get", "pods"]})
        assert et.scope == SCOPE_READONLY

    def test_kubectl_args_as_shell_string(self):
        et = infer_effective_target("kubectl", "get pods -n prod")
        assert et.scope == SCOPE_READONLY

    def test_kubectl_args_as_dict_string_value(self):
        et = infer_effective_target("kubectl", {"command": "get pods"})
        assert et.scope == SCOPE_READONLY

    def test_kubectl_empty_args(self):
        et = infer_effective_target("kubectl", [])
        assert et.scope == SCOPE_UNKNOWN


class TestProductionKubectlShape:
    """The chaos_agent kubectl tool's actual schema is
    ``{subcommand: str, v_args: str, kubeconfig?, context?, cluster?}``.

    These tests pin the classifier against the SHAPE the LLM actually
    emits in production — without them, screener verdicts in real
    traffic would diverge from what the unit tests promise.
    """

    def test_subcommand_plus_v_args_read_only(self):
        et = infer_effective_target("kubectl", {
            "subcommand": "get",
            "v_args": "pods -n prod",
        })
        assert et.scope == SCOPE_READONLY

    def test_subcommand_plus_v_args_scale(self):
        et = infer_effective_target("kubectl", {
            "subcommand": "scale",
            "v_args": "deploy/myapp --replicas=0 -n prod",
        })
        assert et.scope == "deployment"
        assert et.namespace == "prod"
        assert et.names == ("myapp",)

    def test_subcommand_plus_v_args_exec_with_inner_blade(self):
        # The most important real-world drift case: kubectl exec POD --
        # blade create k8s node-cpu fullload --node node-7 (escape).
        et = infer_effective_target("kubectl", {
            "subcommand": "exec",
            "v_args": "pod-a -n ns -- blade create k8s node-cpu fullload --node node-7",
        })
        assert et.scope == "node"
        assert et.names == ("node-7",)

    def test_subcommand_plus_v_args_delete_with_force(self):
        # Production shape + boolean flag combo
        et = infer_effective_target("kubectl", {
            "subcommand": "delete",
            "v_args": "--force pod/my-pod -n ns",
        })
        assert et.scope == "pod"
        assert et.names == ("my-pod",)

    def test_subcommand_plus_v_args_ignores_kubeconfig_field(self):
        # kubeconfig / context / cluster select the cluster, not the
        # target resource — they must NOT influence classification.
        et = infer_effective_target("kubectl", {
            "subcommand": "get",
            "v_args": "pods",
            "kubeconfig": "/tmp/kc",
            "context": "prod",
            "cluster": "prod-cluster",
        })
        assert et.scope == SCOPE_READONLY

    def test_subcommand_only_no_v_args(self):
        et = infer_effective_target("kubectl", {
            "subcommand": "version",
        })
        assert et.scope == SCOPE_READONLY

    def test_subcommand_apply_still_banned(self):
        et = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f x.yaml",
        })
        assert et.scope == SCOPE_BANNED

    def test_subcommand_empty_v_args(self):
        # Boundary: subcommand alone with empty v_args still classifies.
        et = infer_effective_target("kubectl", {
            "subcommand": "cordon",
            "v_args": "",
        })
        # cordon with no node name is malformed, returns UNKNOWN.
        assert et.scope == SCOPE_UNKNOWN
