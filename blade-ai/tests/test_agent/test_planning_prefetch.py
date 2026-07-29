from chaos_agent.agent.capabilities.context import AgentCapabilityContext
from chaos_agent.agent.planning.prefetch import build_guided_prefetch
from chaos_agent.agent.planning.mode import PLANNING_MODE_EXPERT, resolve_planning_mode
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state


def _context(profile: str, *tool_names: str) -> AgentCapabilityContext:
    return AgentCapabilityContext(
        profile=profile,
        phase="plan",
        target_authority="environment",
        provider_candidates=(),
        active_tool_names=frozenset(tool_names),
        supported=True,
    )


def test_k8s_guided_prefetch_uses_namespace_scoped_resource_listing():
    prefetch = build_guided_prefetch(
        FaultSpec(scope="pod", namespace="prod"), _context("k8s", "kubectl_read"),
    )

    assert prefetch is not None
    assert prefetch.tool_name == "kubectl_read"
    assert prefetch.args["v_args"] == "pods -n prod -o wide"


def test_host_guided_prefetch_anchors_the_connected_host_identity():
    prefetch = build_guided_prefetch(
        FaultSpec(scope="node", blade_target="cpu"), _context("host", "host_read"),
    )

    assert prefetch is not None
    assert prefetch.tool_name == "host_read"
    assert prefetch.args == {"command": "hostname"}


def test_unknown_profile_has_no_prefetch_strategy():
    assert build_guided_prefetch(None, _context("unknown")) is None


def test_complete_fault_spec_uses_expert_mode_when_no_mode_is_explicit():
    spec = FaultSpec(
        scope="pod",
        namespace="prod",
        names=("api-0",),
        blade_target="cpu",
        blade_action="fullload",
    )
    state = build_inject_initial_state(task_id="task-1", fault_spec=spec)

    assert "planning_mode" not in state
    assert resolve_planning_mode(state, spec) == PLANNING_MODE_EXPERT
