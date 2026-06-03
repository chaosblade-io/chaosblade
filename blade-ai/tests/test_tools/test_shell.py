"""Tests for safe async subprocess runner."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.errors import ToolGuardError, ToolTimeoutError
from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.shell import get_tool_guard, run_command


class TestRunCommandSuccess:
    """Test successful command execution."""

    async def test_successful_execution(self, mocker):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"output", b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        result = await run_command(["echo", "hello"], skip_guard=True)
        assert isinstance(result, CommandResult)
        assert result.exit_code == 0

    async def test_nonzero_exit_code(self, mocker):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error msg"))
        mock_proc.returncode = 1
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        result = await run_command(["blade", "create"], skip_guard=True)
        assert result.exit_code == 1
        assert "error msg" in result.stderr

    async def test_stdout_stderr_capture(self, mocker):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"stdout data", b"stderr data"))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        result = await run_command(["kubectl", "get", "pods"], skip_guard=True)
        assert result.stdout == "stdout data"
        assert result.stderr == "stderr data"

    async def test_duration_ms_positive(self, mocker):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())
        # Use return_value instead of side_effect to avoid StopIteration
        # when asyncio event loop internally calls time.monotonic during cleanup
        mocker.patch("chaos_agent.tools.shell.time.monotonic", return_value=100.0)

        result = await run_command(["blade", "status"], skip_guard=True)
        assert result.duration_ms >= 0


class TestRunCommandTimeout:
    """Test timeout handling."""

    async def test_timeout_raises_error(self, mocker):
        mock_proc = MagicMock()
        mock_proc.kill = MagicMock()
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch(
            "asyncio.wait_for",
            side_effect=asyncio.TimeoutError(),
        )

        with pytest.raises(ToolTimeoutError):
            await run_command(["sleep", "1000"], timeout=1, skip_guard=True)

    async def test_timeout_error_message_includes_timeout(self, mocker):
        mock_proc = MagicMock()
        mock_proc.kill = MagicMock()
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch(
            "asyncio.wait_for",
            side_effect=asyncio.TimeoutError(),
        )

        with pytest.raises(ToolTimeoutError) as exc_info:
            await run_command(["sleep", "1000"], timeout=5, skip_guard=True)
        assert "5s" in str(exc_info.value)


class TestRunCommandToolGuard:
    """Test ToolGuard integration."""

    async def test_blocked_command_raises_guard_error(self):
        with pytest.raises(ToolGuardError):
            await run_command(["rm", "-rf", "/"])

    async def test_skip_guard_bypasses_check(self, mocker):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        # This would normally be blocked, but skip_guard=True bypasses
        result = await run_command(["some-random-cmd"], skip_guard=True)
        assert result.exit_code == 0


class TestRunCommandAuditLog:
    """Test audit logging."""

    async def test_audit_log_called(self, mocker):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        mock_guard = MagicMock()
        mock_guard.check.return_value = (True, "OK")
        mock_guard.audit_log = MagicMock()
        mocker.patch("chaos_agent.tools.shell.get_tool_guard", return_value=mock_guard)

        result = await run_command(["blade", "status"], task_id="task-1")
        mock_guard.audit_log.assert_called_once()

    async def test_skip_guard_no_audit(self, mocker):
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        mock_guard = MagicMock()
        mocker.patch("chaos_agent.tools.shell.get_tool_guard", return_value=mock_guard)

        await run_command(["cmd"], skip_guard=True)
        mock_guard.audit_log.assert_not_called()


class TestGetToolGuard:
    """Test singleton ToolGuard."""

    def test_returns_tool_guard_instance(self):
        guard = get_tool_guard()
        from chaos_agent.tools.guard import ToolGuard

        assert isinstance(guard, ToolGuard)

    def test_singleton(self):
        guard1 = get_tool_guard()
        guard2 = get_tool_guard()
        assert guard1 is guard2


class TestUtf8Decoding:
    """Test UTF-8 decoding with error handling."""

    async def test_invalid_utf8_replaced(self, mocker):
        invalid_bytes = b"\xff\xfe invalid"
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(invalid_bytes, b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        result = await run_command(["blade", "status"], skip_guard=True)
        # Should not raise, errors='replace' handles it
        assert isinstance(result.stdout, str)


class TestPersistToSession:
    """Test SessionStore persistence from run_command()."""

    async def test_persists_on_success(self, mocker):
        """Successful command execution is recorded to SessionStore."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b'{"code":200,"success":true}', b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        mock_store = MagicMock()
        mocker.patch("chaos_agent.tools.shell.get_global_session_store", return_value=mock_store)

        await run_command(
            ["kubectl", "get", "pods"], task_id="task-1", skip_guard=True,
        )

        mock_store.append_raw_message.assert_called_once()
        call_args = mock_store.append_raw_message.call_args
        assert call_args[0][0] == "task-1"
        msg = call_args[0][1]
        assert msg["type"] == "tool_execution"
        assert msg["detail"]["exit_code"] == 0
        assert msg["detail"]["command"] == "kubectl get pods"
        assert "200" in msg["detail"]["stdout_preview"]

    async def test_persists_on_failure(self, mocker):
        """Failed command execution includes stderr in SessionStore."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"Error: not found"))
        mock_proc.returncode = 1
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        mock_store = MagicMock()
        mocker.patch("chaos_agent.tools.shell.get_global_session_store", return_value=mock_store)

        await run_command(
            ["blade", "create"], task_id="task-2", skip_guard=True,
        )

        mock_store.append_raw_message.assert_called_once()
        call_args = mock_store.append_raw_message.call_args
        assert call_args[0][0] == "task-2"
        msg = call_args[0][1]
        assert msg["detail"]["exit_code"] == 1
        assert "stderr" in msg["detail"]
        assert "not found" in msg["detail"]["stderr"]

    async def test_persists_on_timeout(self, mocker):
        """Timed-out command is recorded with exit_code=-1 before raising."""
        mock_proc = MagicMock()
        mock_proc.kill = MagicMock()
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", side_effect=asyncio.TimeoutError())

        mock_store = MagicMock()
        mocker.patch("chaos_agent.tools.shell.get_global_session_store", return_value=mock_store)

        with pytest.raises(ToolTimeoutError):
            await run_command(["sleep", "1000"], timeout=1, task_id="task-3", skip_guard=True)

        mock_store.append_raw_message.assert_called_once()
        call_args = mock_store.append_raw_message.call_args
        assert call_args[0][0] == "task-3"
        msg = call_args[0][1]
        assert msg["detail"]["exit_code"] == -1
        assert "timed out" in msg["detail"]["stderr"].lower()

    async def test_no_persist_when_no_task_id(self, mocker):
        """No SessionStore write when task_id is empty."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        mock_store = MagicMock()
        mocker.patch("chaos_agent.tools.shell.get_global_session_store", return_value=mock_store)

        await run_command(["echo", "hello"], skip_guard=True)

        mock_store.append_raw_message.assert_not_called()

    async def test_no_error_when_store_missing(self, mocker):
        """No error when global SessionStore is None."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())
        mocker.patch("chaos_agent.tools.shell.get_global_session_store", return_value=None)

        # Should not raise
        result = await run_command(["echo", "hello"], task_id="task-1", skip_guard=True)
        assert result.exit_code == 0

    async def test_no_error_when_append_raises(self, mocker):
        """No error when SessionStore.append_raw_message raises an exception."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_proc.returncode = 0
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
        mocker.patch("asyncio.wait_for", return_value=await mock_proc.communicate())

        mock_store = MagicMock()
        mock_store.append_raw_message.side_effect = RuntimeError("store down")
        mocker.patch("chaos_agent.tools.shell.get_global_session_store", return_value=mock_store)

        result = await run_command(["echo", "hello"], task_id="task-1", skip_guard=True)
        assert result.exit_code == 0
        mock_store.append_raw_message.assert_called_once()
