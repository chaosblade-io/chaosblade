"""Global test fixtures for blade-ai tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.tools.guard import CommandResult


# ── Reset global singletons ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_task_store(tmp_path, monkeypatch):
    """Ensure the TaskStore singleton is isolated per test.

    1. Redirects the DB path to a temp directory so tests NEVER write
       to the production ``~/.blade-ai/memory/tasks.db``.
    2. Resets the in-memory singleton so each test starts fresh.

    Individual tests that need a specific DB path can still monkeypatch
    ``settings.tasks_db_path`` after this fixture runs — their override
    will take precedence since it runs in the same test scope.
    """
    import chaos_agent.persistence.task_store as _ts_mod
    from chaos_agent.config import settings as _settings_mod

    # Close any leftover connection and reset the singleton
    _ts_mod._sync_close_store()

    # Redirect DB path to tmp so tests never touch production data
    monkeypatch.setattr(_settings_mod.settings, "tasks_db_path", tmp_path / "tasks.db")
    monkeypatch.setattr(_settings_mod.settings, "memory_dir", tmp_path)

    yield

    _ts_mod._sync_close_store()


# ── Settings ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings(monkeypatch):
    """Override environment variables to avoid depending on .env file."""
    monkeypatch.setenv("BLADE_AI_LLM_API_KEY", "test-key")
    monkeypatch.setenv("BLADE_AI_MODEL_NAME", "test-model")
    monkeypatch.setenv("BLADE_AI_SERVER_PORT", "9999")
    monkeypatch.setenv("BLADE_AI_BLADE_PATH", "blade")
    monkeypatch.setenv("BLADE_AI_KUBECTL_PATH", "kubectl")
    monkeypatch.setenv("BLADE_AI_SAFETY_BLACKLIST_NS", "kube-system,kube-public")


# ── Temporary skills directory ────────────────────────────────────────────


@pytest.fixture
def tmp_skills_dir(tmp_path):
    """Create a temporary skills directory with a valid SKILL.md."""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir(parents=True)

    (skill_dir / "SKILL.md").write_text(
        """\
---
name: test-skill
description: A test skill for unit testing
version: "1.0"
category: test
target: pod
required_tools: [blade, kubectl]
tags: [test]
parameters:
  - name: time
    type: int
    required: true
    description: Delay in milliseconds
    example: "3000"
---

## Pre-checks
1. Verify target pod exists

## Injection Procedure
1. Construct blade command

## Recovery
blade_destroy(uid="{{blade_uid}}")
""",
        encoding="utf-8",
    )

    # Add a scripts directory with a valid file
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "verify.py").write_text("print('verify')", encoding="utf-8")

    # Add a references directory
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "troubleshooting.md").write_text("# Troubleshooting", encoding="utf-8")

    return skills_dir


@pytest.fixture
def tmp_invalid_skill_dir(tmp_path):
    """Create a skill directory with invalid SKILL.md (missing required fields)."""
    skill_dir = tmp_path / "invalid-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: ""
description: ""
---

No useful content.
""",
        encoding="utf-8",
    )
    return skill_dir


# ── Mock run_command ──────────────────────────────────────────────────────


@pytest.fixture
def mock_run_command(mocker):
    """Mock chaos_agent.tools.shell.run_command to avoid real subprocess calls.

    Also patches the already-imported references in blade and kubectl modules,
    and stubs _get_blade_path to return "blade" for deterministic assertions.
    """
    async def _mock_run(cmd, timeout=None, task_id="", skip_guard=False):
        return CommandResult(
            exit_code=0,
            stdout='{"code": 200, "success": true, "result": "abc123"}',
            stderr="",
            duration_ms=100.0,
        )

    # Patch the source module
    shell_mock = mocker.patch(
        "chaos_agent.tools.shell.run_command",
        side_effect=_mock_run,
    )

    # Also patch the already-imported references in blade and kubectl modules.
    # NOTE: ``import chaos_agent.tools.kubectl`` resolves to the @tool-decorated
    # function (same name as the module), not the module itself.  Use
    # ``sys.modules`` to get the real module object for patch.object().
    import sys
    import chaos_agent.tools.blade as blade_mod
    kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
    mocker.patch.object(blade_mod, "run_command", shell_mock)
    mocker.patch.object(kubectl_mod, "run_command", shell_mock)

    # Stub _get_blade_path so tests see "blade" instead of the real binary path
    mocker.patch.object(blade_mod, "_get_blade_path", return_value="blade")

    return shell_mock


