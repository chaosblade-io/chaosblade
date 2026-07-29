"""Tests for TransportTarget construction and TransportChannel protocol."""
from unittest.mock import patch


from chaos_agent.transports.base import TransportChannel, TransportTarget


class TestTransportTargetDefaults:
    """Test TransportTarget default field values."""

    def test_default_scope_is_k8s(self):
        t = TransportTarget()
        assert t.scope == "k8s"

    def test_default_kubeconfig_empty(self):
        t = TransportTarget()
        assert t.kubeconfig == ""

    def test_default_ssh_port_22(self):
        t = TransportTarget()
        assert t.ssh_port == 22


class TestTransportTargetFromState:
    """Test TransportTarget.from_state() construction logic."""

    @patch("chaos_agent.transports.base.settings")
    def test_kubeconfig_mode_from_settings(self, mock_settings):
        """When no kubewiz_cluster_uuid in state or settings, use kubeconfig."""
        mock_settings.kube_connection_mode = ""
        mock_settings.kubewiz_cluster_uuid = ""
        mock_settings.kubewiz_profile = ""
        mock_settings.kubeconfig_path = "/tmp/kubeconfig"
        mock_settings.kube_context = ""
        mock_settings.host_name = ""
        mock_settings.ssh_host = ""
        mock_settings.ssh_user = ""
        mock_settings.ssh_key_path = ""
        mock_settings.ssh_port = 22

        t = TransportTarget.from_state({})
        assert t.scope == "k8s"
        assert t.kubeconfig == "/tmp/kubeconfig"
        assert t.kubewiz_cluster_uuid == ""

    @patch("chaos_agent.transports.base.settings")
    def test_kubewiz_k8s_from_settings(self, mock_settings):
        """When kubewiz_cluster_uuid in settings, use kubewiz_k8s."""
        mock_settings.kube_connection_mode = ""
        mock_settings.kubewiz_cluster_uuid = "cluster-abc"
        mock_settings.kubewiz_profile = "profile-1"
        mock_settings.kubeconfig_path = ""
        mock_settings.kube_context = ""
        mock_settings.host_name = ""
        mock_settings.ssh_host = ""
        mock_settings.ssh_user = ""
        mock_settings.ssh_key_path = ""
        mock_settings.ssh_port = 22

        t = TransportTarget.from_state({})
        assert t.scope == "k8s"
        assert t.kubewiz_cluster_uuid == "cluster-abc"
        assert t.kubewiz_profile == "profile-1"

    @patch("chaos_agent.transports.base.settings")
    def test_state_overrides_settings(self, mock_settings):
        """State fields take priority over settings."""
        mock_settings.kube_connection_mode = "kubeconfig"
        mock_settings.kubewiz_cluster_uuid = ""
        mock_settings.kubewiz_profile = ""
        mock_settings.kubeconfig_path = "/tmp/default"
        mock_settings.kube_context = ""
        mock_settings.host_name = ""
        mock_settings.ssh_host = ""
        mock_settings.ssh_user = ""
        mock_settings.ssh_key_path = ""
        mock_settings.ssh_port = 22

        t = TransportTarget.from_state({"kubeconfig": "/tmp/override"})
        assert t.kubeconfig == "/tmp/override"

    @patch("chaos_agent.transports.base.settings")
    def test_host_scope_with_host_name(self, mock_settings):
        """host_name in state switches scope to host."""
        mock_settings.kube_connection_mode = "kubeconfig"
        mock_settings.kubewiz_cluster_uuid = ""
        mock_settings.kubewiz_profile = "profile-1"
        mock_settings.kubeconfig_path = "/tmp/kc"
        mock_settings.kube_context = ""
        mock_settings.host_name = ""
        mock_settings.ssh_host = ""
        mock_settings.ssh_user = ""
        mock_settings.ssh_key_path = ""
        mock_settings.ssh_port = 22

        t = TransportTarget.from_state({"host_name": "10.0.0.1"})
        assert t.scope == "host"
        assert t.host_name == "10.0.0.1"
        assert t.kubewiz_profile == "profile-1"

    @patch("chaos_agent.transports.base.settings")
    def test_host_scope_with_ssh_host(self, mock_settings):
        """ssh_host in state switches scope to host."""
        mock_settings.kube_connection_mode = "kubeconfig"
        mock_settings.kubewiz_cluster_uuid = ""
        mock_settings.kubewiz_profile = ""
        mock_settings.kubeconfig_path = "/tmp/kc"
        mock_settings.kube_context = ""
        mock_settings.host_name = ""
        mock_settings.ssh_host = ""
        mock_settings.ssh_user = "root"
        mock_settings.ssh_key_path = "/tmp/key"
        mock_settings.ssh_port = 2222

        t = TransportTarget.from_state({"ssh_host": "10.0.0.2"})
        assert t.scope == "host"
        assert t.ssh_host == "10.0.0.2"
        assert t.ssh_user == "root"
        assert t.ssh_key_path == "/tmp/key"
        assert t.ssh_port == 2222

    @patch("chaos_agent.transports.base.settings")
    def test_channel_override_from_settings(self, mock_settings):
        """settings.kube_connection_mode populates channel_override."""
        mock_settings.kube_connection_mode = "ssh"
        mock_settings.kubewiz_cluster_uuid = ""
        mock_settings.kubewiz_profile = ""
        mock_settings.kubeconfig_path = ""
        mock_settings.kube_context = ""
        mock_settings.host_name = ""
        mock_settings.ssh_host = ""
        mock_settings.ssh_user = ""
        mock_settings.ssh_key_path = ""
        mock_settings.ssh_port = 22

        t = TransportTarget.from_state({})
        assert t.channel_override == "ssh"

    @patch("chaos_agent.transports.base.settings")
    def test_channel_override_state_over_settings(self, mock_settings):
        """State kube_connection_mode takes priority over settings."""
        mock_settings.kube_connection_mode = "kubeconfig"
        mock_settings.kubewiz_cluster_uuid = ""
        mock_settings.kubewiz_profile = ""
        mock_settings.kubeconfig_path = ""
        mock_settings.kube_context = ""
        mock_settings.host_name = ""
        mock_settings.ssh_host = ""
        mock_settings.ssh_user = ""
        mock_settings.ssh_key_path = ""
        mock_settings.ssh_port = 22

        t = TransportTarget.from_state({"kube_connection_mode": "kubewiz_k8s"})
        assert t.channel_override == "kubewiz_k8s"

    @patch("chaos_agent.transports.base.settings")
    def test_channel_override_empty_by_default(self, mock_settings):
        """Empty settings/state leaves channel_override empty."""
        mock_settings.kube_connection_mode = ""
        mock_settings.kubewiz_cluster_uuid = ""
        mock_settings.kubewiz_profile = ""
        mock_settings.kubeconfig_path = ""
        mock_settings.kube_context = ""
        mock_settings.host_name = ""
        mock_settings.ssh_host = ""
        mock_settings.ssh_user = ""
        mock_settings.ssh_key_path = ""
        mock_settings.ssh_port = 22

        t = TransportTarget.from_state({})
        assert t.channel_override == ""

    @patch("chaos_agent.transports.base.settings")
    def test_override_still_fills_full_field_superset(self, mock_settings):
        """Regression: an explicit channel_override must NOT stop the full
        connection-field superset from being populated.  Here override says
        'kubeconfig' while settings also carry kubewiz + ssh fields — every
        field must still land on the target so resolve() finds what it needs."""
        mock_settings.kube_connection_mode = "kubeconfig"
        mock_settings.kubewiz_cluster_uuid = "cluster-abc"
        mock_settings.kubewiz_profile = "prof-1"
        mock_settings.kubeconfig_path = "/tmp/kc"
        mock_settings.kube_context = "ctx"
        mock_settings.host_name = "host-1"
        mock_settings.ssh_host = "10.0.0.3"
        mock_settings.ssh_user = "root"
        mock_settings.ssh_key_path = "/tmp/key"
        mock_settings.ssh_port = 2200

        t = TransportTarget.from_state({})
        assert t.channel_override == "kubeconfig"
        assert t.kubeconfig == "/tmp/kc"
        assert t.kube_context == "ctx"
        assert t.kubewiz_cluster_uuid == "cluster-abc"
        assert t.kubewiz_profile == "prof-1"
        assert t.host_name == "host-1"
        assert t.ssh_host == "10.0.0.3"
        assert t.ssh_user == "root"
        assert t.ssh_key_path == "/tmp/key"
        assert t.ssh_port == 2200


class TestTransportChannelProtocol:
    """Test that channel implementations satisfy the Protocol."""

    def test_protocol_is_runtime_checkable(self):
        """TransportChannel should be runtime_checkable."""
        # Just verify it doesn't raise
        assert hasattr(TransportChannel, "_is_protocol")
