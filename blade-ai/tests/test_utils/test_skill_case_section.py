"""Tests for skill_case_section.py — 章节定位单一源（双形态 + 围栏感知）。

三层：
1. 单元（合成 fixture）——## 主形态、legacy 回退、围栏/frontmatter mask、
   子标题不截断、``---`` 边界、词表外 ## 不构成边界等解析行为面；
2. victim 回归——真实迁移文件断言（限定语下移、同行内容下移、子标题
   包含性），每条对应历史缺陷形态；
3. 全量钉死——95 文件语料扫描：全部 ``##`` 标题词表内、七全量章节
   95/95、过度延伸 0、legacy 加粗章节标题残留 0。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chaos_agent.utils.skill_case_section import (
    find_section,
    find_section_span,
    has_section,
    is_case_document,
    _match_section_name,
    _mask_non_prose,
)

_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
_CATALOGUE_GLOB = "*/references/catalogue/**/*.md"


# ── Layer 1: 单元（合成 fixture）────────────────────────────────────


class TestMdHeadingForm:
    """## 主形态（迁移后的目标形态）。"""

    def test_basic_extraction(self):
        content = "## 故障现象\n目标 CPU 90%。\n\n## 资源准备\n- 集群\n"
        assert find_section(content, "故障现象") == "\n目标 CPU 90%。\n\n"

    def test_qualified_heading_normalizes(self):
        content = "## 降级方案（原生命令）\n手动 kubectl。\n"
        body = find_section(content, "降级方案")
        assert body is not None and "手动 kubectl" in body

    def test_h1_also_matches(self):
        content = "# 注入恢复\n正文。\n"
        assert has_section(content, "注入恢复")

    def test_h3_is_not_section(self):
        # ### 及更深是子结构，不构成章节——标准 markdown 层级语义
        content = "## 演练步骤\n\n### 手动小节\n细节\n"
        body = find_section(content, "演练步骤")
        assert "### 手动小节" in body and "细节" in body

    def test_unregistered_md_heading_is_not_boundary(self):
        # 未登记的新 ## 章节不构成边界——前一章节延伸到词表内下一章节。
        # 新章节名必须登记进词表，否则钉死测试会红（响亮失败）。
        content = "## 演练步骤\n步骤甲\n\n## 新章节名\n新内容\n\n## 注入验证\n验证\n"
        body = find_section(content, "演练步骤")
        assert "步骤甲" in body and "新内容" in body


class TestLegacyBoldForm:
    """legacy 加粗回退（第三方旧格式技能）。"""

    def test_basic_legacy(self):
        content = "**故障现象**：\n目标内存 95%。\n\n**资源准备**：\n"
        assert "目标内存 95%" in find_section(content, "故障现象")

    def test_legacy_qualifier_outside_bold_kept_in_body(self):
        # **章名**（限定语）： —— 限定语保留在正文（路由信息，消费方契约）
        content = "**恢复验证**（两种手段共用）：\n检查规则已清除。\n"
        body = find_section(content, "恢复验证")
        assert body is not None
        assert body.startswith("两种手段共用")
        assert "检查规则已清除" in body

    def test_legacy_same_line_content(self):
        content = "**故障定位**：持续型故障——窗口内进程反复被杀。\n后续行。\n"
        body = find_section(content, "故障定位")
        assert body is not None
        assert body.startswith("持续型故障")
        assert "后续行" in body

    def test_legacy_qualifier_inside_bold(self):
        content = "**降级方案（原生命令）**：\n手动执行。\n"
        body = find_section(content, "降级方案")
        assert body is not None and "手动执行" in body

    def test_mixed_forms_first_occurrence_wins(self):
        # ## 与 legacy 统一按文档位置序竞争，先出现者为起点
        content = (
            "## 注入验证\n主形态正文。\n\n"
            "**注入验证**：\nlegacy 正文。\n\n## 注入恢复\n恢复。\n"
        )
        assert "主形态正文" in find_section(content, "注入验证")

    def test_duplicate_name_takes_first(self):
        content = "## 恢复验证\n第一处。\n\n## 恢复验证\n第二处。\n"
        body = find_section(content, "恢复验证")
        assert "第一处" in body and "第二处" not in body


