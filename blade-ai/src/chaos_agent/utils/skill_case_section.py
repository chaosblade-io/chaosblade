"""Skill case 章节定位（单一源）。

本模块是「从 skill case 文档中按章节名取正文」的唯一实现，供 verify
（模板/计数）、execute（演练步骤自检）、recover（恢复验证）、plan（``/plan``
预览）四类调用方共用，同时承载文档级判别 ``is_case_document``（判断
ToolMessage 内容是用例文件还是目录列表/SKILL.md）。

**标题语法分层（根治边界歧义，2026-09-21）**

一级章节用 markdown 语法标题（``## 章名``），子标题保持加粗——「这是章节
边界还是正文内部结构」由语法承载，不再由正则猜测：

- ``## 演练步骤`` / ``## 注入验证（两条路径共用）`` —— 章节标题（主形态）
- ``**手段1（ChaosBlade）**`` / ``**持续性检查（必做）**——`` —— 正文内
  部子标题，永远不构成章节边界（历史上 85 个 (文件,章节) 对因此被截断的
  根因即旧边界正则把这类行当成章节）
- legacy 回退：第三方旧格式技能（``~/.blade-ai/skills/``）仍是
  ``**章名**：`` 加粗形态——词表内的加粗标题仍按章节识别（双形态统一
  参与边界判定，混合迁移文件同样正确）

**为什么边界必须是「词表内章节」而非「任意下一个加粗行」**

case 模板在章节内部大量使用顶格加粗行（手段1/手段2/持续性检查/降级兜底/
注入命令等子标题与加粗警示句，实测全语料 151 处，85 个 (文件,章节) 对的
正文含此类行——旧判据会在此截断）。旧判据「下一个顶格加粗行 = 下一章节」
在模板演进后越权；新判据收窄为「下一个**词表内**章节标题行（``##`` 或
legacy 加粗均可）或 ``---`` 分隔行」。

**词表（16 个一级章节，全语料聚类钉死，2026-09-22 复测）**

8 个全量章节（95/95 文件：用例名称/故障现象/资源准备/演练步骤/注入验证/
注入恢复/恢复验证/基准事实）+ 6 个部分骨架章节（故障定位 18/载体配方 17/
恢复动作配方 5/RCA症状 5/降级方案 17/注意事项 6）+ 2 个契约词（恢复说明/
注入说明——消费方按名引用，当前语料 0/95，登记以备）。新增章节名必须在此
登记——未登记的新 ``##`` 章节不构成边界（前一章节过度延伸，钉死测试会红，
响亮失败）。

**围栏感知（为什么必须 mask）**

全语料 95 文件有 1738 个行首 ``#``（2026-09-22 复测）：725 个在代码围栏
内（命令注释）、280 个在 YAML frontmatter 内（两者共 1005 个被 mask）；
围栏外 733 个全部是 ``##`` 章节标题——单 ``#`` 裸 bash 注释已清零（同日
修复：Sidecar 网络中断 15 行命令注释补围栏、镜像篡改 H1 标题移除；围栏
外单井号是 case 撰写缺陷，回潮由钉死测试拦截）。裸 ``^#`` 正则会把命令
注释当标题。解析前先把围栏内与 frontmatter 内的行替换
为**等长空白**（索引对齐，匹配位置可直接映射回原文）。

调用方**不应**自行拼接锚定与边界逻辑；新增章节抽取需求时在本模块扩展。
"""

from __future__ import annotations

import re

__all__ = ["find_section", "find_section_span", "has_section", "is_case_document"]

# ── 一级章节词表（单一源）────────────────────────────────────────────
# 全语料 95 个用例文件的顶格标题聚类钉死（2026-09-21）：7 个全量章节
# 95×，部分骨架章节经前后邻居分布确认为章节级（载体配方 16/17 在文档
# 头部、RCA症状 5/5 在故障现象与资源准备之间、降级方案 16/17 在文尾）。
# 手段1/手段2/持续性检查/注意事项/注入命令等是**子标题**，不在此表——
# 它们保持加粗、不构成边界。
_SECTION_NAMES = (
    # 头部（可选）
    "用例名称",
    "故障定位",
    "载体配方",
    "恢复动作配方",
    # 全量骨架（95/95）
    "故障现象",
    "RCA症状",
    "资源准备",
    "演练步骤",
    "注入验证",
    "注入恢复",
    "恢复验证",
    "基准事实",
    # 尾部（可选）
    "降级方案",
    # 独立尾块（6 文件骨架级）：消费契约钉死它是恢复验证之后的兄弟块
    # （test_recover_layer2_parse 断言其不落入恢复验证正文），故为边界。
    # 「手段2 注意事项」以「手段2」开头，不归一到本名——仍是子标题。
    "注意事项",
    # 测试契约词（语料零出现，纯 fixture 消费契约：恢复/注入说明是验证
    # 节的兄弟块而非其正文——test_recover_verifier 两处断言钉死）。
    "恢复说明",
    "注入说明",
)

