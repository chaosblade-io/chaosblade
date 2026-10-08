"""Tests for the LLM-based skill catalog generator."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.skills.catalog_generator import (
    _content_fingerprint,
    _dir_fingerprint,
    _extract_fault_symptom,
    _generate_from_catalogue,
    _parse_llm_json,
    build_nl_cmd,
    generate_skill_catalog,
    infer_blade_params,
    infer_scope,
)

_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"
_REAL_CASES = sorted(_SKILLS_ROOT.glob("*/references/catalogue/**/*.md"))
_CATALOGUE_DIRS = sorted(
    (d / "references" / "catalogue")
    for d in _SKILLS_ROOT.iterdir()
    if (d / "references" / "catalogue").is_dir()
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

    def test_pycache_and_ds_store_ignored(self, tmp_path):
        """不参与蒸馏的内容不计入指纹：

        - __pycache__/*.pyc、.DS_Store：运行时缓存产物，后端重启会重写，
          若计入会导致注册表被误判过期（case 内容未变却触发 stale）
        - scripts/：辅助脚本，capabilities-sync 只蒸馏 catalogue case
          文档，脚本改动不影响注册表产物
        """
        d = tmp_path / "skill"
        (d / "__pycache__").mkdir(parents=True)
        (d / "scripts").mkdir()
        (d / "SKILL.md").write_text("hello", encoding="utf-8")
        fp1 = _dir_fingerprint(d)
        (d / "__pycache__" / "loader.cpython-311.pyc").write_bytes(b"\x00\x01")
        (d / ".DS_Store").write_bytes(b"junk")
        (d / "scripts" / "list_scenarios.py").write_text("print(1)", encoding="utf-8")
        fp2 = _dir_fingerprint(d)
        assert fp1 == fp2


class TestGenerateSkillCatalog:
    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached(self, tmp_path):
        skill_content = "test skill content"
        fp = _content_fingerprint(skill_content)

        # Pre-populate cache
        cache_file = tmp_path / "memory" / "skill_catalog" / "skill_catalog_cache.json"
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
        cache_file = tmp_path / "memory" / "skill_catalog" / "skill_catalog_cache.json"
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
        cache_file = tmp_path / "memory" / "skill_catalog" / "skill_catalog_cache.json"
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
        cache_file = tmp_path / "memory" / "skill_catalog" / "skill_catalog_cache.json"
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


class TestInferScope:
    def test_node_prefix(self):
        assert infer_scope("Node_CPU使用率过高") == "node"

    def test_node_chinese_prefix(self):
        assert infer_scope("节点容器运行时磁盘使用率过高") == "node"

    def test_pod_prefix(self):
        assert infer_scope("Pod_Pending") == "pod"

    def test_service_prefix(self):
        assert infer_scope("Service_调用失败") == "service"

    def test_workload_prefix(self):
        assert infer_scope("workload_副本被缩容") == "workload"

    def test_daemonset_prefix(self):
        # extract_fault_type doesn't recognize "daemonset" as workload
        # (it checks "副本", "deployment", etc.) — falls back to "DaemonSet"
        # which infer_scope normalizes to default "pod"
        assert infer_scope("DaemonSet_未完全调度") == "pod"

    def test_dns_maps_to_pod(self):
        assert infer_scope("DNS_解析失败") == "pod"

    def test_unknown_defaults_to_pod(self):
        assert infer_scope("Unknown_Category") == "pod"


class TestInferBladeParams:
    def test_node_cpu(self):
        result = infer_blade_params("Node_CPU使用率过高")
        assert result == {"scope": "node", "target": "cpu", "action": "fullload"}

    def test_node_mem(self):
        result = infer_blade_params("Node_内存使用率过高")
        assert result == {"scope": "node", "target": "mem", "action": "load"}

    def test_node_disk_io(self):
        result = infer_blade_params("Node_磁盘IO过高")
        assert result == {"scope": "node", "target": "disk", "action": "burn"}

    def test_node_disk_fill(self):
        result = infer_blade_params("节点容器运行时磁盘使用率过高")
        assert result == {"scope": "node", "target": "disk", "action": "fill"}

    def test_pod_cpu(self):
        result = infer_blade_params("Pod_cpu使用率过高")
        assert result == {"scope": "pod", "target": "cpu", "action": "fullload"}

    def test_pod_cpu_throttling(self):
        result = infer_blade_params("Pod_CPU_Throttling")
        assert result == {"scope": "pod", "target": "cpu", "action": "fullload"}

    def test_pod_oom(self):
        result = infer_blade_params("Pod_OOM内存异常")
        assert result == {"scope": "pod", "target": "mem", "action": "load"}

    def test_pod_network_drop(self):
        result = infer_blade_params("Pod_网络丢包")
        assert result == {"scope": "pod", "target": "network", "action": "drop"}

    def test_pod_disk_fill(self):
        result = infer_blade_params("Pod_磁盘空间使用率过高")
        assert result == {"scope": "pod", "target": "disk", "action": "fill"}

    def test_dns_resolution(self):
        result = infer_blade_params("DNS_解析失败")
        assert result == {"scope": "pod", "target": "network", "action": "dns"}

    def test_pod_pending_returns_none(self):
        assert infer_blade_params("Pod_Pending") is None

    def test_pod_crashloop_returns_none(self):
        assert infer_blade_params("Pod_CrashLoopBackOff") is None

    def test_pod_image_pull_returns_none(self):
        assert infer_blade_params("Pod_镜像拉取失败") is None

    def test_service_returns_none(self):
        assert infer_blade_params("Service_调用失败") is None

    def test_workload_returns_none(self):
        assert infer_blade_params("workload_副本被缩容") is None


class TestBuildNlCmd:
    def test_node_scope_no_namespace(self):
        cmd = build_nl_cmd("异常进程占用", "Node_CPU使用率过高", "node")
        assert "命名空间" not in cmd
        assert "<node-name>" in cmd
        assert "kubeconfig" in cmd

    def test_pod_scope_has_namespace(self):
        cmd = build_nl_cmd("应用性能问题", "Pod_cpu使用率过高", "pod")
        assert "命名空间为<namespace>" in cmd
        assert "目标为<name>" in cmd


class TestGenerateFromCatalogueIntegration:
    def test_node_category_no_namespace_in_nl(self, tmp_path):
        cat_dir = tmp_path / "Node_CPU使用率过高"
        cat_dir.mkdir()
        (cat_dir / "Node_CPU使用率过高_异常进程占用.md").write_text(
            "**故障现象**：\n1. CPU使用率超过90%", encoding="utf-8"
        )
        result = _generate_from_catalogue(tmp_path, "test-skill")
        assert result and len(result) == 1
        uc = result[0]
        assert "命名空间" not in uc["example_cmd"]
        assert "<node-name>" in uc["example_cmd"]

    def test_pod_category_has_namespace_in_nl(self, tmp_path):
        cat_dir = tmp_path / "Pod_OOM内存异常"
        cat_dir.mkdir()
        (cat_dir / "Pod_OOM内存异常_内存压力过大.md").write_text(
            "**故障现象**：\n1. OOM Killed", encoding="utf-8"
        )
        result = _generate_from_catalogue(tmp_path, "test-skill")
        assert result and len(result) == 1
        uc = result[0]
        assert "命名空间为<namespace>" in uc["example_cmd"]
        assert "目标为<name>" in uc["example_cmd"]

    def test_symptom_category(self, tmp_path):
        cat_dir = tmp_path / "Pod_Pending"
        cat_dir.mkdir()
        (cat_dir / "Pod_Pending_节点资源不足.md").write_text(
            "**故障现象**：\n1. Pod stuck in Pending", encoding="utf-8"
        )
        result = _generate_from_catalogue(tmp_path, "test-skill")
        assert result and len(result) == 1
        uc = result[0]
        assert "命名空间为<namespace>" in uc["example_cmd"]


class TestExtractFaultSymptom:
    """_extract_fault_symptom 回归——迁移打断的「第 8 个抽取器」。

    历史缺陷：它用独立硬正则匹配加粗标记 ``**故障现象**``（不走单源
    skill_case_section），bold→markdown 迁移后对全语料返回空
    （95/95 → 0/95），fault_symptom 字段全空回落占位符；而满绿套件无一
    变红——旧 fixture 用旧加粗形态、且从不断言 fault_symptom。
    """

    def test_md_heading_form(self, tmp_path):
        # 迁移后的 ## 主形态——历史缺陷形态（旧硬正则在此返回空）
        f = tmp_path / "case.md"
        f.write_text(
            "## 故障现象\n\n1. 主机 CPU 持续超过 90%\n2. Load 升高\n",
            encoding="utf-8",
        )
        assert _extract_fault_symptom(f) == "主机 CPU 持续超过 90%"

    def test_legacy_bold_form(self, tmp_path):
        # legacy 加粗形态（第三方旧技能）仍须兼容——双形态
        f = tmp_path / "case.md"
        f.write_text("**故障现象**：\n1. OOM Killed\n", encoding="utf-8")
        assert _extract_fault_symptom(f) == "OOM Killed"

    def test_same_line_content(self, tmp_path):
        # 同行内容下移后的首行纯文本（无列表标记）
        f = tmp_path / "case.md"
        f.write_text("## 故障现象\n节点持续不可达\n", encoding="utf-8")
        assert _extract_fault_symptom(f) == "节点持续不可达"

    def test_bullet_form(self, tmp_path):
        f = tmp_path / "case.md"
        f.write_text("## 故障现象\n- 内存占用持续增长\n", encoding="utf-8")
        assert _extract_fault_symptom(f) == "内存占用持续增长"

    def test_no_section_returns_empty(self, tmp_path):
        f = tmp_path / "case.md"
        f.write_text("## 演练步骤\n步骤\n", encoding="utf-8")
        assert _extract_fault_symptom(f) == ""


@pytest.mark.skipif(not _REAL_CASES, reason="语料不在测试环境")
class TestExtractFaultSymptomRealCorpus:
    """真实语料端到端非空率——关键抽取器必须在真实语料断言非空率，不能
    只测合成 fixture（这正是历史回归满绿不可见的根因）。"""

    def test_fault_symptom_nonempty_rate(self):
        # 迁移后全语料 fault_symptom 必须 95/95 非空（历史缺陷：0/95）
        empty = [p for p in _REAL_CASES if not _extract_fault_symptom(p)]
        assert not empty, (
            f"fault_symptom 空返回 {len(empty)}/{len(_REAL_CASES)}："
            f"{[p.name for p in empty][:5]}"
        )

    def test_generate_from_catalogue_fills_symptom(self):
        # 端到端主路径：每个技能 catalogue 产出条目的 fault_symptom 填充率 100%
        for catalogue_dir in _CATALOGUE_DIRS:
            result = _generate_from_catalogue(catalogue_dir, catalogue_dir.name)
            assert result, f"{catalogue_dir}: catalogue 产出为空"
            unfilled = [uc for uc in result if not uc.get("fault_symptom")]
            assert not unfilled, (
                f"{catalogue_dir}: {len(unfilled)}/{len(result)} 条 "
                f"fault_symptom 为空，例如 {unfilled[0].get('resource_path')}"
            )