class TestSubheadingNoTruncation:
    """顶格加粗子标题不截断——38 处历史截断的根因回归。"""

    def test_subheading_inside_section(self):
        content = (
            "## 注入验证\n前置说明\n"
            "**手段1（ChaosBlade）**：\nblade 输出。\n"
            "**持续性检查（必做）**——窗口内采样。\n"
            "**降级兜底**：\n手动路径。\n"
            "\n## 注入恢复\n恢复。\n"
        )
        body = find_section(content, "注入验证")
        assert body is not None
        for expect in ("前置说明", "手段1（ChaosBlade）", "blade 输出",
                       "持续性检查", "降级兜底", "手动路径"):
            assert expect in body, f"子标题截断: 缺 {expect}"
        assert "## 注入恢复" not in body

    def test_legacy_subheading_beyond_boundary(self):
        # legacy 形态下子标题同样不终止章节（混合迁移文件正确性）
        content = (
            "**演练步骤**：\n1. 基线\n"
            "**手段2（kubectl-native）**：\nkubectl 序列\n"
            "\n---\n"
        )
        body = find_section(content, "演练步骤")
        assert "kubectl 序列" in body


class TestFenceAndFrontmatterMask:
    """围栏与 frontmatter 感知——537 处围栏内 # 注释、262 处 frontmatter。"""

    def test_bash_comment_not_heading(self):
        content = (
            "## 注入恢复\n"
            "```bash\n"
            "# 注入恢复：定时器已武装\n"
            "kubectl delete x\n"
            "```\n"
            "收尾。\n"
        )
        body = find_section(content, "注入恢复")
        assert body is not None
        assert "kubectl delete x" in body
        assert "收尾" in body

    def test_fenced_heading_not_boundary(self):
        # 围栏内的 ## 章名（如 markdown 示例文档）不构成边界
        content = (
            "## 演练步骤\n前置\n"
            "```markdown\n"
            "## 注入验证\n围栏内假章节\n```\n"
            "后置内容\n"
            "\n## 注入恢复\n恢复\n"
        )
        body = find_section(content, "演练步骤")
        assert "围栏内假章节" in body and "后置内容" in body

    def test_frontmatter_masked(self):
        content = (
            "---\n"
            "name: case\n"
            "# 故障现象: yaml 注释\n"
            "---\n"
            "## 故障现象\n真正文。\n"
        )
        body = find_section(content, "故障现象")
        assert body is not None and "真正文" in body

    def test_mask_preserves_positions(self):
        # 等长空白替换：遮蔽文本上的坐标可映射回原文
        content = "## 故障现象\n正文\n```bash\n# x\n```\n"
        masked = _mask_non_prose(content)
        assert len(masked) == len(content)
        assert masked.split('\n')[3] == ' ' * len("# x")


class TestRuleBoundary:
    """``---`` 分隔/候选分割行是章节终点。"""

    def test_rule_ends_section(self):
        content = "## 基准事实\n事实A\n\n---\n\n**手段2（kubectl-native）**\n其他\n"
        body = find_section(content, "基准事实")
        assert body is not None
        assert "事实A" in body and "手段2" not in body

    def test_nearest_rule_wins_over_far_heading(self):
        # --- 更近时不能被更远的章节标题覆盖
        content = "## 演练步骤\n内容\n\n--- Candidate 2: x ---\n\n## 注入验证\nv\n"
        body = find_section(content, "演练步骤")
        assert body is not None and "内容" in body
        assert "--- Candidate 2" not in body


class TestSemantics:
    def test_missing_section_returns_none(self):
        assert find_section("无关文档", "注入验证") is None
        assert find_section("", "注入验证") is None
        assert find_section("## 注入验证\nx", "") is None

    def test_empty_section_returns_empty_string(self):
        assert find_section("## 注入验证\n\n## 注入恢复\n", "注入验证") == "\n\n"

    def test_body_reference_does_not_hijack(self):
        # 正文行内提及章节名（「见注入验证」）不劫持锚点——行首锚定
        content = "## 故障现象\n见注入验证与恢复验证章节。\n\n## 注入验证\n真验证。\n"
        body = find_section(content, "注入验证")
        assert body is not None and "真验证" in body

    def test_match_section_name_forms(self):
        assert _match_section_name("演练步骤") == "演练步骤"
        assert _match_section_name("注入验证（两条路径共用）") == "注入验证"
        assert _match_section_name("降级方案（原生命令）") == "降级方案"
        assert _match_section_name("手段1（ChaosBlade）") is None
        assert _match_section_name("恢复说明2") is None


