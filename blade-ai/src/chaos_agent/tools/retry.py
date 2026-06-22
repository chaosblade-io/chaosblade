"""Retry with exponential backoff for async operations.

Provides a generic retry mechanism that can be used by shell.py,
blade.py, kubectl.py, and LLM call wrappers.
"""

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Awaitable, TypeVar

from chaos_agent.errors import is_transient

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryConfig:
    """Retry configuration."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True


async def retry_with_backoff(
    func: Callable[[], Awaitable[T]],
    config: RetryConfig | None = None,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Execute an async function with exponential backoff retry.

    Args:
        func: A zero-arg callable that returns an awaitable (e.g. a lambda
            or partial wrapping an async function). Called fresh on each
            attempt so a new coroutine is produced every time.
        config: Retry configuration. Uses defaults if not provided.
        retryable_exceptions: Exception types that should trigger a retry.

    Returns:
        The result of the function.

    Raises:
        The last exception if all retries are exhausted.
    """
    if config is None:
        config = RetryConfig()

    last_exception: Exception | None = None

    for attempt in range(config.max_retries + 1):
        try:
            return await func()
        except retryable_exceptions as e:
            last_exception = e

            if attempt == config.max_retries:
                break

            delay = min(
                config.base_delay * (config.exponential_base**attempt),
                config.max_delay,
            )
            if config.jitter:
                delay *= 0.5 + random.random()

            logger.warning(
                f"Retry {attempt + 1}/{config.max_retries} after {delay:.1f}s: {e}"
            )
            await asyncio.sleep(delay)

    raise last_exception  # type: ignore[misc]


async def retry_if_transient(
    func: Callable[[], Awaitable[T]],
    config: RetryConfig | None = None,
) -> T:
    """Retry only on transient ChaosAgentError instances.

    Permanent and recoverable errors are not retried.
    """
    if config is None:
        config = RetryConfig()

    last_exception: Exception | None = None

    for attempt in range(config.max_retries + 1):
        try:
            return await func()
        except Exception as e:
            last_exception = e

            # Only retry transient errors
            if not is_transient(e):
                raise

            if attempt == config.max_retries:
                break

            delay = min(
                config.base_delay * (config.exponential_base**attempt),
                config.max_delay,
            )
            if config.jitter:
                delay *= 0.5 + random.random()

            logger.warning(
                f"Retry {attempt + 1}/{config.max_retries} after {delay:.1f}s "
                f"(transient): {e}"
            )
            await asyncio.sleep(delay)

    raise last_exception  # type: ignore[misc]
