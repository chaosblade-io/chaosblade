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
            "K8s chaos-engineering and observability expert: fault injection, "
            "cluster state queries, a fault use-case library, and deterministic "
            "recovery. Executes for real on top of chaosblade + kubectl."
        ),
        capabilities=[
            # — Fault injection (scope × target × action matrix) —
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
            # — Fault recovery —
            "chaos.recover",
            # — Read-only cluster observation —
            "k8s.observe.pods",
            "k8s.observe.nodes",
            "k8s.observe.events",
            "k8s.observe.logs",
            "k8s.observe.endpoints",
            "k8s.observe.api_discovery",
            "k8s.observe.in_pod_probe",
            "k8s.observe.host_probe",
            # — Fault use-case library —
            "chaos.catalogue.pod_lifecycle",
            "chaos.catalogue.workload",
            "chaos.catalogue.service",
            "chaos.catalogue.node",
            "chaos.catalogue.storage",
        ],
        capability_groups=[
            {
                "name": "Fault injection",
                "summary": (
                    "Inject real faults across a scope × target × action matrix. "
                    "scope ∈ {pod, container, node}, "
                    "target ∈ {cpu, mem, network, disk, process}, "
                    "action ∈ {fullload, load, delay, loss, fill, kill, burn}."
                ),
                "examples": [
                    "Put a 60s CPU full load on the web pods in the cms-demo namespace",
                    "Inject 200ms of network latency into the nginx pod for 2 minutes",
                    "Kill the main process of the payment service",
                    "Fill node-1's disk to 90%",
                ],
            },
            {
                "name": "Fault recovery",
                "summary": "Deterministically destroy a fault experiment by blade_uid and verify the resource recovered.",
                "examples": [
                    "Recover that CPU injection from just now",
                    "Clear out all the faults",
                ],
            },
            {
                "name": "Read-only cluster observation",
                "summary": (
                    "Query K8s resource state, events, logs, endpoints, and API resources; "
                    "supports in-pod probes (exec ps/df/ping/nslookup) and host probes (debug node)."
                ),
                "examples": [
                    "Show me the status of every pod under cms-demo",
                    "What is node-2's CPU/memory usage right now",
                    "Any abnormal events in the last 5 minutes",
                    "Are payment-service's endpoints healthy",
                ],
            },
            {
                "name": "Fault use-case library",
                "summary": (
                    "20+ preset K8s fault scenarios (Pod_Pending / CrashLoopBackOff / "
                    "Terminating / OOM / insufficient replicas / Service unreachable / "
                    "PVC anomalies, and more) — users can reproduce one by naming the scenario."
                ),
                "examples": [
                    "Reproduce a Pod_Pending scenario",
                    "Create a CrashLoopBackOff",
                    "Simulate a PVC mount failure",
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
            # Recovery / destroy related
            "recover",
            "恢复",
            "回滚",
            "destroy",
            "撤销",
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
