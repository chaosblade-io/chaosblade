"""``blade-ai uninstall`` derives paths instead of reading a manifest.

Everything it needs is a convention ``scripts/install.sh`` established:

    ~/.blade-ai/versions/    every installed version
    ~/.local/bin/blade-ai    the symlink on PATH
    ~/.blade-ai/             config, skills, memory

``install-manifest.json`` used to be required, and was worse in two measurable
ways:

* Losing the file made uninstall impossible — exit 1, delete by hand.
* It recorded ONE ``install_dir`` and ``update`` never rewrote it, so after any
  update the manifest named the PREVIOUS version. Uninstall then deleted the old
  directory and left the one actually in use.
"""

import os
import re
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from chaos_agent.cli.commands.uninstall import (
    PROFILE_CANDIDATES,
    uninstall_command,
)

runner = CliRunner()


@pytest.fixture
def app():
    cli = typer.Typer()
    cli.command()(uninstall_command)
    return cli


@pytest.fixture
def home(monkeypatch):
    """An isolated $HOME; ``~`` expansion is redirected into it."""
    root = Path(tempfile.mkdtemp())
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~", str(root)))
    return root


def _install(home: Path, *versions: str, symlink_to: str | None = None,
             config: bool = True, profile: bool = True) -> None:
    """Lay down the on-disk shape install.sh (+ any updates) would produce."""
    vdir = home / ".blade-ai" / "versions"
    for ver in versions:
        target = vdir / f"blade-ai-v{ver}"
        target.mkdir(parents=True)
        (target / "blade-ai").write_text(f"binary-{ver}")
    if symlink_to:
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "blade-ai").symlink_to(
            vdir / f"blade-ai-v{symlink_to}" / "blade-ai"
        )
    if config:
        (home / ".blade-ai").mkdir(parents=True, exist_ok=True)
        (home / ".blade-ai" / "config.json").write_text("{}")
    if profile:
        (home / ".zshrc").write_text(
            'export PATH="$HOME/.local/bin:$PATH"  # blade-ai\nalias ll=ls\n'
        )


