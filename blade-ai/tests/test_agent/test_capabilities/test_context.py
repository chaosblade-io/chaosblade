"""Capability context tests for environment-specific prompt/tool isolation."""

from __future__ import annotations

from types import SimpleNamespace

from chaos_agent.agent.capabilities import (
    build_capability_context,
    build_intent_discovery_context,
    filter_tools_for_context,
    is_tool_name_allowed_for_context,
)
from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.prompts.builders import (
    build_execute_system_prompt,
    build_inject_system_prompt,
    build_intent_clarification_prompt,
    build_plan_builder_prompt,
    build_verifier_prompt,
)
from chaos_agent.agent.prompts.sections.recovery import (
    build_recover_verifier_system_prompt,
)
from chaos_agent.agent.nodes.recover._recover_layer1 import (
    _build_layer1_recovery_prompt,
)
from chaos_agent.transports import PROFILE_UNKNOWN


def _tool(name: str):
    return SimpleNamespace(name=name)


def test_host_execute_hides_k8s_provider_tools():
    FaultProviderRegistry.register_builtins()
    tools = [
        _tool("blade_create"),
        _tool("kubectl"),
        _tool("host_inject"),
        _tool("host_read"),
        _tool("time_wait"),
    ]

    context = build_capability_context(
        {
            "fault_spec": {"scope": "host"},
            "kube_connection_mode": "ssh",
            "ssh_host": "host.example",
        },
        "execute",
        tools,
    )
    visible = {tool.name for tool in filter_tools_for_context(tools, context)}

    assert context.profile == "host"
    assert {"blade_create", "host_inject", "time_wait"} <= visible
    assert "kubectl" not in visible
    assert "kubectl" not in context.prompt_fragment().lower()


def test_k8s_execute_hides_host_shell_tools():
    FaultProviderRegistry.register_builtins()
    tools = [
        _tool("blade_create"),
        _tool("kubectl"),
        _tool("host_inject"),
        _tool("time_wait"),
    ]

    context = build_capability_context({"fault_spec": {"scope": "pod"}}, "execute", tools)
    visible = {tool.name for tool in filter_tools_for_context(tools, context)}

    assert context.profile == "k8s"
    assert {"blade_create", "kubectl", "time_wait"} <= visible
    assert "host_inject" not in visible


def test_intent_discovery_uses_transport_without_hiding_host_semantics():
    FaultProviderRegistry.register_builtins()
    tools = [
        _tool("kubectl_read"), _tool("host_read"), _tool("activate_skill"),
        _tool("submit_fault_intent"),
    ]

    context = build_intent_discovery_context(
        {
            "fault_spec": {"scope": "host"},
            "kube_connection_mode": "kubeconfig",
        },
        tools,
    )
    visible = {tool.name for tool in filter_tools_for_context(tools, context)}

    assert context.profile == "k8s"
    assert "kubectl_read" in visible
    assert "host_read" not in visible
    assert {"activate_skill", "submit_fault_intent"} <= visible


def _strip_knowledge_index(prompt: str) -> str:
    """Drop the dynamic Domain Knowledge doc catalogue before leak checks.

    The knowledge index is a shared, on-demand document catalogue rendered from
    the knowledge registry; it legitimately lists k8s-specific doc filenames
    (e.g. ``kubectl-guide.md``). It is the same catalogue for every profile and
    is loaded on demand, not host-facing behavioural prose — so it is excluded
    from the ``do not leak k8s capabilities`` check, which targets prose.
    """
    out: list[str] = []
    skipping = False
    for line in prompt.split("\n"):
        if line.startswith("## Domain Knowledge"):
            skipping = True
            continue
        if skipping and (line.startswith("## ") or line.startswith("# ")):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def test_host_prompt_builders_do_not_leak_k8s_capabilities():
    prompts = [
        build_intent_clarification_prompt(profile="host"),
        build_plan_builder_prompt(profile="host"),
        build_inject_system_prompt(skill_catalog="", profile="host"),
        build_execute_system_prompt(skill_catalog="", profile="host"),
        build_verifier_prompt(profile="host"),
        build_recover_verifier_system_prompt(profile="host"),
    ]

    for prompt in prompts:
        lowered = _strip_knowledge_index(prompt).lower()
        assert "kubectl" not in lowered
        assert "namespace" not in lowered
        assert "kubernetes" not in lowered


