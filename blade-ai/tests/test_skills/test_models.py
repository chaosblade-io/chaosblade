"""Tests for skill data models."""

from chaos_agent.skills.models import Skill, SkillMetadata, SkillParameter


class TestSkillParameter:
    """Test SkillParameter dataclass."""

    def test_basic_creation(self):
        p = SkillParameter(name="time", type="int", required=True, description="Delay ms")
        assert p.name == "time"
        assert p.type == "int"
        assert p.required is True
        assert p.description == "Delay ms"

    def test_defaults(self):
        p = SkillParameter(name="offset")
        assert p.type == "string"
        assert p.required is False
        assert p.default is None
        assert p.description == ""
        assert p.example is None

    def test_with_all_fields(self):
        p = SkillParameter(
            name="interface",
            type="string",
            required=False,
            default="eth0",
            description="Network interface",
            example="eth0",
        )
        assert p.default == "eth0"
        assert p.example == "eth0"


class TestSkillMetadata:
    """Test SkillMetadata dataclass."""

    def test_basic_creation(self):
        m = SkillMetadata(name="pod-kill", description="Kill a pod")
        assert m.name == "pod-kill"
        assert m.description == "Kill a pod"

    def test_defaults(self):
        m = SkillMetadata(name="test", description="test")
        assert m.version == "1.0"
        assert m.category == ""
        assert m.target == ""
        assert m.required_tools == []
        assert m.tags == []
        assert m.parameters == []

    def test_with_all_fields(self):
        params = [SkillParameter(name="time", type="int", required=True)]
        m = SkillMetadata(
            name="pod-network-delay",
            description="Inject network delay",
            version="2.0",
            category="network",
            target="pod",
            required_tools=["blade", "kubectl"],
            tags=["network", "delay"],
            parameters=params,
        )
        assert m.version == "2.0"
        assert m.category == "network"
        assert m.target == "pod"
        assert len(m.required_tools) == 2
        assert len(m.tags) == 2
        assert len(m.parameters) == 1


class TestSkill:
    """Test Skill dataclass."""

    def test_basic_creation(self):
        meta = SkillMetadata(name="test", description="test")
        s = Skill(metadata=meta)
        assert s.metadata is meta
        assert s.instructions == ""
        assert s.skill_dir == ""

    def test_with_instructions(self):
        meta = SkillMetadata(name="test", description="test")
        s = Skill(metadata=meta, instructions="## Steps\n1. Do stuff", skill_dir="/path")
        assert "Steps" in s.instructions
        assert s.skill_dir == "/path"
