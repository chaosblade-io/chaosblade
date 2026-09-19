"""Tests for chaos_agent.mcp.config — mcp.json parsing + validation."""

import json
from pathlib import Path

import pytest

from chaos_agent.mcp.config import (
    McpConfigError,
    McpServerConfig,
    _interpolate,
    load_mcp_config,
)


class TestEnvInterpolation:
    def test_simple_substitution(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "abc123")
        assert _interpolate("Bearer ${MY_TOKEN}", {}) == "Bearer abc123"

    def test_local_env_wins_over_os(self, monkeypatch):
        monkeypatch.setenv("V", "from-os")
        assert _interpolate("${V}", {"V": "from-local"}) == "from-local"

    def test_missing_var_raises(self, monkeypatch):
        monkeypatch.delenv("NOT_DEFINED_VAR", raising=False)
        with pytest.raises(McpConfigError, match="NOT_DEFINED_VAR"):
            _interpolate("${NOT_DEFINED_VAR}", {})

    def test_multiple_references(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _interpolate("${A}-${B}-${A}", {}) == "1-2-1"

    def test_no_references_returns_unchanged(self):
        assert _interpolate("plain", {}) == "plain"


class TestLoadMcpConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        configs = load_mcp_config(tmp_path / "nonexistent.json")
        assert configs == []

    def test_stdio_server(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "fs": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "attach_to": ["clarification"],
                }
            }
        }))
        configs = load_mcp_config(path)
        assert len(configs) == 1
        c = configs[0]
        assert c.name == "fs"
        assert c.transport == "stdio"
        assert c.command == "npx"
        assert c.args == ("-y", "@modelcontextprotocol/server-filesystem", "/tmp")
        assert c.attach_to == ("clarification",)
        assert c.enabled is True
        assert c.timeout_seconds == 30

    def test_http_server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret")
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "docs": {
                    "transport": "http",
                    "url": "https://mcp.example.com/v1",
                    "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
                    "attach_to": ["phase1", "verifier"],
                    "timeout_seconds": 60,
                }
            }
        }))
        configs = load_mcp_config(path)
        assert len(configs) == 1
        c = configs[0]
        assert c.transport == "http"
        assert c.url == "https://mcp.example.com/v1"
        assert c.headers == {"Authorization": "Bearer secret"}
        assert c.timeout_seconds == 60
        assert set(c.attach_to) == {"phase1", "verifier"}

    def test_sse_server_legacy(self, tmp_path):
        """Legacy HTTP+SSE remains a valid transport value so blade-ai
        can still reach older SSE-only servers."""
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "legacy": {
                    "transport": "sse",
                    "url": "https://old.example.com/sse",
                }
            }
        }))
        configs = load_mcp_config(path)
        assert len(configs) == 1
        assert configs[0].transport == "sse"
        assert configs[0].url == "https://old.example.com/sse"

    def test_disabled_servers_omitted(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "active": {"command": "x", "args": [], "enabled": True},
                "off": {"command": "y", "args": [], "enabled": False},
            }
        }))
        configs = load_mcp_config(path)
        names = [c.name for c in configs]
        assert "active" in names
        assert "off" not in names

    def test_invalid_server_skipped_others_kept(self, tmp_path):
        """A broken server should be logged + skipped; valid siblings
        must still load. Critical isolation property."""
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "good": {"command": "ok", "args": []},
                "missing_command": {"transport": "stdio"},  # no command
                "bad_transport": {"transport": "websocket"},
                "bad_phase": {"command": "x", "attach_to": ["wrong_phase"]},
            }
        }))
        configs = load_mcp_config(path)
        names = [c.name for c in configs]
        assert names == ["good"]

    def test_attach_to_empty_default(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"fs": {"command": "x"}}
        }))
        configs = load_mcp_config(path)
        assert configs[0].attach_to == ()

    def test_attach_to_validation(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"fs": {"command": "x", "attach_to": ["unknown_phase"]}}
        }))
        configs = load_mcp_config(path)
        assert configs == []  # rejected with warning

    def test_cwd_supported(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"fs": {"command": "x", "cwd": "/srv/fs"}}
        }))
        configs = load_mcp_config(path)
        assert configs[0].cwd == "/srv/fs"

    def test_env_interpolation_in_args(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_DIR", "/data")
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"fs": {"command": "x", "args": ["--root", "${MY_DIR}"]}}
        }))
        configs = load_mcp_config(path)
        assert configs[0].args == ("--root", "/data")

    def test_env_interpolation_in_command(self, tmp_path, monkeypatch):
        """Regression: ``command`` field must support ${VAR} consistently
        with args/env/headers/url. Without this, a config like
        ``"command": "${NODE_PATH}/npx"`` would try to exec a literal
        string and fail with a confusing FileNotFoundError."""
        monkeypatch.setenv("NODE_BIN", "/opt/node/bin")
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"fs": {"command": "${NODE_BIN}/npx", "args": []}}
        }))
        configs = load_mcp_config(path)
        assert configs[0].command == "/opt/node/bin/npx"

    def test_env_interpolation_in_cwd(self, tmp_path, monkeypatch):
        """Regression: ``cwd`` field must support ${VAR}, same as
        every other user-supplied string field."""
        monkeypatch.setenv("WORK_DIR", "/var/lib/fs")
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"fs": {"command": "x", "cwd": "${WORK_DIR}/run"}}
        }))
        configs = load_mcp_config(path)
        assert configs[0].cwd == "/var/lib/fs/run"

    def test_missing_env_in_command_raises(self, tmp_path, monkeypatch):
        """Missing env var in command must fail loudly at load time —
        same loud-failure rule as other string fields."""
        monkeypatch.delenv("NOT_DEFINED_MCP_VAR", raising=False)
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "fs": {"command": "${NOT_DEFINED_MCP_VAR}/npx", "args": []}
            }
        }))
        configs = load_mcp_config(path)
        # Per _parse_one: invalid server is skipped with a warning, not
        # propagated. Verify the broken server didn't sneak through.
        assert configs == []