def test_host_layer1_recovery_prompt_has_no_cluster_assumptions():
    # With the host profile, the Layer-1 recovery execution prompt must carry
    # the host capability fragment and no Kubernetes resource semantics. (The
    # host fragment itself says "do not assume cluster-specific semantics", so
    # we assert the same k8s-term set as the sibling verifier test above rather
    # than banning the word "cluster".)
    prompt = _build_layer1_recovery_prompt(profile="host").lower()

    assert "kubectl" not in prompt
    assert "namespace" not in prompt
    assert "kubernetes" not in prompt
    assert "configured host" in prompt


def test_unknown_profile_prompt_fails_closed():
    prompt = build_execute_system_prompt(skill_catalog="", profile="unsupported")

    assert "unsupported" in prompt.lower()
    assert "do not attempt injection" in prompt.lower()


def test_scope_transport_conflict_hides_all_tools():
    FaultProviderRegistry.register_builtins()
    tools = [_tool("kubectl"), _tool("host_inject"), _tool("submit_fault_intent")]

    context = build_capability_context(
        {
            "fault_spec": {"scope": "host"},
            "kube_connection_mode": "kubeconfig",
        },
        "execute",
        tools,
    )

    assert context.profile == PROFILE_UNKNOWN
    assert context.supported is False
    assert filter_tools_for_context(tools, context) == []


def test_invalid_transport_override_hides_all_tools():
    FaultProviderRegistry.register_builtins()
    tools = [_tool("kubectl"), _tool("blade_create"), _tool("submit_fault_intent")]

    context = build_capability_context(
        {
            "fault_spec": {"scope": "pod"},
            "kube_connection_mode": "not-a-channel",
        },
        "execute",
        tools,
    )

    assert context.profile == PROFILE_UNKNOWN
    assert context.supported is False
    assert filter_tools_for_context(tools, context) == []


def test_unregistered_scope_fails_closed_instead_of_using_transport_default():
    tools = [_tool("kubectl"), _tool("blade_create"), _tool("time_wait")]
    state = {
        "fault_spec": {"scope": "typo_scope"},
        "kube_connection_mode": "kubeconfig",
    }

    context = build_capability_context(state, "execute", tools)

    assert context.profile == PROFILE_UNKNOWN
    assert context.supported is False
    assert filter_tools_for_context(tools, context) == []
    assert is_tool_name_allowed_for_context("kubectl", state, "execute") is False


