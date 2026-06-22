"""Sync skill capabilities: LLM generates commands for each catalogue case.

`sync_capabilities` is the main entry point called by `blade-ai capabilities sync`.
It probes blade -h for real capabilities, then asks an LLM to produce three
command variants (nl_cmd, structured_cmd, direct_cmd) for each skill case,
using the blade capabilities as a generation reference.

The output is a single JSON file at ~/.blade-ai/memory/skill_capabilities.json
that `blade-ai list` reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from chaos_agent.skills.capability_probe import _INFRA_FLAGS, probe_capabilities
from chaos_agent.skills.loader import load_skill_metadata
from chaos_agent.skills.models import SKILL_TYPE_FAULT_INJECTION

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are a blade-ai capability generator. Given a fault drill use-case .md file, produce three injection command variants.

## blade-ai real capabilities reference (from blade v{blade_version} -h — ONLY use scope-target/action/flag listed here)

### Valid resources and actions
{resources_text}

### Valid experiment flags per action
{flags_text}

### Official command examples
{examples_text}

## Generation rules

For the given use-case .md, output JSON:
```json
{{
  "inject_kind": "blade" | "kubectl" | "mixed",
  "fault_symptom": "one-line fault symptom description",
  "nl_cmd": "blade-ai inject -i \"<natural language description with <namespace>, <name>, <kubeconfig> placeholders>\"",
  "structured_cmd": "blade-ai inject --scope <s> --target <t> --action <a> [--labels app=<app>|-n <node>] [--namespace <ns>] --params <k=v,...> --kubeconfig <kubeconfig>",
  "direct_cmd": "blade-ai inject --direct --scope <s> --target <t> --action <a> ... --params ... --kubeconfig <kubeconfig>",
  "direct_hint": ""
}}
```

### Three command types
1. **nl_cmd** (always generate): Natural language intent `blade-ai inject -i "..."`, the LLM reads docs and executes
2. **structured_cmd** (always generate): Structured params (no --direct), LLM executes based on spec + docs (more precise than NL)
3. **direct_cmd** (blade primitives only): With --direct, deterministic injection skipping LLM; leave empty for control-plane cases

### inject_kind classification
- **blade**: The use-case's real injection method is a blade resource stress primitive (cpu/mem/disk/network/process)
- **kubectl**: The use-case's real injection method is a kubectl control-plane operation (PVC/Taint/probe/image/finalizer/scale/cordon etc, no blade primitive)
- **mixed**: Requires blade injection + kubectl cooperation to manifest symptoms (e.g., kubectl delete pod first, then blade network drop)

### structured_cmd rules
- Format: `blade-ai inject --scope <s> --target <t> --action <a> <SEL> [--namespace <ns>] --params <P> --kubeconfig <kubeconfig>`
- <SEL>: node scope uses `-n <node>`; pod/container uses `--labels app=<app>`
- node scope omits --namespace; pod/container includes --namespace <ns>
- --params: comma-separated k=v pairs; boolean flags (read/write etc) as bare keys. Example: `path=/data,read,write`
- scope/target/action **MUST be selected from the reference table above**
- **inject_kind=blade or mixed**: generate structured_cmd with valid blade scope/target/action
- **inject_kind=kubectl**: structured_cmd="" (these faults use kubectl operations, no blade command exists)

### direct_cmd rules (blade primitives only)
- Same format as structured_cmd but with `--direct` prepended
- **Only generate when inject_kind=blade or the blade part of mixed**
- Every flag in --params **MUST exist in the reference table's valid experiment flags for that action** — do NOT use abbreviations (e.g., use `network-traffic=out` not bare `out`)
- Parameter values: use specific values from the .md when available (e.g., path=/var/lib/containerd); otherwise use placeholders <...> or safe defaults (cpu-percent=80, mem-percent=90, percent=90, path=/data etc)
- For control-plane cases (inject_kind=kubectl): direct_cmd="" and direct_hint="Control-plane fault (kubectl scale/patch), no blade direct command available. Use nl_cmd or structured_cmd instead."

### mixed handling
- structured_cmd: full blade structured params
- direct_cmd: blade part only (if the blade part can independently inject)
- direct_hint: describe prerequisite/postrequisite kubectl commands (e.g., "Requires: kubectl delete pod <pod> -n <ns> before injection")

Output JSON only, no other content."""


