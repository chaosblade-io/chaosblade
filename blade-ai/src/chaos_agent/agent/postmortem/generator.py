"""LLM-driven postmortem markdown generation.

Single async function: ``generate_postmortem(context, llm, *, timeout)``.
Returns markdown body (without writing to disk — store.py handles that).

The prompt deliberately constrains the LLM to FOUR markdown constructs
(## headings, - lists, **bold**, `inline code`) so the lightweight TUI
renderer can cover the full output without ink-markdown / mdast deps.
LLM-side compliance is hint-not-enforce; the renderer falls back to
plain text if the LLM smuggles in a table or link.

Output integrity:
- Empty / no-summary outputs are rejected (returned as ``""``) so the
  caller can skip the save+attach path and avoid surfacing junk to
  the user.
- Outputs above ``MAX_MARKDOWN_BYTES`` are truncated to keep the SSE
  envelope + checkpoint footprint bounded.

Privacy boundary:
- The ``context`` dict passed to ``generate_postmortem`` contains
  ``fault_spec`` (namespace, pod names, params), ``user_input`` (the
  user's raw NL request), ``messages`` summaries (last N tool calls +
  AI replies), ``verification`` / ``side_effects`` / ``baseline_capture``.
- All of this is shipped to the configured LLM provider. When that
  provider is cloud-hosted (DashScope / OpenAI / Anthropic), the data
  LEAVES the local host.
- Operators handling sensitive production data should either:
    (a) set ``BLADE_AI_POSTMORTEM_ENABLED=false`` to opt out entirely,
    (b) configure a local LLM (Ollama, vLLM, etc.) so the data stays
        on-host, or
    (c) reduce ``BLADE_AI_POSTMORTEM_MAX_MESSAGES`` to shrink the
        upload surface.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Upper bound on the markdown body the LLM may return. Above this we
# truncate with a marker — a sane LLM postmortem is 1-3 KB; 50 KB is a
# 10-25× safety margin that bounds: (a) SSE envelope size, (b)
# LangGraph state checkpoint per-task overhead, (c) TUI re-render cost.
MAX_MARKDOWN_BYTES: int = 50_000

# Minimum structural requirement for accepting an LLM output as a real
# postmortem. The prompt asks for a "## Summary" section first; if the
# LLM refused or hallucinated unrelated content, this gate filters it
# out cleanly so the caller can degrade to postmortem=None.
_REQUIRED_HEADING: str = "## Summary"


_SYSTEM_PROMPT = """You are an SRE writing a postmortem for a chaos engineering experiment.

Output ONLY a markdown document. Use these constructs and NOTHING else:
  - `## Heading` and `### Subheading`  (no `#` top-level — caller adds it)
  - `- list item`                       (no nested lists)
  - `**bold**` for emphasis
  - `` `inline code` `` for identifiers / commands

