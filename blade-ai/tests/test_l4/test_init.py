"""Tests for chaos_agent.l4 — public API (__init__.py)."""

from chaos_agent.l4 import L4ResilienceAgent, create_l4_adapter, get_agent_card


class TestCreateL4Adapter:
    """Test factory function."""

    def test_returns_agent_instance(self):
        agent = create_l4_adapter()
        assert isinstance(agent, L4ResilienceAgent)

    def test_new_instance_each_call(self):
        a1 = create_l4_adapter()
        a2 = create_l4_adapter()
        assert a1 is not a2


class TestGetAgentCard:
    """Test AgentCard metadata generation."""

    def test_returns_dict(self):
        card = get_agent_card()
        assert isinstance(card, dict)

    def test_agent_id(self):
        card = get_agent_card()
        assert card["agent_id"] == "resilience"
        assert card["agent_type"] == "resilience"

    def test_capabilities_non_empty(self):
        card = get_agent_card()
        assert len(card["capabilities"]) >= 6
        assert "chaos.inject.pod.cpu" in card["capabilities"]
        assert "chaos.recover" in card["capabilities"]

    def test_capability_groups_present(self):
        card = get_agent_card()
        groups = card["capability_groups"]
        assert isinstance(groups, list) and len(groups) >= 4
        names = [g["name"] for g in groups]
        assert "故障注入" in names
        assert "集群只读观察" in names
        # 每组都有 summary + 至少一个 example
        for g in groups:
            assert g.get("summary")
            assert g.get("examples")

    def test_keywords_include_chinese(self):
        card = get_agent_card()
        assert "故障演练" in card["keywords"]

    def test_input_schema_present(self):
        card = get_agent_card()
        schema = card["input_schema"]
        assert schema["type"] == "object"
        assert schema["required"] == ["fault_intent"]
        assert "fault_intent" in schema["properties"]
        assert "fault_scope" not in schema["properties"]

    def test_output_schema_present(self):
        card = get_agent_card()
        schema = card["output_schema"]
        assert "blade_uid" in schema["properties"]
        assert "verification" in schema["properties"]

    def test_sla_fields(self):
        card = get_agent_card()
        assert card["sla"]["p50_ms"] == 120000
        assert card["sla"]["p99_ms"] == 600000
        assert card["sla"]["success_rate"] == 0.9

    def test_protocol_default(self):
        card = get_agent_card()
        assert card["protocol"] == "direct"

    def test_version(self):
        card = get_agent_card()
        assert card["version"] == "v1"

    def test_test_types(self):
        card = get_agent_card()
        assert card["test_types"] == ["resilience"]