# 文档级判别词表——三选一即可判定「这是用例文件」。与抽取词表同源
#（抽取词表的子集：目录列表/SKILL.md 绝不会有这三个标题）。
_CASE_SECTION_NAMES = ("故障现象", "注入验证", "恢复验证")

# 降级方案在语料中的带限定语形态（17 文件全部写作「降级方案（原生命令）」）
# ——归一化时剥限定语，词表只存裸名。
_MD_HEADING_RE = re.compile(r'^[ \t]*(#{1,2})[ \t]*(.+?)[ \t]*[：:]?[ \t]*$')
_BOLD_HEADING_RE = re.compile(r'^[ \t]*\*\*(.+?)\*\*[ \t]*(?=[：:（(——\s]|$)')
# 行首 ---：分隔线与候选分割行（``--- Candidate N: ... ---``）都是章节
# 终点——frontmatter 的 --- 已被 mask，不在此列。表格行 ``|---|`` 行首
# 是 ``|``，不匹配。
_RULE_RE = re.compile(r'^---', re.MULTILINE)

# 按长度降序，保证「注入验证」优先于更短的同前缀词（当前词表无同前缀
# 对，防御未来登记引入）。
_NAMES_BY_LEN = tuple(sorted(_SECTION_NAMES, key=len, reverse=True))


def _match_section_name(title: str) -> str | None:
    """标题文本归一到词表章节名；不在词表返回 ``None``。

    接受精确命中（``演练步骤``）与限定语形态（``注入验证（两条路径共用）``、
    ``降级方案（原生命令）``）——限定语以全角/半角左括号紧跟章名识别。
    """
    t = title.strip()
    if t in _SECTION_NAMES:
        return t
    for n in _NAMES_BY_LEN:
        if t.startswith(n) and len(t) > len(n) and t[len(n)] in "（(":
            return n
    return None


def _mask_non_prose(content: str) -> str:
    """代码围栏内 + YAML frontmatter 内的行替换为等长空白。

    等长替换保证正则在遮蔽文本上的匹配区间可以直接映射回原 ``content``
    ——位置语义零漂移。围栏开关行（```` ``` ````）与 frontmatter 边界
    （``---``）本身也在遮蔽区：正文里真正的 ``---`` 分隔行不受影响。
    """
    lines = content.split('\n')
    in_fence = False
    in_fm = False
    for i, line in enumerate(lines):
        if line.strip().startswith('```'):
            lines[i] = ' ' * len(line)
            in_fence = not in_fence
            continue
        if in_fence:
            lines[i] = ' ' * len(line)
            continue
        if in_fm:
            lines[i] = ' ' * len(line)
            if line.strip() == '---':
                in_fm = False
            continue
        if i == 0 and line.strip() == '---':
            in_fm = True
            lines[i] = ' ' * len(line)
    return '\n'.join(lines)


