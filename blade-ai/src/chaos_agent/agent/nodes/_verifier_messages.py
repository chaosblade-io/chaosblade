"""Verifier messages domain: synthetic message construction for Layer 2.

Extracted from verifier.py to isolate the messages construction logic
(baseline ToolMessages and the full Layer 2 prompt builder) from the
verifier node entry points and orchestration code.
"""

import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes._verifier_hints import (
    _extract_baseline_key_metrics,
    _derive_disk_fill_partition,
    _BASELINE_INTEGRITY_PROMPT,
    _get_fault_verification_hints,
)
from chaos_agent.agent.nodes._verifier_layer1 import Layer1Result
from chaos_agent.agent.nodes._verifier_layer2_parse import (
    _extract_verification_step_descriptions,
    _has_injection_verification_section,
)
from chaos_agent.agent.nodes._verifier_shared import _IMAGEFS_PATHS, _NODEFS_PATHS
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)
_BASELINE_TOOL_CALL_ID = "baseline_collector"
_METRICS_TOOL_CALL_ID = "baseline_collector_metrics"

# Aggregate set of all synthetic tool_call_ids used for state persistence.
_SYNTHETIC_TOOL_CALL_IDS = frozenset({
    _BASELINE_TOOL_CALL_ID,
    _METRICS_TOOL_CALL_ID,
})

# Marker for the main verifier context HumanMessage — used to identify
# the ephemeral HumanMessage that should be persisted to AgentState on
# count==1 so it remains visible on subsequent iterations.
_VERIFIER_CONTEXT_KWARGS_KEY = "_verifier_main_context"

# ---------------------------------------------------------------------------
# Baseline data → synthetic ToolMessage injection
#
# Baseline data was previously injected as a plain-text section inside the
# verifier HumanMessage.  LLMs treated it as "external reference material"
# and often ignored it (confirmation bias: baseline contradicts the conclusion
# the LLM already formed → LLM discards baseline and declares BaselineUsed=false).
#
# By converting baseline data into synthetic AIMessage+ToolMessage pairs,
# the LLM perceives it as "tool call results I already obtained" — creating
# a causal narrative ("I ran kubectl before injection → got these numbers →
# now I must compare") instead of "someone gave me a data section".
#
# Academic basis:
#   - Lost in the Middle (Liu 2023): position > role for attention; early
#     placement avoids "middle invisibility"
#   - TIM-PRM (Kuang 2024): independent tool queries eliminate confirmation
#     bias by decoupling evidence acquisition from reasoning chain
#   - VERITAS (Xu 2025): LLMs don't inherently trust ToolMessage more than
#     HumanMessage — the causal narrative framing is what matters, not the
#     message role per se.
# ---------------------------------------------------------------------------


def _build_baseline_tool_messages(
    baseline: dict,
    blade_target: str,
    blade_action: str,
    blade_parsed: dict | None = None,
) -> list:
    """Build synthetic AIMessage + ToolMessage pairs for pre-injection baseline data.

    Converts the baseline_data dict (from baseline_capture node) into one or
    two synthetic tool call result pairs that inject baseline observations and
    extracted key metrics into the LLM's context as "already obtained" tool
    results, with causal narrative framing.

    Returns:
        List of [AIMessage, ToolMessage] pairs (2–4 messages total).
        Empty list if baseline has no usable data.
    """
    if not baseline or baseline.get("success_count", 0) <= 0:
        return []

    captured_at = baseline.get("captured_at", "unknown time")
    source = baseline.get("source", "unknown")
    observations = baseline.get("observations", [])

    # ── Pair 1: Raw baseline observations ──
    # Each successful observation (exit_code=0, has stdout) becomes part
    # of the tool result content, with causal narrative framing.
    obs_lines = []
    for obs in observations:
        if obs.get("exit_code") != 0 or not obs.get("stdout"):
            continue
        desc = obs.get("description", "unknown metric")
        cmd = obs.get("command", "")
        output = obs["stdout"][:1500]
        if len(obs["stdout"]) > 1500:
            output += "\n... (truncated)"
        obs_lines.append(
            f"### {desc}\n"
            f"Command: `{cmd}`\n"
            f"```\n{output}\n```"
        )

    if not obs_lines:
        return []

    raw_content = (
        f"Pre-injection baseline collected at {captured_at} "
        f"(strategy: {source}, {baseline.get('success_count', 0)}/{baseline.get('total_count', 0)} succeeded).\n\n"
        f"These metrics were captured BEFORE fault injection using the same "
        f"kubectl commands you would use for verification. "
        f"This is your authoritative reference — compare every post-injection "
        f"observation against these values using delta format: "
        f"\"baseline: X → current: Y (ΔZ)\".\n\n"
        + "\n\n".join(obs_lines)
    )

    ai_msg_1 = AIMessage(
        content="",
        tool_calls=[{
            "name": "baseline_collector",
            "args": {"phase": "pre-injection", "target": "all_metrics"},
            "id": _BASELINE_TOOL_CALL_ID,
            "type": "tool_call",
        }],
    )
    tool_msg_1 = ToolMessage(
        content=raw_content,
        tool_call_id=_BASELINE_TOOL_CALL_ID,
        name="baseline_collector",
    )

    # ── Pair 2: Extracted key metrics + comparison semantics ──
    # Pre-extracted metrics so LLM doesn't need to parse raw kubectl output
    # to find key numbers.  Includes mandatory comparison format instructions.
    key_metrics = _extract_baseline_key_metrics(baseline, blade_target, blade_action)
    metrics_parts = []
    if key_metrics:
        metrics_lines = "\n".join(f"- {k}: {v}" for k, v in key_metrics.items())
        metrics_parts.append(
            f"### Baseline Key Metrics (extracted — compare against these)\n"
            f"{metrics_lines}"
        )
    # Disk-fill partition targeting
    if blade_target == "disk" and blade_action == "fill":
        _ptype = _derive_disk_fill_partition(blade_parsed) if blade_parsed else None
        _path = blade_parsed.get("path", "") if blade_parsed else ""
        partition_desc = (
            f"### Baseline Partition Target (for disk-fill)\n"
            f"Injection --path={_path} → target partition: "
            f"{_ptype or 'unknown (check df -h bare output)'}\n"
        )
        if _ptype == "imagefs":
            partition_desc += (
                "In the baseline `df -h` above, find the overlay/imagefs line "
                "(e.g., `/dev/vdb` or similar, typically no `/host` in the mount point). "
                "Compare POST-injection overlay usage against THIS line. "
                "DO NOT compare against the nodefs (root) line — that partition was NOT targeted.\n"
            )
        elif _ptype == "nodefs":
            partition_desc += (
                "In the baseline `df -h` above, find the nodefs (root) line "
                "(e.g., `/dev/vda3`, typically mounted at `/host`). "
                "Compare POST-injection root usage against THIS line. "
                "DO NOT compare against the overlay line — that partition was NOT targeted.\n"
            )
        else:
            partition_desc += (
                "Partition type could not be determined from --path. "
                "Check ALL partitions in the baseline df -h above. "
                "Your post-injection df -h (bare) will show which partition changed.\n"
            )
        metrics_parts.append(partition_desc)

    # Comparison semantics — the causal narrative that makes baseline USAGE mandatory
    semantics = (
        "### Baseline Comparison Rules\n"
        "You now have PRE-INJECTION baseline data (captured BEFORE the fault was injected). "
        "This is MORE RELIABLE than first-check-as-baseline because the baseline values "
        "are guaranteed to be unaffected by the fault.\n"
        "Rules:\n"
        "- Compare post-injection metrics against the pre-injection baseline above, "
        "NOT against your first post-injection check.\n"
        "- A significant change from baseline (e.g., disk 10%→13%, CPU 100m→800m, "
        "RestartCount 7→8) is STRONG evidence the fault is in effect.\n"
        "- If metrics are SIMILAR to baseline, the fault may not be working.\n"
        "- This PRE-INJECTION baseline takes priority over the general FIRST-CHECK-AS-BASELINE "
        "rule in the Baseline Integrity prompt.\n\n"
        "**FORMAT REQUIREMENT (mandatory when Pre-Injection Baseline is available)**:\n"
        "Each checklist step's evidence MUST include baseline comparison in the format:\n"
        "  \"baseline: <metric from above> → post-injection: <metric you observe NOW> (Δ<change>)\"\n"
        "Steps that omit baseline comparison when baseline data is available "
        "will be flagged as INCOMPLETE and may trigger re-verification.\n"
        "In your VERIFICATION_RESULT, set BaselineUsed: true.\n"
    )
    metrics_parts.append(semantics)

    if not metrics_parts:
        # No key metrics or partition info — the raw pair alone is sufficient
        return [ai_msg_1, tool_msg_1]

    metrics_content = "\n\n".join(metrics_parts)

    # Second synthetic pair for structured metrics + semantics
    ai_msg_2 = AIMessage(
        content="",
        tool_calls=[{
            "name": "baseline_collector",
            "args": {"phase": "pre-injection", "target": "key_metrics_summary"},
            "id": _METRICS_TOOL_CALL_ID,
            "type": "tool_call",
        }],
    )
    tool_msg_2 = ToolMessage(
        content=metrics_content,
        tool_call_id=_METRICS_TOOL_CALL_ID,
        name="baseline_collector",
    )

    return [ai_msg_1, tool_msg_1, ai_msg_2, tool_msg_2]








