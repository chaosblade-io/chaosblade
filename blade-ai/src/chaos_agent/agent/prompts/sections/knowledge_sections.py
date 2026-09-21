"""Knowledge sections: the on-demand summary index."""

import re


def get_skill_index_section(skill_catalog: str) -> str:
    """T0 skill discovery index — name + full description for accurate selection.

    Parses the catalog string produced by ``build_catalog_prompt()``
    (format: ``- name: description`` per line). Full descriptions are
    kept to preserve coverage keywords (fault levels, trigger words)
    that LLMs need for accurate skill-to-intent matching — truncating
    to first sentence risks selection failure.
    """
    if not skill_catalog:
        return "## Skill Index\n\nNo skills available."

    # build_catalog_prompt() produces "- name: description" entries.
    # Descriptions can span multiple lines (YAML block scalar with |),
    # so we parse entry-by-entry rather than line-by-line.
    #
    # Previous regex r'(?:^#{1,3}\s+|\*\*)(\w[\w-]*)' could not parse
    # the "- name: description" format and always fell back to raw
    # catalog text (no header, no guide message), making the P2
    # compact-index feature ineffective.
    entries: list[str] = []
    current_name: str | None = None
    current_desc_parts: list[str] = []

    for line in skill_catalog.splitlines():
        # Detect new entry: "- name: rest"
        m = re.match(r'^-\s+(\S+):\s*(.*)', line)
        if m:
            # Flush previous entry if exists
            if current_name is not None:
                desc = "\n".join(current_desc_parts).strip()
                entries.append(f"- `{current_name}`: {desc}")
            current_name = m.group(1)
            current_desc_parts = [m.group(2)] if m.group(2) else []
            continue
        # Continuation line for current entry's description
        if current_name is not None:
            stripped = line.strip()
            if stripped:
                current_desc_parts.append(stripped)

    # Flush last entry
    if current_name is not None:
        desc = "\n".join(current_desc_parts).strip()
        entries.append(f"- `{current_name}`: {desc}")

    # Fallback: if no entries were parsed (catalog format not recognized),
    # include the raw catalog text verbatim — better than showing an empty
    # index. This handles test fixtures and backward-compat inputs that
    # don't follow the "- name: description" format.
    if not entries:
        lines = ["## Skill Index", ""]
        lines.append(skill_catalog)
    else:
        lines = ["## Skill Index", ""]
        lines.extend(entries)

    lines.append("")
    lines.append("Call `activate_skill(skill_name)` to load full instructions and use-case resources.")
    return "\n".join(lines)


def get_knowledge_summary_section(phase: str | None = None) -> str:
    """Compact knowledge index — high-density reference table.

    Profile-agnostic: the registry list is a generic on-demand index, not a
    k8s-specific artifact; both profiles see the same index and load only the
    documents applicable to the current environment via read_knowledge_resource.

    ``phase`` ("plan" / "execute" / "verify" / "recover") filters the index
    down to the documents relevant to the current phase — each doc declares
    its phases in YAML frontmatter (``phases: [...]``); docs without the
    field stay visible everywhere. Filtering keeps phases from paying index
    tokens for docs they cannot use (planning traces in the verifier, etc.).

    Metadata is auto-discovered from YAML frontmatter in knowledge/*.md files.
    LLM can call ``read_knowledge_resource(filename, section)`` for full or
    section-level content on demand.

    Design rationale: shows ``summary`` (truncated) + all ``fault_types``
    (compressed) per document — 100% information density instead of the
    previous ``topics[0]; fault_types[0]`` format which lost 90% of
    coverage info and caused LLM to miss relevant documents.

    Budget: enforced via MAX_KNOWLEDGE_SUMMARY_BYTES from constants.py.
    """
    from chaos_agent.agent.prompts.constants import MAX_KNOWLEDGE_SUMMARY_BYTES
    from chaos_agent.agent.prompts.knowledge_registry import get_knowledge_registry

    registry = get_knowledge_registry()
    if phase:
        registry = [
            e for e in registry
            if e.get("phases") is None or phase in e["phases"]
        ]
    lines = [
        "## Domain Knowledge (on-demand)",
        f"{len(registry)} documents available. Call `read_knowledge_resource(filename='<name>', section='<heading>')` to load content.",
        "Use `section='outline'` to see headings with size hints (~Nc/~XKc) before loading — helps judge context cost.",
        "",
    ]

    MAX_SUMMARY_LEN = 80
    MAX_FAULT_TYPES_SHOWN = 8

    for entry in registry:
        # Use summary (truncated) instead of topics[0]
        summary_short = entry["summary"][:MAX_SUMMARY_LEN]
        if len(entry["summary"]) > MAX_SUMMARY_LEN:
            summary_short = summary_short.rstrip() + "…"

        # Compress fault_types: "all" stays as-is, others joined by |
        ft = entry["fault_types"]
        if ft == ["all fault types"] or ft == ["all"]:
            ft_display = "all"
        else:
            shown = ft[:MAX_FAULT_TYPES_SHOWN]
            ft_display = "|".join(shown)
            if len(ft) > MAX_FAULT_TYPES_SHOWN:
                ft_display += f"|+{len(ft) - MAX_FAULT_TYPES_SHOWN}"

        # Size hint: helps LLM judge context cost before loading full doc
        size_chars = entry["size_chars"]
        if size_chars >= 1000:
            size_hint = f"(~{size_chars / 1000:.1f}Kc)"
        else:
            size_hint = f"(~{size_chars}c)"

        lines.append(f"- `{entry['filename']}` — {summary_short}; {ft_display} {size_hint}")

    result = "\n".join(lines)

    # Enforce byte budget — truncate summary lines if over limit
    budget = MAX_KNOWLEDGE_SUMMARY_BYTES
    while len(result.encode("utf-8")) > budget and MAX_SUMMARY_LEN > 40:
        MAX_SUMMARY_LEN -= 10
        # Rebuild with shorter summaries
        new_lines = lines[:4]  # header lines
        for entry in registry:
            summary_short = entry["summary"][:MAX_SUMMARY_LEN]
            if len(entry["summary"]) > MAX_SUMMARY_LEN:
                summary_short = summary_short.rstrip() + "…"
            ft = entry["fault_types"]
            if ft == ["all fault types"] or ft == ["all"]:
                ft_display = "all"
            else:
                shown = ft[:MAX_FAULT_TYPES_SHOWN]
                ft_display = "|".join(shown)
                if len(ft) > MAX_FAULT_TYPES_SHOWN:
                    ft_display += f"|+{len(ft) - MAX_FAULT_TYPES_SHOWN}"
            size_chars = entry["size_chars"]
            if size_chars >= 1000:
                size_hint = f"(~{size_chars / 1000:.1f}Kc)"
            else:
                size_hint = f"(~{size_chars}c)"
            new_lines.append(f"- `{entry['filename']}` — {summary_short}; {ft_display} {size_hint}")
        result = "\n".join(new_lines)

    return result
