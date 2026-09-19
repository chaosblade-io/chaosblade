"""Phase-8 golden fixation: byte-exact snapshots of the native carriers'
step self-check outputs plus the D4 narrowing pins.

The two byte-exact tests are the mechanical proof that the T3.5 rewrite
(``scan_step_actions`` hook dispatch, provider-owned vocabulary) kept the
native outputs IDENTICAL. The D4 tests pin the explicitly-decided behaviour
change: experiment methods (``host_blade`` / ``kubectl_exec``) no longer
fall into a kubectl-verbs branch — they return ``None`` (experiment
completion is judged by the UID evidence chain). Their pre-rewrite
ancestors recorded the former behaviour on purpose, so the narrowing diff
is documented, not silent.

Also pins the ``_was_kubectl_injection_attempted`` wrapper's dispatch
vocabulary (one mutating / one read-only representative; the 13-case
suite in ``test_verifier.py`` carries the full behavioural coverage)."""

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.execute._injection_detection import (
    _was_kubectl_injection_attempted,
    build_injection_step_selfcheck,
)

K8S_CASE = """**演练步骤**：
1. 使用 kubectl 将该节点标记为不可调度：`kubectl cordon <node>`
2. 给该节点添加污点：`kubectl taint nodes <node> key=val:NoSchedule`
3. 删除该节点上的 Pod，触发重建

**注入验证**：
1. 执行 `kubectl get nodes`
"""

HOST_CASE = """**演练步骤**：
1. 记录当前连接基线：`ss -s`
2. 使用 iptables 丢弃目标端口入向流量
3. 武装定时恢复

**注入验证**：
1. 确认连接超时
"""

_MESSAGES_K8S = [
    AIMessage(
        content="",
        tool_calls=[
            {
                "name": "kubectl",
                "args": {"subcommand": "cordon", "v_args": "node-1"},
                "id": "tc-1",
            }
        ],
    ),
    ToolMessage(content="node/node-1 cordoned", name="kubectl", tool_call_id="tc-1"),
]

_TAIL = (
    "\nReconsider (this check is heuristic and may be inaccurate): if you "
    "have ALREADY performed the actions needed for the fault effect — a tool "
    "may have timed out but still applied — STOP calling tools and let "
    "verification confirm it. Do NOT repeat actions already done, and do NOT "
    "loop deleting/observing to watch the effect (observation is the "
    "verification phase's job). If an action was genuinely SKIPPED, do it now."
)

GOLDEN_K8S_NATIVE = (
    "[Step self-check] This multi-step skill case has an injection action "
    "that may not have been performed yet. Steps outlined:\n"
    "1. 使用 kubectl 将该节点标记为不可调度：`kubectl cordon <node>`\n"
    "2. 给该节点添加污点：`kubectl taint nodes <node> key=val:NoSchedule`\n"
    "3. 删除该节点上的 Pod，触发重建\n"
    "\nPossibly not yet performed: taint (给该节点添加污点：`kubectl taint "
    "nodes <node> key=val:NoSchedule`), delete (删除该节点上的 Pod，触发重建)\n"
    + _TAIL
)

GOLDEN_HOST_NATIVE = (
    "[Step self-check] This multi-step skill case has an injection action "
    "that may not have been performed yet. Steps outlined:\n"
    "1. 记录当前连接基线：`ss -s`\n"
    "2. 使用 iptables 丢弃目标端口入向流量\n"
    "3. 武装定时恢复\n"
    "\nPossibly not yet performed: iptables (使用 iptables 丢弃目标端口入向流量)\n"
    + _TAIL
)


def test_kubectl_native_selfcheck_output_is_byte_exact():
    """Golden: cordon executed, taint/delete missing → soft reminder listing
    exactly the missing verbs, byte-identical through the T3.5 rewrite."""
    out = build_injection_step_selfcheck(K8S_CASE, _MESSAGES_K8S, "kubectl_native")
    assert out == GOLDEN_K8S_NATIVE


