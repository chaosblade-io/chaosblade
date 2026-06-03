"""LLM-based skill catalog generator with file cache.

Reads each skill's SKILL.md content, sends it to an LLM, and asks the LLM
to produce a list of injectable fault scenarios with example commands.
Results are cached to a JSON file keyed by content fingerprint so that
repeated `blade-ai list` calls are fast.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from chaos_agent.config.settings import settings
from chaos_agent.utils.fault_type import extract_fault_type

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

_CACHE_FILENAME = "skill_catalog_cache.json"


def _cache_path(work_dir: Path) -> Path:
    """Return the cache file path under the working directory."""
    p = work_dir / "memory" / "tool_cache" / _CACHE_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_cache(cache_file: Path) -> dict:
    """Load the entire cache dict from disk (or return empty dict)."""
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache_file: Path, data: dict) -> None:
    """Persist the entire cache dict to disk."""
    try:
        cache_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.warning(f"Failed to save skill catalog cache: {e}")


def _content_fingerprint(content: str) -> str:
    """MD5 of skill content — changes invalidate cache entry."""
    return hashlib.md5(content.encode()).hexdigest()


def _dir_fingerprint(skill_dir: Path) -> str:
    """MD5 fingerprint from all files under skill_dir.

    Based on relative paths + modification times so that any addition,
    deletion, or content change invalidates the cache.
    """
    if not skill_dir.exists() or not skill_dir.is_dir():
        return ""
    parts: list[str] = []
    for f in sorted(skill_dir.rglob("*")):
        if f.is_file():
            try:
                rel = f.relative_to(skill_dir)
                mtime = f.stat().st_mtime
                parts.append(f"{rel}:{mtime}")
            except OSError:
                continue
    if not parts:
        return ""
    raw = "|".join(parts)
    return hashlib.md5(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_CATALOG_SYSTEM_PROMPT = """\
根据 skill 目录结构，为每个故障用例生成标准化的列表条目，输出 JSON 数组。

每个条目包含：
- category: 故障分类（目录名），如"Pod_Pending""Pod_OOM内存异常"
- use_case_name: 格式"根因 导致 分类"，如"节点资源不足 导致 Pod_Pending"
- fault_symptom: 故障现象一句话
- resource_path: 参考文件相对路径
- example_cmd: 可直接执行的blade-ai inject命令