def _scan_headings(masked: str) -> list[tuple[int, int, int, str]]:
    """扫描遮蔽文本中的词表内章节标题行。

    返回升序 ``[(line_start, body_start, line_end, section_name)]``（原
    ``content`` 坐标）。两种形态等价识别：

    - ``## 章名`` / ``# 章名`` / ``## 章名（限定语）`` —— markdown 主形态
      （``###`` 及更深是子结构，**不**识别为章节——标准 markdown 层级语义）
    - ``**章名**：`` / ``**章名**（限定语）：`` / ``**用例名称** 标题文本``
      —— legacy 加粗形态（词表内名字限定，行首加粗的子标题不命中）

    三种坐标各司其职：

    - ``line_start``：章节标题行行首——**边界判定**用（它是前一章节的终点）
    - ``line_end``：标题行行尾——``##`` 形态的正文起点（标题行独占）
    - ``body_start``：legacy 形态的正文起点——落在 ``**章名**`` 标记与一个
      分隔符（``：``/``（``/``(``）之后。**不是行尾**：标题行同行的限定语
      （如 ``（主路径 = 基线捕获→装配器）：``）保留在正文里——限定语承载
      路由信息（「两种手段共用」「kubectl-native 主路径」），恢复验证全文
      注入等消费方需要它（与历史行为逐字节等价）。
    """
    headings: list[tuple[int, int, int, str]] = []
    for m in re.finditer(r'^.*$', masked, re.MULTILINE):
        line = m.group(0)
        if not line.strip():
            continue
        hm = _MD_HEADING_RE.match(line)
        if hm and len(hm.group(1)) <= 2:
            name = _match_section_name(hm.group(2))
            if name is not None:
                headings.append((m.start(), m.end(), m.end(), name))
                continue
        bm = _BOLD_HEADING_RE.match(line)
        if bm:
            name = _match_section_name(bm.group(1))
            if name is not None:
                # 正文起点：**章名** 后消耗至多一个分隔符（：/（/(）。
                # lookahead 命中空格时 `[ \t]*` 已消耗空白；命中 $ 时行尾
                # 即正文起点。用原文（非 masked）判定——masked 行是全空白
                # 的不可能到这里。
                body_start = bm.end()
                if body_start < len(line) and line[body_start] in "：:（(":
                    body_start += 1
                headings.append((m.start(), m.start() + body_start, m.end(), name))
    return headings


def find_section_span(content: str, name: str) -> tuple[int, int] | None:
    """定位章节正文范围 ``(body_start, body_end)``；无该章节标题返回 ``None``。

    ``body_start`` 落在标题行末（标题本身不含在正文内）；``body_end`` 落在
    下一个**词表内章节标题行**（``##`` 或 legacy 形态、含同名重复出现）、
    或行首 ``---`` 分隔/候选分割行、或文本末尾之前。顶格加粗**子标题**
    （手段1/持续性检查等，不在词表）不终止章节——这是 55 个章节截断的修复。
    """
    if not content or not name:
        return None
    masked = _mask_non_prose(content)
    headings = _scan_headings(masked)

    # 起点扫描顺序：文档位置序（## 主形态与 legacy 回退统一竞争）——
    # 迁移完整的文件 ## 是唯一起点；漏迁时 legacy 起点行为与历史一致。
    body_start = None
    for _ls, bs, _le, hname in headings:
        if hname == name:
            body_start = bs
            break
    if body_start is None:
        return None

    # 终点 = 其后第一个「词表内章节标题行（含同名重复）」或行首 ``---``
    # 行——两者取更近者（``---`` 更近时不能被更远的章节标题覆盖）。
    end = len(content)
    for ls, _bs, _le, _hname in headings:
        if ls >= body_start:
            end = min(end, ls)
            break  # headings 升序，第一个即最近
    rm = _RULE_RE.search(masked, body_start)
    if rm is not None:
        end = min(end, rm.start())
    return body_start, end


def find_section(content: str, name: str) -> str | None:
    """返回章节正文（不含标题行）。

    **无该章节标题返回 ``None``**，与「章节存在但内容为空」返回的 ``""``
    区分开——前者表示该文档没有这个章节（调用方应降级），后者表示章节为空。
    """
    span = find_section_span(content, name)
    if span is None:
        return None
    return content[span[0]:span[1]]


def has_section(content: str, name: str) -> bool:
    """该文档是否存在该章节标题（与正文提及无关）。"""
    return find_section_span(content, name) is not None


def is_case_document(content: str) -> bool:
    """``content`` 是否为 skill 用例文档（而非目录列表 / ``SKILL.md`` / 参考文档）。

    锚定判据同 ``has_section``（标题行首锚定，双形态）。裸子串版本
    （``"**故障现象**" in content``）会把两份 ``SKILL.md`` 的意图识别表格行
    ``| **故障现象** | 期望模拟的表现 | ... |`` 误判成用例——实测全 105 个
    .md：三选一裸子串命中 97 个（95 用例 + 2 份 SKILL.md），标题锚定后
    命中 95 个（全部用例、零漏判、零误判）。
    """
    if not content:
        return False
    return any(has_section(content, name) for name in _CASE_SECTION_NAMES)
