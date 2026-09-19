"""Resolve an MCP tool's read/write *effect* label — advisory only.

Posture B (transparency, never gating): the resolved label is surfaced
to the LLM inside the tool description and recorded in the connect log,
so a server-declared destructive tool is never silently presented as
read-only. It does NOT gate execution — the target-guard classifier
still passes every registered MCP tool through as READONLY (posture A,
``registry.py``), and the operator owns the wiring. The LLM decides,
with the attribute in hand, whether a call is appropriate.

The framework cannot infer an arbitrary MCP tool's semantics, so the
label comes from the only two sources that exist, in precedence order:

  1. the operator's per-tool declaration in ``mcp.json``
     (``tool_effects``) — authoritative, because the operator installed
     the server and vouches for it;
  2. the server's own MCP ``ToolAnnotations`` (``readOnlyHint`` /
     ``destructiveHint``) — self-declared and therefore UNTRUSTED, used
     only as a default the operator can override;
  3. ``unspecified`` — neither source declared anything.
"""

from __future__ import annotations

from typing import Any

EFFECT_READONLY = "readonly"
EFFECT_DESTRUCTIVE = "destructive"
EFFECT_UNSPECIFIED = "unspecified"

# Values an operator may write in ``mcp.json`` ``tool_effects``.
_VALID_EFFECTS = frozenset({EFFECT_READONLY, EFFECT_DESTRUCTIVE})

# Source tags recorded alongside the label for audit.
SOURCE_CONFIG = "config"
SOURCE_ANNOTATION = "annotation"
SOURCE_UNSPECIFIED = "unspecified"


def effect_from_annotations(annotations: Any) -> str:
    """Derive the effect label from MCP ``ToolAnnotations`` (or ``None``).

    ``annotations`` may be a plain dict or an mcp ``ToolAnnotations``
    object; both are handled so the resolver is decoupled from the SDK
    type. ``destructiveHint`` wins when both it and ``readOnlyHint`` are
    set — the label leans toward caution, which costs nothing here
    because it is advisory (never a block).
    """
    if not annotations:
        return EFFECT_UNSPECIFIED

    def _get(key: str) -> Any:
        if isinstance(annotations, dict):
            return annotations.get(key)
        return getattr(annotations, key, None)

    if _get("destructiveHint"):
        return EFFECT_DESTRUCTIVE
    if _get("readOnlyHint"):
        return EFFECT_READONLY
    return EFFECT_UNSPECIFIED


def resolve_tool_effect(user_effect: str | None, annotations: Any) -> tuple[str, str]:
    """Return ``(effect, source)`` for one MCP tool.

    ``effect`` is one of :data:`EFFECT_READONLY` / :data:`EFFECT_DESTRUCTIVE`
    / :data:`EFFECT_UNSPECIFIED`; ``source`` is ``config`` / ``annotation`` /
    ``unspecified`` so an audit can tell an operator override from a
    server-self-declared default. An invalid ``user_effect`` (which
    ``config.py`` already rejects at load time) is ignored here rather
    than trusted, falling through to the annotation.
    """
    if user_effect in _VALID_EFFECTS:
        return user_effect, SOURCE_CONFIG
    eff = effect_from_annotations(annotations)
    if eff == EFFECT_UNSPECIFIED:
        return EFFECT_UNSPECIFIED, SOURCE_UNSPECIFIED
    return eff, SOURCE_ANNOTATION


__all__ = [
    "EFFECT_READONLY",
    "EFFECT_DESTRUCTIVE",
    "EFFECT_UNSPECIFIED",
    "SOURCE_CONFIG",
    "SOURCE_ANNOTATION",
    "SOURCE_UNSPECIFIED",
    "effect_from_annotations",
    "resolve_tool_effect",
]