class TestCaseDocumentDetection:
    def test_case_document_detected(self):
        assert is_case_document("## 故障现象\nx\n## 注入恢复\ny\n")

    def test_legacy_case_document_detected(self):
        assert is_case_document("**故障现象**：\nx\n")

    def test_skill_md_table_not_case(self):
        # SKILL.md 意图识别表格行 | **故障现象** | ... | 不误判
        table = "| **故障现象** | 期望模拟的表现 | 意图识别 |\n|---|---|---|\n"
        assert not is_case_document(table)

    def test_catalogue_listing_not_case(self):
        listing = "- 故障现象类\n  - Pod_CPU满载.md\n"
        assert not is_case_document(listing)

    def test_empty(self):
        assert not is_case_document("")


# ── Layer 2: victim 回归（真实迁移文件）──────────────────────────────

_VICTIM_DIR = _SKILLS_DIR / "k8s-chaos-skills" / "references" / "catalogue"
_HOST_VICTIM_DIR = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"


def _read(rel: str) -> str:
    return (_VICTIM_DIR / rel).read_text(encoding="utf-8")


class TestVictimRegressions:
    """每条对应一类历史缺陷形态的真实文件。"""

    def test_qualifier_moved_to_body(self):
        # 限定语外置 victim：载体配方的 recovery_channel 路由信息留在正文
        content = _read("ConfigMap_内容错误/ConfigMap_内容错误_关键配置被篡改.md")
        body = find_section(content, "载体配方")
        assert body is not None
        assert "recovery_channel: apiserver-write" in body

    def test_same_line_content_moved_to_body(self):
        # 同行内容 victim：故障定位首句下移，正文完整
        content = (_HOST_VICTIM_DIR
                   / "Host_进程异常/Host_进程异常_进程被杀死.md"
                   ).read_text(encoding="utf-8")
        body = find_section(content, "故障定位")
        assert body is not None
        assert body.lstrip("\n").startswith("持续型故障")
        assert "本用例不提供一次性杀死" in body

    def test_persistent_check_subheading_not_truncating(self):
        # 38 处截断代表 victim：注入验证正文必须完整包含持续性检查子标题
        content = _read(
            "Container_进程异常/Container_进程异常_Sidecar进程被挂起.md"
        )
        body = find_section(content, "注入验证")
        assert body is not None
        assert "持续性检查（必做）" in body
        assert "## 注入恢复" not in body

    def test_recovery_section_full_contract(self):
        # 恢复验证全文注入消费方：限定语 + 手动降级序列都在正文内
        content = _read("ConfigMap_内容错误/ConfigMap_内容错误_关键配置被篡改.md")
        body = find_section(content, "注入恢复")
        assert body is not None
        assert "主路径下 CM 还原无需 Agent 执行动作" in body
        assert "降级兜底" in body


# ── Layer 3: 全量钉死（95 文件语料）──────────────────────────────────

_ALL_CASES = sorted(_SKILLS_DIR.glob(_CATALOGUE_GLOB))

# 全量骨架章节（95/95 文件必含）——词表子集，钉死防语料漂移
_CORE_SECTIONS = (
    "故障现象", "资源准备", "演练步骤", "注入验证", "注入恢复", "恢复验证", "基准事实",
)

_LEGACY_BOLD_SECTION_RE = re.compile(
    r'^\*\*(' + "|".join((
        "故障现象", "RCA症状", "资源准备", "演练步骤", "注入验证", "注入恢复",
        "恢复验证", "基准事实", "降级方案", "故障定位", "载体配方", "恢复动作配方",
        "注意事项",
    )) + r')\*\*'
)


