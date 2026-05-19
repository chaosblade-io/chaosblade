"""Domain-aware markdown rendering for chaos-eng output (PR-C3 / §9.6).

Rich's default ``Markdown`` is fine for prose but is a flat ``Code`` box
for everything inside a fence. Two of our highest-frequency content
shapes deserve better:

* **yaml / json fences** — agent plans and chaos-blade specs are dense
  yaml. ``rich.syntax.Syntax`` (``theme="monokai"``) gives us free
  key/value coloring; the default ``Code`` is monochrome and noisy.
* **``kubectl get …`` tabular output** — multi-column header rows like
  ``NAME  READY  STATUS  RESTARTS  AGE`` are *visually* a table but the
  default ``Code`` block leaves them as a single block of preformatted
  text. We auto-detect the header shape and re-emit as ``rich.table.Table``
  so the columns land on real grid cells (and survive narrow widths).

Inline ``blade <uid>`` and IP-port pairs also get a small
treatment: we wrap them in markdown backticks before handing the segment
to ``Markdown``. This routes them through the inline-code style which
already has a distinct background — enough visual weight to scan a
paragraph for "what's the experiment ID" without us having to subclass
``Markdown`` to add a custom highlighter (a much heavier change).

Anything that doesn't match any of the above falls through to the stock
``Markdown``, so we never make ordinary prose worse.
"""

from __future__ import annotations

import re

from rich.console import Group, RenderableType
from rich.highlighter import RegexHighlighter
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# Match a ```lang ... ``` fence. The ``lang`` group is optional (bare ``` is
# common in agent output). DOTALL so the body can span lines; non-greedy so
# adjacent fences don't merge into one match.
_FENCE_RE = re.compile(r"```([\w+-]*)\s*\n(.*?)```", re.DOTALL)

# A kubectl table header is the dead giveaway: 3+ ALL-CAPS tokens separated
# by 2+ spaces, no lowercase letters, optional dashes/underscores. This is
# specific enough that yaml / shell / log lines won't false-positive.
_KUBECTL_HEADER_RE = re.compile(
    r"^([A-Z][A-Z0-9_-]*)(\s{2,}[A-Z][A-Z0-9_-]*){2,}\s*$"
)

# chaosblade UIDs are 32 hex chars; we accept 24-40 to cover variants.
_BLADE_UID_RE = re.compile(r"(?<![\w`])([a-f0-9]{24,40})(?![\w`])")
# IPv4 with optional ``:port``. The negative-look avoids touching version
# strings like ``1.2.3.4.5``.
_IP_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?)(?![\d.])")

# Block-level markdown signals that warrant routing prose to the full
# Markdown class instead of plain Text+Highlighter — headings, lists,
# blockquotes, tables. If none of these are present we use Text so that
# UID / IP / namespace / fault_type get *real* theme colors instead of
# inline-code backgrounds (Markdown's inline render doesn't go through
# the console highlighter, so theme-color injection only works on Text).
_MD_BLOCK_RE = re.compile(
    r"(?m)^[ \t]{0,3}(#{1,6}\s|[*+-]\s|\d+\.\s|>\s|\|.*\|)"
)


class _ChaosInlineHighlighter(RegexHighlighter):
    """Apply theme colors to chaos-eng terms inside a ``Text`` span.

    Each named capture group corresponds to a Rich style registered on
    the ChaosConsole's theme (see ``console.py:_CHAOS_THEME``). The
    capture-group name *is* the style name — Rich's RegexHighlighter
    substitutes that lookup automatically.
    """

    base_style = ""
    highlights = [
        # blade UIDs — 24-40 hex chars, agent-purple
        r"(?P<chaos_uid>\b[a-f0-9]{24,40}\b)",
        # IPv4 (optionally with :port) — brand blue
        r"(?P<chaos_ip>\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b)",
        # ``namespace foo`` / ``ns kube-system`` — orange (scope)
        r"\b(?:namespace|ns)\s+(?P<chaos_ns>[a-z0-9][a-z0-9-]*)",
        # ``fault_type: cpu-fullload`` / ``fault-type=mem-leak`` — red (failure)
        r"\b(?:fault_type|fault-type|chaos_type)[\s:=]+(?P<chaos_ft>[\w-]+)",
    ]


_HIGHLIGHTER = _ChaosInlineHighlighter()


