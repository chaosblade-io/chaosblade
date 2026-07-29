"""Bounded-retry ``ChatOpenAI`` wrapper.

Every LLM in the agent is built by ``make_llm`` (factory.py) as a
``ResilientChatOpenAI``, so this one class is the single place where an LLM
call's failure handling lives.

Why wrap at all
---------------
Under the hood even a plain ``ainvoke`` streams the completion (a long-lived
SSE read), and the OpenAI SDK's own ``max_retries`` only covers failures
*before* the stream starts. A mid-stream drop (laptop sleep, Wi-Fi handoff,
gateway reset) or a provider-side error (rate limit, 5xx, quota) otherwise
propagates raw and aborts the whole turn.

What it does
------------
On ANY exception from ``(a)invoke`` it retries with exponential backoff up to
``settings.retry_max_retries`` times, then re-raises the ORIGINAL error so the
turn boundary can surface its content and end gracefully (no traceback crash).
The retry is deliberately GENERIC — provider error taxonomies are large and
volatile, so we do not enumerate/branch per error type; a couple of cheap
backed-off retries clear the common transient cases (rate limit, mid-stream
drop, brief 5xx), and anything still failing after the budget is reported
as-is.

The one deliberate exclusion is a genuine response/read timeout — it means the
model was simply too slow, so retrying just doubles an already-long wait
(``llm_read_timeout`` defaults to 600s). Those re-raise immediately.

Notes
-----
* Because ``ainvoke`` returns a single aggregated message (nothing is handed to
  the caller until it completes), simply re-running it on failure is safe — the
  returned value always comes from the final, successful attempt.
* ``asyncio.CancelledError`` / ``KeyboardInterrupt`` subclass ``BaseException``,
  not ``Exception``, so user cancellation is never swallowed by the retry.
* Only ``ainvoke`` / ``invoke`` are wrapped. Direct ``astream`` is left
  untouched (retrying a partially-yielded stream would duplicate tokens); the
  agent never calls ``llm.astream`` directly — token streaming to the UI comes
  from the graph's ``astream_events`` tapping the ``on_chat_model_stream``
  events emitted *inside* ``ainvoke``.
* Tool binding (``bind_tools`` / ``bind``) returns a ``RunnableBinding`` whose
  ``ainvoke`` delegates back to this instance's wrapped method, so the retry
  survives binding.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from functools import lru_cache

from langchain_openai import ChatOpenAI

from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _no_retry_exc_types() -> tuple[type[BaseException], ...]:
    """Timeouts that mean 'the model was genuinely slow' — retrying just
    doubles an already-long wait, so these re-raise immediately instead of
    going through the generic retry.

    The 600s read timeout surfaces as ``openai.APITimeoutError`` (the SDK wraps
    ``httpx`` timeouts); ``httpx.ReadTimeout`` covers a raw mid-stream read.
    Resolved lazily + cached so the imports happen once.
    """
    excs: list[type[BaseException]] = []
    try:
        import httpx

        excs.append(httpx.ReadTimeout)
    except Exception:  # pragma: no cover - httpx always present via langchain
        pass
    try:
        import openai

        excs.append(openai.APITimeoutError)
    except Exception:  # pragma: no cover - openai always present
        pass
    return tuple(excs)


def _backoff_delay(attempt: int) -> float:
    """Seconds to wait before the ``attempt``-th retry (0-indexed).

    Full-jitter exponential backoff:
    ``uniform(0, min(base * mult**attempt, cap))``. Reuses the shared
    ``retry_*`` settings so operators tune a single knob set.
    """
    ceiling = min(
        settings.retry_base_delay * (settings.retry_exponential_base ** attempt),
        settings.retry_max_delay,
    )
    if settings.retry_jitter:
        return random.uniform(0, ceiling)
    return ceiling


class ResilientChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` that retries any (a)invoke failure with backoff."""

    async def ainvoke(self, *args, **kwargs):  # type: ignore[override]
        max_retries = max(0, int(settings.retry_max_retries))
        attempt = 0
        while True:
            try:
                return await super().ainvoke(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - generic by design
                # Slow-model response/read timeout: retrying only doubles an
                # already-long wait — surface it immediately.
                if isinstance(exc, _no_retry_exc_types()):
                    logger.warning(
                        "LLM ainvoke timed out (%s); not retried (model too slow).",
                        type(exc).__name__,
                    )
                    raise
                if attempt >= max_retries:
                    logger.warning(
                        "LLM ainvoke failed (%s); retries exhausted after "
                        "%d attempt(s), re-raising.",
                        type(exc).__name__, attempt + 1,
                    )
                    raise
                delay = _backoff_delay(attempt)
                logger.warning(
                    "LLM ainvoke failed (%s); retry %d/%d in %.2fs.",
                    type(exc).__name__, attempt + 1, max_retries, delay,
                )
                await asyncio.sleep(delay)
                attempt += 1

    def invoke(self, *args, **kwargs):  # type: ignore[override]
        max_retries = max(0, int(settings.retry_max_retries))
        attempt = 0
        while True:
            try:
                return super().invoke(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - generic by design
                # Slow-model response/read timeout: retrying only doubles an
                # already-long wait — surface it immediately.
                if isinstance(exc, _no_retry_exc_types()):
                    logger.warning(
                        "LLM invoke timed out (%s); not retried (model too slow).",
                        type(exc).__name__,
                    )
                    raise
                if attempt >= max_retries:
                    logger.warning(
                        "LLM invoke failed (%s); retries exhausted after "
                        "%d attempt(s), re-raising.",
                        type(exc).__name__, attempt + 1,
                    )
                    raise
                delay = _backoff_delay(attempt)
                logger.warning(
                    "LLM invoke failed (%s); retry %d/%d in %.2fs.",
                    type(exc).__name__, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                attempt += 1