# ---------------------------------------------------------------------------
# Refactor 7: 提取 Layer 2 prompt 构建为独立函数
# 原因: prompt 拼接 + 收敛提示逻辑与 LLM 调用逻辑混在一起
# 做法: 独立函数，输入结构化参数，输出 HumanMessage 列表
# ---------------------------------------------------------------------------


def _build_convergence_hint(count: int) -> str:
    """Build a convergence hint string based on iteration count.

    3-tier system (matching execute_loop pattern):
    - Tier 1: soft warning when iterations are running low
    - Tier 2: urgent warning on second-to-last iteration
    - Empty string when no hint is needed
    """
    remaining = settings.max_verifier_loop - count
    if settings.max_verifier_loop - 3 <= count < settings.max_verifier_loop - 1:
        return (
            f"\n\n**Iteration Progress**: You are on iteration {count} of max {settings.max_verifier_loop} "
            f"({remaining} remaining). "
            f"If you have gathered enough evidence, output the VERIFICATION_RESULT format now. "
            f"If you need more data, focus on the most critical checks only."
        )
    if count >= settings.max_verifier_loop - 1:
        return (
            f"\n\n**VERIFICATION DEADLINE**: This is iteration {count} of max {settings.max_verifier_loop} — "
            f"your SECOND-TO-LAST iteration.\n"
            f"Based on ALL evidence gathered so far:\n"
            f"  - If you have sufficient data, output the VERIFICATION_RESULT format NOW.\n"
            f"  - If you need ONE more check, do it now — but you MUST conclude on the next iteration.\n\n"
            f"Your Overall conclusion must be one of:\n"
            f"  - **verified**: Fault effect is confirmed present on the target\n"
            f"  - **partial**: Some evidence supports the fault, but not fully confirmed\n"
            f"  - **unverified**: Fault effect could NOT be confirmed despite checks\n"
        )
    return ""