class TestToolEffects:
    """Per-tool read/write label override (posture B, advisory only)."""

    def _write(self, tmp_path, server_body):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"s": server_body}}))
        return path

    def test_absent_defaults_to_empty(self, tmp_path):
        path = self._write(tmp_path, {"command": "x", "args": []})
        assert load_mcp_config(path)[0].tool_effects == {}

    def test_valid_effects_parsed_and_lowercased(self, tmp_path):
        path = self._write(tmp_path, {
            "command": "x", "args": [],
            "tool_effects": {"get": "ReadOnly", "cancel": "DESTRUCTIVE"},
        })
        assert load_mcp_config(path)[0].tool_effects == {
            "get": "readonly", "cancel": "destructive",
        }

    def test_url_transport_also_carries_effects(self, tmp_path):
        path = self._write(tmp_path, {
            "transport": "http", "url": "http://x/mcp",
            "tool_effects": {"run": "destructive"},
        })
        assert load_mcp_config(path)[0].tool_effects == {"run": "destructive"}

    def test_invalid_effect_value_dropped_with_warning(self, caplog):
        """Posture B: a bad per-tool label is advisory, so it is DROPPED
        with a warning — NOT fatal. The server still loads and the
        mislabeled tool falls back to annotation/unspecified."""
        from chaos_agent.mcp.config import _parse_one
        with caplog.at_level("WARNING"):
            cfg = _parse_one("s", {
                "command": "x", "args": [],
                "tool_effects": {"get": "maybe"},
            })
        assert cfg.tool_effects == {}            # bad entry dropped
        assert "tool_effects" in caplog.text     # warned, not silent

    def test_mixed_effects_keep_good_drop_bad(self, tmp_path, caplog):
        """Blast radius is the single bad entry: a good label survives
        alongside a bad one, and the whole server is kept."""
        path = self._write(tmp_path, {
            "command": "x", "args": [],
            "tool_effects": {"get": "readonly", "cancel": "nope"},
        })
        with caplog.at_level("WARNING"):
            cfgs = load_mcp_config(path)
        assert len(cfgs) == 1                              # server kept
        assert cfgs[0].tool_effects == {"get": "readonly"}  # good kept, bad dropped
        assert "cancel" in caplog.text                     # warned about the bad one

    def test_non_dict_tool_effects_degrades_with_warning(self, caplog):
        """A structurally wrong tool_effects (e.g. a list) degrades to no
        overrides with a warning, instead of killing the whole server."""
        from chaos_agent.mcp.config import _parse_one
        with caplog.at_level("WARNING"):
            cfg = _parse_one(
                "s", {"command": "x", "args": [], "tool_effects": ["a"]}
            )
        assert cfg.tool_effects == {}
        assert "must be a dict" in caplog.text

    def test_invalid_effect_keeps_server_on_load(self, tmp_path, caplog):
        """Regression flip of the old strict behavior: an invalid effect
        value must NOT drop the whole server. The server still loads,
        minus the bad label, with a warning."""
        path = self._write(tmp_path, {
            "command": "x", "args": [], "tool_effects": {"get": "nope"},
        })
        with caplog.at_level("WARNING"):
            cfgs = load_mcp_config(path)
        assert len(cfgs) == 1               # server survives (was == [])
        assert cfgs[0].tool_effects == {}   # bad label dropped
        assert "tool_effects" in caplog.text
