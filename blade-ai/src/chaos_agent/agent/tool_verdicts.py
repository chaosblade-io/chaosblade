"""Single source for the "did this tool call fail?" verdict.

WHY THIS MODULE EXISTS
----------------------
Every consumer that needs to know whether a ``ToolMessage`` reports a
failure used to answer it locally, by matching the result TEXT against
``content.startswith("Error")`` (sometimes ``"Error:"``, sometimes with an
extra ``"[target_guard]"`` term). That works while every tool renders
failures the same way — kubectl does, and so does the ``Error:``-prefixed
exit path of the blade tools. It stops working the moment a tool reports
failures STRUCTURALLY, inside a JSON receipt: the prefix never matches, so
each consumer independently concludes "not a failure".

Two consequences were observed, and they are the reason this is a module
rather than one more ``startswith`` term:

* ``execute_loop._build_replan_context`` grew a ``if name ==
  "blade_create"`` branch with its own ``json.loads`` + ``data.get(
  "success", True)`` — a per-tool special case hardcoded in a generic
  node. Any further JSON-shaped tool needs its own branch.
* ``react_helpers.detect_transient_retry_exhaustion`` inverted the miss
  into a VERDICT ("no ``Error`` prefix ⇒ the blip healed ⇒ fresh budget"),
  so a structurally-reporting tool reset its own retry counter on every
  failure and the guard stayed permanently silent for it.

THE CONTRACT
------------
The verdict is split by ownership:

1. GENERIC (this module): ``ToolMessage.status == "error"`` and the
   framework's own textual renderings (:data:`GENERIC_ERROR_PREFIXES`).
   These are backend-independent by construction.
2. BACKEND-SPECIFIC (the provider): a tool whose result shape only its
   own carrier can read declares that shape through
   ``FaultProvider.result_shape_tool_names`` +
   ``FaultProvider.tool_result_error_text``. The provider owns the
   judgement; this module only routes to it.

Three-valued, not boolean. A provider that does not recognise a shape
ABSTAINS (``None``) — the same rule the error-text matchers follow: a
matcher may degrade to silence, it may not assert. Callers that need the
positive direction ("this proves success") must ask for it explicitly;
"not failed" is NOT "succeeded".

Deliberately NOT routed through here: scans that are already scoped to
kubectl's text dialect by construction (``providers/message_scanning.py``,
``target_health.py``, the faultdrill provider's own ``_result_is_error``).
They match ``"Error:"`` exactly against kubectl output and widening them
would change attribution semantics for no gain.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: Result prefixes the GENERIC layer recognises as failures on its own —
#: framework renderings that are identical for every backend:
#:
#: * ``Error`` — the tool-layer convention for both the exception path and
#:   the non-zero-exit path (kubectl.py renders both with exactly this
#:   prefix). Matched WITHOUT the colon on purpose: ``execute_loop`` and
#:   ``react_helpers`` have always used the wider form, and a rendering
#:   like ``Error(1234):`` must not slip through.
#: * ``[target_guard]`` — the route gate's rejection rendering, emitted
#:   BEFORE dispatch, so it never carries the tool-layer prefix.
GENERIC_ERROR_PREFIXES: tuple[str, ...] = ("Error", "[target_guard]")


def _coerce(content: object) -> str:
    if isinstance(content, str):
        return content
    return "" if content is None else str(content)


def provider_error_text(tool_name: str, content: str) -> Optional[str]:
    """The owning provider's failure verdict on ``content``, or ``None``.

    ``None`` covers both "no provider owns this tool's result shape" and
    "the owner abstains on this shape" — callers must not distinguish
    them, since neither is evidence of success.

    The registry import is deferred: this module is imported by the
    execute-loop helpers, and the registry pulls in the provider classes
    (hence the tool modules) at import time.
    """
    if not tool_name:
        return None
    try:
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        return FaultProviderRegistry.tool_result_error_text(tool_name, content)
    except Exception:  # noqa: BLE001 — a verdict seam must never crash a loop
        logger.debug("provider result-shape verdict failed for %s", tool_name,
                     exc_info=True)
        return None


def tool_result_error_text(
    tool_name: str,
    content: object,
    *,
    status: Optional[str] = None,
) -> Optional[str]:
    """The failure evidence in a tool result, or ``None`` when there is none.

    Order matters: the generic renderings are checked first because they
    are unambiguous and cheap, and because a provider declaration must
    never be able to talk a ``[target_guard]`` rejection into success.
    The returned text is the evidence itself — for a generic prefix it is
    the whole result, for a provider shape it is the message the provider
    extracted (precise enough for ``errors.classify_error`` to match on,
    without dragging unrelated receipt fields into the classification).
    """
    text = _coerce(content)
    if status == "error":
        return text or "tool reported status=error"
    stripped = text.lstrip()
    for prefix in GENERIC_ERROR_PREFIXES:
        if stripped.startswith(prefix):
            return text
    return provider_error_text(tool_name, text)


def tool_result_failed(
    tool_name: str,
    content: object,
    *,
    status: Optional[str] = None,
) -> bool:
    """Whether a tool result is a failure — the single-source verdict."""
    return tool_result_error_text(tool_name, content, status=status) is not None


def message_result_error_text(message: object) -> Optional[str]:
    """:func:`tool_result_error_text` reading a ``ToolMessage`` directly."""
    return tool_result_error_text(
        str(getattr(message, "name", "") or ""),
        getattr(message, "content", ""),
        status=getattr(message, "status", None),
    )


def message_result_failed(message: object) -> bool:
    """:func:`tool_result_failed` reading a ``ToolMessage`` directly."""
    return message_result_error_text(message) is not None


def loads_dict(content: object) -> Optional[dict]:
    """Parse a tool result as a JSON object, else ``None``.

    Shared by the provider declarations so each one does not re-derive the
    same defensive parse. A compacted receipt is JSON-SHAPED but not valid
    JSON (``memory.tool_compactor`` truncates by bytes once
    ``smart_strip_k8s_json`` declines the body), and that case must abstain
    rather than be read as success.
    """
    text = _coerce(content).strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
