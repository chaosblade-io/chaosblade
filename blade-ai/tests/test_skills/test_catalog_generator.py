"""Tests for the LLM-based skill catalog generator."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.skills.catalog_generator import (
    _content_fingerprint,
    _dir_fingerprint,
    _parse_llm_json,
    generate_skill_catalog,
)


class TestParseLlmJson:
    def test_valid_json_array(self):
        raw = '[{"category": "Pod_Pending", "use_case_name": "CPU high", "fault_symptom": "CPU fullload", "resource_path": "ref/a.md", "example_cmd": "blade-ai inject -i test"}]'
        result = _parse_llm_json(raw)
        assert len(result) == 1
        assert result[0]["use_case_name"] == "CPU high"
        assert result[0]["fault_symptom"] == "CPU fullload"
        assert result[0]["category"] == "Pod_Pending"

    def test_json_in_markdown_code_block(self):
        raw = '```json\n[{"category": "Pod_OOM", "use_case_name": "test", "fault_symptom": "desc", "resource_path": "ref/b.md", "example_cmd": "cmd"}]\n```'
        result = _parse_llm_json(raw)
        assert len(result) == 1
        assert result[0]["use_case_name"] == "test"

    def test_json_with_extra_text(self):
        raw = 'Here is the result:\n[{"category": "a", "use_case_name": "a", "fault_symptom": "b", "resource_path": "c", "example_cmd": "c"}]\nEnd.'
        result = _parse_llm_json(raw)
        assert len(result) == 1

    def test_invalid_json_returns_none(self):
        raw = "This is not JSON at all"
        result = _parse_llm_json(raw)
        assert result is None

    def test_non_list_json_returns_none(self):
        raw = '{"key": "value"}'
        result = _parse_llm_json(raw)
        assert result is None

    def test_missing_fields_get_defaults(self):
        raw = '[{"use_case_name": "only name"}]'
        result = _parse_llm_json(raw)
        assert len(result) == 1
        assert result[0]["fault_symptom"] == ""
        assert result[0]["example_cmd"] == ""
        assert result[0]["category"] == ""
        assert result[0]["resource_path"] == ""

    def test_non_dict_items_skipped(self):
        raw = '["string_item", {"category": "x", "use_case_name": "valid", "fault_symptom": "d", "resource_path": "r", "example_cmd": "c"}]'
        result = _parse_llm_json(raw)
        assert len(result) == 1
        assert result[0]["use_case_name"] == "valid"


class TestContentFingerprint:
    def test_same_content_same_fingerprint(self):
        assert _content_fingerprint("abc") == _content_fingerprint("abc")

    def test_different_content_different_fingerprint(self):
        assert _content_fingerprint("abc") != _content_fingerprint("def")


class TestDirFingerprint:
    def test_same_dir_same_fingerprint(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        (d / "SKILL.md").write_text("hello", encoding="utf-8")
        assert _dir_fingerprint(d) == _dir_fingerprint(d)

    def test_file_change_different_fingerprint(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        (d / "SKILL.md").write_text("old", encoding="utf-8")
        fp1 = _dir_fingerprint(d)
        (d / "SKILL.md").write_text("new", encoding="utf-8")
        fp2 = _dir_fingerprint(d)
        assert fp1 != fp2

    def test_new_file_different_fingerprint(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        (d / "SKILL.md").write_text("hello", encoding="utf-8")
        fp1 = _dir_fingerprint(d)
        (d / "extra.md").write_text("extra", encoding="utf-8")
        fp2 = _dir_fingerprint(d)
        assert fp1 != fp2

    def test_nonexistent_dir_returns_empty(self):
        assert _dir_fingerprint(Path("/nonexistent")) == ""


class TestGenerateSkillCatalog:
    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached(self, tmp_path):
        skill_content = "test skill content"
        fp = _content_fingerprint(skill_content)

        # Pre-populate cache
        cache_file = tmp_path / "memory" / "tool_cache" / "skill_catalog_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_data = {
            "test-skill": {
                "fingerprint": fp,
                "use_cases": [{"category": "Pod_Pending", "use_case_name": "cached", "fault_symptom": "d", "resource_path": "r", "example_cmd": "c"}],
            }
        }
        cache_file.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")

        llm = MagicMock()  # Should NOT be called

        result = await generate_skill_catalog(
            skill_name="test-skill",
            skill_content=skill_content,
            skill_dir=None,
            llm=llm,
            work_dir=tmp_path,
            no_cache=False,
        )

        assert len(result) == 1
        assert result[0]["use_case_name"] == "cached"
        llm.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_calls_llm(self, tmp_path):
        llm_response = MagicMock()
        llm_response.content = json.dumps([
            {"category": "Pod_CPU", "use_case_name": "Pod CPU high", "fault_symptom": "Inject CPU", "resource_path": "ref/a.md", "example_cmd": "blade-ai inject -i test"}
        ])
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=llm_response)

        result = await generate_skill_catalog(
            skill_name="test-skill",
            skill_content="some skill content",
            skill_dir=None,
            llm=llm,
            work_dir=tmp_path,
            no_cache=False,
        )

        assert len(result) == 1
        assert result[0]["use_case_name"] == "Pod CPU high"
        llm.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_cache_forces_llm(self, tmp_path):
        skill_content = "test skill content"
        fp = _content_fingerprint(skill_content)

        # Pre-populate cache
        cache_file = tmp_path / "memory" / "tool_cache" / "skill_catalog_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_data = {
            "test-skill": {
                "fingerprint": fp,
                "use_cases": [{"category": "Pod_Pending", "use_case_name": "cached", "fault_symptom": "d", "resource_path": "r", "example_cmd": "c"}],
            }
        }
        cache_file.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")

        llm_response = MagicMock()
        llm_response.content = json.dumps([
            {"category": "x", "use_case_name": "regenerated", "fault_symptom": "new", "resource_path": "r", "example_cmd": "cmd"}
        ])
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=llm_response)

        result = await generate_skill_catalog(
            skill_name="test-skill",
            skill_content=skill_content,
            skill_dir=None,
            llm=llm,
            work_dir=tmp_path,
            no_cache=True,
        )
    
        assert len(result) == 1
        assert result[0]["use_case_name"] == "regenerated"
        llm.ainvoke.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty(self, tmp_path):
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(side_effect=Exception("LLM error"))

        result = await generate_skill_catalog(
            skill_name="test-skill",
            skill_content="content",
            skill_dir=None,
            llm=llm,
            work_dir=tmp_path,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_content_change_invalidates_cache(self, tmp_path):
        old_content = "old skill content"
        old_fp = _content_fingerprint(old_content)

        # Pre-populate cache with old content fingerprint
        cache_file = tmp_path / "memory" / "tool_cache" / "skill_catalog_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_data = {
            "test-skill": {
                "fingerprint": old_fp,
                "use_cases": [{"category": "z", "use_case_name": "old", "fault_symptom": "d", "resource_path": "r", "example_cmd": "c"}]
            }
        }
        cache_file.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")

        llm_response = MagicMock()
        llm_response.content = json.dumps([
            {"category": "y", "use_case_name": "new", "fault_symptom": "n", "resource_path": "r", "example_cmd": "nc"}
        ])
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=llm_response)

        # Call with DIFFERENT content — should miss cache and call LLM
        result = await generate_skill_catalog(
            skill_name="test-skill",
            skill_content="new skill content",
            skill_dir=None,
            llm=llm,
            work_dir=tmp_path,
        )

        assert len(result) == 1
        assert result[0]["use_case_name"] == "new"
        llm.ainvoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_dir_fingerprint_change_invalidates_cache(self, tmp_path):
        """When skill_dir files change, directory fingerprint changes and cache is invalidated."""
        # Create a skill directory with a file
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("old content", encoding="utf-8")

        old_fp = _dir_fingerprint(skill_dir)

        # Pre-populate cache with old directory fingerprint
        cache_file = tmp_path / "memory" / "tool_cache" / "skill_catalog_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_data = {
            "my-skill": {
                "fingerprint": old_fp,
                "use_cases": [{"category": "z", "use_case_name": "old", "fault_symptom": "d", "resource_path": "r", "example_cmd": "c"}]
            }
        }
        cache_file.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")

        llm_response = MagicMock()
        llm_response.content = json.dumps([
            {"category": "y", "use_case_name": "new", "fault_symptom": "n", "resource_path": "r", "example_cmd": "nc"}
        ])
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=llm_response)

        # Modify a file in the skill directory
        (skill_dir / "SKILL.md").write_text("new content", encoding="utf-8")

        # Call with the same skill_dir — fingerprint should differ, cache miss
        result = await generate_skill_catalog(
            skill_name="my-skill",
            skill_content="skill content",
            skill_dir=skill_dir,
            llm=llm,
            work_dir=tmp_path,
        )

        assert len(result) == 1
        assert result[0]["use_case_name"] == "new"
        llm.ainvoke.assert_called_once()
