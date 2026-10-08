"""Tests for the intent phase's two-gate screen: transport match, then
read-only classification (spec: universal-cognitive-architecture /
domain-command-guard, 预授权相位只读强制)."""

from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes.planning.intent_screener import intent_screener


def _tool_call(name: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": name,
        "id": "probe-1",
        "args": {"command": "df -h"},
    }])


def test_rejects_host_probe_on_k8s_transport_without_rejecting_host_semantics():
    result = intent_screener({
        "kube_connection_mode": "kubeconfig",
        "fault_spec": {"scope": "host", "fault_target": "cpu", "fault_action": "fullload"},
        "messages": [_tool_call("host_read")],
    })

    assert result["intent_screener_route"] == "retry"
    assert result["messages"][0].name == "host_read"


def test_refusal_names_the_transport_in_force():
    # The refusal used to be one fixed sentence ("unavailable for the current
    # environment") for every tool in every profile — it never said WHICH
    # environment was connected, leaving the model to retry variations. The
    # capability gate already holds the resolved profile (the same rule the
    # execute-phase screener follows), so the message must name it.
    result = intent_screener({
        "kube_connection_mode": "kubeconfig",
        "fault_spec": {"scope": "host", "fault_target": "cpu", "fault_action": "fullload"},
        "messages": [_tool_call("host_read")],
    })

    content = result["messages"][0].content
    assert content.startswith("Error:")
    # Names the tool, the profile it belongs to, and the transport in force.
    assert "host_read" in content
    assert "k8s" in content, f"transport in force not named: {content}"
    # Keeps the standing instruction so the cause is paired with a move.
    assert "Select a tool bound to the active transport." in content
    # Not the old profile-agnostic template.
    assert "unavailable for the current environment" not in content


def test_allows_k8s_probe_even_when_semantic_intent_is_host():
    # The pass path now runs the read-only classifier gate too, which needs
    # the provider registry the production graph registers at build time.
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()

    result = intent_screener({
        "kube_connection_mode": "kubeconfig",
        "fault_spec": {"scope": "host", "fault_target": "cpu", "fault_action": "fullload"},
        "messages": [_tool_call("kubectl_read")],
    })

    assert result["intent_screener_route"] == "pass"


def test_plan_builder_rejects_stale_host_tool_on_k8s_transport():
    """Same contract, now enforced by the plan_builder_screener edge node.

    A ``plan_builder_screener`` node used to live in ``intent_screener`` with
    this exact rule, but was never wired into ``build_pipeline_graph`` — so this
    test passed while ``plan_builder_tools`` actually ran unscreened. That gap
    is closed by ``make_phase_screener(capability_phase="plan", ...)`` wired
    into the pipeline graph. The screener refuses the WHOLE batch
    (phase1/tool_screener protocol): the offending call gets a rejection and
    the legitimate sibling a skipped notice — nothing is dispatched.
    """
    import asyncio

    from chaos_agent.agent.nodes._phase_screener import make_phase_screener
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()
    screener, route = make_phase_screener(
        capability_phase="plan", stop_retry_hint=True,
    )
    state = {
        "kube_connection_mode": "kubeconfig",
        "fault_spec": {"scope": "pod", "fault_target": "cpu", "fault_action": "fullload"},
        "messages": [AIMessage(content="", tool_calls=[
            {"name": "host_read", "id": "probe-1", "args": {"command": "df -h"}},
            {"name": "kubectl_read", "id": "probe-2",
             "args": {"subcommand": "get", "v_args": "pods"}},
        ])],
    }

    out = asyncio.run(screener(state))

    assert out["screener_route"] == "retry"
    assert route({**state, **out}) == "retry"
    by_id = {m.tool_call_id: m for m in out["messages"]}
    assert by_id["probe-1"].name == "host_read"
    assert by_id["probe-1"].content.startswith("Error:")
    assert by_id["probe-1"].status == "error"
    # The legitimate sibling does NOT run — it gets the skipped notice and the
    # whole batch goes back to the model to be re-issued cleanly.
    assert "skipped" in by_id["probe-2"].content
    assert by_id["probe-2"].status == "error"