def render_markdown(content: str) -> RenderableType:
    """Render *content* with chaos-eng markdown customizations.

    Splits on ``` fences, dispatches each fence to the right renderer
    (yaml/json → ``Syntax``; kubectl-shaped → ``Table``; other → stock
    ``Code`` via ``Markdown``). Plain-text segments get inline UID/IP
    backticking before being handed to ``Markdown``.
    """
    if not content:
        return Text("")

    parts: list[RenderableType] = []
    last = 0
    for match in _FENCE_RE.finditer(content):
        if match.start() > last:
            parts.append(_render_prose(content[last : match.start()]))
        parts.append(_render_fence(match.group(1).lower(), match.group(2)))
        last = match.end()
    if last < len(content):
        parts.append(_render_prose(content[last:]))

    parts = [p for p in parts if p is not None]
    if not parts:
        return Text("")
    if len(parts) == 1:
        return parts[0]
    return Group(*parts)


def _render_prose(text: str) -> RenderableType | None:
    """Render a non-fence segment with chaos-term theme highlighting.

    Two paths:

    * **Pure prose** (no headings / lists / tables / blockquotes) → emit
      a ``Text`` and let ``_ChaosInlineHighlighter`` apply theme colors
      directly to UID / IP / namespace / fault_type spans. This is the
      common case for streaming LLM output and is the only way to get
      *theme* colors (Rich's Markdown class bypasses console highlighters
      on inline text segments).
    * **Block-level markdown** (headings, lists, etc.) → fall back to
      the stock ``Markdown`` to preserve structure. UID / IP get backtick
      wrapping so they at least pop visually via inline-code styling,
      but they won't carry the named theme color in this path.
    """
    if not text.strip():
        return None
    if _MD_BLOCK_RE.search(text):
        wrapped = _BLADE_UID_RE.sub(r"`\1`", text)
        wrapped = _IP_RE.sub(r"`\1`", wrapped)
        return Markdown(wrapped)
    rich_text = Text(text)
    _HIGHLIGHTER.highlight(rich_text)
    return rich_text


def _render_fence(lang: str, body: str) -> RenderableType:
    """Dispatch a fenced block to the most useful Rich renderable."""
    body = body.rstrip("\n")
    if lang in ("yaml", "yml"):
        return Syntax(body, "yaml", theme="monokai", line_numbers=False, word_wrap=True)
    if lang == "json":
        return Syntax(body, "json", theme="monokai", line_numbers=False, word_wrap=True)
    if not lang and _looks_like_kubectl(body):
        return _kubectl_to_table(body)
    if lang:
        # Honor any explicit lang we don't special-case (bash, python, …).
        return Syntax(body, lang, theme="monokai", line_numbers=False, word_wrap=True)
    # No lang and not kubectl-shaped — keep stock Markdown code styling.
    return Markdown(f"```\n{body}\n```")


def _looks_like_kubectl(body: str) -> bool:
    """Cheap structural test: first non-empty line matches the header shape."""
    for line in body.splitlines():
        if line.strip():
            return bool(_KUBECTL_HEADER_RE.match(line))
    return False


def _kubectl_to_table(body: str) -> Table:
    """Re-emit kubectl-style tabular text as ``rich.table.Table``.

    Column boundaries are read off the header row's whitespace runs —
    that's how kubectl itself formats output. Each subsequent row is
    sliced at those column starts and stripped, which tolerates rows
    where a value (e.g. STATUS=ContainerCreating) extends slightly into
    the next column's padding.
    """
    lines = [line for line in body.splitlines() if line.strip()]
    header = lines[0]

    cols: list[tuple[int, str]] = []
    in_word = False
    start = 0
    for i, ch in enumerate(header):
        if ch.isspace():
            if in_word:
                cols.append((start, header[start:i]))
                in_word = False
        elif not in_word:
            start = i
            in_word = True
    if in_word:
        cols.append((start, header[start:]))

    table = Table(
        show_edge=False,
        show_header=True,
        header_style="bold dim",
        padding=(0, 2),
        pad_edge=False,
    )
    for _, name in cols:
        table.add_column(name)

    for line in lines[1:]:
        row: list[str] = []
        for idx, (col_start, _) in enumerate(cols):
            col_end = cols[idx + 1][0] if idx + 1 < len(cols) else len(line)
            cell = line[col_start:col_end] if col_start < len(line) else ""
            row.append(cell.strip())
        table.add_row(*row)
    return table