def _build_system_prompt(caps: dict) -> str:
    """Build system prompt with real blade capabilities injected."""
    # Resources + actions
    lines = []
    for res, actions in sorted(caps.get("resources", {}).items()):
        lines.append(f"- {res}: {', '.join(actions)}")
    resources_text = "\n".join(lines)

    # Flags per action (with descriptions so LLM understands semantics)
    flag_lines = []
    for key, flag_list in sorted(caps.get("flags", {}).items()):
        parts = [f"--{f['name']} ({f['desc']})" if isinstance(f, dict) else f"--{f}" for f in flag_list]
        flag_lines.append(f"- {key}: {', '.join(parts)}")
    flags_text = "\n".join(flag_lines)

    # Examples (first example per action, truncated)
    ex_lines = []
    for key, exs in sorted(caps.get("examples", {}).items()):
        if exs:
            ex_lines.append(f"- {key}: `{exs[0][:120]}`")
    examples_text = "\n".join(ex_lines)

    return _SYSTEM_PROMPT_TEMPLATE.format(
        blade_version=caps.get("blade_version", "unknown"),
        resources_text=resources_text,
        flags_text=flags_text,
        examples_text=examples_text,
    )


# ---------------------------------------------------------------------------
# Self-check: validate direct_cmd against capabilities
# ---------------------------------------------------------------------------

_PARAMS_RE = re.compile(r"--params\s+(\S+)")
_SCOPE_RE = re.compile(r"--scope\s+(\S+)")
_TARGET_RE = re.compile(r"--target\s+(\S+)")
_ACTION_RE = re.compile(r"--action\s+(\S+)")


def _self_check_direct(direct_cmd: str, caps: dict) -> list[str]:
    """Check direct_cmd's action/flags against blade capabilities.

    Returns list of error strings (empty = valid).
    """
    if not direct_cmd:
        return []
    errs = []
    scope_m = _SCOPE_RE.search(direct_cmd)
    target_m = _TARGET_RE.search(direct_cmd)
    action_m = _ACTION_RE.search(direct_cmd)
    scope = scope_m.group(1) if scope_m else ""
    target = target_m.group(1) if target_m else ""
    action = action_m.group(1) if action_m else ""
    if not all([scope, target, action]):
        errs.append("无法解析 scope/target/action")
        return errs

    res = f"{scope}-{target}"
    legal_actions = caps.get("resources", {}).get(res, [])
    if action not in legal_actions:
        errs.append(f"{res} 无 action '{action}' (合法: {legal_actions})")

    # Check params flags (skip infra/global flags like timeout/namespace — they're always valid)
    m = _PARAMS_RE.search(direct_cmd)
    if m:
        params_str = m.group(1)
        raw_flags = caps.get("flags", {}).get(f"{res} {action}", [])
        legal_flags = {f["name"] if isinstance(f, dict) else f for f in raw_flags}
        for part in params_str.split(","):
            flag_name = part.split("=")[0].strip()
            if flag_name and flag_name not in legal_flags and flag_name not in _INFRA_FLAGS:
                errs.append(f"{res} {action} 无 flag '--{flag_name}' (合法: {sorted(legal_flags)})")
    return errs


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _parse_llm_json(content: str) -> dict:
    """Parse JSON from LLM response text (handles markdown code blocks)."""
    text = content.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


async def _llm_invoke(llm, messages: list) -> tuple[dict, str]:
    """Invoke LLM with messages, return (parsed_dict, raw_content)."""
    response = await llm.ainvoke(messages)
    content = response.content if isinstance(response.content, str) else str(response.content)
    if not content.strip():
        rc = getattr(response, "additional_kwargs", {}).get("reasoning_content", "")
        if rc:
            content = rc
    return _parse_llm_json(content), content


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