def _build_layer2_messages(
    state: AgentState,
    layer1: Layer1Result,
    blade_uid: str,
    skill_name: str,
    kubeconfig: str,
    count: int,
    tool_pod_name: str | None = None,
) -> list:
    """Build messages for Layer 2 LLM invocation.

    On first iteration, injects the full Layer 1 context.
    On subsequent iterations approaching the limit, injects a convergence hint.
    """
    messages = list(state.get("messages", []))
    convergence_hint = _build_convergence_hint(count)

    # ── Position-optimized synthetic message injection ──
    # Baseline ToolMessages before the HumanMessage or convergence hint
    # (Lost in the Middle: early placement gets higher attention).
    # Inject on EVERY iteration, not just count==1, because they are NOT
    # persisted in AgentState.messages by default (result_update only
    # contains the LLM's response).  Without this, LLM loses baseline
    # data on count > 1.  When already in state history (persisted from
    # count==1 via result_update), skip to avoid duplication.
    # State-derived variables needed by _build_baseline_tool_messages.
    from chaos_agent.agent.fault_spec import read_fault_spec as _rfs_vm
    _spec_vm = _rfs_vm(state)
    _blade_target = _spec_vm.blade_target if _spec_vm else ""
    _blade_action = _spec_vm.blade_action if _spec_vm else ""
    _blade_parsed = state.get("blade_parsed_flags") or {}

    _baseline = state.get("baseline_data")
    if _baseline and _baseline.get("success_count", 0) > 0:
        _baseline_in_state = any(
            getattr(m, "tool_call_id", "") == _BASELINE_TOOL_CALL_ID
            for m in messages if isinstance(m, ToolMessage)
        )
        if not _baseline_in_state:
            messages.extend(_build_baseline_tool_messages(
                _baseline, _blade_target, _blade_action, _blade_parsed,
            ))
    if count == 1:
        context = _build_first_iteration_context(
            state, layer1, blade_uid, skill_name, kubeconfig,
            tool_pod_name, convergence_hint,
        )
        messages.append(HumanMessage(
            content=context,
            additional_kwargs={_VERIFIER_CONTEXT_KWARGS_KEY: True},
        ))
    elif convergence_hint:
        # Subsequent iterations approaching limit: inject convergence nudge
        messages.append(HumanMessage(content=convergence_hint.strip()))

    # Final-iteration conclusion prompt (tools will be unbound at this count)
    if count >= settings.max_verifier_loop:
        messages.append(HumanMessage(content=(
            f"**FINAL VERIFICATION ITERATION**: This is iteration {count} of max {settings.max_verifier_loop}. "
            f"NO more iterations available. Tools are no longer available.\n"
            f"You MUST provide your final verification conclusion NOW in this EXACT format:\n\n"
            f"VERIFICATION_CHECKLIST:\n"
            f"- Step 1: passed/failed/skipped — brief evidence\n"
            f"- Step 2: passed/failed/skipped — brief evidence\n"
            f"- ...\n\n"
            f"VERIFICATION_RESULT:\n"
            f"- Layer1 (blade_status): passed/failed/skipped\n"
            f"- Layer2 (fault-specific): passed/failed/skipped - evidence summary\n"
            f"- Overall: verified/partial/unverified\n"
            f"- BaselineUsed: true/false (whether pre-injection baseline was compared in evidence)\n"
            f"- Warnings: any warnings, or \"none\"\n\n"
            f"Layer 2 Status Definitions: 'passed' = fault effect IS observable (injection WORKED); "
            f"'failed' = fault effect is NOT observable (injection may not have worked); "
            f"'skipped' = could not verify.\n"
            f"Do NOT conclude 'failed' if evidence shows the fault IS in effect.\n\n"
            f"If you cannot determine the result, set Overall to \"unverified\" and explain why in Layer2 details."
        )))

    return messages