@pytest.fixture
def mock_run_command_fail(mocker):
    """Mock run_command that returns a non-zero exit code."""
    async def _mock_run(cmd, timeout=None, task_id="", skip_guard=False):
        return CommandResult(
            exit_code=1,
            stdout="",
            stderr="command failed",
            duration_ms=50.0,
        )

    shell_mock = mocker.patch(
        "chaos_agent.tools.shell.run_command",
        side_effect=_mock_run,
    )

    import sys
    import chaos_agent.tools.blade as blade_mod
    kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
    mocker.patch.object(blade_mod, "run_command", shell_mock)
    mocker.patch.object(kubectl_mod, "run_command", shell_mock)

    # Stub _get_blade_path for consistency with mock_run_command
    mocker.patch.object(blade_mod, "_get_blade_path", return_value="blade")

    return shell_mock


# ── AgentState sample ────────────────────────────────────────────────────


def replace_fault_spec(state: dict, **field_updates) -> None:
    """Test helper: update specific fields of state.fault_spec in place.

    Tests that used to mutate ``state['target']`` / ``state['blade_scope']``
    / etc. should call this helper instead — the FaultSpec refactor
    consolidated those scattered fields into a single immutable spec.

    Example::

        replace_fault_spec(state, namespace="kube-system", names=("coredns",))
        replace_fault_spec(state, scope="node", blade_target="cpu")
    """
    from chaos_agent.agent.fault_spec import FaultSpec
    existing = FaultSpec.from_dict(state.get("fault_spec")) or FaultSpec()
    state["fault_spec"] = existing.replace(**field_updates).to_dict()


@pytest.fixture
def sample_agent_state():
    """Build a minimal AgentState dict for testing."""
    from chaos_agent.agent.fault_spec import FaultSpec
    _spec = FaultSpec.from_cli_structured({
        "scope": "pod",
        "target": "kill",
        "action": "delete",
        "namespace": "default",
        "target_name": "my-pod",
        "params": {"duration": "60"},
    })
    return {
        "task_id": "task-20260420-120000-abc123",
        "operation": "inject",
        "skill_name": None,
        "fault_spec": _spec.to_dict(),
        "safety_status": "pending",
        "safety_reason": None,
        "needs_confirmation": False,
        "plan": None,
        "blade_uid": None,
        "result": None,
        "error": None,
        "nl": None,
        "compressed_summary": None,
        "experiment_history": None,
        "operational_notes": None,
        "agent_loop_count": 0,
        "execute_loop_count": 0,
        "messages": [],
        # Intent clarification fields (Phase 1 additions)
        "confirmed_intent": None,       # "inject" | "recover" | "chat" | None
        "interaction_mode": "cli",      # "cli" / "tui"
        "intent_context": None,         # Intent description text
        "intent_confidence": 0.0,       # Confidence score 0.0-1.0
        "clarification_round": 0,       # Clarification loop round tracking
        "intent_reasoning": None,       # LLM classification reasoning
        "needs_task_selection": False,   # RECOVER intent needs user to pick a task
    }


# ── Temporary memory directory ────────────────────────────────────────────


@pytest.fixture
def tmp_memory_dir(tmp_path):
    """Create a temporary memory directory structure."""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text(
        "# Operational Memory\n\n## Known Issues\n(none)\n",
        encoding="utf-8",
    )
    exp_dir = mem_dir / "experiments"
    exp_dir.mkdir()
    (exp_dir / "history.jsonl").write_text("", encoding="utf-8")
    sess_dir = mem_dir / "sessions"
    sess_dir.mkdir()
    return mem_dir


# ── Mock LLM ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm():
    """Create a mock LLM instance with configurable ainvoke."""
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(return_value=MagicMock(content="[Summary] test summary"))
    return llm


# ── Mock SkillRegistry ────────────────────────────────────────────────────


@pytest.fixture
def mock_registry(tmp_skills_dir):
    """Create a SkillRegistry loaded from tmp_skills_dir."""
    from chaos_agent.skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_from_directory(tmp_skills_dir)
    return registry


# ── Mock config file path ────────────────────────────────────────────────


@pytest.fixture
def tmp_mode_dir(tmp_path, monkeypatch):
    """Override CONFIG_DIR to use a temporary directory."""
    import chaos_agent.cli.config_manager as cm

    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path / ".blade-ai")
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / ".blade-ai" / "config.json")
    monkeypatch.setattr(cm, "MODE_FILE", tmp_path / ".blade-ai" / "mode.json")
    return tmp_path / ".blade-ai"
