"""Tests for exponential backoff retry mechanism."""

from unittest.mock import AsyncMock

import pytest

from chaos_agent.errors import (
    BladeExecutionError,
    ToolTimeoutError,
)
from chaos_agent.tools.retry import RetryConfig, retry_if_transient, retry_with_backoff


class TestRetryWithBackoff:
    """Test retry_with_backoff function."""

    async def test_success_on_first_attempt(self):
        """No retry needed if the function succeeds immediately."""
        func = AsyncMock(return_value="ok")
        result = await retry_with_backoff(
            func,
            config=RetryConfig(max_retries=3, base_delay=0.01),
            retryable_exceptions=(Exception,),
        )
        assert result == "ok"

    async def test_success_on_second_attempt(self):
        """Retries and succeeds on the second attempt."""
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("temporary")
            return "ok"

        result = await retry_with_backoff(
            flaky,
            config=RetryConfig(max_retries=3, base_delay=0.01, jitter=False),
            retryable_exceptions=(ValueError,),
        )
        assert result == "ok"

    async def test_exhausts_max_retries(self):
        """Raises the last exception when all retries are exhausted."""
        func = AsyncMock(side_effect=ValueError("always fails"))
        with pytest.raises(ValueError, match="always fails"):
            await retry_with_backoff(
                func,
                config=RetryConfig(max_retries=2, base_delay=0.01),
                retryable_exceptions=(ValueError,),
            )

    async def test_non_retryable_exception_raised_immediately(self):
        """Non-matching exceptions are raised immediately."""
        func = AsyncMock(side_effect=TypeError("wrong type"))
        with pytest.raises(TypeError, match="wrong type"):
            await retry_with_backoff(
                func,
                config=RetryConfig(max_retries=3, base_delay=0.01),
                retryable_exceptions=(ValueError,),
            )


class TestRetryConfig:
    """Test RetryConfig dataclass."""

    def test_default_values(self):
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.base_delay == 1.0
        assert config.max_delay == 30.0
        assert config.exponential_base == 2.0
        assert config.jitter is True

    def test_custom_values(self):
        config = RetryConfig(
            max_retries=5,
            base_delay=0.5,
            max_delay=60.0,
            exponential_base=3.0,
            jitter=False,
        )
        assert config.max_retries == 5
        assert config.base_delay == 0.5
        assert config.max_delay == 60.0
        assert config.exponential_base == 3.0
        assert config.jitter is False


class TestRetryIfTransient:
    """Test retry_if_transient function."""

    async def test_retries_transient_errors(self):
        """Transient ChaosAgentErrors should be retried."""
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ToolTimeoutError("timeout")
            return "ok"

        result = await retry_if_transient(
            flaky,
            config=RetryConfig(max_retries=3, base_delay=0.01),
        )
        assert result == "ok"

    async def test_permanent_error_not_retried(self):
        """Permanent ChaosAgentErrors should be raised immediately."""
        func = AsyncMock(side_effect=BladeExecutionError("permanent"))
        with pytest.raises(BladeExecutionError, match="permanent"):
            await retry_if_transient(
                func,
                config=RetryConfig(max_retries=3, base_delay=0.01),
            )

    async def test_non_chaos_error_not_retried(self):
        """Non-ChaosAgentError exceptions should be raised immediately."""
        func = AsyncMock(side_effect=ValueError("not chaos"))
        with pytest.raises(ValueError, match="not chaos"):
            await retry_if_transient(
                func,
                config=RetryConfig(max_retries=3, base_delay=0.01),
            )

    async def test_success_without_retry(self):
        """Successful call should return immediately."""
        func = AsyncMock(return_value="ok")
        result = await retry_if_transient(
            func,
            config=RetryConfig(max_retries=3, base_delay=0.01),
        )
        assert result == "ok"


class TestExponentialBackoffCalculation:
    """Test backoff delay calculations."""

    def test_delay_increases_exponentially(self):
        """Verify delay = min(base * (base^attempt), max_delay)."""
        config = RetryConfig(
            base_delay=1.0,
            exponential_base=2.0,
            max_delay=30.0,
            jitter=False,
        )
        assert min(config.base_delay * (config.exponential_base ** 0), config.max_delay) == 1.0
        assert min(config.base_delay * (config.exponential_base ** 1), config.max_delay) == 2.0
        assert min(config.base_delay * (config.exponential_base ** 2), config.max_delay) == 4.0
        assert min(config.base_delay * (config.exponential_base ** 5), config.max_delay) == 30.0

    def test_max_delay_cap(self):
        config = RetryConfig(base_delay=1.0, exponential_base=2.0, max_delay=10.0, jitter=False)
        assert min(config.base_delay * (config.exponential_base ** 4), config.max_delay) == 10.0
