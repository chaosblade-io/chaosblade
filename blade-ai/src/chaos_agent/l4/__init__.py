"""chaos_agent.l4 — L4 Agent SDK adapter for blade-ai.

Public API for ai-testing-platform integration:
- L4ResilienceAgent: main adapter implementing L4 lifecycle
- create_l4_adapter(): factory function
- get_agent_card(): returns AgentCard metadata dict for registration
"""

from __future__ import annotations

import dataclasses

from chaos_agent.l4.agent import L4ResilienceAgent
from chaos_agent.l4.schemas import FAULT_PAYLOAD_SCHEMA, L4AgentCard

__all__ = [
    "L4ResilienceAgent",
    "create_l4_adapter",
    "get_agent_card",
]


def create_l4_adapter() -> L4ResilienceAgent:
    """Create an L4 adapter instance."""
    return L4ResilienceAgent()


def get_agent_card() -> dict:
    """Return AgentCard metadata dict for ai-testing-platform registration."""
    card = L4AgentCard(
        agent_id="resilience",
        agent_type="resilience",
        description=(
            "K8s 混沌工程与可观测性专家：故障注入、集群状态查询、"
            "故障用例库、确定性恢复。基于 chaosblade + kubectl 真实执行。"
        ),
        capabilities=[
            # — 故障注入（scope × target × action 矩阵）—
            "chaos.inject.pod.cpu",
            "chaos.inject.pod.mem",
            "chaos.inject.pod.network",
            "chaos.inject.pod.disk",
            "chaos.inject.pod.process",
            "chaos.inject.container",
            "chaos.inject.node.cpu",
            "chaos.inject.node.mem",
            "chaos.inject.node.network",
            "chaos.inject.node.disk",
            # — 故障恢复 —
            "chaos.recover",
            # — 集群只读观察 —
            "k8s.observe.pods",
            "k8s.observe.nodes",
            "k8s.observe.events",
            "k8s.observe.logs",
            "k8s.observe.endpoints",
            "k8s.observe.api_discovery",
            "k8s.observe.in_pod_probe",
            "k8s.observe.host_probe",
            # — 故障用例库 —
            "chaos.catalogue.pod_lifecycle",
            "chaos.catalogue.workload",
            "chaos.catalogue.service",
            "chaos.catalogue.node",
            "chaos.catalogue.storage",
        ],
        capability_groups=[
            {
                "name": "故障注入",
                "summary": (
                    "按 scope × target × action 矩阵注入真实故障。"
                    "scope ∈ {pod, container, node}，"
                    "target ∈ {cpu, mem, network, disk, process}，"
                    "action ∈ {fullload, load, delay, loss, fill, kill, burn}。"
                ),
                "examples": [
                    "对 cms-demo namespace 的 web pod 打 60s CPU 满载",
                    "给 nginx pod 注入 200ms 网络延迟，持续 2 分钟",
                    "kill payment 服务的主进程",
                    "把 node-1 的磁盘填到 90%",
                ],
            },
            {
                "name": "故障恢复",
                "summary": "基于 blade_uid 确定性销毁故障实验，并验证资源恢复。",
                "examples": [
                    "恢复刚才那次 CPU 注入",
                    "把所有故障都清掉",
                ],
            },
            {
                "name": "集群只读观察",
                "summary": (
                    "查询 K8s 资源状态、事件、日志、端点、API 资源；"
                    "支持 pod 内部探针（exec ps/df/ping/nslookup）和宿主探针（debug node）。"
                ),
                "examples": [
                    "看一下 cms-demo 下所有 pod 的状态",
                    "node-2 现在 CPU/内存使用率多少",
                    "最近 5 分钟有什么异常事件",
                    "payment-service 的端点是不是健康",
                ],
            },
            {
                "name": "故障用例库",
                "summary": (
                    "20+ 预置 K8s 故障场景（Pod_Pending / CrashLoopBackOff / "
                    "Terminating / OOM / 副本不足 / Service 不可达 / PVC 异常 等），"
                    "用户可直接报场景名复现。"
                ),
                "examples": [
                    "复现一个 Pod_Pending 场景",
                    "造一个 CrashLoopBackOff",
                    "模拟 PVC 挂载失败",
                ],
            },
        ],
        keywords=[
            "chaos",
            "kubernetes",
            "k8s",
            "chaosblade",
            "blade",
            "resilience",
            "fault",
            "fault_injection",
            "故障演练",
            "故障注入",
            "混沌",
            "kubectl",
            "pod",
            "node",
            "namespace",
        ],
        test_types=["resilience"],
        input_schema=FAULT_PAYLOAD_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "blade_uid": {"type": "string"},
                "verification": {"type": "object"},
                "task_state": {"type": "string"},
                "recovery_level": {"type": "string"},
                "recover_verification": {"type": "object"},
            },
        },
        sla={"p50_ms": 120000, "p99_ms": 600000, "success_rate": 0.9},
        cost_profile={"tokens_per_task": 5000, "infra_cost_cents": 50},
        health_endpoint="",
    )
    return dataclasses.asdict(card)
