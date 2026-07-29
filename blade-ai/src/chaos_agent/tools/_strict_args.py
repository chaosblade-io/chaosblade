"""Strict tool-argument schemas: unknown kwargs are REFUSED, never dropped.

Why this exists — task-46317228: the LLM called ``host_read`` eight times and
every call carried ``node="cn-shanghai-cloudspe.25.209.68.1"``, i.e. it stated
which machine it wanted to read. ``host_read``'s signature is
``(command, timeout, task_id)``, so LangChain built an args model without
``node`` and **silently dropped it**. The command then ran through whatever
channel the config resolved to (``kubewiz_k8s``) and landed on the KubeWiz
platform executor pod — returning plausible-looking ``load average 0.02`` from
an unrelated machine, which the verifier then used to contradict the target
node's real 90% CPU.

That is the worst failure shape available: an intent was expressed, it was not
honoured, nothing warned, and the caller got a successful-looking answer.

Silently ignoring extra keys is LangChain/pydantic default behaviour and cannot
be turned off from the outside, so a tool that must not be re-targeted per call
has to declare its argument schema explicitly and refuse unknown keys itself.
Subclass :class:`StrictToolArgs` and pass it as ``@tool(args_schema=...)``.

The refusal surfaces as a ``ValidationError`` whose text contains
``unknown_key_hint``. ToolNodes with LangGraph's default error handling return
that text to the model as the tool result, so it can correct itself. Nodes with a
CUSTOM ``handle_tool_errors`` must let it through — see
:data:`UNKNOWN_ARG_REFUSAL_MARKER`.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

# Stable substring of the refusal text. A node that rewrites tool errors uses it
# to tell "this tool rejected an argument" (message already actionable, pass it
# through) apart from "this tool is not available here" (its own wording wins).
UNKNOWN_ARG_REFUSAL_MARKER = "does not accept these parameters"


class StrictToolArgs(BaseModel):
    """Base args schema that refuses keys it does not declare.

    Subclasses set ``tool_display_name`` / ``unknown_key_hint`` and declare
    their real fields. ``extra="forbid"`` is the backstop; the validator below
    runs first so the caller gets an actionable message instead of pydantic's
    generic ``extra_forbidden``.
    """

    model_config = ConfigDict(extra="forbid")

    # Name used in the error text — the LLM knows the tool by this name.
    tool_display_name: ClassVar[str] = "this tool"
    # Tool-specific explanation of why the key has nowhere to go, plus the
    # correct alternative. Without it a rejection is just as unhelpful as the
    # silent drop it replaces.
    unknown_key_hint: ClassVar[str] = ""

    @classmethod
    def unknown_key_advice(cls) -> str:
        """Advice appended to the refusal. Override for environment-dependent text.

        The right alternative can depend on the CURRENT environment: telling a
        host-channel session to "use kubectl_read instead" names a tool that the
        capability gate refuses on that very session, sending the model from one
        dead end to another. Subclasses that have such a split override this.
        """
        return cls.unknown_key_hint

    @model_validator(mode="before")
    @classmethod
    def _refuse_unknown_keys(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        unknown = sorted(set(data) - set(cls.model_fields))
        if unknown:
            raise ValueError(
                f"{cls.tool_display_name} {UNKNOWN_ARG_REFUSAL_MARKER}: "
                f"{', '.join(unknown)}. "
                f"{cls.unknown_key_advice()}".rstrip()
            )
        return data


__all__ = ["UNKNOWN_ARG_REFUSAL_MARKER", "StrictToolArgs"]