async def sync_capabilities(
    blade_path: str,
    catalogue_root: Path,
    llm,
    output_path: Path,
) -> dict:
    """Sync: probe blade + LLM generate commands for each case → write JSON.

    Args:
        blade_path: Path to blade binary
        catalogue_root: Path to skills/.../references/catalogue/
        llm: LangChain LLM instance (from make_llm)
        output_path: Where to write the result JSON

    Returns:
        The full catalog dict (also written to output_path)
    """
    # Skill type filter: capabilities-sync only handles fault-injection skills.
    # catalogue_root points at <skill_dir>/references/catalogue/, so the skill
    # dir is two levels up. Non-fault-injection skills (e.g. tool-use type)
    # have no blade injection commands to generate, so we skip early.
    skill_dir = catalogue_root.parent.parent
    try:
        skill_meta = load_skill_metadata(skill_dir)
    except Exception as e:
        logger.warning("Failed to load skill metadata for %s: %s", skill_dir, e)
        skill_meta = None
    if skill_meta and skill_meta.skill_type != SKILL_TYPE_FAULT_INJECTION:
        logger.info(
            "Skipping capabilities-sync for skill '%s' (skill_type=%s, not fault-injection)",
            skill_meta.name, skill_meta.skill_type,
        )
        empty_catalog = {
            "blade_version": "",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": 0,
            "cases": [],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(empty_catalog, f, ensure_ascii=False, indent=2)
        return empty_catalog

    logger.info("Probing blade capabilities (help-only)...")
    caps = probe_capabilities(blade_path)
    logger.info(
        "Blade %s: %d resources, %d action-flag entries",
        caps["blade_version"],
        len(caps["resources"]),
        len(caps["flags"]),
    )

    system_prompt = _build_system_prompt(caps)

    # Collect all tasks (metadata + md content)
    tasks: list[dict] = []
    for cat_dir in sorted(p for p in catalogue_root.iterdir() if p.is_dir()):
        category = cat_dir.name
        for md_file in sorted(cat_dir.glob("*.md")):
            stem = md_file.stem
            prefix = category + "_"
            root_cause = stem[len(prefix):] if stem.startswith(prefix) else stem
            tasks.append({
                "category": category,
                "use_case_name": f"{root_cause} 导致 {category}",
                "resource_path": str(md_file.relative_to(catalogue_root.parent.parent)),
                "stem": stem,
                "md_content": md_file.read_text(encoding="utf-8"),
            })

    CONCURRENCY = 5

    completed_count = 0
    total_count = len(tasks)

    _KIND_ICON = {"blade": "B", "kubectl": "K", "mixed": "M", "unknown": "?"}

    def _print_progress(done: int, total: int, name: str, kind: str, status: str) -> None:
        bar_width = 20
        filled = int(bar_width * done / total) if total else bar_width
        bar = "#" * filled + "-" * (bar_width - filled)
        icon = _KIND_ICON.get(kind, "?")
        if status == "ok":
            mark = f"[{icon}]"
        elif status == "check-fail":
            mark = f"[{icon}!]"
        else:
            mark = "[X]"
        pct = int(100 * done / total) if total else 100
        sys.stderr.write(f"\r  [{bar}] {pct:3d}% ({done}/{total}) {mark} {name[:45]}\033[K\n")
        sys.stderr.flush()

    MAX_RETRIES = 3

    async def _process_one(task_info: dict) -> dict:
        nonlocal completed_count
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        stem = task_info["stem"]
        logger.info("  Processing: %s", task_info["use_case_name"])

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=task_info["md_content"]),
        ]

        result = {}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result, raw = await _llm_invoke(llm, messages)
            except Exception as e:
                logger.error("    LLM failed for %s (attempt %d): %s", stem, attempt, e)
                result = {}
                break

            if not result:
                break

            direct_cmd = result.get("direct_cmd", "")
            if not direct_cmd:
                break

            errs = _self_check_direct(direct_cmd, caps)
            if not errs:
                break

            if attempt < MAX_RETRIES:
                logger.info("    Retry %d/%d for %s: %s", attempt, MAX_RETRIES, stem, "; ".join(errs))
                messages.append(AIMessage(content=raw))
                messages.append(HumanMessage(
                    content=(
                        f"Your direct_cmd failed validation. Please fix and output the full JSON again.\n"
                        f"Errors: {'; '.join(errs)}\n"
                        f"Reminder: every flag in --params MUST be a valid experiment flag for that action "
                        f"as listed in the reference table. Do NOT use abbreviations "
                        f"(e.g., use network-traffic=out instead of bare out)."
                    )
                ))
            else:
                logger.warning("    Self-check failed after %d attempts for %s: %s", MAX_RETRIES, stem, "; ".join(errs))

        if not result:
            completed_count += 1
            _print_progress(completed_count, total_count, task_info["use_case_name"], "unknown", "FAILED")
            return {
                "category": task_info["category"],
                "use_case_name": task_info["use_case_name"],
                "resource_path": task_info["resource_path"],
                "fault_symptom": "",
                "inject_kind": "unknown",
                "nl_cmd": "",
                "structured_cmd": "",
                "direct_cmd": "",
                "direct_hint": "LLM generation failed. Re-run blade-ai capabilities-sync.",
                "_self_check_fail": False,
            }

        direct_cmd = result.get("direct_cmd", "")
        structured_cmd = result.get("structured_cmd", "")
        direct_hint = result.get("direct_hint", "")
        self_check_fail = False

        # Validate structured_cmd: for kubectl-only cases it should be empty;
        # for blade/mixed cases it must pass the same self-check as direct_cmd.
        inject_kind = result.get("inject_kind", "unknown")
        if inject_kind == "kubectl" and structured_cmd:
            logger.info("    Clearing invalid structured_cmd for kubectl case: %s", stem)
            structured_cmd = ""
        elif structured_cmd:
            s_errs = _self_check_direct(structured_cmd, caps)
            if s_errs:
                logger.warning("    structured_cmd self-check failed for %s: %s", stem, "; ".join(s_errs))
                structured_cmd = ""

        if direct_cmd:
            errs = _self_check_direct(direct_cmd, caps)
            if errs:
                direct_hint = f"自检未通过({'; '.join(errs)})，已清空 direct_cmd；请用 structured_cmd 或 nl_cmd"
                direct_cmd = ""
                self_check_fail = True

        completed_count += 1
        status = "check-fail" if self_check_fail else "ok"
        _print_progress(completed_count, total_count, task_info["use_case_name"], inject_kind, status)

        return {
            "category": task_info["category"],
            "use_case_name": task_info["use_case_name"],
            "resource_path": task_info["resource_path"],
            "fault_symptom": result.get("fault_symptom", ""),
            "inject_kind": inject_kind,
            "nl_cmd": result.get("nl_cmd", ""),
            "structured_cmd": structured_cmd,
            "direct_cmd": direct_cmd,
            "direct_hint": direct_hint,
            "_self_check_fail": self_check_fail,
        }

    sem = asyncio.Semaphore(CONCURRENCY)

    async def _bounded(t):
        async with sem:
            return await _process_one(t)

    raw_results = await asyncio.gather(*[_bounded(t) for t in tasks], return_exceptions=True)
    cases = [r for r in raw_results if isinstance(r, dict)]

    # Stats (computed after gather, no concurrency issue)
    stats: dict[str, int] = {}
    for c in cases:
        kind = c.get("inject_kind", "unknown")
        stats[kind] = stats.get(kind, 0) + 1
        if c.pop("_self_check_fail", False):
            stats["self_check_fail"] = stats.get("self_check_fail", 0) + 1

    catalog = {
        "blade_version": caps["blade_version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(cases),
        "cases": cases,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(output_path.parent), suffix=".json.tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(output_path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    logger.info(
        "Sync complete: %d cases (blade=%d, kubectl=%d, mixed=%d, self_check_fail=%d)",
        len(cases), stats.get("blade", 0), stats.get("kubectl", 0),
        stats.get("mixed", 0), stats.get("self_check_fail", 0),
    )
    return catalog