def test_control_signal_batch_passes_the_readonly_gate():
    """Intent's own routing surface must survive the read-only gate.

    submit_batch_intent / query_active_experiments / recover_task are
    bound in this phase (factory.py clarification static_base) but were
    invisible to the classifier until this phase gained a classifier-
    screened gate — the B81 finish_execution gap class. The whitelist row
    in ``target_guard.classifier`` is what keeps the phase routable.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()

    result = intent_screener({
        "kube_connection_mode": "kubeconfig",
        "messages": [AIMessage(content="", tool_calls=[
            {"name": "submit_fault_intent", "id": "c1",
             "args": {"fault_type": "pod-cpu-fullload"}},
            {"name": "submit_batch_intent", "id": "c2",
             "args": {"faults": [
                 {"scope": "pod", "target": "cpu", "action": "fullload"},
             ]}},
            {"name": "query_active_experiments", "id": "c3", "args": {}},
            {"name": "recover_task", "id": "c4",
             "args": {"task_id": "inject-abc"}},
            {"name": "update_progress", "id": "c5",
             "args": {"note": "probed the target"}},
        ])],
    })

    assert result["intent_screener_route"] == "pass"
    assert "messages" not in result


def test_readonly_gate_passes_read_commands_and_the_capability_probe():
    """Read commands pass regardless of carrier; the shared probe
    exception holds (kubectl_read debug creates the ephemeral, self-gated,
    auto-cleaned probe pod — same exemption phase1/verification share).
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()

    result = intent_screener({
        "kube_connection_mode": "kubeconfig",
        "messages": [AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "id": "r1",
             "args": {"subcommand": "get", "v_args": "pods"}},
            # Full kubectl is NOT bound in this phase, but `get` is
            # read-only: the gate rules on the command, not on the binding
            # (Layer A binding is not the carrier of read-only-ness).
            {"name": "kubectl", "id": "r2",
             "args": {"command": ["get", "pods"]}},
            {"name": "kubectl_read", "id": "r3",
             "args": {"subcommand": "debug",
                      "command": "node/n1 --image=ubuntu -- which stress-ng"}},
        ])],
    })

    assert result["intent_screener_route"] == "pass"
    assert "messages" not in result


def test_readonly_gate_refuses_smuggled_mutation_with_the_classifier_verdict():
    """task-ce9647931ce1's intent-phase shape, closed.

    A crafted/stale batch carrying full ``kubectl delete`` PASSES the
    transport rule (full kubectl is a k8s-native tool) and previously
    reached the ToolNode unscreened — intent read-only-ness rode the
    binding, and binding is what the perception-surface collapse removes.
    The gate must refuse it with the verdict the classifier reached, NOT
    with a binding story: the spec's 「拒绝 MUST NOT 依赖变更工具未绑定」.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()

    result = intent_screener({
        "kube_connection_mode": "kubeconfig",
        "messages": [AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "smuggle-1",
            "args": {"command": ["delete", "pod", "x"]},
        }])],
    })

    assert result["intent_screener_route"] == "retry"
    msg = result["messages"][0]
    assert msg.name == "kubectl"
    assert msg.tool_call_id == "smuggle-1"
    assert msg.status == "error"
    assert msg.content.startswith("Error: intent_readonly_violation")
    # The cause is the classifier's mutation verdict (truth-first
    # renderer: raw command + the scope it would mutate), never the
    # transport sentence a binding/capability refusal would use.
    assert "would mutate" in msg.content
    assert "delete" in msg.content
    assert "Select a tool bound to the active transport." not in msg.content
    # Phase boundary named, so the model reads a PHASE rule it can act on.
    assert "read-only by runtime enforcement" in msg.content


def test_refused_batch_answers_every_call_with_a_paired_message():
    """Whole-batch refusal keeps the conversation well-formed.

    Mixed batch: a legitimate read probe beside a smuggled mutation and a
    transport offender. Every tool_call gets exactly one ToolMessage
    (LangChain contract) — rejections with causes, the legitimate sibling
    with the skipped notice.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()

    result = intent_screener({
        "kube_connection_mode": "kubeconfig",
        "messages": [AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "id": "ok-1",
             "args": {"subcommand": "get", "v_args": "pods"}},
            {"name": "kubectl", "id": "bad-1",
             "args": {"command": ["delete", "pod", "x"]}},
            {"name": "host_read", "id": "bad-2",
             "args": {"command": "df -h"}},
        ])],
    })

    assert result["intent_screener_route"] == "retry"
    assert len(result["messages"]) == 3
    by_id = {m.tool_call_id: m for m in result["messages"]}
    assert by_id["bad-1"].content.startswith("Error: intent_readonly_violation")
    assert "would mutate" in by_id["bad-1"].content
    # The transport offender keeps the transport refusal (capability
    # verdict fires first — the call never reaches the classifier gate).
    assert by_id["bad-2"].content.startswith("Error:")
    assert "Select a tool bound to the active transport." in by_id["bad-2"].content
    assert "skipped" in by_id["ok-1"].content
    assert by_id["ok-1"].status == "error"


def test_unclassified_execute_carrier_is_default_denied_until_claimed():
    """The future universal ``execute`` carrier has no classifier branch yet.

    Both its read and mutation forms fall to the default-deny UNKNOWN
    here — the honest conservative verdict for an unrecognised tool (it
    is also unbound today, so it would fail at the ToolNode anyway).
    Stage 5 (tasks 6.x) routes the carrier through this same classifier
    instead of a new parser; once claimed, read commands pass this gate
    with no gate change.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    FaultProviderRegistry.register_builtins()

    result = intent_screener({
        "kube_connection_mode": "kubeconfig",
        "messages": [AIMessage(content="", tool_calls=[{
            "name": "execute", "id": "uni-1",
            "args": {"command": "kubectl get pods"},
        }])],
    })

    assert result["intent_screener_route"] == "retry"
    content = result["messages"][0].content
    assert "unrecognized tool 'execute'" in content
