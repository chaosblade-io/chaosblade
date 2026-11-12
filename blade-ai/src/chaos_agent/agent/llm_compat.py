"""Provider-compatibility layer for blade-ai's OpenAI-compatible LLM integration.

blade-ai talks to arbitrary OpenAI-compatible gateways (One-API/New-API style
proxies, DashScope, DeepSeek, ...). Issue #1344 collected four reproducible
failures that all trace back to the integration assuming one narrow provider
shape — plain chat-completions, non-thinking models:

1. ``enable_thinking`` is sent unconditionally; providers that do not know
   the parameter reject the whole request with a 400.
2. Thinking models (DeepSeek reasoning mode) require the model's own
   ``reasoning_content`` from a prior turn to be echoed back in the next
   request; the outgoing message conversion dropped it.
3. A tool message whose ``tool_call_id`` has no matching pending tool call
   (a history rebuilt after a failed attempt) is rejected by providers with
   ``Messages with role 'tool' must be a response to a preceding message
   with 'tool_calls'``.
4. Gateways that route chat-completions through OpenAI's Responses API fail
   with low-level errors about ``item_reference`` / ``previous_response_id``;
   the raw traceback surfaces straight to the CLI.

This module addresses each: a ChatOpenAI subclass that sanitizes the message
sequence, retries once without ``enable_thinking`` when a provider rejects
it (and remembers the answer), and wraps otherwise-unexplained provider 400s
into :class:`LLMProviderIncompatibilityError` with an actionable message;
plus an outgoing-message patch that echoes ``reasoning_content`` back.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class LLMProviderIncompatibilityError(RuntimeError):
    """The configured LLM provider rejected a request in a way that suggests
    it is incompatible with blade-ai's chat-completions tool-calling flow.

    The original provider error is attached as ``__cause__``; the message
    tells the user what to check or change.
    """


def _is_unsupported_param_error(err: BaseException, param: str) -> bool:
    """True when *err* is a 400 whose text marks *param* as unsupported."""
    text = str(err).lower()
    name = type(err).__name__.lower()
    return (
        ("badrequest" in name or "status" in name)
        and param in text
        and ("unsupported parameter" in text or "unexpected" in text or "unknown" in text)
    )


def _is_responses_routing_error(err: BaseException) -> bool:
    """True when *err* suggests the gateway routed the call through OpenAI's
    stateful Responses API, which this tool-calling flow cannot continue."""
    text = str(err).lower()
    return (
        "item_reference" in text
        or "previous_response_id" in text
        or ("function_call_output" in text and "item_reference" in text)
    )


def sanitize_tool_sequence(messages: list) -> list:
    """Drop tool messages that no preceding assistant message asked for.

    OpenAI-compatible providers reject a ``role: "tool"`` message whose
    ``tool_call_id`` does not answer a ``tool_calls`` entry on a preceding
    assistant message. Message histories rebuilt after a failed attempt
    (error feedback, replan hints inserted between turns) can end up with
    such orphans; rather than letting the provider 400 the whole turn, the
    orphans are dropped here and logged.
    """
    out = []
    pending_ids: set = set()
    dropped = 0
    for msg in messages:
        type_name = type(msg).__name__
        if type_name == "AIMessage":
            for tc in getattr(msg, "tool_calls", None) or []:
                if isinstance(tc, dict):
                    pending_ids.add(tc.get("id", ""))
            out.append(msg)
            continue
        if type_name == "ToolMessage":
            call_id = getattr(msg, "tool_call_id", None)
            if call_id and call_id in pending_ids:
                out.append(msg)
                # A tool call is answered once; OpenAI-style APIs reject a
                # second tool message for the same id.
                pending_ids.discard(call_id)
            else:
                dropped += 1
                logger.debug(
                    "Dropping orphan tool message (call_id=%s, name=%s): no "
                    "preceding assistant tool_calls asked for it",
                    call_id,
                    getattr(msg, "name", None),
                )
            continue
        out.append(msg)
    if dropped:
        logger.warning(
            "Dropped %d orphan tool message(s) that no assistant tool_calls "
            "asked for — providers reject these as tool messages with no "
            "preceding tool_calls",
            dropped,
        )
    return out


def patch_openai_message_conversion() -> None:
    """Echo ``reasoning_content`` back to thinking models in outgoing messages.

    DeepSeek's reasoning mode rejects the second-and-later turns of a
    multi-tool-call conversation unless the assistant message carries the
    ``reasoning_content`` the model produced in the first turn. The incoming
    patch in ``agent.factory`` already preserves that field on
    ``AIMessage.additional_kwargs``; this patch adds it to the outgoing
    payload. Idempotent.
    """
    from langchain_openai.chat_models import base as _lc_base

    if getattr(_lc_base, "_blade_reasoning_echo", False):
        return

    _orig = _lc_base._convert_message_to_dict

    def _convert_message_to_dict_with_reasoning(message, *args, **kwargs):
        message_dict = _orig(message, *args, **kwargs)
        if (
            type(message).__name__ == "AIMessage"
            and message_dict.get("role") == "assistant"
        ):
            reasoning = (message.additional_kwargs or {}).get("reasoning_content")
            if reasoning and "reasoning_content" not in message_dict:
                message_dict["reasoning_content"] = reasoning
        return message_dict

    _lc_base._convert_message_to_dict = _convert_message_to_dict_with_reasoning
    _lc_base._blade_reasoning_echo = True
    logger.debug(
        "Patched langchain-openai to echo reasoning_content on outgoing "
        "assistant messages"
    )


def _without_enable_thinking(extra_body: Any) -> Any:
    if isinstance(extra_body, dict):
        stripped = {k: v for k, v in extra_body.items() if k != "enable_thinking"}
        return stripped or None
    return extra_body


class ProviderCompatMixin:
    """Mixin adding provider-compatibility behaviour to a ChatOpenAI subclass.

    Overrides the generate/stream entry points to sanitize the message
    sequence, and to fall back cleanly when a provider rejects
    ``enable_thinking``. The fallback is remembered on the instance, so
    later calls skip the rejected parameter instead of re-failing.
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from openai import BadRequestError

        messages = sanitize_tool_sequence(list(messages))
        extra = getattr(self, "extra_body", None)
        try:
            return super()._generate(
                messages=messages, stop=stop, run_manager=run_manager, **kwargs
            )
        except BadRequestError as err:
            if _is_unsupported_param_error(err, "enable_thinking") and extra:
                logger.warning(
                    "Provider rejected enable_thinking (%s); retrying without "
                    "it and disabling it for this session",
                    str(err)[:160],
                )
                self.extra_body = _without_enable_thinking(extra)
                return super()._generate(
                    messages=messages, stop=stop, run_manager=run_manager, **kwargs
                )
            self._explain_provider_error(err)
            raise

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        from openai import BadRequestError

        messages = sanitize_tool_sequence(list(messages))
        extra = getattr(self, "extra_body", None)
        try:
            async for chunk in super()._astream(
                messages=messages, stop=stop, run_manager=run_manager, **kwargs
            ):
                yield chunk
        except BadRequestError as err:
            if _is_unsupported_param_error(err, "enable_thinking") and extra:
                logger.warning(
                    "Provider rejected enable_thinking (%s); retrying without "
                    "it and disabling it for this session",
                    str(err)[:160],
                )
                self.extra_body = _without_enable_thinking(extra)
                async for chunk in super()._astream(
                    messages=messages, stop=stop, run_manager=run_manager, **kwargs
                ):
                    yield chunk
                return
            self._explain_provider_error(err)
            raise

    @staticmethod
    def _explain_provider_error(err: BaseException) -> None:
        """Translate provider 400s the executor cannot fix into actionable
        errors instead of a raw openai.BadRequestError traceback."""
        if _is_responses_routing_error(err):
            raise LLMProviderIncompatibilityError(
                "The configured model is routed through an OpenAI Responses API "
                "channel, which blade-ai's tool-calling flow (classic chat "
                "completions with tool_calls / tool messages) cannot continue "
                "against. Pick a model whose gateway serves plain chat "
                "completions, or point llm_api_base_url at a channel that does."
            ) from err
        if _is_unsupported_param_error(err, "enable_thinking"):
            raise LLMProviderIncompatibilityError(
                "The provider rejected blade-ai's enable_thinking request "
                "parameter. Set BLADE_AI_LLM_ENABLE_THINKING=false in the "
                "environment (or llm_enable_thinking: false in the config) "
                "and retry."
            ) from err
