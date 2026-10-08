"""Task 4.5 — class ↔ probe-form consistency gate + label-only derivation.

Anchors the member-level mapping introduced by baseline-observation-contract:

* ``_command_class_shape_reason`` — the four-class closed enum × syntactic
  form matrix (each class names the forms it accepts; every other form is
  a mismatch with a stated reason).
* ``_derive_command_class_default`` — LABEL-ONLY inference for commands
  that arrive without a class field (old outputs, registry templates).
  Never rejects.
* ``_validate_and_filter_commands`` wiring — declared class runs the
  consistency gate, absent class runs derivation, mismatches surface
  through the Case #63 reason channel.

Case #61 is the motivating regression: a failed ``container_internal``
exec was replaced with an ``api_object`` ``kubectl get pods -o
jsonpath=...`` — the replacement passed the whitelist and returned exit
0, so the receipt said ``7/7`` while the container dimension went
unmeasured. The gate below rejects that substitution shape by
construction.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.nodes.baseline._llm_derive import (
    _command_class_shape_reason,
    _derive_command_class_default,
    _validate_and_filter_commands,
)


# ---------------------------------------------------------------------------
# Predicate: _command_class_shape_reason (member-level per class)
# ---------------------------------------------------------------------------


class TestContainerInternalShapeGate:
    """container_internal ↔ ``kubectl exec <pod>`` (non-debug-pod)."""

    @pytest.mark.parametrize("cmd,mode", [
        ("kubectl exec my-pod -n ns -- df -h", "simple"),
        ("kubectl exec {target_pod} -n ns -- df -h", "simple"),
        ("kubectl exec deploy/my-app -n ns -- ps aux", "simple"),
        ("kubectl exec my-pod -n ns -c my-container -- ls /tmp", "simple"),
    ])
    def test_legal_forms_pass(self, cmd, mode):
        assert _command_class_shape_reason(cmd, mode, "container_internal", "k8s") is None

    @pytest.mark.parametrize("cmd,mode,reason_contains", [
        # Not exec at all — api_object shape declared as container_internal.
        ("kubectl get pod my-pod -n ns", "simple", "kubectl exec"),
        ("kubectl describe pod my-pod -n ns", "simple", "kubectl exec"),
        ("kubectl top pod my-pod -n ns", "simple", "kubectl exec"),
        # Debug-pod escape is node_level, not container_internal.
        ("kubectl exec {debug_pod} -n chaosblade -- df -h", "debug_two_step",
         "debug pod"),
    ])
    def test_illegal_forms_rejected_with_reason(self, cmd, mode, reason_contains):
        reason = _command_class_shape_reason(cmd, mode, "container_internal", "k8s")
        assert reason is not None
        assert reason_contains in reason

    def test_selector_form_exec_is_shape_gate_responsibility_not_class_gate(self):
        """Boundary anchor: the Case #61 selector form ``kubectl exec -l
        app=... -- id`` is rejected UPSTREAM by ``kubectl_exec_target_form_reason``
        (task 1.1) inside ``validate_command_with_reason``, never reaching
        the class gate. The class predicate stays orthogonal — it judges
        class↔subcommand consistency, not selector-flag legality (single-
        source discipline: one predicate, one job).
        """
        from chaos_agent.agent.nodes.baseline._baseline_profiles import (
            validate_command_with_reason,
        )
        cmd = "kubectl exec -l app=my-app -n ns -- id"
        # Upstream gate rejects (form reason, mentions selector).
        upstream_reason = validate_command_with_reason(cmd, "k8s")
        assert upstream_reason is not None
        assert "selector" in upstream_reason
        # Class gate, called in isolation, does NOT duplicate the shape
        # judgement — subcommand is ``exec`` and no debug pod, so the
        # class↔form mapping is satisfied. This is intentional: the
        # class gate runs only after validate_command_with_reason passes.
        assert _command_class_shape_reason(
            cmd, "simple", "container_internal", "k8s",
        ) is None


class TestApiObjectShapeGate:
    """api_object ↔ ``kubectl get|describe|top`` against a NON-node kind."""

    @pytest.mark.parametrize("cmd", [
        "kubectl get pod my-pod -n ns",
        "kubectl get pods -n ns -o wide",
        "kubectl describe deployment my-deploy -n ns",
        "kubectl top pod my-pod -n ns",
        "kubectl get endpoints my-svc -n ns",
        "kubectl get svc -n ns",
        "kubectl describe statefulset my-sts -n ns",
    ])
    def test_legal_forms_pass(self, cmd):
        assert _command_class_shape_reason(cmd, "simple", "api_object", "k8s") is None

    @pytest.mark.parametrize("cmd,reason_contains", [
        # Node-scoped API read belongs to node_level.
        ("kubectl describe node my-node", "node_level"),
        ("kubectl top node my-node", "node_level"),
        ("kubectl get nodes", "node_level"),
        ("kubectl get pods -A --field-selector spec.nodeName=my-node", "node_level"),
        # exec form is container_internal or node_level, not api_object.
        ("kubectl exec my-pod -n ns -- df -h", "get|describe|top"),
    ])
    def test_illegal_forms_rejected_with_reason(self, cmd, reason_contains):
        reason = _command_class_shape_reason(cmd, "simple", "api_object", "k8s")
        assert reason is not None
        assert reason_contains in reason


class TestNodeLevelShapeGate:
    """node_level ↔ node-kind API read OR debug-pod escape."""

    @pytest.mark.parametrize("cmd,mode", [
        ("kubectl describe node my-node", "simple"),
        ("kubectl top node my-node", "simple"),
        ("kubectl get nodes", "simple"),
        ("kubectl get node my-node -o wide", "simple"),
        ("kubectl get pods -A --field-selector spec.nodeName=my-node", "simple"),
        ("kubectl exec {debug_pod} -n chaosblade -- df -h", "debug_two_step"),
        ("kubectl exec {debug_pod} -n chaosblade -- cat /proc/diskstats",
         "debug_two_step"),
        ("kubectl exec {debug_pod} -n chaosblade -- nsenter -t 1 -m -u -i -n -p "
         "-- iostat -xd 1 3", "debug_two_step"),
    ])
    def test_legal_forms_pass(self, cmd, mode):
        assert _command_class_shape_reason(cmd, mode, "node_level", "k8s") is None

    @pytest.mark.parametrize("cmd,mode,reason_contains", [
        # Pod-scoped API read is api_object, not node_level.
        ("kubectl get pod my-pod -n ns", "simple", "describe/top node"),
        ("kubectl describe deployment my-deploy -n ns", "simple", "describe/top node"),
        # Container exec (non-debug-pod) is container_internal.
        ("kubectl exec my-pod -n ns -- df -h", "simple", "describe/top node"),
    ])
    def test_illegal_forms_rejected_with_reason(self, cmd, mode, reason_contains):
        reason = _command_class_shape_reason(cmd, mode, "node_level", "k8s")
        assert reason is not None
        assert reason_contains in reason


class TestHostLevelShapeGate:
    """host_level ↔ host profile (any shape — profile is the discriminator)."""

    @pytest.mark.parametrize("cmd", [
        "df -h",
        "top -bn1",
        "free -m",
        "iostat -xd 1 2",
        "cat /proc/meminfo",
    ])
    def test_legal_on_host_profile(self, cmd):
        assert _command_class_shape_reason(cmd, "simple", "host_level", "host") is None

    @pytest.mark.parametrize("cmd", [
        "kubectl get pod my-pod -n ns",
        "kubectl exec my-pod -n ns -- df -h",
        "kubectl describe node my-node",
    ])
    def test_rejected_on_k8s_profile(self, cmd):
        """host_level is out-of-domain on the k8s profile — the enum says
        it names a bare-host observation, not a kubectl one."""
        reason = _command_class_shape_reason(cmd, "simple", "host_level", "k8s")
        assert reason is not None
        assert "host_level" in reason
        assert "k8s" in reason


class TestHostProfileRejectsK8sClasses:
    """On the host profile, only host_level is admissible — any k8s-flavored
    class declaration is out-of-domain even if the command itself is a valid
    host diagnostic.
    """

    @pytest.mark.parametrize("declared", [
        "container_internal", "api_object", "node_level",
    ])
    def test_k8s_classes_rejected_on_host_profile(self, declared):
        reason = _command_class_shape_reason("df -h", "simple", declared, "host")
        assert reason is not None
        assert "host profile" in reason or "host_level" in reason


class TestClosedEnumRejection:
    """Unknown class values are rejected with the enum surfaced — the LLM
    gets an actionable reason (name the legal set), not a bare 'invalid'."""

    @pytest.mark.parametrize("declared", [
        "garbage_class", "Container_Internal", "API_OBJECT", "", "pod_level",
    ])
    def test_unknown_class_rejected(self, declared):
        reason = _command_class_shape_reason(
            "kubectl get pod x -n ns", "simple", declared, "k8s",
        )
        assert reason is not None
        assert "closed enum" in reason
        # Every legal value is named so the LLM can correct on retry.
        for legal in ("container_internal", "api_object", "node_level", "host_level"):
            assert legal in reason


class TestPredicateSelfConsistency:
    """The predicate is a pure function — repeated calls with the same
    input return the same verdict (no caller-order dependence, per the
    readonly.py predicate discipline).
    """

    def test_same_input_same_verdict(self):
        args = ("kubectl get pod x -n ns", "simple", "container_internal", "k8s")
        first = _command_class_shape_reason(*args)
        second = _command_class_shape_reason(*args)
        assert first == second
        assert first is not None  # and it is a mismatch, not a pass


# ---------------------------------------------------------------------------
# Predicate: _derive_command_class_default (label-only, never rejects)
# ---------------------------------------------------------------------------


class TestDefaultDerivationBranches:
    """The three branches the spec calls out: no field / empty field /
    registry template — all fall through to derivation, and derivation
    never rejects.
    """

    def test_host_profile_always_host_level(self):
        # Any host command derives to host_level.
        for cmd in ("df -h", "top -bn1", "free -m", "cat /proc/meminfo"):
            assert _derive_command_class_default(cmd, "simple", "host") == "host_level"

    def test_k8s_exec_debug_pod_derives_node_level(self):
        assert _derive_command_class_default(
            "kubectl exec {debug_pod} -n chaosblade -- df -h",
            "debug_two_step", "k8s",
        ) == "node_level"

    def test_k8s_exec_debug_pod_derives_node_level_even_with_wrong_mode(self):
        # ``{debug_pod}`` presence alone is enough — a stale ``mode`` field
        # must not flip the derivation.
        assert _derive_command_class_default(
            "kubectl exec {debug_pod} -n chaosblade -- df -h",
            "simple", "k8s",
        ) == "node_level"

    def test_k8s_exec_non_debug_derives_container_internal(self):
        assert _derive_command_class_default(
            "kubectl exec my-pod -n ns -- df -h", "simple", "k8s",
        ) == "container_internal"

    def test_k8s_exec_target_pod_placeholder_derives_container_internal(self):
        # ``{target_pod}`` is the container probe placeholder (task 3);
        # it derives to container_internal, not node_level.
        assert _derive_command_class_default(
            "kubectl exec {target_pod} -n ns -- df -h", "simple", "k8s",
        ) == "container_internal"

    @pytest.mark.parametrize("cmd", [
        "kubectl describe node my-node",
        "kubectl top node my-node",
        "kubectl get nodes",
        "kubectl get node my-node -o wide",
    ])
    def test_k8s_node_kind_api_read_derives_node_level(self, cmd):
        assert _derive_command_class_default(cmd, "simple", "k8s") == "node_level"

    def test_k8s_node_scoped_field_selector_derives_node_level(self):
        assert _derive_command_class_default(
            "kubectl get pods -A --field-selector spec.nodeName=my-node",
            "simple", "k8s",
        ) == "node_level"

    @pytest.mark.parametrize("cmd", [
        "kubectl get pod my-pod -n ns",
        "kubectl get pods -n ns -o wide",
        "kubectl describe deployment my-deploy -n ns",
        "kubectl top pod my-pod -n ns",
        "kubectl get endpoints my-svc -n ns",
    ])
    def test_k8s_non_node_api_read_derives_api_object(self, cmd):
        assert _derive_command_class_default(cmd, "simple", "k8s") == "api_object"

    def test_unparseable_command_returns_none_not_raise(self):
        # Defensive: shlex failure yields None (label-only), never raises.
        assert _derive_command_class_default(
            "kubectl get pod 'unterminated", "simple", "k8s",
        ) is None

    def test_unknown_profile_returns_none(self):
        # A profile the derivation doesn't recognize yields None, not a
        # fabricated label.
        assert _derive_command_class_default(
            "kubectl get pod x", "simple", "cloud",
        ) is None


# ---------------------------------------------------------------------------
# Wiring: _validate_and_filter_commands
# ---------------------------------------------------------------------------


class TestValidateAndFilterClassWiring:
    """End-to-end wiring: declared class runs the consistency gate, absent
    class runs derivation, mismatches surface through the reason channel.
    """

    def test_declared_class_matching_form_accepted_with_tag(self):
        cmds = [{
            "description": "container fs",
            "command": "kubectl exec my-pod -n ns -- df -h",
            "mode": "simple",
            "class": "container_internal",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert len(accepted) == 1
        assert rejected == []
        assert accepted[0].class_value == "container_internal"

    def test_declared_class_mismatch_rejected_with_reason(self):
        """Case #61 replacement shape: an ``api_object`` command declared
        as ``container_internal`` — the gate must refuse it, and the
        reason must surface (not just log).
        """
        cmds = [{
            "description": "pod listing",
            "command": "kubectl get pods -n ns -o jsonpath={.items[0].metadata.name}",
            "mode": "simple",
            "class": "container_internal",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert accepted == []
        assert len(rejected) == 1
        cmd, reason = rejected[0]
        assert cmd.startswith("kubectl get pods")
        assert reason
        # Reason names the mismatch: declares class + expected form.
        assert "container_internal" in reason
        assert "kubectl exec" in reason

    def test_absent_class_derives_and_stamps(self):
        """Old output shape (no class field) → derived, not rejected."""
        cmds = [{
            "description": "pod listing",
            "command": "kubectl get pods -n ns",
            "mode": "simple",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert len(accepted) == 1
        assert rejected == []
        assert accepted[0].class_value == "api_object"

    def test_empty_string_class_derives_and_stamps(self):
        """Empty string is treated like absent (falsy) — derived, not
        rejected as unknown-enum.
        """
        cmds = [{
            "description": "node describe",
            "command": "kubectl describe node my-node",
            "mode": "simple",
            "class": "",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert len(accepted) == 1
        assert rejected == []
        assert accepted[0].class_value == "node_level"

    def test_none_class_derives_and_stamps(self):
        cmds = [{
            "description": "container fs",
            "command": "kubectl exec my-pod -n ns -- df -h",
            "mode": "simple",
            "class": None,
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert len(accepted) == 1
        assert accepted[0].class_value == "container_internal"

    def test_non_string_class_rejected(self):
        """A non-string ``class`` value is a schema violation — rejected
        with a typed reason (not silently coerced)."""
        cmds = [{
            "description": "x",
            "command": "kubectl get pod x -n ns",
            "mode": "simple",
            "class": 42,
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert accepted == []
        assert len(rejected) == 1
        assert "must be a string" in rejected[0][1]

    def test_unknown_class_value_rejected_with_enum_surfaced(self):
        cmds = [{
            "description": "x",
            "command": "kubectl get pod x -n ns",
            "mode": "simple",
            "class": "not_a_real_class",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert accepted == []
        assert len(rejected) == 1
        assert "closed enum" in rejected[0][1]

    def test_registry_shape_commands_pass_through_derivation(self):
        """Registry-sourced command shapes (which arrive without a class
        field, since ``BaselineCommand`` defaults ``class_value`` to
        None) must survive the derive path when a test / caller feeds
        them through this filter — derivation is label-only.
        """
        cmds = [
            {"description": "Pod status", "command": "kubectl get pod x -n ns"},
            {"description": "Pod events", "command": "kubectl describe pod x -n ns"},
            {"description": "Pod CPU/Memory", "command": "kubectl top pod x -n ns"},
        ]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert len(accepted) == 3
        assert rejected == []
        assert {c.class_value for c in accepted} == {"api_object"}

    def test_debug_two_step_mode_autocorrect_then_class_gate(self):
        """The mode auto-correction runs BEFORE the class gate, so a
        ``{debug_pod}`` command with a stale ``mode=simple`` still
        derives / validates as node_level."""
        cmds = [{
            "description": "node disk",
            "command": "kubectl exec {debug_pod} -n chaosblade -- df -h",
            "mode": "simple",  # will be auto-corrected to debug_two_step
            "class": "node_level",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert len(accepted) == 1
        assert rejected == []
        assert accepted[0].mode == "debug_two_step"
        assert accepted[0].class_value == "node_level"

    def test_target_pod_placeholder_container_internal_consistent(self):
        """Task 3's ``{target_pod}`` placeholder is the container_internal
        canonical form; a matching declaration must pass the gate."""
        cmds = [{
            "description": "container fs",
            "command": "kubectl exec {target_pod} -n ns -- df -h",
            "mode": "simple",
            "class": "container_internal",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert len(accepted) == 1
        assert rejected == []
        assert accepted[0].class_value == "container_internal"

    def test_host_profile_class_wiring(self):
        cmds = [{
            "description": "cpu",
            "command": "top -bn1",
            "mode": "simple",
            "class": "host_level",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "host")
        assert len(accepted) == 1
        assert rejected == []
        assert accepted[0].class_value == "host_level"

    def test_host_profile_k8s_class_rejected(self):
        cmds = [{
            "description": "cpu",
            "command": "top -bn1",
            "mode": "simple",
            "class": "container_internal",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "host")
        assert accepted == []
        assert len(rejected) == 1
        assert "host" in rejected[0][1].lower()


class TestCase61ReplacementShapeRejected:
    """Case #61 concrete regression: a container_internal failure was
    replaced by an api_object probe (``kubectl get pods -o jsonpath=...``)
    that "succeeded" with exit 0 while the container dimension went
    unmeasured. Under the class gate the mismatched replacement is
    filtered BEFORE execution, so it cannot enter the success ledger.
    """

    def test_c61_half_way_replacement_rejected(self):
        # The LLM's retry output for a failed ``kubectl exec -l app=... -- id``
        # was a pod-listing jsonpath query. If it declares class honestly
        # (container_internal, to match the failed dimension), the gate
        # refuses the api_object shape.
        cmds = [{
            "description": "pod name discovery",
            "command": "kubectl get pods -n default -l app=drill-perms-target "
                       "-o jsonpath={.items[0].metadata.name}",
            "mode": "simple",
            "class": "container_internal",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        assert accepted == []
        assert len(rejected) == 1
        reason = rejected[0][1]
        # The reason names the mismatch so the next retry round can correct.
        assert "container_internal" in reason
        assert "kubectl exec" in reason

    def test_c61_replacement_if_honest_class_passes_gate_but_is_wrong_dim(self):
        """If the LLM instead declares the replacement honestly as
        ``api_object``, the class gate lets it through — the class gate
        does not police dimension PRESERVATION across retries (that is
        #61's other half, addressed by the retry-prompt teaching + the
        # task 5 double-gate on the merge side). This test documents the
        boundary so a future maintainer does not conflate the two.
        """
        cmds = [{
            "description": "pod name discovery",
            "command": "kubectl get pods -n default -l app=drill-perms-target "
                       "-o jsonpath={.items[0].metadata.name}",
            "mode": "simple",
            "class": "api_object",
        }]
        accepted, rejected = _validate_and_filter_commands(cmds, "k8s")
        # Passes the class gate (form matches declaration). Dimension
        # preservation is enforced elsewhere (retry prompt + task 5).
        assert len(accepted) == 1
        assert rejected == []
