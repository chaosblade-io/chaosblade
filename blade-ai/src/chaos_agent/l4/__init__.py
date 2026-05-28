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
        description="K8s chaos engineering: fault injection, verification, recovery",
        capabilities=[
            "resilience.chaos.pod_cpu",
            "resilience.chaos.pod_mem",
            "resilience.chaos.pod_network",
            "resilience.chaos.pod_disk",
            "resilience.chaos.node_network",
            "resilience.chaos.node_disk",
            "resilience.verification.two_layer",
            "resilience.recovery.deterministic",
        ],
        keywords=[
            "chaos",
            "kubernetes",
            "chaosblade",
            "resilience",
            "fault_injection",
            "故障演练",
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