@pytest.mark.skipif(not _ALL_CASES, reason="语料不在测试环境")
class TestCorpusPinned:
    """全量语料钉死——迁移完成态与解析完备性的持久契约。"""

    def test_corpus_size_pinned(self):
        # 98 = host 18 + k8s 71 + python-app 9；增删用例时同步更新此数
        assert len(_ALL_CASES) == 98

    def test_all_md_headings_in_wordlist(self):
        # 独立护栏（不经 _scan_headings）：围栏外每个 ## 标题归一化后必须 ∈ 词表。
        # _scan_headings 在返回前就过滤掉未登记名（name is None 不 append），
        # 用它自检等于「实现证明自己」——未登记的新 ## 被静默吞掉、前一章节
        # 过度延伸而测试仍绿。这里改用独立正则扫描全部 ##，未登记者在此变红。
        # 形态域裁定（2026-09-22）：护栏只认规范形态 ``## 章名``（井号后空格、
        # 无拖尾冒号）——无空格 ``##名``、单井号、顶格加粗的未登记名都不是
        # 合法章节写法，属 case 撰写缺陷，修 case 文档而非扩护栏容错（扩到
        # 这些形态是过度保护）；围栏逃逸单井号由
        # test_no_stray_single_hash_outside_fences 单独钉死。
        from chaos_agent.utils.skill_case_section import _SECTION_NAMES
        heading_re = re.compile(r"^[ \t]*#{2}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
        for path in _ALL_CASES:
            masked = _mask_non_prose(path.read_text(encoding="utf-8"))
            for m in heading_re.finditer(masked):
                title = m.group(1)
                assert _match_section_name(title) in _SECTION_NAMES, (
                    f"{path.name}: 未登记 ## 章节 {title!r}"
                    "（不构成边界 → 前一章节过度延伸）"
                )

    def test_every_case_has_core_sections(self):
        for path in _ALL_CASES:
            content = path.read_text(encoding="utf-8")
            for name in _CORE_SECTIONS:
                assert has_section(content, name), (
                    f"{path.name}: 缺全量章节 {name}"
                )

    def test_no_overshoot_into_next_section(self):
        # 过度延伸归零（独立判据）：用独立正则扫描每个章节正文，断言不含任何
        # 围栏外 ## 标题行。旧版只比对「下一词表章节标题」，而实现的
        # end=min(end,下一词表标题) 数学上保证正文不含词表标题——自证空转。
        # 改为扫描任意 ##：未登记 ## 不是边界、会被吞进正文，此扫描能捕获。
        # 形态域与 test_all_md_headings_in_wordlist 同口径（只扫规范形态
        # ``## ``，井号后空格；非规范形态的裁定见该测试注释）。
        from chaos_agent.utils.skill_case_section import _SECTION_NAMES
        heading_re = re.compile(r"^[ \t]*#{2}[ \t]+", re.MULTILINE)
        for path in _ALL_CASES:
            content = path.read_text(encoding="utf-8")
            masked = _mask_non_prose(content)
            for name in _SECTION_NAMES:
                span = find_section_span(content, name)
                if span is None:
                    continue
                # 正文起点已在本章标题行之后，故扫到的任何 ## 都是被吞并的后章
                body_masked = masked[span[0]:span[1]]
                assert heading_re.search(body_masked) is None, (
                    f"{path.name}: [{name}] 正文吞并了一个 ## 标题行（过度延伸）"
                )

    def test_no_stray_single_hash_outside_fences(self):
        # 「case 自己改」的探测器（2026-09-22 裁定）：围栏外单井号行不是合法
        # 章节写法——bash 注释必须在代码围栏内、文档不用单井号标题。出现即
        # case 撰写缺陷，修 case 文档，不为解析器/护栏加容错（非规范 md
        # 形态不保护）。清零于同日：Sidecar 网络中断 15 行命令注释补围栏、
        # 镜像篡改 H1 标题移除。死区风险实证：Sidecar 的逃逸块位于尾部
        # ``---`` 规则线之后——若其中注释恰含词表名会被误认成章节起点。
        stray_re = re.compile(r"^[ \t]*#(?!#)")
        for path in _ALL_CASES:
            masked = _mask_non_prose(path.read_text(encoding="utf-8"))
            for i, line in enumerate(masked.split("\n"), 1):
                assert not stray_re.match(line), (
                    f"{path.name}:{i} 围栏外单井号行（bash 注释须入围栏/"
                    f"不用单井号标题——修 case，不加解析容错）: {line.strip()[:60]}"
                )

    def test_no_legacy_bold_section_residue(self):
        # 迁移完成钉死：词表章节的 legacy 加粗形态残留 0（第三方技能不受限）
        for path in _ALL_CASES:
            for i, line in enumerate(
                path.read_text(encoding="utf-8").split('\n'), 1
            ):
                assert not _LEGACY_BOLD_SECTION_RE.match(line), (
                    f"{path.name}:{i} legacy 加粗章节残留: {line[:40]}"
                )

    def test_case_document_detection_full_corpus(self):
        # 全部用例命中、零漏判（is_case_document 消费契约：execute/planning）
        for path in _ALL_CASES:
            assert is_case_document(path.read_text(encoding="utf-8")), (
                f"{path.name}: 未被识别为用例文档"
            )
