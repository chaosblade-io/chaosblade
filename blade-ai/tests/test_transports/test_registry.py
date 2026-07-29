"""Tests for TransportRegistry register/resolve logic."""
from unittest.mock import MagicMock, patch

import pytest

from chaos_agent.transports.base import TransportTarget
from chaos_agent.transports.channels import (
    KubeconfigChannel,
    KubewizHostChannel,
    KubewizK8sChannel,
    SSHChannel,
)
from chaos_agent.transports.registry import (
    PROFILE_UNKNOWN,
    TransportRegistry,
    is_host_scope_channel,
    is_kubewiz_channel,
    profile_of,
    resolve_channel_name,
)


class TestTransportRegistryResolve:
    def test_resolve_k8s_kubeconfig(self):
        """k8s scope with no kubewiz_cluster_uuid → KubeconfigChannel."""
        target = TransportTarget(scope="k8s", kubeconfig="/tmp/kc")
        ch = TransportRegistry.resolve(target)
        assert isinstance(ch, KubeconfigChannel)

    def test_resolve_k8s_kubewiz(self):
        """k8s scope with kubewiz_cluster_uuid → KubewizK8sChannel."""
        target = TransportTarget(scope="k8s", kubewiz_cluster_uuid="uuid-1")
        ch = TransportRegistry.resolve(target)
        assert isinstance(ch, KubewizK8sChannel)

    def test_resolve_host_kubewiz(self):
        """host scope with host_name → KubewizHostChannel."""
        target = TransportTarget(scope="host", host_name="10.0.0.1")
        ch = TransportRegistry.resolve(target)
        assert isinstance(ch, KubewizHostChannel)

    def test_resolve_host_ssh(self):
        """host scope with ssh_host (no host_name) → SSHChannel."""
        target = TransportTarget(scope="host", ssh_host="10.0.0.2")
        ch = TransportRegistry.resolve(target)
        assert isinstance(ch, SSHChannel)

    def test_resolve_host_no_params_raises(self):
        """host scope with neither host_name nor ssh_host → ValueError."""
        target = TransportTarget(scope="host")
        with pytest.raises(ValueError, match="host scope requires"):
            TransportRegistry.resolve(target)

    def test_resolve_unsupported_scope_raises(self):
        target = TransportTarget(scope="invalid")
        with pytest.raises(ValueError, match="unsupported scope"):
            TransportRegistry.resolve(target)


class TestTransportRegistryChannelOverride:
    """Explicit channel_override takes precedence over field inference."""

    def test_override_kubeconfig(self):
        target = TransportTarget(channel_override="kubeconfig")
        assert isinstance(TransportRegistry.resolve(target), KubeconfigChannel)

    def test_override_kubewiz_k8s(self):
        target = TransportTarget(channel_override="kubewiz_k8s")
        assert isinstance(TransportRegistry.resolve(target), KubewizK8sChannel)

    def test_override_kubewiz_host(self):
        target = TransportTarget(channel_override="kubewiz_host")
        assert isinstance(TransportRegistry.resolve(target), KubewizHostChannel)

    def test_override_ssh(self):
        target = TransportTarget(channel_override="ssh")
        assert isinstance(TransportRegistry.resolve(target), SSHChannel)

    def test_override_wins_over_field_inference(self):
        """Override forces kubeconfig even when kubewiz_cluster_uuid is set."""
        target = TransportTarget(
            scope="k8s", kubewiz_cluster_uuid="uuid-1", channel_override="kubeconfig"
        )
        assert isinstance(TransportRegistry.resolve(target), KubeconfigChannel)

    def test_override_selects_kubewiz_without_uuid(self):
        """Override can select kubewiz_k8s even with no uuid configured."""
        target = TransportTarget(scope="k8s", channel_override="kubewiz_k8s")
        assert isinstance(TransportRegistry.resolve(target), KubewizK8sChannel)

    def test_unknown_override_raises(self):
        target = TransportTarget(channel_override="aaa")
        with pytest.raises(ValueError, match="unknown channel_override"):
            TransportRegistry.resolve(target)

    def test_empty_override_falls_back_to_inference(self):
        """Empty override → normal field-based inference."""
        target = TransportTarget(scope="k8s", kubewiz_cluster_uuid="uuid-1", channel_override="")
        assert isinstance(TransportRegistry.resolve(target), KubewizK8sChannel)


