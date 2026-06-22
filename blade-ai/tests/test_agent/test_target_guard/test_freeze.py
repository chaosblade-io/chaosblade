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
from chaos_agent.agent.fault_spec import FaultSpec


class TestFreezeApprovedTarget:
    def test_full_pod_inject_freezes_cleanly(self):
        d = freeze_approved_target(
            target={
                "namespace": "prod", "names": ["pod-a"],
                "labels": {}, "resource_type": "pod",
            },
            params={"scope": "pod", "target": "cpu", "action": "fullload"},
            blade_scope="pod",
            blade_target="cpu",
            blade_action="fullload",
        )
        assert d == {
            "scope": "pod", "namespace": "prod",
            "names": ["pod-a"], "labels": {},
            "is_namespace_wide": False,
            "blade_target": "cpu", "blade_action": "fullload",
            "lock_fault_type": True,
            "owner_names": [],
            "secondary_scopes": ["pvc", "persistentvolumeclaim", "pv", "persistentvolume", "configmap", "secret", "pod", "node"],
            "secondary_namespace": "prod",
        }

    def test_container_scope_normalised_to_pod(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p1"]},
            params={"scope": "container"},
            blade_scope=None, blade_target="jvm", blade_action="oom",
        )
        assert d["scope"] == "pod"

    def test_node_scope_clears_namespace(self):
        d = freeze_approved_target(
            target={"namespace": "leftover", "names": ["node-1"]},
            params={"scope": "node"},
            blade_scope="node", blade_target="cpu", blade_action="fullload",
        )
        assert d["scope"] == "node"
        # Cluster-scoped → namespace nulled in the snapshot.
        assert d["namespace"] == ""

    def test_default_namespace_when_missing(self):
        d = freeze_approved_target(
            target={"names": ["p1"]},  # no namespace
            params={"scope": "pod"},
            blade_scope=None, blade_target=None, blade_action=None,
        )
        assert d["namespace"] == "default"

    def test_namespace_wide_when_no_names_or_labels(self):
        d = freeze_approved_target(
            target={"namespace": "ns"},
            params={"scope": "pod"},
            blade_scope=None, blade_target=None, blade_action=None,
        )
        assert d["is_namespace_wide"] is True

    def test_labels_only_is_not_namespace_wide(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "labels": {"app": "demo"}},
            params={"scope": "pod"},
            blade_scope=None, blade_target=None, blade_action=None,
        )
        assert d["is_namespace_wide"] is False

    def test_names_csv_string_normalised_to_list(self):
        # Back-compat path: some callers pass names as CSV.
        d = freeze_approved_target(
            target={"namespace": "ns", "names": "a,b,c"},
            params={"scope": "pod"},
            blade_scope=None, blade_target=None, blade_action=None,
        )
        assert d["names"] == ["a", "b", "c"]

    def test_explicit_blade_fields_win_over_params(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={"target": "mem", "action": "ram"},
            blade_scope="pod",
            blade_target="cpu",
            blade_action="fullload",
        )
        assert d["blade_target"] == "cpu"
        assert d["blade_action"] == "fullload"

    def test_falls_back_to_params_when_blade_fields_empty(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={"scope": "pod", "target": "mem", "action": "ram"},
            blade_scope=None, blade_target=None, blade_action=None,
        )
        assert d["blade_target"] == "mem"
        assert d["blade_action"] == "ram"

    def test_no_scope_returns_none(self):
        # No scope anywhere — caller should treat as "no approval"
        # and disable guarding for the turn.
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={},
            blade_scope=None, blade_target=None, blade_action=None,
        )
        assert d is None

    def test_none_target_and_params(self):
        d = freeze_approved_target(
            target=None, params=None,
            blade_scope=None, blade_target=None, blade_action=None,
        )
        assert d is None

    def test_lock_fault_type_default_true(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={"scope": "pod"},
            blade_scope=None, blade_target="cpu", blade_action=None,
        )
        assert d["lock_fault_type"] is True

    def test_lock_fault_type_can_be_overridden(self):
        d = freeze_approved_target(
            target={"namespace": "ns", "names": ["p"]},
            params={"scope": "pod"},
            blade_scope=None, blade_target="cpu", blade_action=None,
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
            blade_target="network",
            blade_action="loss",
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
            blade_scope="pod",
            blade_target="network",
            blade_action="loss",
            owner_names=("deploy-a",),
        )

        assert direct == legacy

    def test_accepts_fault_spec_dict(self):
        spec = FaultSpec(
            namespace="prod",
            scope="pod",
            names=("pod-a",),
            blade_target="cpu",
            blade_action="fullload",
        )

        d = freeze_approved_target_from_spec(spec.to_dict())

        assert d is not None
        assert d["scope"] == "pod"
        assert d["namespace"] == "prod"
        assert d["names"] == ["pod-a"]
        assert d["blade_target"] == "cpu"

    def test_none_or_malformed_spec_returns_none(self):
        assert freeze_approved_target_from_spec(None) is None
        assert freeze_approved_target_from_spec({"scope": []}) is None


class TestApprovedFromDict:
    def test_round_trip(self):
        original = freeze_approved_target(
            target={"namespace": "prod", "names": ["a", "b"]},
            params={"scope": "pod"},
            blade_scope=None, blade_target="cpu", blade_action="fullload",
        )
        approved = approved_from_dict(original)
        assert isinstance(approved, ApprovedTarget)
        assert approved.scope == "pod"
        assert approved.namespace == "prod"
        assert approved.names == ("a", "b")
        assert approved.labels == {}
        assert approved.blade_target == "cpu"
        assert approved.blade_action == "fullload"
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
            "names": ["a"], "blade_target": "cpu",
        })
        assert approved is not None
        assert approved.lock_fault_type is True
