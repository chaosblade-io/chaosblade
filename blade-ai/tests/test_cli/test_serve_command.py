"""Tests for the public ``blade-ai server`` command.

The GitHub release ships a single PyInstaller binary (``blade-ai``); the
``blade-ai-server`` console script exists only for pip installs. ``server`` is
what makes hosting the API possible from a release artifact, so these tests pin
its registration, visibility and delegation contract.

``serve`` was the original name and is now REMOVED, not aliased: nothing in the
codebase invokes it (the TS TUI spawns ``__embedded_server__`` or ``python -m
chaos_agent.server.app``), so a second binding would only be dead surface. The
last test pins the removal so it cannot creep back.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from chaos_agent.cli.main import app

runner = CliRunner()


class TestServerCommandSurface:
    def test_server_is_visible_in_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "server" in result.output

    def test_help_advertises_exactly_one_name(self):
        """The alias must stay hidden, or --help lists the same command twice.

        Asserted on the whole word: ``"serve" in output`` passes on the substring
        of ``server`` and would therefore pass even if the alias were visible.
        """
        result = runner.invoke(app, ["--help"])
        lines = [ln for ln in result.output.splitlines() if "HTTP API server" in ln]
        assert len(lines) == 1, result.output
        assert "Alias of" not in result.output

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


class TestServerDelegation:
    def test_defaults_defer_to_settings(self):
        """No flags → host/port stay None so BLADE_AI_SERVER_* remain
        authoritative (same resolution as the blade-ai-server script)."""
        with patch("chaos_agent.server.app.run_server") as run_server:
            result = runner.invoke(app, ["server"])

        assert result.exit_code == 0
        assert run_server.call_args.kwargs == {
            "host": None, "port": None, "ready_stdout": False,
        }

    def test_flags_override(self):
        with patch("chaos_agent.server.app.run_server") as run_server:
            result = runner.invoke(app, [
                "server", "--host", "127.0.0.1", "--port", "9000",
                "--ready-stdout",
            ])

        assert result.exit_code == 0
        assert run_server.call_args.kwargs == {
            "host": "127.0.0.1", "port": 9000, "ready_stdout": True,
        }


class TestServeIsGone:
    """The old name must not resolve — neither visibly nor hidden."""

    def test_serve_is_rejected(self):
        with patch("chaos_agent.server.app.run_server") as run_server:
            result = runner.invoke(app, ["serve"])
        assert result.exit_code != 0, "the removed 'serve' command still resolves"
        assert not run_server.called

    def test_registry_holds_no_serve_command(self):
        names = {getattr(c, "name", "") for c in app.registered_commands}
        assert "serve" not in names
        assert "server" in names