def test_host_native_selfcheck_output_is_byte_exact():
    """Golden: baseline step filtered by intent-prefix, iptables missing →
    soft reminder, byte-identical through the T3.5 rewrite."""
    out = build_injection_step_selfcheck(HOST_CASE, [], "host_native")
    assert out == GOLDEN_HOST_NATIVE


def test_d4_experiment_methods_do_not_claim_step_selfcheck():
    """D4 POST-REWRITE PIN (phase-8 narrowing, explicitly decided):
    ``kubectl_exec`` (an experiment method) resolves to the ChaosBlade
    backend, which does not claim the ``scan_step_actions`` hook → the
    self-check returns ``None``. Experiment completion is judged by the
    UID evidence chain, not step-verb heuristics; the pre-rewrite behaviour
    (falling into the kubectl-verbs branch) was a historical approximation,
    pre-pinned by this test's ancestor before the T3.5 rewrite."""
    out = build_injection_step_selfcheck(K8S_CASE, _MESSAGES_K8S, "kubectl_exec")
    assert out is None


def test_d4_host_blade_does_not_claim_step_selfcheck():
    """D4 POST-REWRITE PIN: ``host_blade`` does not claim the hook either —
    ``None`` for both a kubectl-verb case (formerly the shared kubectl
    branch) and a host-binary case (formerly ``None`` only incidentally,
    because the kubectl branch found no verbs in it)."""
    out = build_injection_step_selfcheck(K8S_CASE, _MESSAGES_K8S, "host_blade")
    assert out is None
    assert build_injection_step_selfcheck(HOST_CASE, [], "host_blade") is None


def test_unresolved_method_does_not_claim_step_selfcheck():
    """Pinned side-effect of the same narrowing: ``injection_method=None``
    (no attribution) also returns ``None`` — the pre-rewrite code fell it
    into the kubectl-verbs ``else`` branch. Unreachable on the main path:
    the execute loop gates the self-check behind
    ``resolve_by_method(method) is not None and provider.is_multi_step``,
    so a method that resolves to no provider never reaches this function
    in production; this pin holds the function-level contract for direct
    callers (and mirrors the D4 experiment-method pins above)."""
    assert build_injection_step_selfcheck(K8S_CASE, _MESSAGES_K8S, None) is None


def test_backscan_dispatch_vocabulary_pinned():
    """Golden: the attempted-injection back-scan credits a mutating
    ``kubectl exec`` fallback (after a failed blade_create) and does NOT
    credit a read-only exec — the wrapper's provider vocabulary stays
    equivalent through the T3.3 move."""
    failed_blade_then = lambda inner_cmd, tc_id: [  # noqa: E731
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "blade_create",
                    "args": {"command": "create cpu fullload"},
                    "id": "tc-b",
                }
            ],
        ),
        ToolMessage(
            content='{"code": 500, "success": false}', name="blade_create",
            tool_call_id="tc-b",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "args": {"subcommand": "exec", "v_args": f"pod-1 -- {inner_cmd}"},
                    "id": tc_id,
                }
            ],
        ),
        ToolMessage(content="ok", name="kubectl", tool_call_id=tc_id),
    ]

    assert (
        _was_kubectl_injection_attempted(
            failed_blade_then("python -c stress", "tc-e")
        )
        is True
    )
    assert (
        _was_kubectl_injection_attempted(
            failed_blade_then("cat /etc/hosts", "tc-e2")
        )
        is False
    )


# ---------------------------------------------------------------------------
# B47: English verbs require COMMAND POSITION (``kubectl [--flags] <verb>``).
# A bare prose mention of a verb is not an action-bearing form — task
# inject-65a44501 (case #34) flagged ``uncordon`` from a REASONING CITATION
# inside a step ("仅放行节点自操作（uncordon 族，#30 实测立法）"), nudging the
# model toward an out-of-scope mutation it correctly refused, at the cost of
# an execute iteration.
# ---------------------------------------------------------------------------