DO NOT use:
  - Tables (| col | col |)
  - Links ([text](url))
  - Blockquotes (>)
  - Code blocks (```)
  - Images, footnotes, HTML

Required sections, in this exact order:
  ## Summary
  ## Background
  ## Timeline
  ## Key Metrics
  ## Verifier Findings
  ## Side Effects
  ## Root Cause Hypothesis
  ## Recommendations

Section guidance:
- Summary: 1-2 sentences, the headline of what happened.
- Background: user request, fault type, target, parameters.
- Timeline: bullet list of key moments. Each message in the context
  has a `time` field (Beijing time HH:MM:SS). Use these real timestamps
  in the output (e.g. "- **20:17:34** blade create 发起注入").
- Key Metrics: before/after numbers from baseline_capture. Skip the
  section entirely if no baseline data.
- Verifier Findings: layer 1 + layer 2 verdicts, safety_score four
  dimensions, what was checked vs what passed.
- Side Effects: compare pre_snapshot counts vs side_effects diff.
  Highlight evicted pods, OOMKilled siblings, HPA scaling, container
  restarts.
- Root Cause Hypothesis: your interpretation of WHY the experiment
  reached its outcome. For failed runs, be specific — name the
  failure_detail.category and infer mechanism.
- Recommendations: 2-4 actionable items. Concrete commands or config
  changes, not platitudes.

Tone: factual, terse. No hedging. No marketing language. Treat the
reader as a senior engineer who wants signal, not narrative.

Write in Chinese unless the input is predominantly English."""


_USER_PROMPT_TEMPLATE = """Generate the postmortem markdown for the experiment below.

Context (JSON):
{context_json}

Output the markdown directly, starting with `## Summary`. Do NOT wrap
in code fences. Do NOT include a top-level `#` heading — the file
header is added by the caller."""


async def generate_postmortem(
    context: dict[str, Any],
    llm,
    *,
    timeout: int = 30,
) -> str:
    """Invoke the LLM to produce a postmortem markdown body.

    Raises asyncio.TimeoutError when LLM exceeds ``timeout`` seconds.
    Caller (save_memory) catches this + any LLM error and degrades to
    postmortem=None so the result envelope still ships.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    context_json = json.dumps(context, ensure_ascii=False, indent=2, default=str)
    user_msg = _USER_PROMPT_TEMPLATE.format(context_json=context_json)

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_msg),
    ]

    async def _invoke() -> str:
        response = await llm.ainvoke(messages)
        content = getattr(response, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)
        return content.strip()

    raw = await asyncio.wait_for(_invoke(), timeout=timeout)
    return _validate_and_bound(raw)


def _validate_and_bound(markdown: str) -> str:
    """Reject LLM refusals / malformed output; cap oversized output.

    Returns ``""`` when the LLM produced something we can't surface as
    a postmortem (caller treats this as "skip"). Returns truncated
    body when the LLM ran long.
    """
    if not markdown:
        return ""

    # Structural sanity: must contain at least the Summary heading.
    # Catches refusals like "I'm sorry, I can't generate..." that pass
    # the non-empty check but aren't real postmortems.
    if _REQUIRED_HEADING not in markdown:
        logger.warning(
            "Postmortem rejected: missing required heading %r (got %d chars)",
            _REQUIRED_HEADING, len(markdown),
        )
        return ""

    # Size cap — truncate at byte boundary to keep envelopes / checkpoints
    # bounded even if the LLM ignores the prompt's brevity guidance.
    body_bytes = markdown.encode("utf-8", errors="replace")
    if len(body_bytes) > MAX_MARKDOWN_BYTES:
        logger.warning(
            "Postmortem oversized: %d bytes > %d cap; truncating",
            len(body_bytes), MAX_MARKDOWN_BYTES,
        )
        truncated_bytes = body_bytes[:MAX_MARKDOWN_BYTES]
        # Decode with errors='ignore' to drop a partial multi-byte
        # sequence at the truncation boundary.
        truncated = truncated_bytes.decode("utf-8", errors="ignore")
        # Append a clear marker so the user knows content was cut.
        marker = (
            f"\n\n---\n"
            f"⚠️ Postmortem truncated at {MAX_MARKDOWN_BYTES // 1024}KB. "
            f"Original was {len(body_bytes) // 1024}KB."
        )
        return truncated + marker

    return markdown


def make_summary(markdown_body: str) -> str:
    """Extract the first paragraph after ## Summary as a 1-line preview.

    Used in result envelope (data.postmortem.summary) so the TUI can
    show a folded one-liner without re-parsing the whole body. Returns
    empty string when no Summary section found.
    """
    lines = markdown_body.splitlines()
    in_summary = False
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            if not in_summary and heading.startswith("summary"):
                in_summary = True
                continue
            if in_summary:
                break  # next section — stop
            continue
        if in_summary and stripped:
            collected.append(stripped)
            if len(" ".join(collected)) > 200:
                break

    summary = " ".join(collected)
    return summary[:200] + ("..." if len(summary) > 200 else "")
