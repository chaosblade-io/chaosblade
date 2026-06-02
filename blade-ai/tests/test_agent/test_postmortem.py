"""Tests for postmortem subsystem (builder / generator / store / save_memory gate)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from chaos_agent.agent.postmortem.builder import (
    build_postmortem_context,
    should_generate_postmortem,
)
from chaos_agent.agent.postmortem.generator import generate_postmortem, make_summary
from chaos_agent.agent.postmortem.store import (
    POSTMORTEM_DIR,
    postmortem_exists,
    read_postmortem,
    save_postmortem,
)


# ─── should_generate_postmortem ────────────────────────────────────


class _Settings:
    """Minimal settings stand-in for gate testing."""

    def __init__(self, *, enabled=True):
        self.postmortem_enabled = enabled


class TestShouldGenerate:
    def test_off_when_setting_disabled(self):
        s = _Settings(enabled=False)
        state = {"confirmed_intent": "inject", "blade_uid": "uid-x"}
        assert should_generate_postmortem(state, s) is False

    def test_off_for_non_inject_intent(self):
        s = _Settings()
        for intent in ("chat", "recover", None, ""):
            state = {"confirmed_intent": intent, "blade_uid": "uid-x"}
            assert should_generate_postmortem(state, s) is False, intent

    def test_on_when_blade_uid_present(self):
        s = _Settings()
        state = {"confirmed_intent": "inject", "blade_uid": "uid-x"}
        assert should_generate_postmortem(state, s) is True

    def test_on_for_whitelisted_failure_categories(self):
        s = _Settings()
        for cat in ("verification_failed", "execution_failed", "replan_exhausted"):
            state = {
                "confirmed_intent": "inject",
                "blade_uid": "",
                "failure_detail": {"category": cat},
            }
            assert should_generate_postmortem(state, s) is True, cat

    def test_off_for_non_whitelisted_failure(self):
        s = _Settings()
        for cat in ("safety_rejected", "user_rejected", "planning_timeout"):
            state = {
                "confirmed_intent": "inject",
                "blade_uid": "",
                "failure_detail": {"category": cat},
            }
            assert should_generate_postmortem(state, s) is False, cat


# ─── build_postmortem_context ──────────────────────────────────────


class TestBuildContext:
    def test_minimal_state_yields_safe_defaults(self):
        ctx = build_postmortem_context({})
        assert ctx["task_id"] == ""
        assert ctx["fault_spec"] == {}
        assert ctx["side_effects"] == {}
        assert ctx["pre_snapshot"] == {"pods_count": 0, "endpoints_count": 0}
        assert ctx["messages"] == []
        assert ctx["messages_elided"] == 0

    def test_pre_snapshot_reduces_to_counts(self):
        state = {
            "se_snapshot": {
                "pods": {"a": {}, "b": {}, "c": {}},
                "endpoints": {"svc1": {}},
            },
        }
        ctx = build_postmortem_context(state)
        assert ctx["pre_snapshot"] == {"pods_count": 3, "endpoints_count": 1}

    def test_messages_tail_elides_oldest(self):
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content=f"m{i}") for i in range(50)]
        ctx = build_postmortem_context({"messages": msgs}, max_messages=10)
        assert len(ctx["messages"]) == 10
        assert ctx["messages_elided"] == 40
        # Tail-truncation: keeps last 10, so the last entry should
        # have content "m49".
        assert ctx["messages"][-1]["content_preview"] == "m49"

    def test_message_content_truncated_at_500_chars(self):
        from langchain_core.messages import HumanMessage
        long_content = "x" * 1000
        ctx = build_postmortem_context(
            {"messages": [HumanMessage(content=long_content)]},
        )
        preview = ctx["messages"][0]["content_preview"]
        assert preview.endswith("...")
        assert len(preview) <= 503

    def test_side_effects_pulled_from_verification(self):
        state = {
            "verification": {
                "level": "verified",
                "side_effects": {"evicted_pods": ["p1"]},
            },
        }
        ctx = build_postmortem_context(state)
        assert ctx["side_effects"] == {"evicted_pods": ["p1"]}
        assert ctx["verification"]["level"] == "verified"


# ─── generator ─────────────────────────────────────────────────────


class TestGenerator:
    @pytest.mark.asyncio
    async def test_generate_calls_llm_with_two_messages(self):
        from langchain_core.messages import AIMessage
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=AIMessage(content="## Summary\nOK"))

        ctx = {"task_id": "task-abc", "skill_name": "test"}
        out = await generate_postmortem(ctx, llm, timeout=5)
        assert out == "## Summary\nOK"

        # Verify the LLM received exactly system + user
        called_args = llm.ainvoke.call_args[0][0]
        assert len(called_args) == 2
        assert called_args[0].type == "system"
        assert called_args[1].type == "human"

    @pytest.mark.asyncio
    async def test_timeout_raises_asyncio_timeout(self):
        async def slow_invoke(_messages):
            await asyncio.sleep(1)
            return None

        llm = AsyncMock()
        llm.ainvoke = slow_invoke

        with pytest.raises(asyncio.TimeoutError):
            await generate_postmortem({}, llm, timeout=0.05)

    def test_make_summary_extracts_first_paragraph_after_summary(self):
        md = (
            "## Summary\n"
            "实验顺利完成，HPA 在 8 秒内扩容。\n"
            "\n"
            "## Background\n"
            "略\n"
        )
        s = make_summary(md)
        assert "HPA" in s

    def test_make_summary_returns_empty_when_no_summary_section(self):
        assert make_summary("## Background\n略") == ""

    def test_make_summary_truncates_at_200_chars(self):
        long = "x " * 200
        md = f"## Summary\n{long}\n\n## End\n"
        s = make_summary(md)
        assert s.endswith("...")
        assert len(s) <= 204

    @pytest.mark.asyncio
    async def test_generate_rejects_missing_summary_heading(self):
        """LLM refusal / off-topic output (no ## Summary) → return "".

        Fix #7 — structural validation gate. Without this an LLM that
        says "I'm sorry, I can't help with that" would still be saved
        as a postmortem and shown to the user."""
        from langchain_core.messages import AIMessage
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(
            return_value=AIMessage(content="I'm sorry, I cannot help with that."),
        )
        out = await generate_postmortem({"task_id": "x"}, llm, timeout=5)
        assert out == ""

    @pytest.mark.asyncio
    async def test_generate_truncates_oversized_output(self):
        """LLM that ignores brevity guidance and writes 200KB → cap at
        MAX_MARKDOWN_BYTES with a clear marker.

        Fix #6 — size cap. Bounds SSE envelope + LangGraph checkpoint
        + TUI re-render cost in the worst-case LLM-runaway scenario."""
        from langchain_core.messages import AIMessage
        from chaos_agent.agent.postmortem.generator import MAX_MARKDOWN_BYTES

        huge = "## Summary\n" + ("x" * (MAX_MARKDOWN_BYTES + 50_000))
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=AIMessage(content=huge))
        out = await generate_postmortem({"task_id": "x"}, llm, timeout=5)

        assert len(out.encode("utf-8")) <= MAX_MARKDOWN_BYTES + 200  # cap + marker
        assert "truncated" in out.lower()

    @pytest.mark.asyncio
    async def test_generate_accepts_normal_output_unchanged(self):
        """Sanity check: well-formed normal-size output passes through."""
        from langchain_core.messages import AIMessage
        body = "## Summary\nAll good.\n\n## Background\nx"
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=AIMessage(content=body))
        out = await generate_postmortem({"task_id": "x"}, llm, timeout=5)
        assert out == body  # unchanged


# ─── store ─────────────────────────────────────────────────────────


class TestStore:
    def test_save_and_read_roundtrip(self, tmp_path):
        body = "## Summary\nAll good."
        path = save_postmortem(
            "task-abc12345", body, root=tmp_path,
            header_meta={"skill_name": "k8s", "namespace": "demo"},
        )
        assert path == tmp_path / "task-abc12345.md"
        text = read_postmortem("task-abc12345", root=tmp_path)
        assert "# Postmortem: k8s on demo" in text
        assert "## Summary" in text
        assert "All good." in text

    def test_header_includes_metadata(self, tmp_path):
        save_postmortem(
            "task-abc12345", "## Summary\n", root=tmp_path,
            header_meta={
                "skill_name": "cpu", "namespace": "cms",
                "status": "verified", "duration": "47s",
            },
        )
        text = read_postmortem("task-abc12345", root=tmp_path)
        assert "**Status**: verified" in text
        assert "**Duration**: 47s" in text

    def test_invalid_task_id_rejected(self, tmp_path):
        for bad in ("", "../etc/passwd", "task-", "TASK-ABC", "task abc"):
            with pytest.raises(ValueError):
                save_postmortem(bad, "body", root=tmp_path)

    def test_postmortem_exists(self, tmp_path):
        assert postmortem_exists("task-abc12345", root=tmp_path) is False
        save_postmortem("task-abc12345", "body", root=tmp_path)
        assert postmortem_exists("task-abc12345", root=tmp_path) is True
        # Malformed task_id → False (not exception)
        assert postmortem_exists("../etc", root=tmp_path) is False

    def test_file_permissions_0o600(self, tmp_path):
        """R1 — written file is owner-only (no co-tenant read access).

        Skipped on Windows where POSIX modes are advisory at best."""
        import os
        import platform
        if platform.system() == "Windows":
            import pytest as _pytest
            _pytest.skip("POSIX file permissions not enforced on Windows")

        path = save_postmortem("task-perm0123", "body", root=tmp_path)
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600, (
            f"Expected 0o600 (owner-only); got 0o{mode:o}. "
            f"Co-tenant readability is a privacy regression."
        )

    def test_directory_permissions_0o700(self, tmp_path):
        """R1 — parent directory is owner-only."""
        import os
        import platform
        if platform.system() == "Windows":
            import pytest as _pytest
            _pytest.skip("POSIX file permissions not enforced on Windows")

        save_postmortem("task-dirperm1", "body", root=tmp_path / "fresh-dir")
        mode = os.stat(tmp_path / "fresh-dir").st_mode & 0o777
        assert mode == 0o700, f"Expected 0o700 directory; got 0o{mode:o}"

    def test_atomic_write_no_tmp_leftover_on_success(self, tmp_path):
        """R2 — successful save leaves NO ``.tmp`` artefact in the dir."""
        save_postmortem("task-atomic01", "body", root=tmp_path)
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == [], f"tmp files leaked: {leftovers}"

    def test_atomic_write_replaces_existing_file(self, tmp_path):
        """R2 — overwriting an existing postmortem yields the NEW
        complete content (not a merge / not corruption)."""
        save_postmortem("task-overw001", "first body", root=tmp_path)
        first = read_postmortem("task-overw001", root=tmp_path)
        assert "first body" in first

        save_postmortem("task-overw001", "second body completely", root=tmp_path)
        second = read_postmortem("task-overw001", root=tmp_path)
        assert "second body completely" in second
        assert "first body" not in second

    def test_get_postmortem_dir_follows_memory_dir(self, tmp_path, monkeypatch):
        """R12 — postmortem dir is resolved from settings.memory_dir,
        not hardcoded under ~/.blade-ai/. Without this the user who sets
        BLADE_AI_MEMORY_DIR=~/custom-data still sees postmortems land
        at ~/.blade-ai/postmortems/ — inconsistent and confusing."""
        from chaos_agent.agent.postmortem.store import get_postmortem_dir
        from chaos_agent.config import settings as s_mod

        # Point memory_dir at a custom location
        custom_memory = tmp_path / "custom" / "memory"
        monkeypatch.setattr(s_mod.settings, "memory_dir", custom_memory)

        resolved = get_postmortem_dir()
        # Should be SIBLING of memory dir, not under HOME/.blade-ai/
        assert resolved.parent == custom_memory.parent
        assert resolved.name == "postmortems"

    def test_symlink_at_target_is_removed_before_write(self, tmp_path):
        """R3 — attacker-planted symlink redirecting to /etc/passwd-ish
        path must NOT be followed. We replace the symlink with a real
        file containing OUR content."""
        target_path = tmp_path / "task-symlink1.md"
        # Create a "real" file the symlink could point at; if our
        # write followed the symlink, this file would get our content.
        decoy = tmp_path / "decoy.txt"
        decoy.write_text("decoy original content", encoding="utf-8")
        os_symlink_supported = True
        try:
            target_path.symlink_to(decoy)
        except (OSError, NotImplementedError):
            import pytest as _pytest
            _pytest.skip("symlink creation not supported on this filesystem")

        save_postmortem("task-symlink1", "real postmortem body", root=tmp_path)

        # Our write hit the real path (no longer a symlink).
        import os as _os
        assert not _os.path.islink(target_path), (
            "symlink at target should have been removed before write"
        )
        # Decoy was NOT modified (we did not follow the symlink).
        assert decoy.read_text(encoding="utf-8") == "decoy original content"
        # Our content landed at the real target path.
        assert "real postmortem body" in target_path.read_text(encoding="utf-8")


# ─── save_memory integration ────────────────────────────────────────


class TestSaveMemoryIntegration:
    """Verify save_memory's postmortem block: enabled vs disabled,
    success vs LLM-failure, and that postmortem ends up in updates."""

    @pytest.mark.asyncio
    async def test_save_memory_skips_postmortem_when_disabled(self, monkeypatch, tmp_path):
        from chaos_agent.config import settings as s_mod
        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", False)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path)

        # Stub sync_to_store + session store + finalize so save_memory
        # only exercises the postmortem branch we care about.
        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(memory_nodes, "sync_node_status_to_session", lambda *a, **k: None)

        state = {
            "task_id": "task-disable",
            "confirmed_intent": "inject",
            "blade_uid": "uid-x",
            "messages": [],
        }
        updates = await memory_nodes.save_memory(state)
        assert updates.get("postmortem") is None  # R11: always-write None (was: not-in)
