"""Typed, priority-aware prompt assembly.

Builders describe prompt material as segments instead of producing one large
string and slicing it.  Invariant/contract segments are never truncated;
context and optional material is omitted as whole segments when over budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

PromptPriority = Literal["invariant", "contract", "context", "optional"]


@dataclass(frozen=True)
class PromptSegment:
    name: str
    content: str
    priority: PromptPriority
    cacheable: bool
    source: str
    max_chars: int | None = None

    def render(self) -> str:
        """Return a complete segment; budget decisions happen at its boundary.

        ``max_chars`` is intentionally evaluated by :func:`assemble_prompt`.
        A raw substring is not a summary and can sever a safety rule or output
        schema midway through its definition.
        """
        return self.content.strip()


def assemble_prompt(segments: Sequence[PromptSegment], budget: int) -> str:
    """Render complete segments, reserving budget by semantic priority.

    Invariant and contract segments are mandatory. Remaining budget is offered
    to runtime context before optional history/catalogue material, while final
    rendering stays in source order so prompt narrative remains coherent.
    """
    rendered: list[tuple[PromptSegment, str]] = []
    for segment in segments:
        content = segment.render()
        if not content:
            continue
        if (
            segment.max_chars is not None
            and len(content) > segment.max_chars
            and segment.priority not in ("invariant", "contract")
        ):
            continue
        rendered.append((segment, content))

    selected = {
        index for index, (segment, _) in enumerate(rendered)
        if segment.priority in ("invariant", "contract")
    }

    def size(indices: set[int]) -> int:
        return len("\n\n".join(
            content for index, (_, content) in enumerate(rendered)
            if index in indices
        ))

    # Contracts may legitimately exceed the budget; never cut a safety rule
    # or output schema. Non-mandatory content is added only when it fits.
    for priority in ("context", "optional"):
        for index, (segment, _) in enumerate(rendered):
            if segment.priority != priority:
                continue
            candidate = selected | {index}
            if size(candidate) <= budget:
                selected = candidate

    return "\n\n".join(
        content for index, (_, content) in enumerate(rendered)
        if index in selected
    )
