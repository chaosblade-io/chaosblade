"""Tests for the public ``blade-ai resume`` command.

Pins the recovery entry's contract: how a sid reaches the TUI process
(argv passthrough), the bare form's list-only behaviour (print + exit,
never reads stdin), and the fail-fast shapes (malformed sid, event-less
sid, remote-mode bare listing). The takeover sequence itself
lives in core (``runSessionResume``) and is covered by the
``commandsResume`` suite — these tests only pin the CLI seam.
"""

import re
from datetime import datetime
from unittest.mock import patch

import os

from typer.testing import CliRunner

from chaos_agent.cli.commands.resume_cmd import (
    _format_last_active,
    _render_session_table,
)
from chaos_agent.cli.main import app

runner = CliRunner()


class _FakeStore:
    """Stand-in for TuiSessionStore — records construction, returns
    canned rows / has_events. The command imports the class lazily
    inside the handler, so patching the module attribute works."""

    rows: list[dict] = []
    has_events_value: bool = True

    def __init__(self, session_dir):
        self.session_dir = session_dir

    def list_resumable_sessions(self, limit: int = 50) -> list[dict]:
        return list(type(self).rows)

    def has_events(self, tui_session_id: str) -> bool:
        return type(self).has_events_value


def _rows(*sids: str) -> list[dict]:
    return [
        {
            "tui_session_id": sid,
            "size_bytes": 1024,
            "event_count": 7,
            "started_at": "2026-09-10T09:12:33",
            "modified_at": 1780000000.0,
            "first_input": f"intent for {sid}",
        }
        for sid in sids
    ]


class TestResumeCommandSurface:
    def test_resume_is_visible_in_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "resume" in result.output


