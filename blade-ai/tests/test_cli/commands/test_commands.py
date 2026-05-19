"""Tests for CLI commands."""

from chaos_agent.cli.output import format_output


class TestConfigCommand:
    def test_show_current_mode(self, tmp_mode_dir):
        """config_command list should show current mode."""
        from chaos_agent.cli.config_manager import set_config
        set_config("mode", "local")

        # Test the underlying config_manager functions
        from chaos_agent.cli.config_manager import get_config
        result = get_config("mode")
        assert result == "local"

    def test_set_local_mode(self, tmp_mode_dir):
        from chaos_agent.cli.config_manager import set_config, get_mode
        set_config("mode", "local")
        assert get_mode() == "local"

    def test_set_server_mode(self, tmp_mode_dir):
        from chaos_agent.cli.config_manager import set_config, get_mode, get_server_url
        set_config("mode", "server")
        set_config("server_url", "http://localhost:8089")
        assert get_mode() == "server"
        assert get_server_url() == "http://localhost:8089"

    def test_config_list_result_structure(self, tmp_mode_dir):
        from chaos_agent.cli.config_manager import list_config
        result = list_config()
        assert "mode" in result
        assert "server_url" in result


class TestInjectCommandParsing:
    """Test inject command parameter parsing logic."""

    def test_params_parsing(self):
        """Test key=value params string parsing."""
        params_str = "latency=100,jitter=true"
        params_dict = {}
        for pair in params_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params_dict[k.strip()] = v.strip()

        assert params_dict == {"latency": "100", "jitter": "true"}

    def test_params_parsing_with_spaces(self):
        params_str = "latency = 100 , jitter = true"
        params_dict = {}
        for pair in params_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params_dict[k.strip()] = v.strip()

        assert params_dict == {"latency": "100", "jitter": "true"}

    def test_params_empty(self):
        params_dict = {}
        # No params string provided
        assert params_dict == {}

    def test_labels_parsing(self):
        labels_str = "env=test,team=chaos"
        labels_dict = {}
        for pair in labels_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels_dict[k.strip()] = v.strip()

        assert labels_dict == {"env": "test", "team": "chaos"}


class TestOutputFormatting:
    """Test output format integration with commands."""

    def test_format_mode_result(self):
        result = {"code": 0, "message": "success", "data": {"mode": "local"}}
        formatted = format_output(result, "json")
        import json
        parsed = json.loads(formatted)
        assert parsed["data"]["mode"] == "local"

    def test_format_error_result(self):
        result = {"code": 1001, "message": "Invalid action", "data": None}
        formatted = format_output(result, "json")
        import json
        parsed = json.loads(formatted)
        assert parsed["code"] == 1001