只输出JSON数组，无其他内容。"""


def _scan_skill_structure(skill_dir: Path) -> str:
    """Scan skill directory and produce a concise text summary for LLM.

    Lists catalogue subdirectories and their .md files, so the LLM doesn't
    have to guess — it only needs to format the information.
    """
    lines: list[str] = []
    catalogue_dir = skill_dir / "references" / "catalogue"
    if catalogue_dir.exists():
        for cat_dir in sorted(catalogue_dir.iterdir()):
            if not cat_dir.is_dir():
                continue
            md_files = sorted(f.name for f in cat_dir.glob("*.md"))
            lines.append(f"{cat_dir.name}/")
            for mf in md_files:
                # Remove .md suffix and category prefix to get root cause
                stem = mf[:-3] if mf.endswith(".md") else mf
                prefix = cat_dir.name + "_"
                root_cause = stem[len(prefix):] if stem.startswith(prefix) else stem
                lines.append(f"  - {mf}  (根因: {root_cause})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_skill_catalog(
    skill_name: str,
    skill_content: str,
    skill_dir: Optional[Path],
    llm,
    work_dir: Path,
    *,
    no_cache: bool = False,
) -> list[dict]:
    """Generate a list of injectable fault scenarios for a skill.

    Strategy:
    1. If skill_dir has references/catalogue/ — generate from directory structure
       directly (no LLM needed, fast & deterministic).
    2. Otherwise — use LLM to analyze SKILL.md content.

    Results are cached to disk, keyed by directory fingerprint.
    Set no_cache=True to force regeneration.
    """

    cache_file = _cache_path(work_dir)
    # Use directory fingerprint (covers ALL files) when available,
    # otherwise fall back to content fingerprint
    if skill_dir and skill_dir.exists():
        fp = _dir_fingerprint(skill_dir)
    else:
        fp = _content_fingerprint(skill_content)

    # Try cache first
    if not no_cache:
        cache = _load_cache(cache_file)
        entry = cache.get(skill_name)
        if entry and entry.get("fingerprint") == fp:
            use_cases = entry.get("use_cases", [])
            logger.debug(f"Skill catalog cache hit for '{skill_name}' ({len(use_cases)} use cases)")
            return use_cases

    # Cache miss or no_cache — regenerate
    logger.info(f"Generating skill catalog for '{skill_name}'...")
    try:
        # Strategy 1: catalogue directory exists → generate from structure
        catalogue_dir = (skill_dir / "references" / "catalogue") if skill_dir else None
        if catalogue_dir and catalogue_dir.exists():
            use_cases = _generate_from_catalogue(catalogue_dir, skill_name)
        else:
            # Strategy 2: no catalogue → use LLM
            use_cases = await _generate_via_llm(skill_name, skill_content, skill_dir, llm)

        if use_cases is None:
            logger.warning(f"Failed to generate catalog for skill '{skill_name}'")
            return []

        # Save to cache
        cache = _load_cache(cache_file)
        cache[skill_name] = {
            "fingerprint": fp,
            "use_cases": use_cases,
        }
        _save_cache(cache_file, cache)
        logger.info(f"Skill catalog generated for '{skill_name}': {len(use_cases)} use cases")

        return use_cases

    except Exception as e:
        logger.error(f"Failed to generate skill catalog for '{skill_name}': {e}")
        return []


def _parse_llm_json(raw_text: str) -> Optional[list[dict]]:
    """Parse the LLM response into a list of use-case dicts.

    Handles cases where the LLM wraps JSON in markdown code blocks.
    """
    # Strip markdown code block wrapper if present
    text = raw_text.strip()
    if text.startswith("```"):
        # Remove opening ```json or ```
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
        # Remove closing ```
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array in the text
        import re
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        else:
            return None

    if not isinstance(parsed, list):
        return None

    # Validate and normalize each entry
    result = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        result.append({
            "category": item.get("category", ""),
            "use_case_name": item.get("use_case_name", ""),
            "fault_symptom": item.get("fault_symptom", ""),
            "resource_path": item.get("resource_path", ""),
            "example_cmd": item.get("example_cmd", ""),
            "example_cmd_direct": item.get("example_cmd_direct", ""),
        })

    return result


def infer_scope(category: str) -> str:
    """Infer blade scope from catalogue directory name."""
    ft = extract_fault_type(category)
    if ft in ("Node", "Pod", "Service", "Workload"):
        return ft.lower()
    cat_lower = category.lower()
    if "dns" in cat_lower:
        return "pod"
    return "pod"


def infer_blade_params(category: str, *, scope: str | None = None) -> dict | None:
    """Try to infer scope/target/action from category name.

    Returns ``{"scope": ..., "target": ..., "action": ...}`` or ``None``
    when the category is a symptom without a clear blade mapping
    (e.g. Pod_Pending, Pod_镜像拉取失败).

    Pass a pre-computed *scope* to skip the redundant ``infer_scope`` call.
    """
    if scope is None:
        scope = infer_scope(category)
    cat_lower = category.lower()

    target: str | None = None
    if any(k in cat_lower for k in ("cpu", "throttling")):
        target = "cpu"
    elif any(k in cat_lower for k in ("内存", "oom", "mem")):
        target = "mem"
    elif any(k in cat_lower for k in ("磁盘", "disk", "云盘")):
        target = "disk"
    elif any(k in cat_lower for k in ("网络", "network", "丢包", "dns")):
        target = "network"

    if not target:
        return None

    action: str | None = None
    if target == "cpu":
        action = "fullload"
    elif target == "mem":
        action = "load"
    elif target == "disk":
        action = "burn" if any(k in cat_lower for k in ("io", "读写")) else "fill"
    elif target == "network":
        if "dns" in cat_lower:
            action = "dns"
        elif any(k in cat_lower for k in ("丢包", "drop")):
            action = "drop"
        else:
            action = "delay"

    if not action:
        return None
    return {"scope": scope, "target": target, "action": action}


def build_nl_cmd(display: str, category: str, scope: str) -> str:
    """Build a scope-aware natural-language example command."""
    desc = f'{display}导致{category.replace("_", " ")}故障'
    if scope == "node":
        return (
            f'blade-ai inject -i "帮我注入{desc}，'
            f'目标为<node-name>，'
            f'kubeconfig路径为<kubeconfig>"'
        )
    return (
        f'blade-ai inject -i "帮我注入{desc}，'
        f'命名空间为<namespace>，目标为<name>，'
        f'kubeconfig路径为<kubeconfig>"'
    )


def build_direct_cmd(params: dict) -> str:
    """Build a structured example command from inferred blade params."""
    scope = params["scope"]
    name_ph = "<node-name>" if scope == "node" else "<name>"
    parts = [
        "blade-ai inject",
        f"--scope {scope}",
        f"--target {params['target']}",
        f"--action {params['action']}",
        f"-n {name_ph}",
    ]
    if scope != "node":
        parts.append("--namespace <namespace>")
    parts.append("--kubeconfig <kubeconfig>")
    return " ".join(parts)


def _generate_from_catalogue(catalogue_dir: Path, skill_name: str) -> Optional[list[dict]]:
    """Generate use-case list directly from catalogue directory structure.

    No LLM needed — deterministic and instant.
    """
    use_cases: list[dict] = []
    for cat_dir in sorted(catalogue_dir.iterdir()):
        if not cat_dir.is_dir():
            continue
        category = cat_dir.name
        for md_file in sorted(cat_dir.glob("*.md")):
            # Extract root cause from filename: {category}_{root_cause}.md
            stem = md_file.stem  # e.g. Pod_Pending_节点资源不足
            prefix = category + "_"
            root_cause = stem[len(prefix):] if stem.startswith(prefix) else stem

            use_case_name = f"{root_cause} 导致 {category}"
            resource_path = f"references/catalogue/{category}/{md_file.name}"

            # Try to read fault_symptom from the .md file
            fault_symptom = _extract_fault_symptom(md_file)

            # Build example commands (scope-aware)
            display = root_cause.replace("_", " ")
            scope = infer_scope(category)
            example_cmd = build_nl_cmd(display, category, scope)

            blade_params = infer_blade_params(category, scope=scope)
            example_cmd_direct = build_direct_cmd(blade_params) if blade_params else ""

            use_cases.append({
                "category": category,
                "use_case_name": use_case_name,
                "fault_symptom": fault_symptom,
                "resource_path": resource_path,
                "example_cmd": example_cmd,
                "example_cmd_direct": example_cmd_direct,
            })

    return use_cases if use_cases else None


def _extract_fault_symptom(md_file: Path) -> str:
    """Try to extract the first fault symptom line from a catalogue .md file."""
    try:
        content = md_file.read_text(encoding="utf-8")
        # Common patterns: **故障现象**：\n1. xxx  or  **故障现象** xxx
        import re
        m = re.search(r"\*\*故障现象\*\*[：:]\s*\n1\.\s*(.+)", content)
        if m:
            return m.group(1).strip()
        m = re.search(r"\*\*故障现象\*\*[：:]\s*(.+)", content)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


async def _generate_via_llm(
    skill_name: str,
    skill_content: str,
    skill_dir: Optional[Path],
    llm,
) -> Optional[list[dict]]:
    """Generate use-case list via LLM when no catalogue directory exists."""
    from langchain_core.messages import SystemMessage, HumanMessage

    human_content = f"## Skill: {skill_name}\n\n{skill_content[:4000]}"
    messages = [
        SystemMessage(content=_CATALOG_SYSTEM_PROMPT),
        HumanMessage(content=human_content),
    ]

    response = await llm.ainvoke(messages)
    # Log reasoning_content in debug mode (enable_thinking)
    additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
    reasoning_content = additional_kwargs.get("reasoning_content", "")
    if reasoning_content and settings.is_debug:
        text = reasoning_content[:300] + ("..." if len(reasoning_content) > 300 else "")
        logger.debug(f"💭 catalog thinking: {text}")
    raw_text = response.content or ""
    return _parse_llm_json(raw_text)
