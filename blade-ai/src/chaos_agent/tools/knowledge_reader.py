"""Tool for reading knowledge documents on demand.

Provides the read_knowledge_resource LangChain tool that allows the LLM
to load full or section-level knowledge document content when needed,
instead of having all documents injected into the system prompt at once.
"""

from pathlib import Path

from langchain_core.tools import tool

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


def _get_allowed_files() -> set[str]:
    """Get allowed filenames from the auto-discovered registry (not hardcoded)."""
    from chaos_agent.utils.knowledge_registry import get_knowledge_registry
    return {entry["filename"] for entry in get_knowledge_registry()}


def _leading_hashes(line: str) -> int:
    """Count only leading # characters (heading level marker)."""
    count = 0
    for ch in line:
        if ch == "#":
            count += 1
        else:
            break
    return count


def _extract_section(content: str, section: str) -> str | None:
    """Extract content under a specific markdown heading.

    Matching is case-insensitive substring — ``section="Q9"`` matches
    ``"### Q9: 【安全红线】..."`` and ``section="安全红线"`` also
    matches it.

    Returns content from the matched heading to the next same-or-higher
    level heading (i.e. a ## section ends at the next ##, a ### ends at
    the next ### or ##).
    """
    lines = content.splitlines()
    target_level: int | None = None
    target_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        level = _leading_hashes(stripped)
        if level < 2:
            continue  # skip # (title) level headings

        heading_text = stripped[level:].strip()
        if section.lower() in heading_text.lower():
            target_level = level
            target_idx = i
            break

    if target_idx is None:
        return None

    # Collect lines until next heading at same or higher level
    result = [lines[target_idx]]
    for line in lines[target_idx + 1:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            level = _leading_hashes(stripped)
            if level <= target_level and level >= 2:
                break
        result.append(line)

    return "\n".join(result)


def _format_size(chars: int) -> str:
    """Format character count as a compact size hint for outline display."""
    if chars >= 1000:
        return f"(~{chars / 1000:.1f}Kc)"
    else:
        return f"(~{chars}c)"


def _calculate_section_sizes(content: str) -> list[tuple[str, int, int]]:
    """Calculate character count for each heading's section content.

    Uses the same boundary logic as _extract_section: a ## section ends
    at the next ##, a ### ends at the next ### or ##, etc.

    Returns list of (clean_heading_text, level, char_count) tuples.
    char_count includes the heading line itself.
    """
    lines = content.splitlines()
    # Collect all heading positions: (clean_text, level, line_index)
    heading_positions: list[tuple[str, int, int]] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        level = _leading_hashes(stripped)
        if level < 2:
            continue  # skip # (title) headings
        clean = stripped[level:].strip()
        heading_positions.append((clean, level, i))

    if not heading_positions:
        return []

    # Calculate size: section extends from heading line to next heading
    # at same or higher level (or end of file)
    total_lines = len(lines)
    sections: list[tuple[str, int, int]] = []

    for idx, (text, level, start_i) in enumerate(heading_positions):
        # Find end boundary — next heading at same or higher level
        end_i = total_lines  # default: end of file
        for next_idx in range(idx + 1, len(heading_positions)):
            _, next_level, next_i = heading_positions[next_idx]
            if next_level <= level:
                end_i = next_i
                break

        # Sum character counts of lines in this section
        section_chars = sum(len(lines[j]) for j in range(start_i, end_i))
        sections.append((text, level, section_chars))

    return sections


def _build_outline(content: str) -> str:
    """Build a compact headings outline with size hints and grouping detection.

    Each heading shows a size hint (``(~200c)`` or ``(~1.5Kc)``) so the
    LLM can judge context cost before loading. ## headings with ### children
    are annotated with ``[N subsections]`` to signal group structure.

    For documents with no ##/### headings, recommends loading the full
    document instead of making many small section calls.
    """
    section_sizes = _calculate_section_sizes(content)

    if not section_sizes:
        total_chars = len(content)
        size_str = _format_size(total_chars)
        return (
            "No section/subsection-level headings found.\n"
            f"Recommend: call without section to load full document ({size_str})."
        )

    lines = ["Available headings:"]
    for idx, (text, level, chars) in enumerate(section_sizes):
        size_str = _format_size(chars)
        indent = "  " * (level - 2)  # ## → 0, ### → 2, #### → 4

        # Grouping annotation on ## headings: count direct ### children
        group_info = ""
        if level == 2:
            sub_count = 0
            for next_idx in range(idx + 1, len(section_sizes)):
                _, next_level, _ = section_sizes[next_idx]
                if next_level <= 2:
                    break
                if next_level == 3:
                    sub_count += 1
            if sub_count > 0:
                group_info = f" [{sub_count} subsections]"

        lines.append(f"{indent}- {text} {size_str}{group_info}")

    return "\n".join(lines)


@tool
async def read_knowledge_resource(filename: str, section: str = "") -> str:
    """Phase 1 / Phase 2 read-only. Read a knowledge document by filename for domain expertise.

    When to use:
      - You need K8s syntax / JSONPath / verification heuristics / chaos
        safety background that is not already in the active skill case.
      - You hit unexpected verifier output and want a known-failure-mode
        catalogue.
      - Do NOT call when the active skill case already explains the
        specific task — skill content is canonical.

    Inputs:
      - filename: a name listed in the Domain Knowledge Index section of
        your system prompt. Pick the entry whose summary / fault_types
        match your scenario. Each entry shows ``(~XKc)`` size hint —
        for large documents, prefer ``section='outline'`` first rather
        than loading the full document.
      - section: optional heading filter to reduce context overhead.
        - "" (default): return the full document.
        - "outline": return headings list with size hints (~100 tokens).
          Each heading shows ``(~Nc)`` or ``(~XKc)`` so you can judge
          context cost before loading. ``##`` groups show ``[N subsections]``.
          Then call again with the specific heading name.
        - "Q9" / "安全红线" / "Pod CPU": return only content under the
          matching heading (case-insensitive substring match).
          Example: section="Q9" matches "### Q9: 【安全红线】...",
          section="Pod CPU" matches "### Pod CPU 验证...".

    Output: markdown content (full, section, or outline), or "Error:" if
            the filename is not in the registry / not on disk / section
            not found.

    Side effects: None (read-only).

    Constraints (MUST READ before calling):
      - Only filenames present in the auto-discovered registry are
        accepted; arbitrary paths are rejected for path-traversal safety.
      - When section is specified but not found, available headings are
        returned so you can retry with the correct heading name.
    """
    # Security: validate against auto-discovered registry (frontmatter-based)
    allowed_files = _get_allowed_files()
    if filename not in allowed_files:
        return f"Error: Unknown knowledge file '{filename}'. Available: {', '.join(sorted(allowed_files))}"

    filepath = KNOWLEDGE_DIR / filename
    # Path traversal protection
    try:
        if not filepath.resolve().is_relative_to(KNOWLEDGE_DIR.resolve()):
            return "Error: Invalid file path."
    except ValueError:
        return "Error: Invalid file path."

    if not filepath.is_file():
        return f"Error: Knowledge file '{filename}' not found on disk."

    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading knowledge file: {e}"

    # Handle section parameter
    if not section:
        return content

    if section.lower() == "outline":
        return _build_outline(content)

    # Try to extract the requested section
    extracted = _extract_section(content, section)
    if extracted is not None:
        return extracted

    # Section not found — return available headings as guidance
    outline = _build_outline(content)
    return (
        f"Section '{section}' not found in '{filename}'.\n\n"
        f"{outline}\n\n"
        f"Retry with one of the headings above, or call again without "
        f"`section` to load the full document."
    )