class TestOwnershipWhitelist:
    """Provider tools are gated by OWNERSHIP, not by the phase's tool list.

    Defect A (task-46317228): the gate derived "is this a provider tool?" from
    ``provider.tools(THIS phase)``. ``host_read`` is HostShell's PLAN/VERIFY
    tool while ``host_inject`` is its EXECUTE one, so in the execute and
    recover_verify phases ``host_read`` was not in that phase's provider list,
    landed in the "non-provider tool" bucket and passed the gate untouched —
    even on a k8s-profile session.
    """

    @staticmethod
    def _k8s_state():
        return {"fault_spec": {"scope": "node"}}

    def _real_tools(self):
        from chaos_agent.tools import host_read, kubectl_read

        return [kubectl_read, host_read]

    def test_host_tool_never_visible_on_k8s_profile_in_any_phase(self):
        FaultProviderRegistry.register_builtins()
        for phase in ("plan", "execute", "verify", "recover_verify"):
            context = build_capability_context(
                self._k8s_state(), phase, self._real_tools()
            )
            assert context.profile == "k8s"
            assert "host_read" not in context.active_tool_names, (
                f"host_read leaked into phase {phase!r}"
            )
            assert "kubectl_read" in context.active_tool_names

    def test_runtime_screener_refuses_cross_profile_tool_in_every_phase(self):
        FaultProviderRegistry.register_builtins()
        for phase in ("plan", "execute", "verify", "recover_verify"):
            assert is_tool_name_allowed_for_context(
                "host_read", self._k8s_state(), phase
            ) is False
            assert is_tool_name_allowed_for_context(
                "kubectl_read", self._k8s_state(), phase
            ) is True

    def test_non_provider_tools_stay_visible(self):
        """Graph control tools and MCP tools are not profile-bound."""
        FaultProviderRegistry.register_builtins()
        tools = self._real_tools() + [_tool("submit_verification"), _tool("time_wait")]
        context = build_capability_context(self._k8s_state(), "verify", tools)
        assert {"submit_verification", "time_wait"} <= context.active_tool_names

    def test_ownership_index_covers_all_phases(self):
        from chaos_agent.agent.capabilities.context import provider_tool_owners

        FaultProviderRegistry.register_builtins()
        owners = provider_tool_owners()
        # Tools from DIFFERENT phases of the same provider must both be owned.
        assert "host_read" in owners      # HostShell PLAN / VERIFY
        assert "host_inject" in owners    # HostShell EXECUTE
        assert "kubectl_read" in owners   # K8sNative PLAN / VERIFY
        assert "kubectl" in owners        # K8sNative EXECUTE


class TestUnregisteredPhaseFailsClosed:
    """Defect B: an unmapped phase used to disable the gate entirely.

    ``_PHASE_TO_PROVIDER_PHASE.get(phase)`` returning ``None`` produced an empty
    provider-name set, so ``names - all_provider_names`` kept EVERYTHING; and
    ``is_tool_name_allowed_for_context`` returned ``True`` outright. A typo in a
    caller's phase string therefore silently removed enforcement.
    """

    @staticmethod
    def _state():
        return {"fault_spec": {"scope": "node"}}

    def test_unknown_phase_yields_no_active_tools(self):
        from chaos_agent.tools import host_read, kubectl_read

        FaultProviderRegistry.register_builtins()
        for phase in ("discovery", "", "VERIFY", "typo_phase"):
            context = build_capability_context(
                self._state(), phase, [kubectl_read, host_read]
            )
            assert context.active_tool_names == frozenset(), (
                f"phase {phase!r} must fail closed"
            )
            assert context.supported is False

    def test_unknown_phase_refuses_every_tool_at_runtime(self):
        FaultProviderRegistry.register_builtins()
        for name in ("host_read", "kubectl_read", "submit_verification"):
            assert is_tool_name_allowed_for_context(
                name, self._state(), "typo_phase"
            ) is False


