"""Tests for the public ``blade-ai serve`` command.

The GitHub release ships a single PyInstaller binary (``blade-ai``); the
``blade-ai-server`` console script exists only for pip installs. ``serve`` is
what makes hosting the API possible from a release artifact, so these tests pin
its registration, visibility and delegation contract.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from chaos_agent.cli.main import app

runner = CliRunner()


class TestServeCommandSurface:
    def test_serve_is_visible_in_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "serve" in result.output

    def test_embedded_server_stays_hidden(self):
        """The TUI bridge command must not leak into the user-facing help."""
        result = runner.invoke(app, ["--help"])
        assert "__embedded_server__" not in result.output

    def test_embedded_server_still_invocable(self):
        """Hidden ≠ removed: the TS TUI spawns it by name in frozen mode."""
        with patch("chaos_agent.server.app.run_server") as run_server:
            result = runner.invoke(app, ["__embedded_server__", "--port", "0"])
        assert result.exit_code == 0
        assert run_server.call_args.kwargs["port"] == 0


class TestServeDelegation:
    def test_defaults_defer_to_settings(self):
        """No flags → host/port stay None so BLADE_AI_SERVER_* remain
        authoritative (same resolution as the blade-ai-server script)."""
        with patch("chaos_agent.server.app.run_server") as run_server:
            result = runner.invoke(app, ["serve"])

        assert result.exit_code == 0
        assert run_server.call_args.kwargs == {
            "host": None, "port": None, "ready_stdout": False,
        }

    def test_flags_override(self):
        with patch("chaos_agent.server.app.run_server") as run_server:
            result = runner.invoke(app, [
                "serve", "--host", "127.0.0.1", "--port", "9000",
                "--ready-stdout",
            ])

        assert result.exit_code == 0
        assert run_server.call_args.kwargs == {
            "host": "127.0.0.1", "port": 9000, "ready_stdout": True,
        }
