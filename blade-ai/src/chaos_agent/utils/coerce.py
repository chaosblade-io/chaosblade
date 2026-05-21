"""Defensive type coercion for LLM-source payloads.

Tool-call arguments and graph-state fields that *should* be a dict /
list / str / int routinely arrive as the wrong shape because LLMs
don't honour strict schemas under load (especially with thinking
mode + long context). The classic symptom is

    AttributeError: 'str' object has no attribute 'items'

raised somewhere deep in a node when ``payload.items()`` is called
on a value that the LLM serialised as ``"k1=v1,k2=v2"`` instead of
``{"k1": "v1", "k2": "v2"}``.

Rather than wrap every consumer with bespoke ``isinstance`` checks,
this module exposes four idempotent coercion helpers that:

  - never raise on common shape drift
  - emit a structured ``logger.warning`` with caller-supplied
    ``context`` so upstream schema bugs stay visible
  - return the conventional empty value (``{}`` / ``[]`` / ``""`` /
    ``0``) on any unrecognised shape, so call sites can use the
    result directly without ``or {}`` / ``or []`` fallback chains

The helpers are intentionally permissive about *recognised* str
forms: JSON objects, k8s label-selector syntax (``"a=b,c=d"``),
single-pair strings (``"a=b"``), and stringified arrays. Anything
unknown is dropped to the empty value with a warning.

Used everywhere a graph node reads ``state["target"]["labels"]``,
``state["fault_intent"]["params"]`` or similar LLM-source fields.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def coerce_to_dict(value: Any, *, context: str = "") -> dict:
    """Coerce a value that *should* be a dict into one.

    Recognised inputs:
      - ``dict``                    → returned as-is
      - ``None`` / ``""`` / ``[]`` / ``{}``  → empty dict
      - ``str`` like ``'{"k":"v"}'`` → ``json.loads``
      - ``str`` like ``"k1=v1,k2=v2"`` (k8s label-selector syntax)
      - ``str`` like ``"k=v"`` (single pair)
      - ``list[dict]``              → first dict element
      - ``list[str]`` of ``"k=v"``  → joined to dict
      - anything else               → ``{}`` + warning log

    The function never raises. ``context`` is included in any
    warning so debugging the upstream schema drift is straightforward.
    """
    if value is None or value == "" or value == [] or value == {}:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        if "=" in s:
            out: dict = {}
            for piece in s.split(","):
                piece = piece.strip()
                if "=" not in piece:
                    continue
                k, v = piece.split("=", 1)
                k = k.strip()
                if k:
                    out[k] = v.strip()
            if out:
                return out
        logger.warning(
            "coerce_to_dict[%s]: unparseable str value: %r",
            context,
            s[:200],
        )
        return {}
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
        out = {}
        for item in value:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                k = k.strip()
                if k:
                    out[k] = v.strip()
        if out:
            return out
    logger.warning(
        "coerce_to_dict[%s]: unexpected type %s, value=%r",
        context,
        type(value).__name__,
        str(value)[:200],
    )
    return {}


def coerce_to_list(value: Any, *, context: str = "") -> list:
    """Coerce a value that *should* be a list into one.

    Recognised inputs:
      - ``list``                       → returned as-is
      - ``None`` / ``""``              → empty list
      - ``str`` like ``"[a, b, c]"``   → ``json.loads``
      - ``str`` like ``"a,b,c"``       → split on commas
      - ``str`` (single non-empty)     → ``[value]``
      - anything else                  → ``[]`` + warning log
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        if "," in s:
            return [p.strip() for p in s.split(",") if p.strip()]
        return [s] if s else []
    logger.warning(
        "coerce_to_list[%s]: unexpected type %s, value=%r",
        context,
        type(value).__name__,
        str(value)[:200],
    )
    return []


def coerce_to_str(value: Any, *, default: str = "", context: str = "") -> str:
    """Coerce a value that *should* be a string into one.

    Numbers / bools are stringified via ``str()``. Containers and
    other unexpected types fall back to ``default`` with a warning.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    logger.warning(
        "coerce_to_str[%s]: unexpected type %s, value=%r",
        context,
        type(value).__name__,
        str(value)[:200],
    )
    return default


def coerce_to_int(value: Any, *, default: int = 0, context: str = "") -> int:
    """Coerce a value that *should* be an int into one.

    ``bool`` is rejected even though it's an int subclass — the
    common bug is passing a flag where a count is expected, and we
    don't want ``True`` to coerce to ``1``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return int(s)
        except ValueError:
            try:
                return int(float(s))
            except ValueError:
                pass
    logger.warning(
        "coerce_to_int[%s]: unexpected value %r",
        context,
        str(value)[:100],
    )
    return default
