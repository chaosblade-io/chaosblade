"""Tests for ``chaos_agent.agent.fault_spec.FaultSpec``.

Coverage:
  - Each constructor (placeholder / cli_structured / cli_nl / http_request /
    intent_args / direct_setup) with realistic inputs and edge cases.
  - to_dict / from_dict round-trip.
  - Derived properties (fault_type / is_namespace_wide / is_complete).
  - replace() immutability semantics.
  - read_fault_spec helper behaviour.
  - Defensive normalisation: JSON-stringified lists, comma strings,
    None values, str-formed labels — all the LLM/external schema drift
    cases that broke the previous scattered-fields design.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec


# ---------------------------------------------------------------------------
# placeholder_nl
# ---------------------------------------------------------------------------


class TestPlaceholderNl:
    def test_empty_stub(self):
        spec = FaultSpec.placeholder_nl(
            user_description="对节点 X 注入 CPU 满载",
            source="tui",
        )
        assert spec.source == "tui"
        assert spec.user_description == "对节点 X 注入 CPU 满载"
        assert spec.namespace == ""
        assert spec.scope == ""
        assert spec.names == ()
        assert spec.labels == {}
        assert spec.blade_target == ""
        assert spec.blade_action == ""
        assert spec.params == {}
        assert spec.duration_seconds == 0

    def test_not_complete(self):
        spec = FaultSpec.placeholder_nl(user_description="anything", source="tui")
        assert spec.is_complete is False

    def test_user_description_none_safe(self):
        spec = FaultSpec.placeholder_nl(user_description=None, source="tui")  # type: ignore
        assert spec.user_description == ""


# ---------------------------------------------------------------------------
# from_cli_structured
# ---------------------------------------------------------------------------


class TestFromCliStructured:
    def test_typical_node_cpu(self):
        spec = FaultSpec.from_cli_structured({
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            "target_name": "cn-hongkong.10.0.1.120",
            "params": {"percent": "80", "timeout": "600"},
            "duration": 600,
        })
        assert spec.scope == "node"
        assert spec.blade_target == "cpu"
        assert spec.blade_action == "fullload"
        assert spec.namespace == "default"
        assert spec.names == ("cn-hongkong.10.0.1.120",)
        assert spec.params == {"percent": "80", "timeout": "600"}
        assert spec.duration_seconds == 600
        assert spec.source == "cli_structured"
        assert spec.is_complete

    def test_csv_target_names(self):
        spec = FaultSpec.from_cli_structured({
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            "target_name": "pod-a, pod-b ,pod-c",  # spaces tolerated
        })
        assert spec.names == ("pod-a", "pod-b", "pod-c")

    def test_labels_dict(self):
        spec = FaultSpec.from_cli_structured({
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            "target_name": "",
            "labels": {"app": "demo", "env": "prod"},
        })
        assert spec.labels == {"app": "demo", "env": "prod"}

    def test_params_flags_tuple(self):
        spec = FaultSpec.from_cli_structured({
            "scope": "pod",
            "target": "disk",
            "action": "burn",
            "namespace": "default",
            "target_name": "pod-a",
            "params_flags": ["read", "write"],
        })
        assert spec.params_flags == ("read", "write")

    def test_missing_optional_fields(self):
        spec = FaultSpec.from_cli_structured({
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            "target_name": "pod-a",
        })
        assert spec.params == {}
        assert spec.params_flags == ()
        assert spec.duration_seconds == 0
        assert spec.labels == {}


# ---------------------------------------------------------------------------
# from_cli_nl
# ---------------------------------------------------------------------------


class TestFromCliNl:
    def test_basic(self):
        spec = FaultSpec.from_cli_nl(input_text="对 pod-a 注入 CPU 满载")
        assert spec.source == "cli_nl"
        assert spec.user_description == "对 pod-a 注入 CPU 满载"
        assert spec.is_complete is False

    def test_captures_tuning_kwargs(self):
        # Regression for Bug 1 (Stage 2 self-audit): CLI accepts
        # ``--input "..." --duration 600 --params percent=80`` to seed
        # the NL flow with hard-pinned tuning. Without this kwargs
        # passthrough, the duration / params / params_flags would be
        # silently dropped from the spec.
        spec = FaultSpec.from_cli_nl(
            input_text="对 pod-a 注入 CPU 满载",
            kwargs={
                "duration": 600,
                "params": {"percent": "80"},
                "params_flags": ["read", "write"],
            },
        )
        assert spec.source == "cli_nl"
        assert spec.user_description == "对 pod-a 注入 CPU 满载"
        # Identity stays empty — LLM will fill via intent_clarification
        assert spec.scope == ""
        assert spec.names == ()
        # Tuning IS captured from CLI flags
        assert spec.duration_seconds == 600
        assert spec.params == {"percent": "80"}
        assert spec.params_flags == ("read", "write")

    def test_no_kwargs_safe(self):
        # Without kwargs, behaves like placeholder_nl (defaults)
        spec = FaultSpec.from_cli_nl(input_text="x")
        assert spec.duration_seconds == 0
        assert spec.params == {}
        assert spec.params_flags == ()


# ---------------------------------------------------------------------------
# from_http_request
# ---------------------------------------------------------------------------


class TestFromHttpRequest:
    def test_structured_request(self):
        req = SimpleNamespace(
            scope="node", target="cpu", action="fullload",
            namespace="default", target_name="node-1",
            labels={}, params={"percent": "80"}, params_flags=None,
            duration=600, input=None,
        )
        spec = FaultSpec.from_http_request(req)
        assert spec.source == "http_structured"
        assert spec.is_complete

    def test_nl_request(self):
        req = SimpleNamespace(
            scope=None, target=None, action=None,
            namespace=None, target_name=None, labels=None,
            params=None, params_flags=None, duration=0,
            input="对节点 X 注入 CPU 满载",
        )
        spec = FaultSpec.from_http_request(req)
        assert spec.source == "http_nl"
        assert spec.user_description == "对节点 X 注入 CPU 满载"
        assert spec.is_complete is False

    def test_labels_only_is_structured(self):
        # Some flows pin target by labels without explicit target_name.
        req = SimpleNamespace(
            scope="pod", target="cpu", action="fullload",
            namespace="default", target_name=None,
            labels={"app": "demo"}, params=None, params_flags=None,
            duration=0, input=None,
        )
        spec = FaultSpec.from_http_request(req)
        assert spec.source == "http_structured"
        assert spec.labels == {"app": "demo"}
        assert spec.names == ()

    def test_namespace_missing_is_nl_not_structured(self):
        # Regression for Bug 2 (Stage 2 self-audit): is_structured
        # must include the namespace check so the spec.source agrees
        # with what the inject.py / inject_stream.py entry branches
        # decide. Previously namespace was missing from the check,
        # so a request with scope/target/action/target_name but no
        # namespace would be tagged http_structured while the route
        # took the NL branch.
        req = SimpleNamespace(
            scope="pod", target="cpu", action="fullload",
            namespace=None,  # <-- missing
            target_name="pod-a",
            labels=None, params=None, params_flags=None,
            duration=0, input="对 pod-a 注入 cpu",
        )
        spec = FaultSpec.from_http_request(req)
        assert spec.source == "http_nl"

    def test_all_five_fields_required_for_structured(self):
        # Each of the 5 canonical fields (scope/target/action/
        # (target_name|labels)/namespace) being absent flips to NL.
        base = {
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "target_name": "p",
            "labels": None, "params": None, "params_flags": None,
            "duration": 0, "input": "fall-back NL",
        }
        # Full → structured
        full_req = SimpleNamespace(**base)
        assert FaultSpec.from_http_request(full_req).source == "http_structured"
        # Each removal → NL
        for missing in ("scope", "target", "action", "namespace"):
            req = SimpleNamespace(**{**base, missing: None})
            assert FaultSpec.from_http_request(req).source == "http_nl", (
                f"Missing {missing} should yield http_nl"
            )


# ---------------------------------------------------------------------------
# from_intent_args — LLM tool_call shape (the previously-broken NL path)
# ---------------------------------------------------------------------------


class TestFromIntentArgs:
    def test_canonical_args(self):
        args = {
            "fault_type": "node-cpu-fullload",
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            "names": ["cn-hongkong.10.0.1.120"],
            "labels": {},
            "params": {"percent": "80", "timeout": "600"},
            "user_description": "对节点 X 注入 CPU 满载",
        }
        spec = FaultSpec.from_intent_args(args)
        assert spec.source == "tui"
        assert spec.scope == "node"
        assert spec.names == ("cn-hongkong.10.0.1.120",)
        assert spec.params == {"percent": "80", "timeout": "600"}
        # timeout in params is hoisted to duration_seconds for convenience
        assert spec.duration_seconds == 600
        assert spec.is_complete

    def test_json_stringified_names(self):
        # The original NL-path bug: qwen-style function-calling returns
        # ``"names": '[\"node-1\"]'`` instead of a real list. FaultSpec
        # must absorb this without crashing.
        spec = FaultSpec.from_intent_args({
            "scope": "node", "target": "cpu", "action": "fullload",
            "namespace": "default",
            "names": '["cn-hongkong.10.0.1.120"]',
            "params": {},
        })
        assert spec.names == ("cn-hongkong.10.0.1.120",)

    def test_json_stringified_params(self):
        spec = FaultSpec.from_intent_args({
            "scope": "node", "target": "cpu", "action": "fullload",
            "namespace": "default",
            "names": ["n1"],
            "params": '{"percent": "80", "timeout": "300"}',
        })
        assert spec.params == {"percent": "80", "timeout": "300"}
        assert spec.duration_seconds == 300

    def test_labels_as_selector_string(self):
        # LLMs sometimes serialise labels as k=v selector syntax.
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default",
            "labels": "app=demo,env=prod",
            "params": {},
        })
        assert spec.labels == {"app": "demo", "env": "prod"}

    def test_existing_user_description_preserved(self):
        # LLM forgot to echo user_description in submit_fault_intent
        # args. The placeholder spec already had it; FaultSpec carries
        # it forward.
        placeholder = FaultSpec.placeholder_nl(
            user_description="原始描述", source="tui",
        )
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "names": ["p1"], "params": {},
        }, existing=placeholder)
        assert spec.user_description == "原始描述"

    def test_explicit_user_description_overrides_existing(self):
        placeholder = FaultSpec.placeholder_nl(
            user_description="原始", source="tui",
        )
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "names": ["p1"], "params": {},
            "user_description": "更准确的描述",
        }, existing=placeholder)
        assert spec.user_description == "更准确的描述"

    def test_source_inherits_from_existing(self):
        # Bug 2: from_intent_args used to default source="tui", which
        # silently mislabelled CLI NL / HTTP NL flows. Now it
        # inherits from the placeholder spec carried in ``existing``.
        placeholder = FaultSpec.placeholder_nl(
            user_description="对 X 注入", source="cli_nl",
        )
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "names": ["p1"],
        }, existing=placeholder)
        assert spec.source == "cli_nl"  # inherited, not "tui"

    def test_source_explicit_override(self):
        placeholder = FaultSpec.placeholder_nl(
            user_description="对 X 注入", source="cli_nl",
        )
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "names": ["p1"],
        }, existing=placeholder, source="http_nl")
        # Explicit source wins over existing inheritance
        assert spec.source == "http_nl"

    def test_source_fallback_when_no_existing(self):
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "names": ["p1"],
        })
        # No existing, no explicit → "tui" default (most common path)
        assert spec.source == "tui"

    def test_timeout_hoist_int_string(self):
        # blade conventionally takes timeout as string seconds
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "names": ["p1"],
            "params": {"timeout": "1200"},
        })
        assert spec.duration_seconds == 1200

    def test_timeout_not_in_params_no_hoist(self):
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "names": ["p1"],
            "params": {"percent": "80"},
        })
        assert spec.duration_seconds == 0


# ---------------------------------------------------------------------------
# from_direct_setup
# ---------------------------------------------------------------------------


class TestFromDirectSetup:
    def test_no_skill_meta_pass_through(self):
        base = FaultSpec.from_cli_structured({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "target_name": "pod-a",
        })
        spec = FaultSpec.from_direct_setup(base=base, skill_meta=None)
        assert spec == base

    def test_skill_meta_fills_default_duration(self):
        base = FaultSpec.from_cli_structured({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "target_name": "pod-a",
        })
        assert base.duration_seconds == 0
        spec = FaultSpec.from_direct_setup(
            base=base, skill_meta={"default_duration": 600},
        )
        assert spec.duration_seconds == 600
        assert spec.scope == base.scope  # other fields untouched

    def test_skill_meta_does_not_override_explicit_duration(self):
        base = FaultSpec.from_cli_structured({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "target_name": "pod-a",
            "duration": 300,
        })
        spec = FaultSpec.from_direct_setup(
            base=base, skill_meta={"default_duration": 600},
        )
        # User's 300 wins over skill's 600
        assert spec.duration_seconds == 300


# ---------------------------------------------------------------------------
# Derived properties
# ---------------------------------------------------------------------------


class TestDerivedProperties:
    def test_fault_type_full(self):
        spec = FaultSpec(scope="node", blade_target="cpu", blade_action="fullload")
        assert spec.fault_type == "node-cpu-fullload"

    def test_fault_type_partial(self):
        spec = FaultSpec(scope="", blade_target="cpu", blade_action="")
        assert spec.fault_type == "cpu"

    def test_fault_type_empty(self):
        spec = FaultSpec()
        assert spec.fault_type == ""

    def test_is_namespace_wide_true(self):
        spec = FaultSpec(namespace="ns")  # no names, no labels
        assert spec.is_namespace_wide

    def test_is_namespace_wide_false_with_names(self):
        spec = FaultSpec(namespace="ns", names=("p1",))
        assert not spec.is_namespace_wide

    def test_is_namespace_wide_false_with_labels(self):
        spec = FaultSpec(namespace="ns", labels={"app": "demo"})
        assert not spec.is_namespace_wide

    def test_is_complete_requires_scope_target_action(self):
        assert not FaultSpec(namespace="ns", names=("p",), blade_target="cpu",
                              blade_action="fullload").is_complete  # no scope
        assert not FaultSpec(namespace="ns", names=("p",), scope="pod",
                              blade_action="fullload").is_complete  # no target
        assert not FaultSpec(namespace="ns", names=("p",), scope="pod",
                              blade_target="cpu").is_complete  # no action

    def test_is_complete_node_no_namespace_ok(self):
        # cluster-scoped resource doesn't need namespace
        spec = FaultSpec(
            scope="node", names=("n1",),
            blade_target="cpu", blade_action="fullload",
        )
        assert spec.is_complete

    def test_is_complete_pod_needs_namespace(self):
        spec = FaultSpec(
            scope="pod", names=("p1",),
            blade_target="cpu", blade_action="fullload",
        )
        assert not spec.is_complete  # namespace missing

    def test_is_complete_namespace_wide_is_allowed(self):
        # ``namespace-wide`` is a legitimate intent: "inject any pod in
        # ns prod". intent_confirm must be able to surface this for
        # user approval. Previously is_complete demanded names or
        # labels and silently blocked namespace-wide flows.
        spec = FaultSpec(
            scope="pod", namespace="ns",
            blade_target="cpu", blade_action="fullload",
        )
        assert spec.is_complete
        assert spec.is_namespace_wide


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_frozen_field_assignment_blocked(self):
        spec = FaultSpec(scope="pod")
        with pytest.raises(FrozenInstanceError):
            spec.scope = "node"  # type: ignore

    def test_replace_returns_new_instance(self):
        spec = FaultSpec(scope="pod", blade_target="cpu")
        replaced = spec.replace(scope="node")
        assert replaced is not spec
        assert spec.scope == "pod"  # original untouched
        assert replaced.scope == "node"
        assert replaced.blade_target == "cpu"  # other fields carried

    def test_caller_mutation_does_not_leak_into_spec(self):
        # Bug 3: frozen=True only blocks attribute reassignment, not
        # internal dict/list content mutation. __post_init__ defensively
        # copies inputs so a caller mutating the original after
        # construction doesn't silently corrupt the spec.
        labels = {"app": "demo"}
        params = {"percent": "80"}
        names_list = ["pod-a"]
        spec = FaultSpec(
            scope="pod", namespace="ns", names=names_list,
            labels=labels, params=params,
            blade_target="cpu", blade_action="fullload",
        )
        # Mutate the originals
        labels["env"] = "prod"
        params["timeout"] = "600"
        names_list.append("pod-b")
        # Spec is untouched
        assert spec.labels == {"app": "demo"}
        assert spec.params == {"percent": "80"}
        assert spec.names == ("pod-a",)

    def test_hash_disabled_clear_error(self):
        # Bug 4: dataclass with dict fields would auto-generate __hash__
        # that crashes at call time with TypeError ("unhashable type:
        # 'dict'"). We disable __hash__ explicitly to give the clearer
        # "spec not hashable" surface — callers must compare via ==,
        # not use spec as a set/dict key.
        spec = FaultSpec(scope="pod", labels={"a": "b"})
        with pytest.raises(TypeError, match="unhashable"):
            hash(spec)
        with pytest.raises(TypeError):
            {spec: "value"}  # type: ignore


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_round_trip_full(self):
        original = FaultSpec.from_cli_structured({
            "scope": "node", "target": "cpu", "action": "fullload",
            "namespace": "default", "target_name": "n1,n2",
            "labels": {"app": "demo"},
            "params": {"percent": "80", "timeout": "600"},
            "params_flags": ["read", "write"],
            "duration": 600,
        })
        d = original.to_dict()
        rehydrated = FaultSpec.from_dict(d)
        assert rehydrated == original

    def test_round_trip_placeholder(self):
        # Placeholder has all default values except source/user_description
        # — round-trip should preserve identity even with mostly-empty
        # dict.
        original = FaultSpec.placeholder_nl(
            user_description="对 X 注入 CPU",
            source="cli_nl",
        )
        d = original.to_dict()
        rehydrated = FaultSpec.from_dict(d)
        assert rehydrated == original
        assert rehydrated is not None
        assert rehydrated.source == "cli_nl"
        assert rehydrated.user_description == "对 X 注入 CPU"

    def test_round_trip_double(self):
        # Round-tripping twice must converge to a stable form (no
        # accidental field shape drift across cycles).
        original = FaultSpec.from_intent_args({
            "scope": "node", "target": "cpu", "action": "fullload",
            "namespace": "default", "names": ["n1"],
            "params": {"percent": "80", "timeout": "300"},
        })
        once = FaultSpec.from_dict(original.to_dict())
        twice = FaultSpec.from_dict(once.to_dict())
        assert once == twice == original

    def test_to_dict_uses_list_not_tuple(self):
        # Tuples don't serialise to JSON; consumers must see lists.
        spec = FaultSpec(names=("a", "b"), params_flags=("x",))
        d = spec.to_dict()
        assert d["names"] == ["a", "b"]
        assert d["params_flags"] == ["x"]
        # Ensure it's actually JSON-serialisable
        import json as _json
        _json.dumps(d)

    def test_from_dict_none_returns_none(self):
        assert FaultSpec.from_dict(None) is None

    def test_from_dict_empty_returns_none(self):
        assert FaultSpec.from_dict({}) is None

    def test_from_dict_non_dict_returns_none(self):
        # Defensive — corrupted checkpoint, wrong type
        assert FaultSpec.from_dict("not a dict") is None  # type: ignore
        assert FaultSpec.from_dict([1, 2]) is None  # type: ignore

    def test_from_dict_partial_fields_safe(self):
        # Older checkpoint missing some fields → defaults filled in
        spec = FaultSpec.from_dict({"scope": "pod"})
        assert spec is not None
        assert spec.scope == "pod"
        assert spec.namespace == ""
        assert spec.params == {}


# ---------------------------------------------------------------------------
# read_fault_spec helper
# ---------------------------------------------------------------------------


class TestToIntentDict:
    """``to_intent_dict`` is the bridge for ``intent_clarification``'s
    internal merge logic — keep the shape stable so that node doesn't
    break."""

    def test_full_fields(self):
        spec = FaultSpec(
            namespace="ns", scope="pod",
            names=("p1", "p2"), labels={"app": "demo"},
            blade_target="cpu", blade_action="fullload",
            params={"percent": "80"},
        )
        d = spec.to_intent_dict()
        assert d == {
            "fault_type": "pod-cpu-fullload",
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "ns",
            "names": ["p1", "p2"],
            "labels": {"app": "demo"},
            "params": {"percent": "80"},
            "user_description": "",
        }

    def test_placeholder(self):
        spec = FaultSpec.placeholder_nl(user_description="x", source="tui")
        d = spec.to_intent_dict()
        assert d["fault_type"] == ""
        assert d["scope"] == ""
        assert d["target"] == ""
        assert d["names"] == []
        assert d["user_description"] == "x"


class TestReadFaultSpec:
    def test_reads_from_state(self):
        original = FaultSpec.from_cli_structured({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "target_name": "p1",
        })
        state = {"fault_spec": original.to_dict()}
        spec = read_fault_spec(state)
        assert spec == original

    def test_missing_fault_spec_returns_none(self):
        assert read_fault_spec({}) is None

    def test_null_fault_spec_returns_none(self):
        assert read_fault_spec({"fault_spec": None}) is None

    def test_unrelated_state_fields_ignored(self):
        spec = FaultSpec(scope="pod", namespace="ns")
        state = {
            "fault_spec": spec.to_dict(),
            "messages": [],
            "task_id": "abc",
        }
        out = read_fault_spec(state)
        assert out == spec


# ---------------------------------------------------------------------------
# Defensive normalisation edge cases (the LLM/schema-drift surface)
# ---------------------------------------------------------------------------


class TestDefensiveNormalisation:
    def test_names_none_becomes_empty_tuple(self):
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "ns", "names": None,
        })
        assert spec.names == ()

    def test_names_comma_string(self):
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "ns", "names": "p1, p2, p3",
        })
        assert spec.names == ("p1", "p2", "p3")

    def test_labels_none_becomes_empty_dict(self):
        spec = FaultSpec.from_intent_args({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "ns", "names": ["p"], "labels": None,
        })
        assert spec.labels == {}

    def test_params_with_non_string_values_stringified(self):
        # Some callers pass int / bool values; downstream blade CLI
        # expects strings.
        spec = FaultSpec.from_cli_structured({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "ns", "target_name": "p1",
            "params": {"percent": 80, "verbose": True, "name": None},
        })
        assert spec.params == {"percent": "80", "verbose": "True", "name": ""}