class TestResumeWithSid:
    def test_sid_passthrough_reaches_tui_argv(self):
        """The whole point of the command: ``-i <sid>`` must hand the
        TUI ``--resume <sid>`` so BootRunner takes over that session."""
        _FakeStore.has_events_value = True
        with (
            patch(
                "chaos_agent.memory.tui_session_store.TuiSessionStore",
                _FakeStore,
            ),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(app, ["resume", "-i", "sess-abc123"])
        assert result.exit_code == 0, result.output
        launch.assert_called_once_with(["--resume", "sess-abc123"])

    def test_long_flag_form_matches_short(self):
        _FakeStore.has_events_value = True
        with (
            patch(
                "chaos_agent.memory.tui_session_store.TuiSessionStore",
                _FakeStore,
            ),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(
                app, ["resume", "--session-id", "sess-longform"]
            )
        assert result.exit_code == 0, result.output
        launch.assert_called_once_with(["--resume", "sess-longform"])

    def test_malformed_sid_fails_before_boot(self):
        """A traversal-shaped sid must die at the CLI, not boot the
        whole server first (the route would reject it too — the CLI
        check is the cheaper of the two guards)."""
        with patch("chaos_agent.cli.main._launch_default_tui") as launch:
            result = runner.invoke(app, ["resume", "-i", "bad..sid"])
        assert result.exit_code == 1
        launch.assert_not_called()
        assert "invalid session id" in result.output

    def test_event_less_sid_fails_with_listing_hint(self):
        _FakeStore.has_events_value = False
        with (
            patch(
                "chaos_agent.memory.tui_session_store.TuiSessionStore",
                _FakeStore,
            ),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(app, ["resume", "-i", "sess-gone"])
        assert result.exit_code == 1
        launch.assert_not_called()
        assert "no events file" in result.output
        assert "blade-ai resume" in result.output


class TestResumeBareListing:
    """Bare ``resume`` is list-only: print the table + hint, exit 0.
    The old number-prompt held the process open on stdin (and hung
    outright wherever stdin never arrived). Every test here invokes
    WITHOUT ``input=`` — under CliRunner a leftover prompt would hit
    EOF and abort to exit 1, so the ``exit_code == 0`` assertions
    double as no-stdin-read guards."""

    def test_no_sessions_exits_zero(self):
        """An empty history is not an error — the user simply has
        nothing to resume yet."""
        _FakeStore.rows = []
        with (
            patch(
                "chaos_agent.memory.tui_session_store.TuiSessionStore",
                _FakeStore,
            ),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(app, ["resume"])
        assert result.exit_code == 0, result.output
        launch.assert_not_called()
        assert "No resumable sessions" in result.output

    def test_listing_renders_column_table_and_exits(self):
        """The listing renders a column-aligned table with a usage
        hint, then exits WITHOUT reading stdin (row shape comes from
        the shared ``list_resumable_sessions``)."""
        _FakeStore.rows = _rows("sess-vis")
        with (
            # Pin the render width — rich's Console reads COLUMNS from
            # the environment, and an exported narrow COLUMNS would
            # silently drop this test into the slim tier (no SIZE
            # column, no "1.0 KB").
            patch.dict(os.environ, {"COLUMNS": "80"}),
            patch(
                "chaos_agent.memory.tui_session_store.TuiSessionStore",
                _FakeStore,
            ),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(app, ["resume"])
        assert result.exit_code == 0, result.output
        launch.assert_not_called()
        for header in (
            "SESSION",
            "LAST ACTIVE",
            "EVENTS",
            "SIZE",
            "FIRST INPUT",
        ):
            assert header in result.output
        assert "sess-vis" in result.output
        assert "intent for sess-vis" in result.output
        assert "1.0 KB" in result.output
        # The takeover affordance replaces the old number-prompt.
        assert "blade-ai resume -i" in result.output

    def test_last_active_column_uses_mtime_not_started_at(self):
        """The listing sorts by events-file mtime, so the LAST ACTIVE
        column must show mtime too — the old rendering showed
        ``started_at`` (creation), which can sit days earlier and
        contradict the "newest first" order it sits under."""
        _FakeStore.rows = [
            {
                "tui_session_id": "sess-mtime",
                "size_bytes": 4096,
                "event_count": 5,
                "started_at": "2026-01-01T00:00:00",
                "modified_at": datetime(datetime.now().year, 8, 27, 13, 43).timestamp(),
                "first_input": "mtime probe",
            }
        ]
        with (
            patch(
                "chaos_agent.memory.tui_session_store.TuiSessionStore",
                _FakeStore,
            ),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(app, ["resume"])
        assert result.exit_code == 0, result.output
        launch.assert_not_called()
        assert "13:43" in result.output  # mtime-derived stamp
        assert "00:00" not in result.output  # started_at must not leak


class TestPickerFormatting:
    def test_format_last_active_current_year_is_compact(self):
        mtime = datetime(datetime.now().year, 5, 6, 7, 8).timestamp()
        assert _format_last_active(mtime) == "05-06 07:08"

    def test_format_last_active_other_year_widens(self):
        """A session from a previous year must not collapse to a
        bare ``MM-DD`` — without the year the user cannot tell how
        stale it is."""
        mtime = datetime(datetime.now().year - 1, 5, 6, 7, 8).timestamp()
        stamp = _format_last_active(mtime)
        assert re.fullmatch(r"\d{4}-05-06", stamp)


class TestPickerWidthTiers:
    """The tier guard defends against rich's squeeze policy: when a
    table's fixed columns exceed the console width, rich zeroes the
    ratio column first, then CHOPS ``width`` columns (an explicit
    width is a preference, not a floor). On a 60-col terminal that
    ate the ``#`` column entirely — the one field the pick depends
    on. The tier must be chosen by MEASURING the console, so every
    tier's fixed columns fit with margin."""

    def _rows(self) -> list[dict]:
        return [
            {
                "tui_session_id": "sess-tier",
                "size_bytes": 4096,
                "event_count": 12,
                "started_at": "2026-01-01T00:00:00",
                "modified_at": datetime(datetime.now().year, 8, 27, 13, 43).timestamp(),
                "first_input": "tier probe 意图",
            }
        ]

    def test_full_tier_keeps_volume_columns(self, capsys):
        with patch.dict(os.environ, {"COLUMNS": "80"}):
            _render_session_table(self._rows())
        out = capsys.readouterr().out
        assert "EVENTS" in out
        assert "SIZE" in out
        assert "tier probe 意图" in out

    def test_slim_tier_keeps_number_and_sid_drops_volume(self, capsys):
        with patch.dict(os.environ, {"COLUMNS": "60"}):
            _render_session_table(self._rows())
        out = capsys.readouterr().out
        # The pick number and the sid survive — the interaction core.
        assert re.search(r"^\s*1\s+sess-tier", out, re.MULTILINE)
        assert "08-27 13:43" in out
        # Volume columns are dropped BY DESIGN (not squeezed).
        assert "EVENTS" not in out
        assert "SIZE" not in out

    def test_compact_tier_renders_two_line_rows(self, capsys):
        with patch.dict(os.environ, {"COLUMNS": "45"}):
            _render_session_table(self._rows())
        out = capsys.readouterr().out
        # No table at all below the slim threshold — nothing to squeeze.
        assert "SESSION" not in out
        assert " 1  sess-tier  08-27 13:43" in out
        assert "tier probe 意图" in out

    def test_tier_switch_points_are_inclusive(self, capsys):
        """Pin the exact boundaries (off-by-one guard): 78 is full,
        77 is slim; 56 is slim, 55 is compact. The docstring's
        ``>= 78`` / ``>= 56`` semantics live or die at these four
        widths — mid-tier probes (80/60/45 above) cannot catch a
        boundary drift."""
        for columns, expect_volume in (("78", True), ("77", False)):
            with patch.dict(os.environ, {"COLUMNS": columns}):
                _render_session_table(self._rows())
            out = capsys.readouterr().out
            assert ("EVENTS" in out) is expect_volume, columns
        for columns, expect_table in (("56", True), ("55", False)):
            with patch.dict(os.environ, {"COLUMNS": columns}):
                _render_session_table(self._rows())
            out = capsys.readouterr().out
            # SESSION is a table header — its presence means a table
            # still rendered instead of the compact two-line rows.
            assert ("SESSION" in out) is expect_table, columns

    _LONG_DIGEST = (
        "模拟节点宕机导致 kubelet 失联，目标 Pod 卡死在 Terminating "
        "状态无法退出，观察驱逐行为与自恢复"
    )

    def test_long_digest_does_not_starve_fixed_columns(self, capsys):
        """Real sessions carry 50+ display-wide CJK digests. rich
        measures a ``no_wrap`` cell's floor as its FULL content
        width, so an untruncated digest inflates the ratio column's
        floor past the tier budget and rich's squeeze then eats the
        fixed columns (# gone, sid chopped) — reproduced on the real
        19-session listing. The digest must be pre-truncated to the
        budget BEFORE ``add_row``."""
        rows = [dict(self._rows()[0], first_input=self._LONG_DIGEST)]
        with patch.dict(os.environ, {"COLUMNS": "80"}):
            _render_session_table(rows)
        out = capsys.readouterr().out
        assert re.search(r"^\s*1\s+sess-tier", out, re.MULTILINE)
        assert "EVENTS" in out
        assert "SIZE" in out
        assert "sess-tier" in out
        # The digest itself renders, truncated with an ellipsis —
        # the 80-col budget is 19 display widths, so only the head
        # of the long CJK digest survives (and that is enough: the
        # digest is for telling sessions apart, not reading).
        assert "模拟节点宕机导致" in out
        assert "观察驱逐行为" not in out

    def test_long_digest_slim_tier_keeps_sid(self, capsys):
        rows = [dict(self._rows()[0], first_input=self._LONG_DIGEST)]
        with patch.dict(os.environ, {"COLUMNS": "60"}):
            _render_session_table(rows)
        out = capsys.readouterr().out
        assert re.search(r"^\s*1\s+sess-tier", out, re.MULTILINE)
        assert "08-27 13:43" in out

    def test_long_digest_compact_tier_keeps_sid_line(self, capsys):
        rows = [dict(self._rows()[0], first_input=self._LONG_DIGEST)]
        with patch.dict(os.environ, {"COLUMNS": "45"}):
            _render_session_table(rows)
        out = capsys.readouterr().out
        # The id line fits 45 cols; the digest line is ellipsised.
        assert " 1  sess-tier  08-27 13:43" in out
        assert "模拟节点宕机导致 kubelet 失联" in out


class TestRemoteServerMode:
    """``BLADE_AI_SERVER`` points the TUI at a remote server — the
    CLI's local-disk checks would adjudicate the wrong machine's
    sessions, so remote mode must skip them (the server adjudicates
    at boot, still fail-loud)."""

    _REMOTE = {"BLADE_AI_SERVER": "http://10.0.0.5:8089"}

    def test_sid_skips_local_disk_check(self):
        # has_events_value=False would abort locally; remote mode must
        # hand the sid to the TUI and let the remote server adjudicate.
        _FakeStore.has_events_value = False
        with (
            patch.dict(os.environ, self._REMOTE),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(app, ["resume", "-i", "sess-remote"])
        assert result.exit_code == 0, result.output
        launch.assert_called_once_with(["--resume", "sess-remote"])

    def test_sid_still_rejects_malformed_form(self):
        # The pattern check is pure format validation — it stays on
        # regardless of where the server lives.
        with (
            patch.dict(os.environ, self._REMOTE),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(app, ["resume", "-i", "bad..sid"])
        assert result.exit_code == 1
        launch.assert_not_called()
        assert "invalid session id" in result.output

    def test_bare_listing_refuses_with_hint(self):
        # The listing can only ever read the LOCAL disk — showing it
        # in remote mode would present the wrong machine's history.
        with (
            patch.dict(os.environ, self._REMOTE),
            patch("chaos_agent.cli.main._launch_default_tui") as launch,
        ):
            result = runner.invoke(app, ["resume"])
        assert result.exit_code == 1
        launch.assert_not_called()
        assert "BLADE_AI_SERVER" in result.output
        assert "blade-ai resume -i" in result.output