class TestEveryVersionIsRemoved:
    """``update`` accumulates versions; uninstall must sweep all of them."""

    def test_all_versions_go(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force"])
        assert result.exit_code == 0, result.output
        assert list((home / ".blade-ai" / "versions").iterdir()) == []

    def test_the_version_in_use_is_not_left_behind(self, app, home):
        """The manifest bug: it named 0.5.2, so 0.6.0 survived uninstall."""
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        runner.invoke(app, ["--force"])
        assert not (home / ".blade-ai" / "versions" / "blade-ai-v0.6.0").exists()

    def test_symlink_is_removed(self, app, home):
        _install(home, "0.6.0", symlink_to="0.6.0")
        runner.invoke(app, ["--force"])
        assert not (home / ".local" / "bin" / "blade-ai").is_symlink()


class TestNoManifestRequired:
    def test_uninstall_works_without_any_metadata_file(self, app, home):
        """No manifest, no receipt — the paths are a convention, not a record."""
        _install(home, "0.6.0", symlink_to="0.6.0")
        assert not (home / ".blade-ai" / "install-manifest.json").exists()
        result = runner.invoke(app, ["--force", "--purge"])
        assert result.exit_code == 0, result.output
        assert not (home / ".blade-ai").exists()

    def test_nothing_installed_is_not_an_error(self, app, home):
        """An empty $HOME means "already uninstalled", not a failure."""
        result = runner.invoke(app, ["--force"])
        assert result.exit_code == 0
        assert "Nothing to uninstall" in result.output


class TestPathEntryCleanup:
    def test_marked_line_is_removed_and_the_rest_kept(self, app, home):
        _install(home, "0.6.0", symlink_to="0.6.0")
        runner.invoke(app, ["--force"])
        assert (home / ".zshrc").read_text() == "alias ll=ls\n"

    def test_profile_without_the_marker_is_untouched(self, app, home):
        _install(home, "0.6.0", symlink_to="0.6.0", profile=False)
        (home / ".zshrc").write_text("alias ll=ls\n")
        runner.invoke(app, ["--force"])
        assert (home / ".zshrc").read_text() == "alias ll=ls\n"


class TestConfigIsKeptUnlessPurgeIsAsked:
    """Deleting ``~/.blade-ai/`` must be requested, never assumed.

    It holds config, task records and postmortems. Binaries and PATH entries are
    replaceable — reinstall and they are back — but drill history is not, so the
    irreversible half of the operation is opt-in.

    This is the reverse of the original design, which deleted the directory by
    default and offered ``--keep-config`` to prevent it: the destructive outcome
    happened to anyone who did not know a flag existed.
    """

    def test_default_keeps_the_directory(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force"])
        assert result.exit_code == 0, result.output
        assert (home / ".blade-ai" / "config.json").exists()

    def test_default_still_removes_everything_replaceable(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        runner.invoke(app, ["--force"])
        assert list((home / ".blade-ai" / "versions").iterdir()) == []
        assert not (home / ".local" / "bin" / "blade-ai").is_symlink()
        assert "# blade-ai" not in (home / ".zshrc").read_text()

    def test_purge_deletes_the_directory(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force", "--purge"])
        assert result.exit_code == 0, result.output
        assert not (home / ".blade-ai").exists()

    def test_default_output_points_at_the_flag(self, app, home):
        """Someone who wanted a clean sweep needs to learn how."""
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force"])
        assert "--purge" in result.output

    def test_prompt_marks_the_deletion_explicitly(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--purge"], input="n\n")
        assert "WILL BE DELETED" in result.output
        assert (home / ".blade-ai" / "config.json").exists()

    def test_prompt_says_it_is_kept_by_default(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, [], input="n\n")
        assert "kept" in result.output



class TestConfirmationPrompt:
    def test_declining_changes_nothing(self, app, home):
        _install(home, "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, input="n\n")
        assert result.exit_code == 0
        assert "Cancelled" in result.output
        assert (home / ".blade-ai" / "versions" / "blade-ai-v0.6.0").exists()
        assert (home / ".local" / "bin" / "blade-ai").is_symlink()

    def test_prompt_lists_every_version(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, input="n\n")
        assert "blade-ai-v0.5.2" in result.output
        assert "blade-ai-v0.6.0" in result.output


class TestRemoveASingleVersion:
    """``--version`` reclaims one version without tearing down the install.

    ``update`` accumulates versions under ``versions/``, so there has to be a way
    to drop the old ones while keeping PATH, config and the version in use. That
    makes it a different operation from uninstalling, with one hazard of its own:
    deleting the version the symlink resolves to would leave ``blade-ai`` on PATH
    pointing at nothing — and with it gone, no ``update`` to repair it either.
    """

    def _state(self, home: Path):
        vdir = home / ".blade-ai" / "versions"
        versions = sorted(
            p.name.removeprefix("blade-ai-v") for p in vdir.iterdir()
        ) if vdir.is_dir() else []
        link = home / ".local" / "bin" / "blade-ai"
        active = (
            Path(os.readlink(link)).parent.name.removeprefix("blade-ai-v")
            if link.is_symlink() else None
        )
        return versions, active, (link.exists() if link.is_symlink() else False)

    def test_removes_only_the_named_version(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force", "--version", "0.5.2"])
        assert result.exit_code == 0, result.output
        versions, active, alive = self._state(home)
        assert versions == ["0.6.0"]
        assert active == "0.6.0" and alive

    def test_config_and_path_entry_are_untouched(self, app, home):
        """Not an uninstall — the install keeps working afterwards."""
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        runner.invoke(app, ["--force", "--version", "0.5.2"])
        assert (home / ".blade-ai" / "config.json").exists()
        assert "# blade-ai" in (home / ".zshrc").read_text()

    def test_removing_the_active_version_switches_the_symlink(self, app, home):
        """The hazard: never leave a dangling binary on PATH."""
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force", "--version", "0.6.0"])
        assert result.exit_code == 0, result.output
        versions, active, alive = self._state(home)
        assert versions == ["0.5.2"]
        assert active == "0.5.2", "symlink must move to the remaining version"
        assert alive, "symlink must still resolve"

    def test_the_only_version_in_use_is_refused(self, app, home):
        """That is a full uninstall; say so rather than breaking the command."""
        _install(home, "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force", "--version", "0.6.0"])
        assert result.exit_code == 1
        assert "only installed version" in result.output
        versions, _active, alive = self._state(home)
        assert versions == ["0.6.0"] and alive, "nothing may be removed"

    def test_unknown_version_lists_what_is_installed(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force", "--version", "9.9.9"])
        assert result.exit_code == 1
        assert "not installed" in result.output
        assert "v0.5.2" in result.output and "v0.6.0" in result.output

    def test_declining_the_prompt_removes_nothing(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--version", "0.5.2"], input="n\n")
        assert result.exit_code == 0
        assert "Cancelled" in result.output
        assert self._state(home)[0] == ["0.5.2", "0.6.0"]

    def test_prompt_warns_when_the_version_is_in_use(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--version", "0.6.0"], input="n\n")
        assert "IN USE" in result.output
        assert "will switch to v0.5.2" in result.output

    def test_without_the_flag_every_version_goes(self, app, home):
        """No --version means remove them all, not just one."""
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        runner.invoke(app, ["--force"])
        assert list((home / ".blade-ai" / "versions").iterdir()) == []


class TestVersionOrderingIsNumeric:
    """Version directories must be compared as numbers, not as strings.

    Sorted as text, "0.10.0" lands before "0.9.0" because '1' < '9'. That made
    "the newest remaining version" wrong exactly when it mattered — repointing
    the symlink after removing the active version would silently downgrade the
    user two releases.
    """

    def test_double_digit_minor_sorts_above_single_digit(self):
        from chaos_agent.cli.commands.uninstall import _version_sort_key

        names = ["blade-ai-v0.9.0", "blade-ai-v0.10.0",
                 "blade-ai-v0.6.0", "blade-ai-v1.0.0"]
        ordered = sorted((Path(n) for n in names), key=_version_sort_key)
        assert [p.name.removeprefix("blade-ai-v") for p in ordered] == \
            ["0.6.0", "0.9.0", "0.10.0", "1.0.0"]

    def test_symlink_moves_to_the_numerically_newest(self, app, home):
        _install(home, "0.9.0", "0.10.0", "1.0.0", symlink_to="0.10.0")
        result = runner.invoke(app, ["--force", "--version", "0.10.0"])
        assert result.exit_code == 0, result.output
        link = home / ".local" / "bin" / "blade-ai"
        assert "blade-ai-v1.0.0" in os.readlink(link)
        assert link.exists()


class TestRemovingTheLastVersion:
    """Not "active" only because the symlink is gone — still the last binary.

    ``_active_version_dir`` returns None when the symlink is missing, points
    elsewhere, or already dangles. The removal then proceeds (correctly — there is
    no symlink to protect), but it leaves config and the PATH entry with nothing
    to run. That state has to be named, not left for the user to discover.
    """

    def test_it_is_allowed_but_reported(self, app, home):
        _install(home, "0.6.0", symlink_to=None)
        result = runner.invoke(app, ["--force", "--version", "0.6.0"])
        assert result.exit_code == 0, result.output
        assert "No versions remain" in result.output
        assert "blade-ai uninstall" in result.output      # how to finish the job

    def test_config_survives_so_a_reinstall_keeps_settings(self, app, home):
        _install(home, "0.6.0", symlink_to=None)
        runner.invoke(app, ["--force", "--version", "0.6.0"])
        assert (home / ".blade-ai" / "config.json").exists()

    def test_the_prompt_warns_before_it_happens(self, app, home):
        _install(home, "0.6.0", symlink_to=None)
        result = runner.invoke(app, ["--version", "0.6.0"], input="n\n")
        assert "LAST installed version" in result.output
        assert (home / ".blade-ai" / "versions" / "blade-ai-v0.6.0").exists()


class TestPurgeWithVersion:
    """``--purge`` applies to the full uninstall, not to removing one version.

    ``--version`` is a maintenance operation on the installed binaries; it never
    touches ``~/.blade-ai/`` either way. Pinned because the two live in separate
    branches, and wiring purge into the single-version path would turn "drop an
    old build" into "delete my drill history".
    """

    def test_version_removal_never_touches_config(self, app, home):
        for args in (
            ["--force", "--version", "0.5.2"],
            ["--force", "--version", "0.5.2", "--purge"],
        ):
            _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
            result = runner.invoke(app, args)
            assert result.exit_code == 0, result.output
            assert (home / ".blade-ai" / "config.json").exists(), args
            assert "# blade-ai" in (home / ".zshrc").read_text(), args
            shutil.rmtree(home / ".blade-ai")
            (home / ".local" / "bin" / "blade-ai").unlink(missing_ok=True)

    def test_purge_does_not_change_which_versions_go(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        runner.invoke(app, ["--force", "--version", "0.5.2", "--purge"])
        remaining = sorted(
            p.name for p in (home / ".blade-ai" / "versions").iterdir()
        )
        assert remaining == ["blade-ai-v0.6.0"]


class TestLeftoverDirectoriesAreNotVersions:
    """``update``'s in-flight directories must not be mistaken for releases.

    It creates ``.tmp-blade-ai-v*-xxxx`` while downloading and
    ``blade-ai-v*.replaced-<pid>`` while swapping. A crash or Ctrl-C can strand
    either under ``versions/``. Counting them was wrong twice over: the
    confirmation prompt listed junk as installed versions, and
    ``blade-ai-v0.6.0.replaced-99999`` produced the sort key ``(0, 6, 0)`` —
    equal to the real 0.6.0 and last in the list, so it got picked as "newest
    remaining" and the PATH symlink was repointed inside a leftover.
    """

    def _litter(self, home: Path) -> None:
        vdir = home / ".blade-ai" / "versions"
        for name in (".tmp-blade-ai-v0.7.0-abc123",
                     "blade-ai-v0.6.0.replaced-99999"):
            (vdir / name).mkdir()
            (vdir / name / "blade-ai").write_text("leftover")

    def test_only_real_versions_are_listed(self, app, home):
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        self._litter(home)
        from chaos_agent.cli.commands.uninstall import _installed_versions

        with patch("os.path.expanduser", lambda p: p.replace("~", str(home))):
            found = [p.name for p in _installed_versions()]
        assert found == ["blade-ai-v0.5.2", "blade-ai-v0.6.0"]

    def test_symlink_never_moves_into_a_leftover(self, app, home):
        """The concrete failure: .replaced-<pid> sorted as the newest version."""
        _install(home, "0.5.2", "0.6.0", symlink_to="0.6.0")
        self._litter(home)
        result = runner.invoke(app, ["--force", "--version", "0.6.0"])
        assert result.exit_code == 0, result.output
        target = os.readlink(home / ".local" / "bin" / "blade-ai")
        assert "blade-ai-v0.5.2" in target
        assert ".replaced-" not in target

    def test_prompt_does_not_offer_leftovers(self, app, home):
        _install(home, "0.6.0", symlink_to="0.6.0")
        self._litter(home)
        result = runner.invoke(app, [], input="n\n")
        assert ".tmp-blade-ai" not in result.output
        assert ".replaced-" not in result.output


class TestVersionArgumentIsValidated:
    """``--version`` is interpolated into a path that gets ``rmtree``'d.

    Measured before this check: ``--version 0.6.0/../../memory`` resolved to
    ``~/.blade-ai/memory``, deleted the task records, and exited 0 reporting
    success. A plain-version pattern is the whole fix — the value never needs to
    contain a separator.
    """

    @pytest.mark.parametrize("bad", [
        "0.6.0/../../memory",       # escapes versions/ and hits real data
        "../memory",
        "0.6.0/../../../..",        # resolves to the user's home
        "$(whoami)",
        "0.6.0;rm -rf /",
        "",
        "latest",
    ])
    def test_malformed_values_are_refused(self, app, home, bad):
        _install(home, "0.6.0", symlink_to="0.6.0")
        (home / ".blade-ai" / "memory").mkdir()
        (home / ".blade-ai" / "memory" / "task.json").write_text("records")

        result = runner.invoke(app, ["--force", "--version", bad])
        assert result.exit_code == 1
        assert "Invalid version" in result.output
        assert (home / ".blade-ai" / "memory" / "task.json").exists()
        assert (home / ".blade-ai" / "versions" / "blade-ai-v0.6.0").exists()

    @pytest.mark.parametrize("good", ["0.6.0", "1", "1.0", "0.10.0", "1.2.3.4"])
    def test_plain_versions_are_accepted(self, app, home, good):
        """Accepted means "parsed", not "installed" — absent ones say so."""
        _install(home, "0.6.0", "0.7.0", symlink_to="0.7.0")
        result = runner.invoke(app, ["--force", "--version", good])
        assert "Invalid version" not in result.output


class TestCustomPrefixInstalls:
    """``install.sh`` can put the binary outside ``~/.blade-ai/versions``.

    ``--prefix PATH`` and ``BLADE_AI_INSTALL_DIR`` do exactly that, while still
    linking from ``~/.local/bin``. Sweeping ``versions/`` then finds nothing, so
    uninstall removed the symlink, printed "✨ blade-ai has been uninstalled" and
    left the whole install on disk — silently, which was the actual defect.

    The directory is named rather than deleted: a custom prefix may be a shared
    location holding unrelated files, and nothing distinguishes that from a
    dedicated one.
    """

    def _prefix_install(self, home: Path) -> Path:
        binary_dir = home / "opt" / "blade-ai"
        binary_dir.mkdir(parents=True)
        (binary_dir / "blade-ai").write_text("binary")
        link_dir = home / ".local" / "bin"
        link_dir.mkdir(parents=True)
        (link_dir / "blade-ai").symlink_to(binary_dir / "blade-ai")
        (home / ".blade-ai").mkdir(exist_ok=True)
        (home / ".blade-ai" / "config.json").write_text("{}")
        return binary_dir

    def test_the_leftover_directory_is_named(self, app, home):
        binary_dir = self._prefix_install(home)
        result = runner.invoke(app, ["--force"])
        assert result.exit_code == 0, result.output
        assert str(binary_dir) in result.output
        assert "custom --prefix" in result.output

    def test_the_files_are_not_deleted(self, app, home):
        """rmtree on an arbitrary --prefix could take unrelated files with it."""
        binary_dir = self._prefix_install(home)
        runner.invoke(app, ["--force"])
        assert (binary_dir / "blade-ai").exists()

    def test_the_prompt_says_it_will_not_be_removed(self, app, home):
        self._prefix_install(home)
        result = runner.invoke(app, [], input="n\n")
        assert "NOT removed" in result.output

    def test_it_is_detected_before_the_symlink_goes(self, app, home):
        """The symlink is the only record of where a --prefix install lives."""
        self._prefix_install(home)
        result = runner.invoke(app, ["--force"])
        assert "Removed symlink" in result.output
        assert "still at" in result.output

    def test_a_standard_install_says_none_of_this(self, app, home):
        _install(home, "0.6.0", symlink_to="0.6.0")
        result = runner.invoke(app, ["--force"])
        assert "custom --prefix" not in result.output
        assert "still at" not in result.output

    def test_detection_returns_none_for_the_standard_layout(self, app, home):
        from chaos_agent.cli.commands.uninstall import _out_of_tree_install

        _install(home, "0.6.0", symlink_to="0.6.0")
        with patch("os.path.expanduser", lambda p: p.replace("~", str(home))):
            assert _out_of_tree_install() is None

    def test_detection_returns_none_without_a_symlink(self, app, home):
        from chaos_agent.cli.commands.uninstall import _out_of_tree_install

        _install(home, "0.6.0", symlink_to=None)
        with patch("os.path.expanduser", lambda p: p.replace("~", str(home))):
            assert _out_of_tree_install() is None


INSTALL_SH = Path(__file__).resolve().parents[3] / "scripts" / "install.sh"
UNINSTALL_SH = Path(__file__).resolve().parents[3] / "scripts" / "uninstall.sh"


class TestProfileListMatchesInstallSh:
    """Every file install.sh can write a PATH line to must be swept.

    ``~/.zprofile`` was missing. install.sh adds the line there for zsh login
    shells, and zsh is macOS's default — so on the most common platform, with a
    ``.zprofile`` present, uninstall reported "Cleaned PATH from ~/.zshrc" and
    silently left an identical line in the other file.

    The list is checked against install.sh itself rather than against a list
    written from memory, since that is how the gap appeared in the first place.
    """

    def test_every_add_to_profile_target_is_covered(self):
        """Parse the actual targets out of install.sh."""
        text = INSTALL_SH.read_text()
        targets = set(re.findall(r'add_to_profile\s+"\$HOME/([^"]+)"', text))
        assert targets, "install.sh's add_to_profile calls should be parseable"
        covered = {c.removeprefix("~/") for c in PROFILE_CANDIDATES}
        missing = targets - covered
        assert missing == set(), f"install.sh writes to uncovered files: {missing}"

    def test_the_fish_config_is_covered_too(self):
        """fish takes a different code path (fish_add_path), not add_to_profile."""
        text = INSTALL_SH.read_text()
        assert "config.fish" in text
        assert "~/.config/fish/config.fish" in PROFILE_CANDIDATES

    def test_zprofile_is_cleaned(self, app, home):
        """The specific regression: two files carried the line, one was cleaned."""
        _install(home, "0.6.0", symlink_to="0.6.0")
        line = 'export PATH="$HOME/.local/bin:$PATH"  # blade-ai\nalias ll=ls\n'
        (home / ".zprofile").write_text(line)

        runner.invoke(app, ["--force"])
        assert "# blade-ai" not in (home / ".zprofile").read_text()
        assert "alias ll=ls" in (home / ".zprofile").read_text()


class TestShellScriptValidatesVersionToo:
    """``uninstall.sh`` is what curl|bash users actually hold.

    It had the same defect as the Python command — ``--version`` interpolated
    into a path that gets ``rm -rf``'d, with only a non-empty check in front of
    it. Fixing only the Python side left every script user exposed, so the guard
    is asserted here against the shipped script text.
    """

    def test_uninstall_sh_rejects_non_plain_versions(self):
        text = UNINSTALL_SH.read_text()
        assert 'err "Invalid --version' in text, "no validation in uninstall.sh"
        assert '^[0-9]+(\\.[0-9]+)*$' in text

    def test_install_sh_rejects_non_plain_versions(self):
        """It rm -rf's INSTALL_DIR; being saved by mktemp's ordering is luck."""
        text = INSTALL_SH.read_text()
        assert 'err "Invalid --version' in text, "no validation in install.sh"

    def test_the_migration_copies_carry_the_same_guard(self):
        """migration/ ships to the chaosblade repo; a divergence there re-opens it."""
        root = Path(__file__).resolve().parents[3] / "migration"
        if not (root / "install.sh").exists():
            pytest.skip(
                "migration/ is gitignored (see .gitignore); present only in dev "
                "trees, not on a clean checkout / CI. The canonical scripts/ "
                "copies are still validated by TestCrossImplementationConsistency."
            )
        for name in ("install.sh", "uninstall.sh"):
            text = (root / name).read_text()
            assert 'err "Invalid --version' in text, f"{name} lost the guard"


REPO_ROOT = Path(__file__).resolve().parents[3]


class TestCrossImplementationConsistency:
    """The same operation exists in five places; they must not disagree.

    ``blade-ai uninstall`` (Python), ``uninstall.sh``/``install.sh`` (what
    curl|bash users hold), and the ``.ps1`` pair. Every divergence found so far
    was a real defect, and each was invisible from inside one implementation:

      * ``--version`` validated in Python but not in the shell — the shell one
        deleted ``~/.blade-ai/memory`` and called it a removed version.
      * config deleted by default in ``.ps1`` long after the Python and ``.sh``
        defaults were reversed.
      * ``.replaced-<pid>`` counted as an installed version by the shell glob.

    These assertions read the shipped scripts, so a future edit to one side
    without the other turns red here rather than in someone's home directory.
    """

    SHELL_SCRIPTS = ("scripts/install.sh", "scripts/uninstall.sh",
                     "migration/install.sh", "migration/uninstall.sh")
    PS_SCRIPTS = ("scripts/uninstall.ps1", "migration/uninstall.ps1")

    def _read(self, rel: str) -> str:
        p = REPO_ROOT / rel
        if not p.exists():
            # migration/ is gitignored (see .gitignore): it ships to the
            # chaosblade repo and is not part of a clean checkout / CI. Skip
            # the migration/* cases there; the scripts/* canonical copies in
            # the same parametrisation still run and carry the real assertions.
            pytest.skip(f"{rel} absent in this checkout (migration/ is gitignored)")
        return p.read_text()

    @pytest.mark.parametrize("rel", SHELL_SCRIPTS)
    def test_every_shell_script_validates_version(self, rel):
        assert 'err "Invalid --version' in self._read(rel), rel

    @pytest.mark.parametrize("rel", PS_SCRIPTS)
    def test_powershell_keeps_config_by_default(self, rel):
        """-KeepConfig meant "delete unless asked not to" — the reverse."""
        text = self._read(rel)
        assert "$RemoveConfigDir = [bool]$Purge" in text, rel
        assert "KeepConfig" not in text, f"{rel} still carries the old flag"

    @pytest.mark.parametrize("rel", ("scripts/uninstall.sh", "migration/uninstall.sh"))
    def test_shell_keeps_config_by_default(self, rel):
        text = self._read(rel)
        assert "PURGE_CONFIG=0" in text, rel
        assert "--keep-config" not in text, f"{rel} still carries the old flag"

    def test_python_keeps_config_by_default(self):
        """All three implementations agree: --purge is opt-in."""
        text = (REPO_ROOT / "src/chaos_agent/cli/commands/uninstall.py").read_text()
        assert '"--purge"' in text
        assert "keep_config" not in text

    @pytest.mark.parametrize("rel", ("scripts/uninstall.sh", "migration/uninstall.sh"))
    def test_shell_enumeration_excludes_inflight_directories(self, rel):
        """Its glob ``blade-ai-v*`` also matches ``...replaced-<pid>``."""
        assert "^blade-ai-v[0-9]+(\\.[0-9]+)*$" in self._read(rel), rel

    @pytest.mark.parametrize("rel", ("scripts/install.sh", "migration/install.sh"))
    def test_install_moves_the_old_version_aside(self, rel):
        """It used to rm -rf then mv: a failure between left nothing installed."""
        text = self._read(rel)
        assert "restore_displaced" in text, rel
        assert 'DISPLACED="${INSTALL_DIR}.replaced-$$"' in text, rel

    def test_the_migration_copies_do_not_drift(self):
        """migration/ is what ships to chaosblade; a stale copy re-opens bugs."""
        for name in ("install.sh", "uninstall.sh"):
            a = self._read(f"scripts/{name}")
            b = self._read(f"migration/{name}")
            for marker in ('err "Invalid --version',):
                assert (marker in a) == (marker in b), f"{name}: {marker} diverged"

    def test_profile_list_matches_the_shell_uninstaller(self):
        """The two implementations must sweep the same set of files.

        ``uninstall.sh`` has carried all six since the start; the Python list was
        written from memory and omitted ``~/.zprofile``. Diffing against the shell
        would have caught it immediately — which is the point of pinning it here
        rather than asserting a hardcoded list on both sides.
        """
        text = self._read("scripts/uninstall.sh")
        # There are two assignments: one reusing the manifest's recorded files,
        # and a literal fallback list. Only the latter carries $HOME entries.
        blocks = re.findall(r"SHELL_RC_CANDIDATES=\((.*?)\)", text, re.S)
        shell_files = {m for b in blocks
                       for m in re.findall(r'"\$HOME/([^"]+)"', b)}
        assert shell_files, "SHELL_RC_CANDIDATES should be parseable"
        python_files = {c.removeprefix("~/") for c in PROFILE_CANDIDATES}
        assert shell_files == python_files, (
            f"only in shell: {sorted(shell_files - python_files)}; "
            f"only in Python: {sorted(python_files - shell_files)}"
        )


class TestLeftoverPathLineCountsAsWork:
    """A stale PATH line is a leftover, even with nothing else installed.

    The "nothing to uninstall" check looked at versions, symlink and config but
    not at the shell profiles. So after a user had removed ``~/.blade-ai`` and the
    symlink by hand, uninstall answered "Nothing to uninstall", exited 0, and left
    ``export PATH=...  # blade-ai`` in their shell config with no command that
    would ever clean it.
    """

    def test_a_lone_path_line_is_cleaned(self, app, home):
        (home / ".zshrc").write_text("export PATH=x  # blade-ai\nalias ll=ls\n")
        result = runner.invoke(app, ["--force"])
        assert result.exit_code == 0, result.output
        assert "Nothing to uninstall" not in result.output
        assert "# blade-ai" not in (home / ".zshrc").read_text()
        assert "alias ll=ls" in (home / ".zshrc").read_text()

    def test_a_genuinely_empty_home_still_reports_nothing(self, app, home):
        """The message must stay for the case it was written for."""
        result = runner.invoke(app, ["--force"])
        assert result.exit_code == 0
        assert "Nothing to uninstall" in result.output

    def test_an_unrelated_profile_does_not_count_as_work(self, app, home):
        (home / ".zshrc").write_text("alias ll=ls\n")
        result = runner.invoke(app, ["--force"])
        assert "Nothing to uninstall" in result.output


class TestDanglingSymlinkIsNotReportedAsFiles:
    """"Its files are still at X" must only be said when X exists.

    ``_out_of_tree_install`` read the symlink target without checking it, so a
    dangling link — a ``--prefix`` directory the user already deleted — produced a
    note pointing at a path with nothing in it.
    """

    def test_no_claim_is_made_for_a_missing_directory(self, app, home):
        link_dir = home / ".local" / "bin"
        link_dir.mkdir(parents=True)
        (link_dir / "blade-ai").symlink_to(home / "opt" / "gone" / "blade-ai")
        (home / ".blade-ai").mkdir(exist_ok=True)
        (home / ".blade-ai" / "config.json").write_text("{}")

        result = runner.invoke(app, ["--force"])
        assert result.exit_code == 0, result.output
        assert "still at" not in result.output
        assert "custom --prefix" not in result.output

    def test_an_existing_prefix_is_still_reported(self, app, home):
        """The detection must not be broken by the existence check."""
        binary_dir = home / "opt" / "blade-ai"
        binary_dir.mkdir(parents=True)
        (binary_dir / "blade-ai").write_text("binary")
        link_dir = home / ".local" / "bin"
        link_dir.mkdir(parents=True)
        (link_dir / "blade-ai").symlink_to(binary_dir / "blade-ai")
        (home / ".blade-ai").mkdir(exist_ok=True)
        (home / ".blade-ai" / "config.json").write_text("{}")

        result = runner.invoke(app, ["--force"])
        assert "still at" in result.output
        assert (binary_dir / "blade-ai").exists()

    def test_detection_returns_none_for_a_dangling_link(self, app, home):
        from chaos_agent.cli.commands.uninstall import _out_of_tree_install

        link_dir = home / ".local" / "bin"
        link_dir.mkdir(parents=True)
        (link_dir / "blade-ai").symlink_to(home / "opt" / "gone" / "blade-ai")
        with patch("os.path.expanduser", lambda p: p.replace("~", str(home))):
            assert _out_of_tree_install() is None
