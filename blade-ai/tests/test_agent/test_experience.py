"""Tests for EXPERIENCE.md experience accumulation."""

from __future__ import annotations


def test_append_experience_uses_fault_spec_fault_type(tmp_path, monkeypatch):
    from chaos_agent.agent import experience

    monkeypatch.setattr(experience, "EXPERIENCE_MD_PATH", tmp_path / "EXPERIENCE.md")

    result = experience.append_experience(
        "",
        {
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
            "error": "planning rejected",
        },
    )

    text = (tmp_path / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert result["status"] == "appended"
    assert result["category"] == "Fault Injection"
    assert "Issue with pod-network-loss" in text
    assert "Issue with stale-skill" not in text


class TestExperienceInjectionTruncationDialect:
    """truncation-debt-cleanup (4.2): both EXPERIENCE.md injection
    truncation paths speak the shared dialect — quantified elision markers
    + the state-evidence notice (marker + honest original size + read_file
    retrieval guidance) — and the notice counts against the byte budget
    (the returned text stays within MAX_EXPERIENCE_MD_BYTES instead of
    silently returning budget + notice). Within budget: verbatim, zero
    truncation noise."""

    @staticmethod
    def _load(tmp_path, monkeypatch, text: str) -> str:
        from chaos_agent.agent import experience

        monkeypatch.setattr(experience, "EXPERIENCE_MD_PATH", tmp_path / "EXPERIENCE.md")
        (tmp_path / "EXPERIENCE.md").write_text(text, encoding="utf-8")
        return experience.load_agent_experience()

    def test_byte_budget_oversized_shared_dialect(self, tmp_path, monkeypatch):
        from chaos_agent.agent.prompts.constants import MAX_EXPERIENCE_MD_BYTES

        # ~28KB in 5 lines — isolates the byte path (line count <= 200).
        # Head/tail runs anchor the both-ends assertions.
        text = f"# Experience\n{'A' * 8000}\n{'m' * 12000}\n{'Z' * 8000}"
        original_bytes = len(text.encode("utf-8"))
        assert original_bytes > MAX_EXPERIENCE_MD_BYTES

        loaded = self._load(tmp_path, monkeypatch, text)

        # The notice counts against the budget — no silent budget + notice.
        assert len(loaded.encode("utf-8")) <= MAX_EXPERIENCE_MD_BYTES
        # Quantified elision marker + three-field state-evidence notice.
        assert loaded.count("bytes elided") == 1
        assert "⚠️ TRUNCATED (state evidence):" in loaded
        assert f"(original {original_bytes} bytes)." in loaded
        assert "read_file" in loaded
        # Both ends survive; the middle run is actually elided.
        assert "A" * 100 in loaded
        assert "Z" * 100 in loaded
        assert "m" * 11000 not in loaded

    def test_byte_budget_cjk_honors_byte_ceiling(self, tmp_path, monkeypatch):
        """Spec Scenario (R3): CJK multi-byte content must stay under the
        BYTE ceiling. A character-budget implementation would keep ~75% of
        30000 CJK chars ≈ 75000 bytes — 3× the 25KB budget; only a
        byte-unit cut honors the ceiling. ASCII-only fixtures cannot pin
        this (chars == bytes there), so this test is the one that fires
        when someone swaps in a character-unit implementation."""
        from chaos_agent.agent.experience import MAX_EXPERIENCE_MD_LINES
        from chaos_agent.agent.prompts.constants import MAX_EXPERIENCE_MD_BYTES

        # 30000 CJK chars = 90000 bytes — 3.6× the byte budget, 5 lines
        # (line path never fires; the byte path is isolated).
        text = f"# 经验\n{'好' * 10000}\n{'学' * 10000}\n{'习' * 9998}"
        original_bytes = len(text.encode("utf-8"))
        assert len(text.split("\n")) <= MAX_EXPERIENCE_MD_LINES  # line path off
        assert original_bytes > 3 * MAX_EXPERIENCE_MD_BYTES

        loaded = self._load(tmp_path, monkeypatch, text)

        # THE byte contract: the returned text respects the byte ceiling.
        assert len(loaded.encode("utf-8")) <= MAX_EXPERIENCE_MD_BYTES
        assert "⚠️ TRUNCATED (state evidence):" in loaded
        assert f"(original {original_bytes} bytes)." in loaded
        assert "bytes elided" in loaded
        # Both ends survive at full CJK fidelity.
        assert loaded.startswith("# 经验")
        assert "习" in loaded

    def test_line_budget_oversized_shared_dialect(self, tmp_path, monkeypatch):
        # 300 short lines (~2.7KB) — isolates the line path (bytes < 25KB).
        lines = [f"line-{i:03d}" for i in range(300)]
        text = "\n".join(lines)
        original_bytes = len(text.encode("utf-8"))

        loaded = self._load(tmp_path, monkeypatch, text)

        # Line-level both-ends cut: head 150 + quantified marker + tail 50.
        assert "line-000" in loaded
        assert "line-149" in loaded
        assert "line-150" not in loaded
        assert "line-250" in loaded
        assert "line-299" in loaded
        assert "...[100 lines elided]..." in loaded
        # Three-field state-evidence notice, byte path NOT triggered.
        assert "⚠️ TRUNCATED (state evidence):" in loaded
        assert f"(original {original_bytes} bytes)." in loaded
        assert "read_file" in loaded
        assert "bytes elided" not in loaded

    def test_both_budgets_fire_single_notice(self, tmp_path, monkeypatch):
        from chaos_agent.agent.prompts.constants import MAX_EXPERIENCE_MD_BYTES

        # >25KB AND >200 lines: BOTH paths fire — exactly ONE notice (the
        # byte-budget notice rides the line-cut tail; the line path must
        # not append a second copy).
        lines = [f"{i:04d}-" + "x" * 90 for i in range(300)]
        text = "\n".join(lines)
        assert len(text.encode("utf-8")) > MAX_EXPERIENCE_MD_BYTES

        loaded = self._load(tmp_path, monkeypatch, text)

        assert len(loaded.encode("utf-8")) <= MAX_EXPERIENCE_MD_BYTES
        assert loaded.count("⚠️ TRUNCATED (state evidence):") == 1
        assert "lines elided" in loaded
        # The byte-path marker sits in the byte-cut middle (~line 196 of
        # the byte-truncated text), which the line cut elides — the line
        # marker + the single notice carry the visible truncation
        # semantics in the stacked scenario (the byte marker's quantified
        # form is pinned by the byte-only test above).

    def test_within_budget_verbatim(self, tmp_path, monkeypatch):
        text = "# Experience\n- rule one\n- rule two"

        loaded = self._load(tmp_path, monkeypatch, text)

        assert loaded == text
        assert "TRUNCATED" not in loaded
        assert "elided" not in loaded
