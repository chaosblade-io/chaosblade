"""Tests for postmortem subsystem (builder / generator / store / save_memory gate)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from chaos_agent.agent.postmortem.builder import (
    build_postmortem_context,
    should_generate_postmortem,
)
from chaos_agent.agent.postmortem.generator import generate_postmortem, make_summary
from chaos_agent.agent.postmortem.store import (
    postmortem_exists,
    read_postmortem,
    render_postmortem_markdown,
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
        state = {"confirmed_intent": "inject", "experiment_uid": "uid-x"}
        assert should_generate_postmortem(state, s) is False

    def test_off_for_non_inject_intent(self):
        s = _Settings()
        for intent in ("chat", "recover", None, ""):
            state = {"confirmed_intent": intent, "experiment_uid": "uid-x"}
            assert should_generate_postmortem(state, s) is False, intent

    def test_blade_uid_alone_no_longer_gates(self):
        """task-349ccf5d: the gate must NOT key on blade_uid. The uid can
        be wiped by a replan path while the experiment DID happen; with
        no verification and no failure_detail there is nothing to report
        on, so this shape stays off regardless of the uid."""
        s = _Settings()
        state = {"confirmed_intent": "inject", "experiment_uid": "uid-x"}
        assert should_generate_postmortem(state, s) is False

    def test_on_when_verification_completed(self):
        s = _Settings()
        for level in ("verified", "unverified", "partial"):
            state = {
                "confirmed_intent": "inject",
                "verification": {"level": level},
            }
            assert should_generate_postmortem(state, s) is True, level

    def test_on_for_failure_categories_outside_skip_list(self):
        """Blacklist semantics: every failure category reports EXCEPT the
        pre-execution gate refusals. planning_rejected is the task-349ccf5d
        regression — the old whitelist missed it."""
        s = _Settings()
        for cat in (
            "verification_failed", "execution_failed", "replan_exhausted",
            "planning_rejected", "planning_timeout", "execution_timeout",
            "recovery_failed", "internal_error", "wall_clock_timeout",
        ):
            state = {
                "confirmed_intent": "inject",
                "experiment_uid": "",
                "failure_detail": {"category": cat},
            }
            assert should_generate_postmortem(state, s) is True, cat

    def test_off_for_pre_execution_rejections(self):
        """USER_REJECTED / SAFETY_REJECTED refused BEFORE any execution —
        no verifier data, no side-effects → a report would be fluff."""
        s = _Settings()
        for cat in ("safety_rejected", "user_rejected"):
            state = {
                "confirmed_intent": "inject",
                "experiment_uid": "",
                "failure_detail": {"category": cat},
            }
            assert should_generate_postmortem(state, s) is False, cat

    def test_off_when_no_failure_category_and_no_verification(self):
        s = _Settings()
        state = {"confirmed_intent": "inject", "experiment_uid": "", "failure_detail": {}}
        assert should_generate_postmortem(state, s) is False


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

    def test_fault_type_projects_canonical_fault_type_from_fault_spec(self):
        ctx = build_postmortem_context({
            "skill_name": "stale-skill",
            "fault_spec": {
                "namespace": "cms-demo",
                "scope": "pod",
                "names": ["pod-a"],
                "labels": {},
                "fault_target": "network",
                "fault_action": "loss",
                "params": {},
                "params_flags": [],
                "duration_seconds": 0,
                "source": "test",
                "user_description": "",
            },
        })

        assert ctx["fault_type"] == "pod-network-loss"
        assert ctx["skill_name"] == "pod-network-loss"

    def test_large_tool_call_args_are_bounded(self):
        """K2: a tool_call carrying a huge arg (embedded YAML / long shell
        command) must be size-bounded so MAX_MESSAGES is a TRUE ceiling on
        the upload surface. Before the fix args rode through unbounded
        while content was capped at 500 — the documented privacy knob only
        half-delivered. Mutation guard: removing the _bound_tool_call_args
        call (args passed raw) must fail this test."""
        from langchain_core.messages import AIMessage
        from chaos_agent.agent.postmortem.builder import _MAX_TOOL_CALL_ARGS_CHARS

        big = "x" * 5000
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "kubectl", "args": {"spec": big},
                         "id": "1", "type": "tool_call"}],
        )
        ctx = build_postmortem_context({"messages": [msg]})
        args = ctx["messages"][0]["tool_calls"][0]["args"]
        assert isinstance(args, str), "oversized args should collapse to a truncated string"
        assert len(args) <= _MAX_TOOL_CALL_ARGS_CHARS + 20
        assert "truncated" in args

    def test_small_tool_call_args_kept_structured(self):
        """K2 counterpart: normal small args are returned UNCHANGED (still
        a structured dict) — the bound must not alter the common case."""
        from langchain_core.messages import AIMessage

        msg = AIMessage(
            content="",
            tool_calls=[{"name": "kubectl", "args": {"subcommand": "get"},
                         "id": "1", "type": "tool_call"}],
        )
        ctx = build_postmortem_context({"messages": [msg]})
        args = ctx["messages"][0]["tool_calls"][0]["args"]
        assert args == {"subcommand": "get"}


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

    @pytest.mark.asyncio
    async def test_system_prompt_directs_english_output(self):
        """The report body must be produced in English. Assert on the
        SystemMessage actually handed to the LLM (not the module constant)
        so the test pins the real prompt reaching the model. Mutation
        guard: reverting the directive to the old Chinese default must
        fail this test."""
        from langchain_core.messages import AIMessage
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=AIMessage(content="## Summary\nOK"))
        await generate_postmortem({"task_id": "x"}, llm, timeout=5)

        system_content = llm.ainvoke.call_args[0][0][0].content
        assert "Write in English" in system_content
        assert "Write in Chinese" not in system_content

    @pytest.mark.asyncio
    async def test_system_prompt_background_delegates_facts_to_program(self):
        """Root-fix contract: the Background guidance asks the model ONLY
        for the facts the execution trace reveals — the concrete injection
        / recovery ACTIONS and the RESOLVED target — and explicitly tells
        it the deterministic facts (fault type / namespace / status /
        duration / user request / target selector / parameters) are already
        program-rendered in the header above ``## Summary`` so it must NOT
        restate them.

        This REPLACES the earlier ``..._requires_concrete_actions`` test,
        which pinned the rejected "LLM restates every deterministic fact"
        contract (it asserted ``Target object:`` / ``Fault type:`` /
        ``Parameters:`` bullets). Those zero-variance facts now come from
        ``render_postmortem_markdown`` (single source, disk == payload);
        leaving them to the model reintroduced transcription variance and a
        second drifting copy. Mutation guards: (a) re-adding a ``Fault
        type:`` / ``Parameters:`` bullet must fail; (b) dropping the "do not
        restate" directive must fail; (c) dropping the action bullets must
        fail."""
        from langchain_core.messages import AIMessage
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=AIMessage(content="## Summary\nOK"))
        await generate_postmortem({"task_id": "x"}, llm, timeout=5)

        system_content = llm.ainvoke.call_args[0][0][0].content
        # Model-owned facts: the ACTIONS + resolved target (trace-only).
        assert "`Injection action:`" in system_content
        assert "`Recovery action:`" in system_content
        assert "`Resolved target:`" in system_content
        # A concrete operation, NOT the carrier enum.
        assert "carrier enum" in system_content
        assert "kubectl_native" in system_content
        # Deterministic facts are NO LONGER model bullets (program renders
        # them). Assert on the backtick bullet labels so the "do not
        # restate" prose mentioning "fault type" doesn't false-positive.
        assert "`Fault type:`" not in system_content
        assert "`Parameters:`" not in system_content
        assert "`User request:`" not in system_content
        assert "`Target object:`" not in system_content
        # The delegation directive itself must be present.
        assert "Do NOT restate" in system_content
        assert "already rendered by the program" in system_content

    @pytest.mark.asyncio
    async def test_generate_accepts_heading_case_and_whitespace_variants(self):
        """K1: a legal ``## Summary`` variant (lowercase / extra space /
        leading or trailing whitespace) must NOT be rejected as a refusal.
        Before the fix the gate demanded the exact literal ``## Summary``,
        so these valid reports were silently dropped (postmortem=None,
        nothing archived). Mutation guard: reverting the gate to
        ``_REQUIRED_HEADING not in markdown`` must fail this test."""
        from langchain_core.messages import AIMessage
        variants = [
            "## summary\nlowercase heading body",
            "##  Summary\ndouble space after hashes",
            "## Summary \ntrailing space on heading",
            "  ## Summary\nleading whitespace before heading",
        ]
        for body in variants:
            llm = AsyncMock()
            llm.ainvoke = AsyncMock(return_value=AIMessage(content=body))
            out = await generate_postmortem({"task_id": "x"}, llm, timeout=5)
            assert out != "", f"valid variant wrongly rejected: {body!r}"

    @pytest.mark.asyncio
    async def test_generate_still_rejects_refusal_without_summary(self):
        """K1 counterpart: the normalised gate must STILL reject a refusal
        that has no Summary heading at all — loosening case-sensitivity
        must not let junk through."""
        from langchain_core.messages import AIMessage
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(
            return_value=AIMessage(content="I'm sorry, I cannot help with that."),
        )
        out = await generate_postmortem({"task_id": "x"}, llm, timeout=5)
        assert out == ""

    def test_gate_and_make_summary_agree_on_heading_variants(self):
        """K1 single-source invariant: the REAL acceptance gate
        (``_validate_and_bound``) and the downstream summary extractor
        must agree on every heading variant — no variant accepted by one
        and rejected by the other (the old bug: gate exact-match vs
        extractor case-insensitive). Asserting on the gate function itself
        (not the helper) so a mutation that bypasses the helper at the
        gate is caught here too."""
        from chaos_agent.agent.postmortem.generator import (
            _validate_and_bound,
            make_summary,
        )
        for body in (
            "## Summary\ncanonical body",
            "## summary\nlowercase body",
            "##  Summary\nextra space body",
        ):
            assert _validate_and_bound(body) != "", body  # gate accepts
            assert make_summary(body) != "", body          # extractor agrees


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
                "fault_type": "pod-cpu-fullload",
                "skill_name": "stale-skill",
                "namespace": "cms",
                "status": "verified", "duration": "47s",
            },
        )
        text = read_postmortem("task-abc12345", root=tmp_path)
        assert "# Postmortem: pod-cpu-fullload on cms" in text
        assert "stale-skill" not in text
        assert "**Status**: verified" in text
        assert "**Duration**: 47s" in text

    def test_render_emits_deterministic_fact_block(self):
        """The program-rendered fact block must carry the deterministic
        facts (user request / target selector / params) ABOVE the LLM body,
        so the platform — which reads ``postmortem.markdown`` — sees them.
        These are exactly the facts the old body-only payload dropped."""
        body = "## Summary\nHPA maxed out."
        out = render_postmortem_markdown(
            "task-abc12345", body,
            header_meta={
                "fault_type": "pod-cpu-fullload", "namespace": "cms",
                "status": "verified", "duration": "47s",
                "generated_at": "2026-11-04T10:00:00",
                "user_request": "saturate CPU on the cms drill pod",
                "target": "cms/app=drill", "params": "cpu-percent=80",
            },
        )
        assert "# Postmortem: pod-cpu-fullload on cms" in out
        assert "**User request**: saturate CPU on the cms drill pod" in out
        assert "**Target**: cms/app=drill" in out
        assert "**Parameters**: cpu-percent=80" in out
        # Fact block precedes the LLM body.
        assert out.index("**Parameters**") < out.index("## Summary")
        assert "HPA maxed out." in out

    def test_render_byte_identical_to_legacy_header_without_extra_facts(self):
        """With none of the three optional facts present the render must be
        byte-identical to the historical ``title + metadata line + body``
        header — so existing disk-format consumers see no change. Guards the
        refactor that moved header construction into the shared renderer."""
        body = "## Summary\nAll good."
        out = render_postmortem_markdown(
            "task-abc12345", body,
            header_meta={"skill_name": "k8s", "namespace": "demo",
                         "status": "verified", "duration": "47s",
                         "generated_at": "G"},
        )
        legacy = (
            "# Postmortem: k8s on demo\n\n"
            "**Task**: `task-abc12345` | **Status**: verified | "
            "**Duration**: 47s | **Generated**: G\n\n"
            "## Summary\nAll good.\n"
        )
        assert out == legacy

    def test_render_omits_empty_fact_lines(self):
        """A partial header_meta must not leave dangling ``**Label**: ``
        lines with nothing after them."""
        out = render_postmortem_markdown(
            "task-abc12345", "## Summary\nx",
            header_meta={"fault_type": "pod-mem-load", "namespace": "cms",
                         "target": "cms/app=drill"},  # no user_request/params
        )
        assert "**Target**: cms/app=drill" in out
        assert "**User request**" not in out
        assert "**Parameters**" not in out

    def test_save_postmortem_disk_equals_rendered_payload(self, tmp_path):
        """Single-source invariant at the store level: the file written to
        disk must be byte-identical to what ``render_postmortem_markdown``
        produces for the same inputs — which is what ``terminal_reports``
        puts in ``payload['markdown']``. This is the nail that keeps disk
        and payload from ever diverging again."""
        body = "## Summary\nAll good."
        meta = {"fault_type": "pod-cpu-fullload", "namespace": "cms",
                "status": "verified", "duration": "47s",
                "generated_at": "2026-11-04T10:00:00",
                "user_request": "saturate CPU", "target": "cms/app=drill",
                "params": "cpu-percent=80"}
        save_postmortem("task-abc12345", body, root=tmp_path, header_meta=meta)
        on_disk = read_postmortem("task-abc12345", root=tmp_path)
        assert on_disk == render_postmortem_markdown(
            "task-abc12345", body, header_meta=meta,
        )

    def test_invalid_task_id_rejected(self, tmp_path):
        for bad in ("", "../etc/passwd", "task-", "TASK-ABC", "task abc",
                    "inject-", "recover-"):
            with pytest.raises(ValueError):
                save_postmortem(bad, "body", root=tmp_path)

    def test_pipeline_prefixes_accepted(self, tmp_path):
        # task_identity mints inject-/recover- ids; the path-safety regex
        # must accept every whitelisted prefix, not only the legacy one.
        for tid in ("inject-abc12345", "recover-def67890"):
            path = save_postmortem(tid, "body", root=tmp_path)
            assert path == tmp_path / f"{tid}.md"
            assert read_postmortem(tid, root=tmp_path) is not None

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
        from chaos_agent.agent.nodes.store import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(memory_nodes, "sync_node_status_to_session", lambda *a, **k: None)

        state = {
            "task_id": "task-disable",
            "confirmed_intent": "inject",
            "experiment_uid": "uid-x",
            "messages": [],
        }
        updates = await memory_nodes.save_memory(state)
        assert updates.get("postmortem") is None  # R11: always-write None (was: not-in)