class TestSharedCapabilityVerdict:
    """One implementation of "may this tool call run here?", used by all four.

    Before this consolidation the rule was written out in four places
    (``intent_screener`` / ``phase1_screener`` / ``tool_screener`` and the
    ToolNode wrapper) — and a FIFTH, ``plan_builder_screener``, existed with a
    passing test but was never wired into any graph. Only the wrapper handled the
    gate raising; the other three would have aborted their node.
    """

    @staticmethod
    def _k8s_state():
        return {"fault_spec": {"scope": "node"}, "kube_connection_mode": "kubeconfig"}

    def test_verdict_failing_closed_is_shared_by_every_screener(self):
        """A raising gate must refuse, never propagate — in ALL screeners."""
        from unittest.mock import patch

        from chaos_agent.agent.capabilities import tool_call_allowed

        with patch(
            "chaos_agent.agent.capabilities.context.is_tool_name_allowed_for_context",
            side_effect=RuntimeError("gate exploded"),
        ):
            assert tool_call_allowed("kubectl_read", self._k8s_state(), "plan") is False
            assert tool_call_allowed("kubectl", self._k8s_state(), "execute") is False
        with patch(
            "chaos_agent.agent.capabilities.context"
            ".is_tool_name_allowed_for_intent_discovery",
            side_effect=RuntimeError("gate exploded"),
        ):
            assert tool_call_allowed(
                "host_read", self._k8s_state(), discovery=True,
            ) is False

    def test_screen_tool_calls_splits_per_call(self):
        from chaos_agent.agent.capabilities import screen_tool_calls
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        calls = [
            {"name": "kubectl_read", "id": "c1", "args": {}},
            {"name": "host_read", "id": "c2", "args": {}},
        ]
        allowed, rejected = screen_tool_calls(calls, self._k8s_state(), "verify")
        assert [c["name"] for c in allowed] == ["kubectl_read"]
        assert [c["name"] for c in rejected] == ["host_read"]

    def test_tool_call_field_handles_both_shapes(self):
        from types import SimpleNamespace

        from chaos_agent.agent.capabilities import tool_call_field

        assert tool_call_field({"name": "a", "id": "1"}, "name") == "a"
        assert tool_call_field(SimpleNamespace(name="a", id="1"), "id") == "1"
        assert tool_call_field({}, "name", "fallback") == "fallback"

    def test_no_screener_bypasses_the_shared_verdict(self):
        """The policy functions must only be reached through the shared wrapper.

        Calling ``is_tool_name_allowed_for_*`` directly re-introduces a variant
        that does not fail closed — which is exactly the state this consolidated.

        Checks the IMPORT, not the call: ``import ... as _f`` would hide a direct
        call from a call-site pattern.
        """
        import ast
        from pathlib import Path

        POLICY = {
            "is_tool_name_allowed_for_context",
            "is_tool_name_allowed_for_intent_discovery",
        }
        src = Path(__file__).resolve().parents[3] / "src" / "chaos_agent"
        allowed_files = {
            "agent/capabilities/context.py",   # the definition + wrapper
            "agent/capabilities/__init__.py",  # re-export
        }
        offenders = []
        for path in src.rglob("*.py"):
            rel = str(path.relative_to(src))
            if rel in allowed_files:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    a.name in POLICY for a in node.names
                ):
                    offenders.append(f"{rel}:{node.lineno} (import)")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(
                        node.func, "attr", None
                    )
                    if name in POLICY:
                        offenders.append(f"{rel}:{node.lineno} (call)")

        assert not offenders, (
            f"these sites reach the policy function directly instead of "
            f"capabilities.tool_call_allowed / screen_tool_calls: {offenders}"
        )

    def test_misuse_without_phase_or_discovery_fails_closed_and_loudly(self, caplog):
        """Passing neither ``phase`` nor ``discovery`` must not be permissive.

        The signature allows it (both are optional), so the degradation has to be
        the safe one: refuse, and name the known phases in the log so the
        programming error is findable.
        """
        import logging

        from chaos_agent.agent.capabilities import tool_call_allowed
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        with caplog.at_level(logging.ERROR):
            assert tool_call_allowed("kubectl_read", self._k8s_state()) is False
        assert "unregistered phase" in caplog.text

    def test_capability_check_is_closed_while_the_classifier_stays_open(self):
        """Two opposite error policies sit next to each other in phase1_screener.

        Capability ("can this tool run here?") fails CLOSED — an unknown
        environment must not grant access. The mutation-equivalence classifier
        fails OPEN — a classifier bug must not invent a rejection the model has no
        way to satisfy. Collapsing them either way is a real bug, so pin both.
        """
        import inspect

        from chaos_agent.agent.nodes.planning import phase1_screener as mod

        src = inspect.getsource(mod.phase1_screener)
        assert 'tool_call_allowed(tool_name, state, "plan")' in src, (
            "the capability check must go through the shared fail-closed verdict"
        )
        assert "failing open (tool_call passes through)" in src, (
            "the classifier's fail-open policy must remain intact"
        )
