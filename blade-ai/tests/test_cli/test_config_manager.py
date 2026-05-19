"""Tests for CLI config_manager."""

import json

from chaos_agent.cli.config_manager import (
    get_config,
    get_mode,
    get_server_url,
    set_config,
    list_config,
    LOCAL,
    SERVER,
)


class TestGetMode:
    def test_default_is_local(self, tmp_mode_dir):
        result = get_mode()
        assert result == LOCAL

    def test_returns_local_when_no_file(self, tmp_mode_dir):
        result = get_mode()
        assert result == LOCAL


class TestSetConfig:
    def test_set_mode_local(self, tmp_mode_dir):
        set_config("mode", LOCAL)
        assert get_mode() == LOCAL

    def test_set_mode_server(self, tmp_mode_dir):
        set_config("mode", SERVER)
        set_config("server_url", "http://localhost:8089")
        assert get_mode() == SERVER
        assert get_server_url() == "http://localhost:8089"

    def test_set_string_value(self, tmp_mode_dir):
        set_config("model_name", "test-model")
        assert get_config("model_name") == "test-model"

    def test_set_preserves_other_keys(self, tmp_mode_dir):
        set_config("mode", LOCAL)
        set_config("model_name", "test-model")
        # model_name should still be there
        assert get_config("model_name") == "test-model"
        # mode should still be there
        assert get_config("mode") == LOCAL

    def test_set_integer_value(self, tmp_mode_dir):
        set_config("server_port", 9999)
        assert get_config("server_port") == 9999


class TestGetServerUrl:
    def test_returns_none_in_local_mode(self, tmp_mode_dir):
        set_config("mode", LOCAL)
        set_config("server_url", None)
        result = get_server_url()
        assert result is None

    def test_returns_url_in_server_mode(self, tmp_mode_dir):
        set_config("mode", SERVER)
        set_config("server_url", "http://my-host:8089")
        result = get_server_url()
        assert result == "http://my-host:8089"


class TestListConfig:
    def test_returns_defaults_when_empty(self, tmp_mode_dir):
        result = list_config()
        assert "mode" in result
        assert result["mode"] == LOCAL

    def test_returns_set_values(self, tmp_mode_dir):
        set_config("model_name", "glm-5.1")
        result = list_config()
        assert result["model_name"] == "glm-5.1"


class TestSwitchModes:
    def test_switch_server_to_local(self, tmp_mode_dir):
        set_config("mode", SERVER)
        set_config("server_url", "http://host:8089")
        assert get_mode() == SERVER

        set_config("mode", LOCAL)
        set_config("server_url", None)
        assert get_mode() == LOCAL
        assert get_server_url() is None

    def test_switch_local_to_server(self, tmp_mode_dir):
        set_config("mode", LOCAL)
        assert get_mode() == LOCAL

        set_config("mode", SERVER)
        set_config("server_url", "http://new-host:9999")
        assert get_mode() == SERVER
        assert get_server_url() == "http://new-host:9999"


class TestMigration:
    def test_migrates_mode_json(self, tmp_mode_dir):
        """If config.json doesn't exist but mode.json does, migrate it."""
        # Create a legacy mode.json
        tmp_mode_dir.mkdir(parents=True, exist_ok=True)
        mode_file = tmp_mode_dir / "mode.json"
        mode_file.write_text(json.dumps({"mode": "server", "server_url": "http://old:8089"}), encoding="utf-8")

        # Reading config should trigger migration
        from chaos_agent.cli.config_manager import _read
        data = _read()
        assert data["mode"] == "server"
        assert data["server_url"] == "http://old:8089"
        # mode.json should be deleted
        assert not mode_file.exists()
        # config.json should exist now
        config_file = tmp_mode_dir / "config.json"
        assert config_file.exists()