class TestTransportRegistryGet:
    def test_get_by_name(self):
        ch = TransportRegistry.get("kubeconfig")
        assert ch.name == "kubeconfig"

    def test_get_kubewiz_k8s(self):
        ch = TransportRegistry.get("kubewiz_k8s")
        assert ch.name == "kubewiz_k8s"

    def test_get_kubewiz_host(self):
        ch = TransportRegistry.get("kubewiz_host")
        assert ch.name == "kubewiz_host"

    def test_get_ssh(self):
        ch = TransportRegistry.get("ssh")
        assert ch.name == "ssh"


class TestTransportRegistryRegister:
    def test_register_custom_channel(self):
        """Registering a custom channel should make it retrievable."""

        class CustomChannel:
            @property
            def name(self):
                return "custom"

            def wrap_command(self, cmd, target, timeout=None):
                return cmd

            def adapt_result(self, result, target):
                return result

            def preflight(self, target):
                return []

            def display_command(self, cmd):
                return " ".join(cmd)

        TransportRegistry.register(CustomChannel())
        try:
            assert TransportRegistry.get("custom").name == "custom"
        finally:
            # ``_channels`` is class-level, so leaving this behind pollutes
            # every later test in the session: ``resolve`` would consult a
            # channel that implements neither ``claims`` nor ``profile``.
            TransportRegistry._channels.pop("custom", None)


class TestResolveChannelNameHelper:
    """resolve_channel_name / is_kubewiz_channel centralize resolve + degrade."""

    def test_resolve_channel_name_returns_channel_name(self):
        mock_ch = MagicMock()
        mock_ch.name = "kubewiz_k8s"
        with patch.object(TransportRegistry, "resolve", return_value=mock_ch):
            assert resolve_channel_name() == "kubewiz_k8s"

    def test_resolve_channel_name_is_unknown_on_valueerror(self):
        with patch.object(
            TransportRegistry, "resolve",
            side_effect=ValueError("host scope requires host_name or ssh_host"),
        ):
            assert resolve_channel_name() == PROFILE_UNKNOWN

    def test_resolve_channel_name_forwards_state(self):
        captured = {}

        def _fake_resolve(target):
            captured["override"] = target.channel_override
            mock_ch = MagicMock()
            mock_ch.name = "ssh"
            return mock_ch

        with patch.object(TransportRegistry, "resolve", side_effect=_fake_resolve):
            name = resolve_channel_name({"kube_connection_mode": "ssh", "ssh_host": "h"})
        assert name == "ssh"
        assert captured["override"] == "ssh"

    @pytest.mark.parametrize(
        "channel_name,expected",
        [
            ("kubewiz_k8s", True),
            ("kubewiz_host", True),
            ("kubeconfig", False),
            ("ssh", False),
        ],
    )
    def test_is_kubewiz_channel(self, channel_name, expected):
        mock_ch = MagicMock()
        mock_ch.name = channel_name
        with patch.object(TransportRegistry, "resolve", return_value=mock_ch):
            assert is_kubewiz_channel() is expected

    def test_is_kubewiz_channel_degrades_to_false(self):
        with patch.object(
            TransportRegistry, "resolve", side_effect=ValueError("bad")
        ):
            assert is_kubewiz_channel() is False


class TestHostScopeChannelHelper:
    """is_host_scope_channel is True for ssh / kubewiz_host, False otherwise."""

    @pytest.mark.parametrize(
        "channel_name,expected",
        [
            ("ssh", True),
            ("kubewiz_host", True),
            ("kubeconfig", False),
            ("kubewiz_k8s", False),
        ],
    )
    def test_is_host_scope_channel(self, channel_name, expected):
        mock_ch = MagicMock()
        mock_ch.name = channel_name
        with patch.object(TransportRegistry, "resolve", return_value=mock_ch):
            assert is_host_scope_channel() is expected

    def test_is_host_scope_channel_degrades_to_false(self):
        """resolve() failure degrades to kubeconfig -> not a host scope."""
        with patch.object(
            TransportRegistry, "resolve", side_effect=ValueError("bad")
        ):
            assert is_host_scope_channel() is False


class TestProfileOf:
    """profile_of maps channel -> capability profile."""

    @pytest.mark.parametrize(
        "channel_name,expected",
        [
            ("kubeconfig", "k8s"),
            ("kubewiz_k8s", "k8s"),
            ("ssh", "host"),
            ("kubewiz_host", "host"),
        ],
    )
    def test_known_channels(self, channel_name, expected):
        assert profile_of(channel_name) == expected

    def test_unknown_channel_is_explicit(self):
        assert profile_of("totally-unknown") == "unknown"