C34_REPRO_CASE = """**演练步骤**：
1. 定位目标 Deployment 并记录副本数（基线快照）
2. 恢复通道对称性定案：宿主 timer 通道仅放行节点自操作（uncordon 族，#30 实测立法），scale deployment 属跨资源写必拒；注入用 `kubectl scale deployment <name> -n <ns> --replicas=0`
3. 观察 Pod 缩容过程和应用状态变化

**注入验证**：
1. 执行 `kubectl get pods`
"""

_MESSAGES_C34 = [
    AIMessage(
        content="",
        tool_calls=[
            {
                "name": "kubectl",
                "args": {
                    "subcommand": "scale",
                    "v_args": "deployment drill --replicas=0",
                },
                "id": "tc-s",
            }
        ],
    ),
    ToolMessage(
        content="deployment.apps/drill scaled", name="kubectl", tool_call_id="tc-s"
    ),
]


def test_b47_prose_verb_mention_is_not_required():
    """B47 regression (the #34 specimen): a step's REASONING citation of
    ``uncordon`` (bare prose, no kubectl invocation around it) must NOT
    become a REQUIRED verb — scale (the real action, in command position)
    executed → no missing action → no reminder. Pre-fix this fired a soft
    nudge toward an out-of-scope ``kubectl uncordon``."""
    out = build_injection_step_selfcheck(C34_REPRO_CASE, _MESSAGES_C34, "kubectl_native")
    assert out is None


def test_b47_command_position_verb_is_still_required():
    """B47 counterpart: the verb in a real kubectl invocation's subcommand
    position — the #21/#30 timer-payload shape with flags between ``kubectl``
    and the verb — IS required, and flags out as missing when not executed."""
    case = """**演练步骤**：
1. 记录基线
2. 武装恢复：chroot /host systemd-run --on-active=600s --unit=blade-restore kubectl --kubeconfig=/etc/kubernetes/kubelet.conf uncordon <node>
3. cordon 目标节点：`kubectl cordon <node>`

**注入验证**：
1. 执行 `kubectl get nodes`
"""
    out = build_injection_step_selfcheck(case, [], "kubectl_native")
    assert out is not None
    # 标记为不可调度 → cordon (chinese map) + uncordon (command position):
    # nothing was executed, both are missing.
    assert "uncordon (" in out
    assert "cordon (" in out


def test_b47_quote_wrapped_payload_verb_is_still_required():
    """B47 counterpart: the verb inside a quoted payload handed to sh -c
    still counts (``sh -c 'kubectl ... <verb> ...'``) — the regex must see
    through the quotes to the invocation, not just match bare tokens."""
    case = """**演练步骤**：
1. 记录基线
2. 武装恢复：kubectl exec <pod> -- sh -c 'systemd-run --on-active=600s kubectl uncordon <node>'

**注入验证**：
1. 执行 `kubectl get nodes`
"""
    out = build_injection_step_selfcheck(case, [], "kubectl_native")
    assert out is not None
    assert "uncordon (" in out


def test_b47_uppercase_prose_rbac_verbs_are_not_required():
    """B47 (scan-driven): prose spellings of RBAC verbs (``PATCH deployment
    摘卷 + DELETE PVC``) are mentions, not actions — lowered, they used to
    match ``\\bpatch\\b`` / ``\\bdelete\\b`` and manufacture REQUIRED verbs.
    Under command-position anchoring they are ignored."""
    case = """**演练步骤**：
1. 记录基线
2. 验权说明（PATCH deployment 摘卷 + DELETE PVC，Role 按标准件第二节推导），注入用 `kubectl patch deployment <name> -p '<json>'`
3. 观察结果

**注入验证**：
1. 执行 `kubectl get pods`
"""
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "args": {
                        "subcommand": "patch",
                        "v_args": "deployment drill -p {}",
                    },
                    "id": "tc-p",
                }
            ],
        ),
        ToolMessage(
            content="deployment.apps/drill patched",
            name="kubectl",
            tool_call_id="tc-p",
        ),
    ]
    out = build_injection_step_selfcheck(case, messages, "kubectl_native")
    # patch was documented in command position AND executed → no missing
    # verbs (the prose DELETE must not be required).
    assert out is None
