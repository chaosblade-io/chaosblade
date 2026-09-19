"""Bounded transient retry for IDEMPOTENT call sites (B35).

Sister module to ``transports.transient`` (P3): both encode the same
skeleton — classify transient → back off → retry → surface with a trail
when exhausted — but they classify DIFFERENT things:

* ``transports.transient`` matches MESSAGE SIGNATURES on a command result
  (``No executor available`` …) because a transport failure arrives as a
  populated ``CommandResult``, not an exception.
* this module matches EXCEPTION TYPES (connection-level network errors)
  because the call sites it guards — the recover verifier's LLM verdict
  calls — fail by raising.

What is transient here (retry — the FAST-FAIL connection family):
* built-in ``ConnectionError`` — refused/reset/aborted pipes.
* ``httpx.TransportError`` MINUS the slow-model timeouts — connect
  errors, mid-stream protocol breaks. ``httpx.ConnectTimeout`` stays
  retryable on purpose: the connect budget is short
  (``llm_connect_timeout``), so a connect timeout IS a fast-fail
  gateway blip, not a slow-model verdict.
* ``openai.APIConnectionError`` MINUS ``APITimeoutError`` — the SDK's
  connection-level family with the timeout subclass carved out.

What is deliberately NOT transient (re-raise immediately):
* ``asyncio.TimeoutError`` / built-in ``TimeoutError`` (same class on
  3.11+). At the recover verifier's call sites that exception means the
  600s ``llm_read_timeout`` fired — the model was genuinely slow, and
  retrying only doubles an already-long wait (the deliberate policy of
  ``agent/resilient_llm._no_retry_exc_types``; this module inherits it).
* ``openai.APITimeoutError`` and ``httpx.ReadTimeout`` — the SAME slow-
  model verdict in its SDK-wrapped form. Subtracting them here keeps
  this classifier consistent with resilient_llm's: without the
  subtraction the Layer 1 verdict call (which has NO outer ``wait_for``)
  would retry a 600s SDK timeout up to ``max_attempts`` times — 30
  minutes of dead waiting on a slow model whose correct handling is
  immediate degradation to the deterministic-merge path.
* ``LLMProviderRejectError`` and every deterministic 4xx verdict — the
  identical request is rejected identically (resilient_llm classifies
  them before we ever see them).

IDEMPOTENCY CONTRACT — decorate/wrap only calls that are safe to repeat:
the recover verifier's LLM verdict calls are pure read-only judgements
(input: a message list; output: a verdict — zero side effects), so a
retry can never duplicate a recovery action. The recovery ACTIONS
themselves are executed through the tool layer (which has its own
transient retry, P3) and are idempotent by existing legislation
(replace = no-op when converged, delete = NotFound is harmless, GET is
always safe). Never wrap a non-idempotent create-class call here — a
409-already-exists outcome must be adjudicated by the caller, not
blindly retried.

Why the recover layer needs this on top of ``resilient_llm``'s generic
retry: the generic retry deliberately EXCLUDES timeouts (see above), so
one transient gateway blip during the recover Layer 2 verdict used to
write a terminal ``RECOVERY_FAILED`` — with ``recovered: false`` — even
though every recovery action had already executed successfully and only
the (idempotent) judgement call failed (B35, case #31). A couple of
short backed-off retries here turn that into a few seconds of latency.

THREE-LAYER MULTIPLICATION (worst-case budget, read before tuning):
an LLM call under this wrapper can traverse THREE independent retry
layers, each judging a DIFFERENT failure semantic —

1. SDK layer (``llm_max_retries``, default 1): transport-level
   connection attempts inside the OpenAI SDK client itself.
2. ``resilient_llm`` (``retry_max_retries``, default 2): APPLICATION-
   level failures — provider rejections worth retrying (408/409/429)
   and connection exceptions the SDK surfaced.
3. THIS module (``retry_transient_max_attempts``, default 3): JUDGEMENT-
   level — the recover verifier's terminal-state protection, so a
   transient blip cannot write a false ``RECOVERY_FAILED``.

The layers never retry the same exception twice (slow-model timeouts are
excluded everywhere, deterministic 4xx only at layer 2), but the worst
case still MULTIPLIES: 3 × (2+1) × 2 = up to 18 HTTP requests and a
minutes-long wall clock on a flapping gateway. That is accepted — each
layer protects a different terminal-state mis-write, and the alternative
(global retry budget shared across layers) would couple an agent-layer
verdict wrapper to the transport stack — but tune any of the three
knobs with the product in mind, not one layer in isolation.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _transient_exc_types() -> tuple[type[BaseException], ...]:
    """Whitelist: the fast-fail connection family (see module docstring).

    Resolved lazily (imports cost) and NOT cached — like the transport
    classifier, callers hit this only on the failure path, once per
    retry decision, so the import lookup is not hot.
    """
    types: list[type[BaseException]] = [ConnectionError]
    try:
        import httpx

        # TransportError covers connect errors, mid-stream breaks AND
        # every TimeoutException subclass — the slow-model read timeout
        # is carved back out by _slow_model_exc_types() in the classifier
        # below (an isinstance subclass exclusion cannot be done by
        # removing the class from this tuple: ``isinstance`` would still
        # match the surviving parent).
        types.append(httpx.TransportError)
    except Exception:  # pragma: no cover - httpx always present via langchain
        pass
    try:
        import openai

        types.append(openai.APIConnectionError)
    except Exception:  # pragma: no cover - openai always present
        pass
    return tuple(types)


def _slow_model_exc_types() -> tuple[type[BaseException], ...]:
    """The slow-model timeout family — never retried at ANY layer.

    Same membership as ``agent/resilient_llm._no_retry_exc_types``;
    re-declared here (rather than imported) to keep utils layering free
    of agent-layer imports — the consistency is pinned by the sister
    tests in tests/test_utils/test_retry_transient.py.
    """
    excs: list[type[BaseException]] = [TimeoutError]  # == asyncio.TimeoutError on 3.11+
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


def is_transient_exception(exc: BaseException) -> bool:
    """True when retrying ``exc`` can plausibly change the outcome.

    Single classification source for this module's callers: NOT in the
    slow-model timeout family (``TimeoutError``/``asyncio.TimeoutError``,
    ``httpx.ReadTimeout``, ``openai.APITimeoutError`` — retrying a
    genuinely slow model only doubles the wait) AND in the fast-fail
    connection family. The exclusion check must run FIRST: those
    timeout types subclass the whitelisted parents, so a plain
    ``isinstance`` against the whitelist alone would wrongly admit
    them.
    """
    if isinstance(exc, _slow_model_exc_types()):
        return False
    return isinstance(exc, _transient_exc_types())


def _backoff_delay(attempt: int) -> float:
    """Full-jitter exponential backoff: ``uniform(0, base * 2**attempt)``."""
    ceiling = settings.retry_transient_base_delay * (2 ** attempt)
    return random.uniform(0, ceiling) if settings.retry_jitter else ceiling


async def call_with_transient_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    log_name: str = "",
) -> T:
    """Await ``fn()`` with a bounded transient-exception retry.

    Call-site wrapping form of the ``retry_transient`` decorator (same
    policy, for places where the call is a one-off expression rather
    than a named function — e.g. a bound-tools LLM invocation).

    Attempts: ``settings.retry_transient_max_attempts`` in total (first
    try + N-1 retries); exhausted transients re-raise the ORIGINAL
    exception so the caller's existing failure paths see the exact same
    shape they always did. ``asyncio.CancelledError`` subclasses
    ``BaseException`` and is never caught — user cancellation is not a
    transient error.
    """
    max_attempts = max(1, int(settings.retry_transient_max_attempts))
    label = log_name or "call"
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — classified below
            if not is_transient_exception(exc) or attempt >= max_attempts - 1:
                raise
            attempt += 1
            delay = _backoff_delay(attempt - 1)
            logger.warning(
                "[retry-transient] %s failed (%s: %s); retry %d/%d in %.1fs",
                label, type(exc).__name__, str(exc)[:120],
                attempt, max_attempts - 1, delay,
            )
            await asyncio.sleep(delay)


def retry_transient(log_name: str = ""):
    """Decorator form of :func:`call_with_transient_retry`.

    Hang this ONLY on idempotent call sites (module docstring contract):
    pure-read judgements, GET-class reads, replace/delete-style recovery
    actions. A non-idempotent call retried on a timeout that actually
    completed server-side duplicates the side effect — the caller must
    adjudicate 409-already-exists semantics itself instead.

    Consumption note (O3): production currently wraps calls through the
    CALL-SITE form (:func:`call_with_transient_retry` — the recover
    verifier's one-off LLM invocations), not this decorator; the
    decorator is kept as the named-function form of the same policy for
    the first named call site that needs it. It is not dead code to
    delete — it is the policy's second spelling, pinned by the sister
    tests.
    """
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def wrapper(*args, **kwargs) -> T:
            return await call_with_transient_retry(
                lambda: fn(*args, **kwargs),
                log_name=log_name or getattr(fn, "__qualname__", ""),
            )
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper
    return decorator