def _build_first_iteration_context(
    state: AgentState,
    layer1: Layer1Result,
    blade_uid: str,
    skill_name: str,
    kubeconfig: str,
    tool_pod_name: str | None,
    convergence_hint: str,
) -> str:
    """Build the full Layer 2 context string for the first verification iteration.

    Assembles Layer 1 results, fault metadata, baseline references,
    skill-case verification strategy, and behavioral rules into a single
    context string for the HumanMessage.
    """
    from chaos_agent.agent.fault_spec import read_fault_spec as _rfs_vm
    _spec_vm = _rfs_vm(state)
    _params = dict(_spec_vm.params) if _spec_vm else {}
    _blade_target = _spec_vm.blade_target if _spec_vm else ""
    _blade_action = _spec_vm.blade_action if _spec_vm else ""
    _blade_parsed = state.get("blade_parsed_flags") or {}

    # First iteration: inject full Layer 1 context
    target = {
        "namespace": _spec_vm.namespace if _spec_vm else "",
        "names": list(_spec_vm.names) if _spec_vm else [],
        "labels": dict(_spec_vm.labels) if _spec_vm else {},
        "resource_type": _spec_vm.scope if _spec_vm else "",
    }
    params = _params
    injection_method = state.get("injection_method")
    blade_scope = _spec_vm.scope if _spec_vm else ""
    blade_target = _blade_target
    blade_action = _blade_action

    # Build Layer 1 context section (adapted for skipped vs passed)
    _is_self_destructive = (
        layer1.status == "skipped"
        and "self-destructive" in (layer1.details or "").lower()
    )
    if _is_self_destructive:
        layer1_context = (
            "## Layer 1 Result (SKIPPED — self-destructive fault)\n"
            f"blade_status unreachable: {layer1.details}\n\n"
            "## WARNING: Target node is NotReady\n"
            "The target node lost connectivity — this is likely the "
            "injection effect itself (e.g. containerd/kubelet stopped). "
            "Your verification MUST use commands that go through the "
            "API server (not the node):\n"
            "- `kubectl get nodes` — confirm node NotReady\n"
            "- `kubectl get pods` — check for ContainerCreating / Terminating\n"
            "- `kubectl describe pod` — check Events for runtime errors\n"
            "- `kubectl get events` — check for node-level events\n"
            "Do NOT use `kubectl exec` into pods on the affected node "
            "(it will fail because the node is unreachable).\n\n"
        )
        layer2_instruction = (
            "This is a self-destructive fault: the injection destroyed "
            "the node's communication channel. Verify the EFFECT of the "
            "fault (node NotReady, pods in abnormal state) rather than "
            "the injection mechanism (blade_status).\n"
        )
    elif layer1.status == "skipped":
        layer1_context = (
            "## Layer 1 Result\n"
            "Layer 1 skipped: non-ChaosBlade fault (no blade_uid). "
            "Proceed directly to Layer 2 verification.\n\n"
        )
        layer2_instruction = (
            "This is a non-ChaosBlade fault injection. "
            "Perform Layer 2 verification: use kubectl tools to verify "
            "the fault is actually in effect on the target.\n"
        )
    else:
        layer1_context = (
            f"## Layer 1 Result (already completed)\n"
            f"blade_status for UID {blade_uid}: {layer1.status}\n"
            f"Details: {layer1.raw_output[:500]}\n\n"
        )
        if layer1.expired:
            layer2_instruction = (
                "Layer 1 shows the experiment has EXPIRED (status: Destroyed/Revoked). "
                "The fault was injected but has already timed out. "
                "You should still perform Layer 2 verification to confirm whether "
                "any residual effects remain, but expect that fault effects have dissipated. "
                "If no fault effects are observable, conclude Layer 2 as "
                "'recovered_before_observation' and add a Warning about the short duration.\n"
            )
        else:
            layer2_instruction = (
                "Layer 1 is PASSED. Now perform Layer 2 verification: "
                "use kubectl tools to verify the fault is actually in effect on the target.\n"
            )

    # Resource coverage from blade_query_k8s (if available)
    coverage_context = ""
    if layer1.affected_count > 0:
        coverage_context = "\n## Resource Coverage (from blade_query_k8s)\n"
        coverage_context += f"Affected resources: {layer1.affected_count}\n"
        if layer1.resource_statuses:
            coverage_context += "Per-resource details:\n"
            for rs in layer1.resource_statuses:
                identifier = rs.get("identifier", rs.get("id", "?"))
                state_val = rs.get("state", "?")
                success = rs.get("success", "?")
                coverage_context += f"  - {identifier}: {state_val} (success={success})\n"
        target_names = target.get("names", [])
        if target_names:
            coverage_context += f"Target resources from context: {target_names}\n"
        coverage_context += (
            "\nCompare affected_count against the expected number of target resources. "
            "If fewer resources are affected than expected, the fault injection has "
            "INCOMPLETE COVERAGE. Investigate and report this in VERIFICATION_RESULT Warnings.\n"
        )

    # Build fault metadata section
    fault_metadata = ""
    if blade_scope or blade_target or blade_action or injection_method:
        parts = []
        if blade_scope:
            parts.append(f"Scope: {blade_scope}")
        if blade_target:
            parts.append(f"Target: {blade_target}")
        if blade_action:
            parts.append(f"Action: {blade_action}")
        if injection_method:
            parts.append(f"Injection method: {injection_method}")
        fault_metadata = " | ".join(parts)

    context = (
        f"{layer1_context}"
        f"{coverage_context}"
        f"## Fault Context\n"
        f"Skill: {skill_name}\n"
        f"Target namespace: {target.get('namespace', '')}\n"
        f"Target names: {target.get('names', [])}\n"
        f"Blade params: {params}\n"
        f"Kubeconfig: {'(kubewiz)' if settings.kube_connection_mode == 'kubewiz' else (kubeconfig or '(default)')}\n"
    )
    # Structured key parameters from parsed flags (e.g. path, percent, size)
    blade_parsed = state.get("blade_parsed_flags") or {}
    if blade_parsed:
        context += f"Blade key parameters: {blade_parsed}\n"
    # Inline path semantics: when path is specified for node-disk scenarios,
    # add a context-specific MANDATORY verification note right next to the
    # path parameter.  This is more effective than a generic hint buried in
    # the Verification Hints section because the LLM sees the constraint
    # at the exact moment it reads the parameter value.
    if blade_target == "disk" and blade_scope == "node" and "path" in blade_parsed:
        _path_val = blade_parsed["path"]
        _path_norm = _path_val.rstrip("/")
        if _path_norm in _IMAGEFS_PATHS or any(
            _path_norm.startswith(p.rstrip("/") + "/") for p in _IMAGEFS_PATHS
        ):
            context += (
                f"⚠ CRITICAL path semantics: --path {_path_val} in K8s CRD mode fills "
                f"INSIDE the container overlay (typically backed by imagefs if the node has "
                f"a separate imagefs; otherwise on nodefs), NOT the host path "
                f"/host{_path_val}. The fill file will NOT appear at /host{_path_val}.\n"
                f"COMMAND PRIORITY: Your FIRST disk check MUST be `df -h` (bare, no path argument) "
                f"to identify ALL partitions. Run it via kubectl exec on the host-access pod "
                f"surfaced in the Verification Hints below. `df -h /host` shows nodefs ONLY — "
                f"if fill targeted imagefs, it shows NO change (false negative).\n"
            )
        elif _path_norm in _NODEFS_PATHS or any(
            _path_norm.startswith(p.rstrip("/") + "/") for p in _NODEFS_PATHS
        ):
            context += (
                f"⚠ CRITICAL path semantics: --path {_path_val} in K8s CRD mode fills "
                f"typically the nodefs (root filesystem). NOTE: /var/lib/docker and "
                f"/var/lib/containerd on a separate disk define imagefs — if this node has "
                f"a separate imagefs, this path may have been on imagefs instead.\n"
                f"COMMAND PRIORITY: Use `df -h` (bare) first to list all partitions, then "
                f"`df -h /host` to confirm nodefs. `df -h /host` shows the root partition usage.\n"
            )
        else:
            context += (
                f"⚠ CRITICAL path semantics: --path {_path_val} — unable to determine "
                f"target partition automatically. YOU MUST use `df -h` (bare, no path) to list "
                f"ALL mounted filesystems and identify which partition shows increased usage.\n"
            )
    if fault_metadata:
        context += f"{fault_metadata}\n"
    # Timeout info: duration is auto-boosted, only add informational note
    _timeout_val = blade_parsed.get("timeout")
    if _timeout_val:
        try:
            _timeout_sec = int(str(_timeout_val).strip())
            if _timeout_sec < 600:
                context += (
                    f"ℹ Duration note: --timeout {_timeout_sec}s. "
                    f"If fault effects are not observable, consider that the fault "
                    f"may have timed out rather than failed.\n"
                )
        except (ValueError, TypeError):
            pass
    # Baseline data is now injected as synthetic AIMessage+ToolMessage pairs
    # (via _build_baseline_tool_messages) BEFORE the main HumanMessage,
    # instead of as a plain-text section inside HumanMessage.
    # This creates a "tool call result" narrative that makes the LLM
    # perceive baseline as "already obtained evidence" rather than
    # "external reference material" — reducing confirmation bias.
    #
    # The fallback "no usable data" note remains in HumanMessage because
    # there's nothing to convert to ToolMessage format.
    baseline = state.get("baseline_data")
    if not (baseline and baseline.get("success_count", 0) > 0):
        # Fallback: baseline capture produced no usable data
        context += (
            "\n## Baseline Data Note\n"
            "Baseline capture was attempted but produced no usable data. "
            "You MUST be cautious when interpreting absolute metric values — "
            "high resource usage does NOT necessarily mean the fault is in effect. "
            "Cross-validate with multiple independent data points "
            "(metrics + events + conditions) before concluding 'verified'.\n"
        )
    # Disk-fill specific: scenario vs injection criterion (applies regardless of baseline)
    if blade_target == "disk" and blade_action == "fill":
        context += (
            "Disk-fill specific: The skill case's '确认超过85%' is a SCENARIO SUCCESS criterion, "
            "NOT an injection verification criterion. If fill data was written (fill file exists OR "
            "disk usage increased by ≈size from baseline) but 85% was not reached → Layer2 = PASSED with Warning: "
            "'Fill verified (X GB written), disk at Y% did not reach scenario target of 85%. "
            "Consider --percent=85 or increase --size.'\n"
        )
    # Tool pod context: provide accurate information about tool pod capabilities
    if blade_scope == "node" and tool_pod_name:
        # Tool pods discovered during injection live in the chaosblade namespace
        # by convention; the legacy default applies here. Host-level checks
        # that need /host filesystem access can also use
        # kubectl_verify(subcommand="debug") to spawn an ephemeral debug pod.
        _tp_ns = "chaosblade"
        context += (
            f"\n## Available Tool Pod\n"
            f"A tool pod is available for cluster-level operations:\n"
            f"- Pod name: `{tool_pod_name}`\n"
            f"- Namespace: `{_tp_ns}`\n"
            f"- Access: kubectl(subcommand='exec', v_args='{tool_pod_name} -n {_tp_ns} -- <command>', kubeconfig='{kubeconfig or '<path>'}')\n"
            f"- Capabilities: ChaosBlade commands (blade status/destroy), kubectl API checks (describe/top/get), "
            f"and host-level checks via `/host/...` (the tool pod typically mounts the host root).\n"
            f"- For CRD-mode disk fill, the fill file IS in the container overlay — "
            f"checking it inside this pod is the PRIMARY verification method.\n"
            f"- For host filesystem verification (e.g., `/proc/loadavg`, `/var/log`), "
            f"prefix paths with `/host` (try `/host/proc/loadavg` first; if missing, "
            f"fall back to bare `/proc/loadavg`). If this tool pod is unavailable or "
            f"lacks /host access, fall back to "
            f"`kubectl_verify(subcommand='debug', v_args='node/<node> --image=busybox -- sleep 3600')` "
            f"and exec into the resulting debug pod.\n"
            f"- **UID Dual Mapping**: The blade_uid ({blade_uid}) is the CRD resource name. "
            f"Inside the tool pod, `blade status <uid>` searches the LOCAL experiment database "
            f"and will likely return 'record not found' (because the experiment was created "
            f"via CRD, not via the local CLI).\n"
            f"  **CORRECT** — query CRD status via API server:\n"
            f"    kubectl(subcommand='exec', v_args='{tool_pod_name} -n {_tp_ns} -- /opt/chaosblade/blade query k8s create {blade_uid}', kubeconfig='{kubeconfig or '<path>'}')\n"
            f"  **CORRECT** — check CRD directly:\n"
            f"    kubectl(subcommand='get', v_args='chaosblade {blade_uid} -o jsonpath=\"{{.status}}\"', kubeconfig='{kubeconfig or '<path>'}')\n"
            f"  **FORBIDDEN** — NEVER use `blade status` with a CRD UID, it will return "
            f"'record not found' and cause a false-negative Layer 2 conclusion:\n"
            f"    kubectl(subcommand='exec', v_args='{tool_pod_name} -n {_tp_ns} -- /opt/chaosblade/blade status {blade_uid}', kubeconfig='{kubeconfig or '<path>'}')\n"
        )
    # Programmatic post-check: injection engine already verified the fill effect
    # during direct_execute. This is authoritative — present it BEFORE verification
    # instructions so the LLM can use it as primary evidence.
    _post_check = state.get("disk_fill_post_check") or params.get("disk_fill_post_check")
    if _post_check and isinstance(_post_check, dict):
        _fill_found = _post_check.get("fill_file_found", False)
        _target_pod = _post_check.get("target_pod", "unknown")
        _ls_out = _post_check.get("ls_output", "")
        _df_out = _post_check.get("df_output", "")
        context += (
            f"\n## Injection Engine Post-Check (already executed)\n"
            f"The injection engine programmatically verified the fill effect on "
            f"target node via tool pod `{_target_pod}`:\n"
        )
        if _fill_found:
            context += (
                f"- **Fill file FOUND** in container overlay — injection is WORKING.\n"
                f"- `ls` output:\n```\n{_ls_out[:300]}\n```\n"
            )
        else:
            context += (
                f"- **Fill file NOT found** in container overlay.\n"
                f"- `ls` output:\n```\n{_ls_out[:300]}\n```\n"
            )
        if _df_out:
            context += (
                f"- `df -h` output:\n```\n{_df_out[:300]}\n```\n"
            )
        context += (
            "Use this as PRIMARY evidence. If fill file was found, you have direct "
            "proof the fault is in effect. If not found, the fault may not have "
            "worked — cross-validate with other checks.\n"
        )
    # Programmatic post-check: injection engine already verified the burn I/O effect
    # during direct_execute. This is authoritative — present it BEFORE verification
    # instructions so the LLM can use it as primary evidence.
    _burn_check = state.get("disk_burn_post_check") or params.get("disk_burn_post_check")
    if _burn_check and isinstance(_burn_check, dict):
        _burn_detected = _burn_check.get("burn_io_detected", False)
        _active_parts = _burn_check.get("active_partitions", [])
        _burn_target_pod = _burn_check.get("target_pod", "unknown")
        _burn_node = _burn_check.get("node", "unknown")
        _burn_scope = _burn_check.get("scope", "node")
        if _burn_scope == "pod":
            _scope_desc = (
                f"target pod's node `{_burn_node}` via pod `{_burn_target_pod}`"
            )
        else:
            _scope_desc = (
                f"target node `{_burn_node}` via tool pod `{_burn_target_pod}`"
            )
        if _burn_detected:
            _parts_str = ", ".join(
                f"{p['name']}: ~{p['write_throughput_mb_s']} MB/s"
                for p in _active_parts[:5]
            )
            context += (
                f"\n## Disk Burn I/O Pre-Check (AUTHORITATIVE)\n"
                f"The injection engine programmatically verified the burn I/O effect on "
                f"{_scope_desc}.\n"
                f"Programmatic check confirmed: disk burn I/O is ACTIVE.\n"
                f"Write throughput on partition(s): {_parts_str}\n"
                f"This is DEFINITIVE evidence that the disk burn fault is in effect — "
                f"the dd processes are actively writing to the container overlay.\n"
                f"The I/O appears on the overlay's backing partition (typically imagefs, "
                f"e.g. /dev/vdb), NOT on the nodefs partition (e.g. /dev/vda3) where "
                f"/host/tmp resides. This is expected: CRD-mode burn writes to the "
                f"container overlay, not the host filesystem.\n"
                f"DO NOT conclude \"failed\" based on /host/tmp/ having no burn files "
                f"or nodefs (vda3) showing no I/O — the burn is on a DIFFERENT partition.\n"
                f"You MUST mark the disk I/O verification step as 'passed' in your checklist.\n"
            )
        else:
            context += (
                f"\n## Disk Burn I/O Pre-Check\n"
                f"The injection engine programmatically verified the burn I/O effect on "
                f"{_scope_desc}.\n"
                f"Burn I/O NOT detected on any partition. "
                f"Active partitions: {_active_parts[:5] or 'none with measurable I/O'}\n"
                f"The fault may not be in effect despite blade query reporting Success. "
                f"Cross-validate with other checks (ps | grep dd, iostat).\n"
            )
    # P0-evidence-snapshot: pre-crash evidence for low-memory pods
    _evidence_snap = state.get("evidence_snapshot")
    if _evidence_snap and isinstance(_evidence_snap, dict):
        context += (
            "\n## Evidence Snapshot (already captured)\n"
            "The injection engine captured a quick evidence snapshot 3s after blade_create\n"
            "(for low-memory pods at risk of OOMKill before verification):\n"
        )
        for _snap_cmd, _snap_data in _evidence_snap.items():
            _snap_rc = _snap_data.get("rc", "?")
            _snap_out = (_snap_data.get("stdout") or "")[:300]
            context += f"- `{_snap_cmd}` → rc={_snap_rc}\n```\n{_snap_out}\n```\n"
        context += (
            "Use this as supplementary evidence. If the pod has since OOMKilled and restarted,\n"
            "this snapshot preserves the pre-crash state.\n"
        )
    # Fill File Check: absolute evidence for node-disk-fill verification (PRIMARY, before warnings)
    if blade_target == "disk" and blade_action == "fill" and blade_scope == "node":
        fill_path = blade_parsed.get("path", "/tmp")
        size_param = blade_parsed.get("size") or params.get("size")
        if tool_pod_name:
            context += (
                f"\n## PRIMARY VERIFICATION: Fill File Check\n"
                f"For node-disk-fill, the MOST RELIABLE verification is checking the fill file "
                f"directly inside the tool pod's container overlay:\n"
                f"1. Run: kubectl(subcommand='exec', v_args='{tool_pod_name} -n chaosblade -- ls -lh {fill_path}/', "
                f"kubeconfig='{kubeconfig or '<path>'}')\n"
                f"2. Look for: chaos_filldisk.log.dat (or similar chaos_fill* pattern)\n"
                f"   - If file EXISTS with size ≈ {size_param or '?'}MB → injection VERIFIED\n"
                f"   - This is ABSOLUTE evidence — no baseline comparison needed\n"
                f"3. DO NOT check /host{fill_path} for fill files — CRD mode writes to container "
                f"overlay, NOT host filesystem. Fill files are NEVER visible at /host{fill_path}.\n"
                f"Evidence hierarchy: (1) Fill file = PRIMARY, (2) Disk delta = SECONDARY, "
                f"(3) Absolute threshold = TERTIARY (scenario criterion only)\n"
            )
    # Disk fill size analysis: warn when size may not trigger observable effects
    if blade_target == "disk" and blade_action == "fill":
        # Fix: size is in blade_parsed_flags (parsed from flags), not a top-level params key
        size_param = (state.get("blade_parsed_flags") or {}).get("size") or params.get("size")
        if size_param:
            try:
                size_mb = int(str(size_param).strip())
                # Rough estimate: 100GB disk is common; 10GB = 10% which is observable
                if size_mb < 5120:
                    context += (
                        f"\n## Disk Fill Size Warning\n"
                        f"The fill size is {size_mb}MB (~{size_mb/1024:.1f}GB). "
                        f"On a typical 100GB node disk, this adds ~{size_mb/1024:.1f}% usage. "
                        f"This may be too small to trigger DiskPressure (>85%) or show visible df -h change. "
                        f"Consider using 'percent' parameter instead for more observable results.\n"
                    )
            except (ValueError, TypeError):
                pass
    context += (
        "\n## Injection Verification Instructions\n"
    )
    # Skill use-case content: PRIMARY AUTHORITY for verification
    skill_case = state.get("skill_case_content", "")
    if skill_case:
        context += (
            f"The following skill use-case defines how to verify this fault. "
            f"You MUST follow its verification approach as the primary reference.\n\n"
            f"<skill-case>\n{skill_case}\n</skill-case>\n\n"
        )
        # Four-tier verification mode based on skill case structure:
        # Mode 0 (Multi-candidate): multiple candidates → LLM chooses
        # Mode 1 (Template): 注入验证 has parseable numbered/bullet steps → pre-filled checklist
        # Mode 2 (Guided):  注入验证 exists but unparseable (prose only) → point LLM to prose
        # Mode 3 (Free):    no 注入验证 → current generic guidance
        is_multi_candidate = "--- Candidate" in skill_case

        if is_multi_candidate:
            # ═══ Mode 0: MULTI-CANDIDATE — LLM picks the right one ═══
            context += (
                f"### Verification Strategy (Multi-candidate)\n"
                f"Multiple skill cases are provided above. You MUST:\n"
                f"1. Read ALL candidates carefully\n"
                f"2. Choose the ONE most relevant to the actual fault "
                f"(scope={blade_scope}, target={blade_target}, "
                f"action={blade_action})\n"
                f"3. State which candidate you chose and why "
                f"(one sentence)\n"
                f"4. Follow THAT candidate's **注入验证** steps as your "
                f"verification checklist. If it has no 注入验证 section, "
                f"design your own checklist based on the candidate's "
                f"content. Do NOT mix content from different candidates\n"
                f"5. Output a VERIFICATION_CHECKLIST section with each "
                f"step and its result before VERIFICATION_RESULT\n\n"
                f"Rules:\n"
                f"1. Replace [status] with: passed, failed, skipped, "
                f"recovered_before_observation, or expected\n"
                f"2. After [status], write \" — \" followed by brief "
                f"evidence\n"
                f"3. If a step cannot be executed, mark as skipped "
                f"with reason\n"
                f"4. **MANDATORY OUTPUT**: You MUST output a "
                f"'VERIFICATION_CHECKLIST:' section BEFORE your final "
                f"'VERIFICATION_RESULT:' section.\n"
                f"5. **chosen_candidate**: When calling "
                f"`submit_verification`, you MUST set "
                f"`chosen_candidate` to the candidate number you chose "
                f"(e.g. `chosen_candidate=2` for Candidate 2).\n"
            )
        else:
            step_descs = _extract_verification_step_descriptions(skill_case)
            has_section = _has_injection_verification_section(skill_case)

        if not is_multi_candidate and step_descs:
            # ═══ Mode 1: TEMPLATE — structured steps extracted ═══
            template_lines = []
            for i, desc in enumerate(step_descs, start=1):
                template_lines.append(f"- Step {i}: [status] — {desc}")
            template_str = "\n".join(template_lines)
            context += (
                f"### Verification Strategy (Structured)\n"
                f"The skill case defines the following verification steps. "
                f"You MUST complete EVERY step. Do NOT invent, merge, skip, "
                f"or reorder steps.\n\n"
                f"**Pre-defined Verification Checklist** "
                f"(fill in [status] and evidence for each):\n"
                f"{template_str}\n\n"
                f"Rules:\n"
                f"1. Replace [status] with: passed, failed, skipped, "
                f"recovered_before_observation, or expected\n"
                f"2. After [status], write \" — \" followed by brief evidence "
                f"(what command you ran and what you observed)\n"
                f"3. If a step cannot be executed, mark as skipped with reason: "
                f"\"Step N: skipped — <reason>\"\n"
                f"4. **MANDATORY**: Your VERIFICATION_CHECKLIST MUST contain "
                f"ALL {len(step_descs)} steps exactly as listed. "
                f"Omitting steps is a protocol violation.\n"
                f"5. **DEVIATION DOCUMENTATION**: If you use a DIFFERENT method "
                f"than the one specified in a step (e.g., skill says 'ping' but "
                f"you use 'wget'), you MUST document the deviation reason: "
                f"\"Step N: passed — <what you did> (deviation: <why you deviated>)\". "
                f"If you execute the step as specified, no deviation note is needed.\n"
            )
        elif not is_multi_candidate and has_section:
            # ═══ Mode 2: GUIDED — 注入验证 exists but unparseable ═══
            context += (
                "### Verification Strategy (Guided)\n"
                "The skill case's **注入验证** section above contains "
                "verification guidance in prose format (no numbered steps). "
                "Read the section carefully and extract the verification "
                "intent. Design your own numbered VERIFICATION_CHECKLIST "
                "based on the checks described in the prose.\n\n"
                "Rules:\n"
                "1. Each checklist item must map to a distinct check "
                "described in the 注入验证 section\n"
                "2. Mark steps you cannot execute as: "
                "\"Step N: skipped — <reason>\"\n"
                "3. Do NOT add checks that are not mentioned in the skill case\n"
                "4. **Programmatic note**: Step coverage validation is "
                "DISABLED for this mode — we trust your extraction\n"
                "5. **MANDATORY OUTPUT**: You MUST output a "
                "'VERIFICATION_CHECKLIST:' section BEFORE your final "
                "'VERIFICATION_RESULT:' section.\n"
            )
        elif not is_multi_candidate:
            # ═══ Mode 3: FREE — no 注入验证 section at all ═══
            context += (
                "### Verification Strategy:\n"
                "1. Follow the **注入验证** section in the skill case above. "
                "You MUST execute EVERY verification step it lists — do not "
                "skip any step.\n"
                "2. If a step cannot be executed (e.g., no Ingress configured "
                "in this cluster), you MUST explicitly note: '[SKIPPED] "
                "Step N: <reason>'. Do NOT silently omit steps.\n"
                "3. Before your final conclusion, output a **Verification "
                "Checklist** listing each step and its result:\n"
                "   - Step 1: passed/failed/skipped — brief evidence\n"
                "   - Step 2: passed/failed/skipped — brief evidence\n"
                "   - ...\n"
                "4. If ALL steps pass → Layer2 'passed'. If ANY step fails → "
                "Layer2 'failed'. If mandatory steps are skipped without "
                "equivalent alternatives → Layer2 'partial'.\n"
                "5. **MANDATORY OUTPUT**: You MUST output a "
                "'VERIFICATION_CHECKLIST:' section BEFORE your final "
                "'VERIFICATION_RESULT:' section. This checklist will be "
                "parsed programmatically. Without it, your verification will "
                "be flagged as potentially incomplete and may be downgraded "
                "from 'verified' to 'partial'.\n"
            )
        pass  # NEGATIVE EVIDENCE moved to core behavioral rules (outside if/else)
        # Mode 1 only: step completeness tracking
        if not is_multi_candidate and step_descs:
            context += (
                "Note — Step completeness: your VERIFICATION_CHECKLIST MUST cover ALL "
                f"{len(step_descs)} steps listed above. Any step not executed must appear "
                "as '[SKIPPED] Step N: <reason>'. Omitting steps is a protocol violation.\n\n"
            )
        context += (
            "**Checklist Status Choice**:\n"
            "- The checklist reports OBSERVED FACTS, not predictions.\n"
            "- Did you call a kubectl command for this step? Yes → 'passed' or 'failed'. No → 'skipped'.\n"
            "- Timing uncertainty belongs in Warnings, not in checklist status.\n"
            "- 'recovered_before_observation': the fault was transient and had dissipated "
            "by the time you checked — distinct from 'failed' (checked, fault absent).\n\n"
        )
        # disk-burn transient fault rules (moved from system prompt)
        if blade_target == "disk" and blade_action == "burn":
            context += (
                "**Transient fault rules** (disk-burn): burn creates temporary files "
                "deleted on experiment completion. If ANY intermediate observation shows "
                "clear fault effects (metrics above baseline, burn files present), mark "
                "'passed' — even if later observation shows effect dissipated. "
                "Use 'recovered_before_observation' ONLY when NO observation at ANY "
                "point showed fault effects. If burn files gone BUT df -h shows usage "
                "increase from baseline (1-2GB+), use 'recovered_before_observation'. "
                "If df -h shows NO change AND no files → 'failed'.\n\n"
            )
        # Container restart detection (moved from system prompt)
        if blade_target in ("mem", "process"):
            context += (
                "**Container Restart Detection**: If the target container restarted "
                "during injection (restartCount increased, lastState.terminated present, "
                "OOMKilling events), classify as 'recovered_before_observation' — the "
                "restart is a SIDE EFFECT, not primary evidence. Set "
                "PrimaryEvidenceObserved: false when only a restart is observed.\n\n"
            )
        # ChaosBlade-specific: layer boundary
        if blade_uid:
            context += (
                "Note — Layer boundary: VERIFICATION_CHECKLIST must ONLY contain Layer 2 "
                "checks (observable fault effects). Do NOT include Layer 1 items "
                "(blade_status, experiment registration).\n\n"
            )
    else:
        context += (
            "No skill use-case content is available. Design verification based on "
            "the fault-specific hints below and your expertise.\n"
            "Before your final conclusion, output a **Verification Checklist** listing "
            "each check and its result:\n"
            "   - Check 1: passed/failed/skipped — brief evidence\n"
            "   - ...\n"
            "**WARNING**: Without skill guidance, verification may be incomplete. "
            "At minimum, verify the fault effect is observable on the target.\n"
            "**Knowledge docs**: Check the Domain Knowledge Index for documents whose "
            "\"When to read\" field covers your current scenario (e.g., verification "
            "strategies, kubectl field reference). Use `read_knowledge_resource` to "
            "load them before designing your verification plan.\n\n"
        )
    # ── Core behavioral rules (always added, positioned early) ──
    context += (
        f"{layer2_instruction}"
    )
    context += (
        "**NEGATIVE EVIDENCE ENUMERATION (CRITICAL)**:\n"
        "Before concluding Layer2 'passed', you MUST include a 'Negative Evidence' section "
        "in your reasoning that explicitly lists EVERY observation contradicting or weakening "
        "the conclusion that the fault is in effect. For each item, either:\n"
        "(a) Dismiss it with factual basis (not speculation), or\n"
        "(b) Accept it as valid counter-evidence.\n"
        "If ANY verification criterion is demonstrably NOT met, you MUST "
        "conclude Layer2 as 'partial' or 'failed' — NOT 'passed'.\n\n"
    )
    context += (
        f"**POLLING STRATEGY (CRITICAL)**: Fault effect may take 5-30s to appear. "
        f"Check at least 3 times before concluding. If any check shows the fault IS "
        f"in effect, conclude 'passed' immediately.\n"
        f"If after 3 checks NO evidence found → Layer2 'failed', Overall 'unverified'. "
        f"Do NOT conclude 'partial' when NO evidence exists.\n\n"
        f"**STEP CONCLUSION RULE**:\n"
        f"You may conclude any step early if continued attempts are unlikely to yield new information.\n"
        f"When concluding early, you MUST provide:\n"
        f"1. What you tried (commands/methods)\n"
        f"2. What you observed (actual output)\n"
        f"3. Why further attempts would not change the outcome\n\n"
    )
    # Add fault-specific verification hints when metadata is available
    verification_hints = _get_fault_verification_hints(
        blade_scope, blade_target, blade_action, injection_method,
        injection_pod_name=tool_pod_name,
        parsed_flags=blade_parsed,
    )
    if verification_hints:
        context += (
            f"\n### Fault-Specific Verification Hints\n"
            f"{verification_hints}\n\n"
        )
    # Injection method note: when kubectl_exec was used
    if injection_method == "kubectl_exec":
        context += (
            "\n### Injection Method Note\n"
            "The fault was injected via `kubectl exec` (the standard `blade_create` tool "
            "was unavailable). This means the injection method may differ from the skill "
            "case's recommended approach. You MUST:\n"
            "1. Check whether the ACTUAL injection method produces the same fault effects "
            "described in the skill case's verification steps\n"
            "2. If the expected fault effect differs, note this as a WARNING "
            "and adapt your verification accordingly\n\n"
        )
    # ── Conditional rules (only when relevant) ──
    if baseline and baseline.get("success_count", 0) > 0:
        context += f"{_BASELINE_INTEGRITY_PROMPT}\n\n"
    if blade_uid:
        context += (
            "**Layer 1 Limitation**: Layer 1 only checks whether the ChaosBlade experiment "
            "is registered. It does NOT verify that the fault effect is observable. "
            "Your Layer 2 verification is the ONLY way to confirm the fault is working.\n\n"
        )
    # BusyBox / minimal container note: conditional on injection method
    if injection_method == "kubectl_exec":
        context += (
            "### BusyBox Compatibility (MANDATORY)\n"
            "You are running verification commands inside a BusyBox container (via kubectl exec on a tool pod). "
            "Common Linux flags/commands may NOT be available — check the BusyBox Quick Reference above BEFORE "
            "issuing any command. Do NOT guess flags. If a command returns \"unrecognized option\" or "
            "\"bad usage\", do NOT retry similar commands — switch to the BusyBox alternative immediately.\n"
            "If kubectl exec commands consistently fail, fall back to `kubectl describe` for Pod-level "
            "metrics (restart count, conditions, events) as an alternative.\n\n"
        )
    else:
        context += (
            "**NOTE**: Some minimal container images lack common shell utilities (top, ps, netstat, etc.). "
            "If kubectl(subcommand='exec', ...) returns empty output or \"command not found\", do NOT retry — "
            "use kubectl(subcommand='describe', ...) instead.\n\n"
        )
    # ── Always-on helpers ──
    context += (
        f"### Test Pod Namespace Selection\n"
        f"When you need a Running application pod for verification tests "
        f"(DNS resolution, network connectivity, service calls), do NOT "
        f"only search the `default` namespace. Search in the TARGET "
        f"namespace first ({target.get('namespace', '') or 'see Fault Context'}), "
        f"then other namespaces with workloads. For cluster-wide faults "
        f"(DNS, node-level), any Running pod with shell access can serve "
        f"as a test target.\n\n"
    )
    if blade_scope == "node":
        context += (
            f"### Debug Pod Cleanup\n"
            f"If you create any pods during verification (e.g., temporary test pods), "
            f"you MUST delete them before finishing verification. Add cleanup as a "
            f"final step in your checklist.\n"
            f"Note: framework-managed host-access pods (the ChaosBlade tool pods "
            f"surfaced in the hints above) are DaemonSet-managed and MUST NOT be "
            f"deleted.\n"
        )
    context += convergence_hint
    return context




