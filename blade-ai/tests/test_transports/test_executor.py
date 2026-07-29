"""Tests for execute_via_transport unified executor."""
from unittest.mock import MagicMock, patch

import pytest

from chaos_agent.errors import ToolGuardError
from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.guard_feedback import GuardFeedback
from chaos_agent.transports.base import TransportTarget
from chaos_agent.transports.executor import display_via_transport, execute_via_transport


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset registry before each test to avoid cross-test contamination."""
    from chaos_agent.transports.registry import TransportRegistry
    TransportRegistry._channels = {}
    yield
    TransportRegistry._channels = {}


class TestExecuteViaTransport:
    """Tests for execute_via_transport()."""

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_guard_check_called_with_raw_cmd(self, mock_run, mock_guard):
        """The command guard must be consulted with the raw command, not wrapped.

        Since the gateway migration the executor funnels command safety through
        ``GuardGateway.check_command`` → ``ToolGuard.evaluate`` (unified
        GuardFeedback), so the guard is invoked via ``evaluate``."""
        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "ok", "")

        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with patch("os.path.isfile", return_value=True):
            await execute_via_transport(
                ["kubectl", "get", "pods"], target, task_id="t1"
            )

        guard.evaluate.assert_called_once_with(["kubectl", "get", "pods"])

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    async def test_guard_rejects_raises(self, mock_guard):
        """If the guard rejects, ToolGuardError is raised."""
        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(
            allowed=False, reason="Command not allowed: rm",
        )
        mock_guard.return_value = guard

        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with pytest.raises(ToolGuardError):
            await execute_via_transport(["rm", "-rf", "/"], target)

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_skip_guard_bypasses_check_and_audit(self, mock_run, mock_guard):
        """skip_guard=True must skip both the guard check and guard.audit_log
        (restores the old run_command(skip_guard=True) semantics for internal
        feasibility probes)."""
        guard = MagicMock()
        # Would block if consulted — proves the check is truly bypassed.
        guard.evaluate.return_value = GuardFeedback(allowed=False, reason="would-block")
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "ok", "")

        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with patch("os.path.isfile", return_value=True):
            result = await execute_via_transport(
                ["kubectl", "top", "nodes"], target, skip_guard=True
            )

        assert result.exit_code == 0
        guard.evaluate.assert_not_called()
        guard.audit_log.assert_not_called()

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_resolve_valueerror_returns_preflight_failed(self, mock_run, mock_guard):
        """An unknown channel_override makes resolve() raise ValueError; the
        executor must convert it into a clean PREFLIGHT_FAILED result instead
        of letting the traceback escape."""
        from chaos_agent.transports.executor import PREFLIGHT_FAILED_EXIT_CODE

        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        mock_guard.return_value = guard

        target = TransportTarget(scope="k8s", channel_override="kubewiz")  # deprecated/unknown
        result = await execute_via_transport(["kubectl", "get", "pods"], target)

        assert result.exit_code == PREFLIGHT_FAILED_EXIT_CODE
        assert "kubewiz" in result.stderr
        mock_run.assert_not_called()  # never reached execution

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_bypass_channel_runs_unwrapped(self, mock_run, mock_guard):
        """bypass_channel=True must skip wrap/adapt and run the raw command
        locally, even when the target would otherwise resolve to a wiz-wrapping
        channel (blade reaches KubeWiz Core natively via --kubewiz-url)."""
        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "raw-out", "")

        # Target would resolve to kubewiz_k8s (wiz wrapping) if not bypassed.
        target = TransportTarget(
            scope="k8s", channel_override="kubewiz_k8s",
            kubewiz_cluster_uuid="uuid-1", kubewiz_profile="prof-1",
        )
        result = await execute_via_transport(
            ["blade", "destroy", "abc", "--kubewiz-url", "http://x"],
            target, bypass_channel=True,
        )

        # run_command received the RAW command, not `wiz task exec ...`.
        sent_cmd = mock_run.call_args[0][0]
        assert sent_cmd[0] == "blade"
        assert "wiz" not in sent_cmd
        # Passthrough result (no wiz protocol adaptation).
        assert result.stdout == "raw-out"
        # Guard check + audit still apply on the bypass path.
        guard.evaluate.assert_called_once()
        guard.audit_log.assert_called_once()

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_skip_guard_passed_to_run_command(self, mock_run, mock_guard):
        """run_command must be called with skip_guard=True."""
        guard = MagicMock()
        guard.check.return_value = (True, "OK")
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "ok", "")

        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with patch("os.path.isfile", return_value=True):
            await execute_via_transport(
                ["kubectl", "get", "pods"], target, task_id="t1"
            )

        _, kwargs = mock_run.call_args
        assert kwargs.get("skip_guard") is True

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_task_id_propagated(self, mock_run, mock_guard):
        """task_id must be forwarded to run_command."""
        guard = MagicMock()
        guard.check.return_value = (True, "OK")
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "ok", "")

        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with patch("os.path.isfile", return_value=True):
            await execute_via_transport(
                ["kubectl", "get", "pods"], target, task_id="task-123"
            )

        _, kwargs = mock_run.call_args
        assert kwargs.get("task_id") == "task-123"

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_audit_log_called_with_raw_cmd(self, mock_run, mock_guard):
        """audit_log must be called with the raw command and final result."""
        guard = MagicMock()
        guard.check.return_value = (True, "OK")
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "output", "")

        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with patch("os.path.isfile", return_value=True):
            await execute_via_transport(
                ["kubectl", "get", "pods"], target, task_id="t1"
            )

        guard.audit_log.assert_called_once()
        call_args = guard.audit_log.call_args
        # First positional arg should be the raw cmd
        assert call_args[0][0] == ["kubectl", "get", "pods"]

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_audit_true_records_a_guard_skipping_call(
        self, mock_run, mock_guard,
    ):
        """``audit`` must be decidable independently of ``skip_guard``.

        The two were fused, so every guard-skipping call fell off the audit
        trail. That is right for internal probes but wrong for the LLM-facing
        host tools, which skip the guard only because the diag binaries sit
        outside ``ALLOWED_COMMANDS`` — not because they are internal.
        """
        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=False, reason="would-block")
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "ok", "")

        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with patch("os.path.isfile", return_value=True):
            await execute_via_transport(
                ["df", "-h"], target, skip_guard=True, audit=True,
            )

        guard.evaluate.assert_not_called()   # still skipped
        guard.audit_log.assert_called_once()  # but on the record
        assert guard.audit_log.call_args[0][0] == ["df", "-h"]

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_audit_false_silences_a_guarded_call(self, mock_run, mock_guard):
        """The override works in the other direction too."""
        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, "ok", "")

        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with patch("os.path.isfile", return_value=True):
            await execute_via_transport(
                ["kubectl", "get", "pods"], target, audit=False,
            )

        guard.evaluate.assert_called_once()
        guard.audit_log.assert_not_called()

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_kubewiz_k8s_wraps_and_adapts(self, mock_run, mock_guard):
        """kubewiz_k8s channel wraps cmd and parses wiz output."""
        guard = MagicMock()
        guard.check.return_value = (True, "OK")
        mock_guard.return_value = guard
        # Simulate wiz output
        mock_run.return_value = CommandResult(
            0, "exit_code: 0\npod/nginx Running", ""
        )

        target = TransportTarget(
            scope="k8s", kubewiz_cluster_uuid="uuid", kubewiz_profile="prof"
        )
        result = await execute_via_transport(
            ["kubectl", "get", "pods"], target, task_id="t1"
        )

        # wiz output should be parsed
        assert result.exit_code == 0
        assert result.stdout == "pod/nginx Running"
        # run_command should have received the wrapped wiz command
        wrapped = mock_run.call_args[0][0]
        assert "wiz" in str(wrapped) or "task" in str(wrapped)

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    async def test_preflight_failure_returns_error(self, mock_guard):
        """Preflight failure should return CommandResult with errors."""
        guard = MagicMock()
        guard.check.return_value = (True, "OK")
        mock_guard.return_value = guard

        # kubewiz_k8s with cluster_uuid but missing profile → preflight fails
        target = TransportTarget(
            scope="k8s", kubewiz_cluster_uuid="uuid-1", kubewiz_profile=""
        )
        result = await execute_via_transport(
            ["kubectl", "get", "pods"], target
        )
        assert result.exit_code == -1
        assert "profile" in result.stderr.lower()


class TestExecuteViaTransportRealGuard:
    """End-to-end tests using the REAL ToolGuard (not mocked) to verify
    that dangerous commands are rejected before any transport wrapping."""

    @pytest.mark.asyncio
    async def test_dd_write_block_device_rejected(self):
        """execute_via_transport must reject dd of=/dev/sda via real guard."""
        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with pytest.raises(ToolGuardError):
            await execute_via_transport(
                ["dd", "if=/dev/zero", "of=/dev/sda", "bs=1M", "count=100"],
                target,
            )

    @pytest.mark.asyncio
    async def test_fio_write_block_device_rejected(self):
        """execute_via_transport must reject fio --filename=/dev/sda via real guard."""
        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with pytest.raises(ToolGuardError):
            await execute_via_transport(
                ["fio", "--filename=/dev/sda", "--rw=write"],
                target,
            )

    @pytest.mark.asyncio
    async def test_rm_rf_rejected(self):
        """execute_via_transport must reject rm -rf via real guard."""
        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with pytest.raises(ToolGuardError):
            await execute_via_transport(
                ["blade", "create", "rm -rf /"],
                target,
            )


class TestDisplayViaTransport:
    def test_kubeconfig_display(self):
        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        with patch("os.path.isfile", return_value=True):
            s = display_via_transport(["kubectl", "get", "pods"], target)
        assert s.startswith("kubectl get pods")
        assert "kubeconfig" in s

    def test_kubewiz_k8s_strips_wrapper(self):
        target = TransportTarget(scope="k8s", kubewiz_cluster_uuid="u")
        wrapped = ["wiz", "task", "exec", "--command", "kubectl get pods",
                    "--cluster-uuid", "u", "--profile", "p"]
        s = display_via_transport(wrapped, target)
        assert s.startswith("kubectl get pods")
        assert "kubewiz_k8s" in s and "cluster u" in s


class TestProfileGate:
    """``expect_profile`` must refuse a cross-profile command BEFORE dispatch.

    Regression guard for task-46317228: a host-profile ``uptime`` travelled the
    ``kubewiz_k8s`` channel, which addresses a CLUSTER (``--cluster-uuid``, no
    ``--name``). KubeWiz ran it on its own executor pod and returned
    ``load average 0.02`` — a successful-looking answer from an unrelated
    machine. Nothing failed, so nothing warned. Refusing beats returning wrong
    data, and the refusal must happen before the command is wrapped so it is
    never sent anywhere.
    """

    @staticmethod
    def _k8s_target():
        return TransportTarget(scope="k8s", kubewiz_cluster_uuid="uuid-1",
                               kubewiz_profile="p1", channel_override="kubewiz_k8s")

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_host_command_on_k8s_channel_is_refused_before_dispatch(
        self, mock_run, mock_guard
    ):
        from chaos_agent.transports.executor import PROFILE_MISMATCH_EXIT_CODE
        from chaos_agent.transports.registry import TransportRegistry

        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        mock_guard.return_value = guard
        TransportRegistry._ensure_default()

        channel = TransportRegistry._channels["kubewiz_k8s"]
        with patch.object(channel, "wrap_command", side_effect=AssertionError("dispatched!")) as wrap:
            result = await execute_via_transport(
                ["uptime"], self._k8s_target(), expect_profile="host", skip_guard=True,
            )

        assert result.exit_code == PROFILE_MISMATCH_EXIT_CODE
        assert wrap.call_count == 0, "must not wrap/dispatch a refused command"
        assert mock_run.call_count == 0, "must not execute a refused command"
        # The message has to name the expectation, the actual channel and a way out.
        assert "'host' profile" in result.stderr
        assert "kubewiz_k8s" in result.stderr
        assert "ssh / kubewiz_host" in result.stderr

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_matching_profile_executes_normally(self, mock_run, mock_guard):
        from chaos_agent.transports.executor import PROFILE_MISMATCH_EXIT_CODE
        from chaos_agent.transports.registry import TransportRegistry

        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, '{"code":200,"result":"ok"}', "")
        TransportRegistry._ensure_default()

        result = await execute_via_transport(
            ["kubectl", "get", "pods"], self._k8s_target(),
            expect_profile="k8s", skip_guard=True,
        )

        # The property under test is "not refused, actually dispatched" — the
        # inner exit code depends on wiz protocol parsing, which is covered
        # elsewhere.
        assert result.exit_code != PROFILE_MISMATCH_EXIT_CODE
        assert mock_run.call_count == 1

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_empty_expect_profile_skips_the_gate(self, mock_run, mock_guard):
        """Unannotated call sites must keep their existing behaviour."""
        from chaos_agent.transports.executor import PROFILE_MISMATCH_EXIT_CODE
        from chaos_agent.transports.registry import TransportRegistry

        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        mock_guard.return_value = guard
        mock_run.return_value = CommandResult(0, '{"code":200,"result":"ok"}', "")
        TransportRegistry._ensure_default()

        result = await execute_via_transport(
            ["uptime"], self._k8s_target(), skip_guard=True,
        )

        assert result.exit_code != PROFILE_MISMATCH_EXIT_CODE
        assert mock_run.call_count == 1

    @pytest.mark.asyncio
    @patch("chaos_agent.tools.shell.get_tool_guard")
    @patch("chaos_agent.tools.shell.run_command")
    async def test_bypass_channel_is_also_gated(self, mock_run, mock_guard):
        """``bypass_channel`` skips the wrapper, not the profile semantics.

        blade in kubewiz mode reaches KubeWiz Core through its own flags, but a
        host-shell command still reaches nothing useful that way.
        """
        from chaos_agent.transports.executor import PROFILE_MISMATCH_EXIT_CODE
        from chaos_agent.transports.registry import TransportRegistry

        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        mock_guard.return_value = guard
        TransportRegistry._ensure_default()

        result = await execute_via_transport(
            ["uptime"], self._k8s_target(), expect_profile="host",
            bypass_channel=True, skip_guard=True,
        )

        assert result.exit_code == PROFILE_MISMATCH_EXIT_CODE
        assert mock_run.call_count == 0
