"""``blade-ai update`` release coordinates and download integrity.

This command had NO tests, which is how four independent URL mistakes survived
in it — every one of them enough to make updating impossible:

  * the API pointed at a ``chaosblade-io/blade-ai`` repo (releases actually ship
    from ``chaosblade-io/chaosblade``),
  * it used ``/releases/latest``, which returns whatever the repo published last
    — normally the ChaosBlade tool's own ``v1.x``, not blade-ai,
  * it built the tag as ``v{version}`` instead of ``blade-ai-v{version}``,
  * it ignored ``BLADE_AI_MIRROR_API``, the mirror knob install.sh offers.

Verified against the real release:
https://github.com/chaosblade-io/chaosblade/releases/tag/blade-ai-v0.6.0

The tests below pin the coordinates to ``scripts/install.sh`` — that script
produced the install this command replaces, so a divergence between them means
``update`` fetches artifacts that do not exist.
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from chaos_agent.cli.commands import update
from chaos_agent.cli.commands.update import (
    _RELEASES_API,
    _RELEASES_DOWNLOAD,
    _TAG_PREFIX,
)

INSTALL_SH = Path(__file__).resolve().parents[3] / "scripts" / "install.sh"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class TestCoordinatesMatchInstallScript:
    """install.sh is the source of truth: it created the install being updated."""

    def test_install_script_is_present(self):
        assert INSTALL_SH.exists(), INSTALL_SH

    def test_releases_api_matches(self):
        assert f'RELEASES_API="{_RELEASES_API}"' in INSTALL_SH.read_text()

    def test_download_base_matches(self):
        assert _RELEASES_DOWNLOAD in INSTALL_SH.read_text()

    def test_tag_prefix_matches(self):
        # install.sh: TAG="blade-ai-v${APP_VERSION}"
        assert f'TAG="{_TAG_PREFIX}$' in INSTALL_SH.read_text()

    def test_repo_is_chaosblade_not_blade_ai(self):
        """The regression that broke the command: blade-ai has no own repo."""
        for url in (_RELEASES_API, _RELEASES_DOWNLOAD):
            assert "chaosblade-io/chaosblade" in url, url
            assert "chaosblade-io/blade-ai" not in url, url


class TestLatestVersionResolution:
    """The releases list is SHARED with the ChaosBlade tool — filter by prefix."""

    #: Shape of the real repo: ChaosBlade's own tags interleaved with ours.
    MIXED = [
        {"tag_name": "v1.8.0", "draft": False, "prerelease": False},
        {"tag_name": "blade-ai-v0.6.0", "draft": False, "prerelease": False},
        {"tag_name": "blade-ai-v0.5.2", "draft": False, "prerelease": False},
        {"tag_name": "v1.7.4", "draft": False, "prerelease": False},
    ]

    def _resolve(self, releases):
        with patch(
            "urllib.request.urlopen",
            return_value=_Resp(json.dumps(releases).encode()),
        ):
            return update._fetch_latest_version()

    def test_picks_highest_blade_ai_tag(self):
        assert self._resolve(self.MIXED) == "0.6.0"

    def test_ignores_chaosblade_own_releases(self):
        """``/releases/latest`` would have returned v1.8.0 — a different product."""
        assert self._resolve(self.MIXED) != "1.8.0"

    def test_skips_prereleases_and_drafts(self):
        releases = self.MIXED + [
            {"tag_name": "blade-ai-v0.7.0-rc.1", "draft": False, "prerelease": True},
            {"tag_name": "blade-ai-v0.9.0", "draft": True, "prerelease": False},
        ]
        assert self._resolve(releases) == "0.6.0"

    def test_no_blade_ai_release_returns_none(self):
        only_chaosblade = [{"tag_name": "v1.8.0", "draft": False, "prerelease": False}]
        assert self._resolve(only_chaosblade) is None

    def test_network_failure_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("x")):
            assert update._fetch_latest_version() is None

    def test_mirror_api_env_is_honoured(self, monkeypatch):
        """install.sh offers BLADE_AI_MIRROR_API; update must too."""
        monkeypatch.setenv("BLADE_AI_MIRROR_API", "https://mirror.internal/releases")
        seen = {}

        def fake_urlopen(req, **_k):
            seen["url"] = req.full_url
            return _Resp(json.dumps(self.MIXED).encode())

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            update._fetch_latest_version()
        assert seen["url"].startswith("https://mirror.internal/releases")


class TestChecksumVerification:
    """install.sh verifies on first install; self-update must not skip it."""

    @staticmethod
    def _archive() -> tuple[Path, str]:
        path = Path(tempfile.mkdtemp()) / "blade-ai.tar.gz"
        path.write_bytes(b"payload")
        return path, hashlib.sha256(b"payload").hexdigest()

    ASSET = "blade-ai-darwin-arm64.tar.gz"

    def test_matching_digest_passes(self):
        archive, digest = self._archive()
        with patch("urllib.request.urlopen",
                   return_value=_Resp(f"{digest}  {self.ASSET}\n".encode())):
            assert update._verify_checksum(archive, "b", "t", self.ASSET) is True

    def test_mismatch_stops_the_update(self):
        """A corrupted or tampered archive must never be unpacked."""
        archive, _ = self._archive()
        with patch("urllib.request.urlopen",
                   return_value=_Resp(f"deadbeef  {self.ASSET}\n".encode())):
            assert update._verify_checksum(archive, "b", "t", self.ASSET) is False

    @pytest.mark.parametrize("manifest", [
        b"abc123  some-other-asset.tar.gz\n",   # no entry for our asset
        b"",                                    # empty manifest
    ])
    def test_missing_entry_warns_but_proceeds(self, manifest):
        """Older releases predate checksums.txt; failing closed would block them."""
        archive, _ = self._archive()
        with patch("urllib.request.urlopen", return_value=_Resp(manifest)):
            assert update._verify_checksum(archive, "b", "t", self.ASSET) is True

    def test_unreachable_manifest_warns_but_proceeds(self):
        archive, _ = self._archive()
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("offline")):
            assert update._verify_checksum(archive, "b", "t", self.ASSET) is True


class TestDownloadUrlShape:
    def test_url_matches_the_published_asset(self):
        """Asserted against the real blade-ai-v0.6.0 release assets."""
        expected = (
            "https://github.com/chaosblade-io/chaosblade/releases/download/"
            "blade-ai-v0.6.0/blade-ai-darwin-arm64.tar.gz"
        )
        built = f"{_RELEASES_DOWNLOAD}/{_TAG_PREFIX}0.6.0/blade-ai-darwin-arm64.tar.gz"
        assert built == expected



class TestSelfUpdateEligibility:
    """Whether this install can replace itself is a RUNTIME fact.

    ``sys.frozen`` is set by PyInstaller, so it answers the question from the
    running process. A ``receipt.json`` used to gate this and was removed: every
    fact it held was already available at runtime, a missing or edited file
    blocked updating a healthy install, and the command rewrote the file's
    ``source`` on success — which then failed the check it had just passed, so
    the SECOND update always refused.
    """

    def test_frozen_binary_qualifies(self):
        with patch.object(sys, "frozen", True, create=True):
            assert update._is_standalone_install() is True

    def test_unfrozen_install_is_refused(self):
        """pip install / source checkout cannot swap its own files."""
        if getattr(sys, "frozen", False):  # pragma: no cover
            pytest.skip("test process is itself frozen")
        assert update._is_standalone_install() is False

    def test_no_receipt_is_read(self):
        """The gate must not touch the filesystem at all."""
        assert not hasattr(update, "_read_receipt")

    def test_version_comes_from_the_running_artifact(self):
        from chaos_agent import __version__
        assert update._current_version() == __version__

    def test_version_unknown_when_package_has_none(self):
        import chaos_agent

        with patch.object(chaos_agent, "__version__", "", create=True):
            assert update._current_version() == "unknown"




def _make_archive(dst: Path, marker: str) -> None:
    """A tar.gz shaped like the real asset: ``top/blade-ai`` under one level.

    ``install`` extracts with ``--strip-components=1``, so the payload must sit
    one directory deep or nothing lands.
    """
    import io as _io
    import tarfile

    buf = _io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = marker.encode()
        info = tarfile.TarInfo("top/blade-ai")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, _io.BytesIO(payload))
    dst.write_bytes(buf.getvalue())


@pytest.fixture
def sandbox(monkeypatch):
    """An isolated ``$HOME`` with the versions dir the installer expects."""
    home = Path(tempfile.mkdtemp())
    (home / ".blade-ai" / "versions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~", str(home)))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(update, "_verify_checksum", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: _Resp(
            _archive_bytes(("top/blade-ai",
                            ("V-" + str(url).split("blade-ai-v")[1].split("/")[0]).encode()))
        ),
    )
    return home


class TestAnyRequestedVersionInstalls:
    """``--version`` is an instruction, not a suggestion.

    There is no ``blade-ai install`` subcommand to redirect downgrades to, so
    refusing them would leave no way to pin back to a known-good build. Same for
    reinstalling the running version, which is how a corrupted install is
    repaired. Only the implicit form (no ``--version``) short-circuits on
    "already latest", because there it means "catch me up".
    """

    def _binary(self, home: Path, ver: str) -> Path:
        return home / ".blade-ai" / "versions" / f"blade-ai-v{ver}" / "blade-ai"

    def test_install_then_downgrade(self, sandbox):
        assert update._download_and_install("0.6.0", "darwin-arm64") is True
        assert self._binary(sandbox, "0.6.0").read_text() == "V-0.6.0"

        assert update._download_and_install("0.5.2", "darwin-arm64") is True
        assert self._binary(sandbox, "0.5.2").read_text() == "V-0.5.2"

    def test_symlink_follows_the_installed_version(self, sandbox):
        update._download_and_install("0.6.0", "darwin-arm64")
        update._download_and_install("0.5.2", "darwin-arm64")
        link = sandbox / ".local" / "bin" / "blade-ai"
        assert link.is_symlink()
        assert "blade-ai-v0.5.2" in os.readlink(link)

    def test_reinstalling_the_same_version_succeeds(self, sandbox):
        assert update._download_and_install("0.6.0", "darwin-arm64") is True
        assert update._download_and_install("0.6.0", "darwin-arm64") is True
        assert self._binary(sandbox, "0.6.0").read_text() == "V-0.6.0"


class TestSwapKeepsTheOldVersionUntilItSucceeds:
    """A failed swap must leave a runnable install behind.

    The previous code did ``rmtree(final_dir)`` and then ``rename``. Anything
    that failed between those two — disk full, permissions, the old binary still
    mapped — destroyed the installed version without putting the new one in
    place, leaving nothing to run and no way to re-run ``update``.
    """

    def test_old_version_survives_a_failed_swap(self, sandbox, monkeypatch):
        assert update._download_and_install("0.6.0", "darwin-arm64") is True
        target = sandbox / ".blade-ai" / "versions" / "blade-ai-v0.6.0"

        real_rename = Path.rename

        def flaky(self, other):
            # Fail only the final move INTO place, not the aside-move.
            if str(other).endswith("blade-ai-v0.6.0") and ".replaced-" not in str(self):
                raise OSError("simulated failure during swap")
            return real_rename(self, other)

        monkeypatch.setattr(Path, "rename", flaky)
        assert update._download_and_install("0.6.0", "darwin-arm64") is False
        assert (target / "blade-ai").read_text() == "V-0.6.0"

    def test_no_temporary_directories_are_left_behind(self, sandbox, monkeypatch):
        update._download_and_install("0.6.0", "darwin-arm64")
        real_rename = Path.rename

        def flaky(self, other):
            if str(other).endswith("blade-ai-v0.6.0") and ".replaced-" not in str(self):
                raise OSError("simulated failure during swap")
            return real_rename(self, other)

        monkeypatch.setattr(Path, "rename", flaky)
        update._download_and_install("0.6.0", "darwin-arm64")

        versions = sandbox / ".blade-ai" / "versions"
        litter = [
            p.name for p in versions.iterdir()
            if ".replaced-" in p.name or p.name.startswith(".tmp")
        ]
        assert litter == [], litter



class _Resp(io.BytesIO):
    """urlopen context manager over in-memory bytes."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _archive_bytes(*entries: tuple[str, bytes]) -> bytes:
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class TestRobustness:
    """Failure modes that must not damage a working install.

    Every one of these was reachable before: Windows produced an opaque 404, a
    first-ever update crashed on a missing versions dir, Ctrl-C stranded a temp
    directory forever, and an archive without the binary reported success while
    pointing the symlink at a path that does not exist — which also removed the
    user's ability to run ``update`` again to recover.
    """

    #: A releases list shaped like the real repo, for the 404 branch below.
    RELEASES = [
        {"tag_name": "v1.8.0", "draft": False, "prerelease": False},
        {"tag_name": "blade-ai-v0.6.0", "draft": False, "prerelease": False},
        {"tag_name": "blade-ai-v0.5.2", "draft": False, "prerelease": False},
        {"tag_name": "blade-ai-v0.5.1", "draft": False, "prerelease": False},
    ]

    def _api_ok_download_404(self, api_reachable: bool = True):
        """urlopen stub: the releases API answers, the download 404s."""
        def fake(target, timeout=None):
            url = str(getattr(target, "full_url", target))
            if "api.github.com" in url:
                if not api_reachable:
                    raise urllib.error.URLError("offline")
                return _Resp(json.dumps(self.RELEASES).encode())
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return fake

    def test_unknown_version_lists_the_published_ones(
        self, sandbox, monkeypatch, capsys,
    ):
        """"That version does not exist" is only half an answer."""
        monkeypatch.setattr("urllib.request.urlopen", self._api_ok_download_404())
        assert update._download_and_install("99.99.99", "darwin-arm64") is False
        err = capsys.readouterr().err
        assert "not among the published releases" in err
        assert "0.6.0" in err and "0.5.2" in err and "0.5.1" in err
        assert "1.8.0" not in err          # ChaosBlade's own tag, not ours

    def test_existing_version_missing_a_platform_says_so(
        self, sandbox, monkeypatch, capsys,
    ):
        """Distinguish the two causes: the fix differs for each."""
        monkeypatch.setattr("urllib.request.urlopen", self._api_ok_download_404())
        assert update._download_and_install("0.6.0", "windows-x64") is False
        err = capsys.readouterr().err
        assert "has no build for 'windows-x64'" in err
        assert "not among the published releases" not in err

    def test_unreachable_api_falls_back_to_the_release_page(
        self, sandbox, monkeypatch, capsys,
    ):
        monkeypatch.setattr(
            "urllib.request.urlopen", self._api_ok_download_404(api_reachable=False),
        )
        assert update._download_and_install("99.99.99", "darwin-arm64") is False
        err = capsys.readouterr().err
        assert "Could not list published versions" in err
        assert "releases/tag/blade-ai-v99.99.99" in err

    def test_a_404_reports_what_was_not_published(self, sandbox, monkeypatch, capsys):
        """Whether an artifact exists is the RELEASE's answer, not a guess.

        A hardcoded platform list was the first attempt here and was wrong: it
        would also reject a platform added in a later release, with no code
        change able to reach it. Build the URL, let the 404 be the fact, and pass
        that fact on.
        """
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: (_ for _ in ()).throw(
                urllib.error.HTTPError("u", 404, "Not Found", {}, None)
            ),
        )
        assert update._download_and_install("0.6.0", "windows-x64") is False
        err = capsys.readouterr().err
        assert "Not published" in err
        assert "blade-ai-windows-x64.tar.gz" in err
        assert "releases/tag/blade-ai-v0.6.0" in err   # where to check assets

    def test_a_platform_not_known_today_still_installs(self, sandbox, monkeypatch):
        """The point of dropping the whitelist: future artifacts just work."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"bin"))),
        )
        assert update._download_and_install("9.9.9", "linux-riscv64") is True

    def test_non_404_http_errors_report_the_status(self, sandbox, monkeypatch, capsys):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: (_ for _ in ()).throw(
                urllib.error.HTTPError("u", 503, "Unavailable", {}, None)
            ),
        )
        assert update._download_and_install("0.6.0", "darwin-arm64") is False
        assert "HTTP 503" in capsys.readouterr().err

    def test_missing_versions_dir_is_created(self, monkeypatch):
        """A machine installed elsewhere has no ~/.blade-ai/versions yet."""
        home = Path(tempfile.mkdtemp())          # deliberately empty
        monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~", str(home)))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(update, "_verify_checksum", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"bin"))),
        )
        assert update._download_and_install("0.6.0", "darwin-arm64") is True

    def test_interrupt_leaves_no_temp_directory(self, sandbox, monkeypatch):
        """Cleanup lives in ``finally`` precisely so Ctrl-C is covered."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            update._download_and_install("0.6.0", "darwin-arm64")
        assert list((sandbox / ".blade-ai" / "versions").iterdir()) == []

    def test_network_error_leaves_no_temp_directory(self, sandbox, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: (_ for _ in ()).throw(
                urllib.error.URLError("connection reset")
            ),
        )
        assert update._download_and_install("0.6.0", "darwin-arm64") is False
        assert list((sandbox / ".blade-ai" / "versions").iterdir()) == []

    def test_archive_without_the_binary_is_rejected(self, sandbox, monkeypatch):
        """Extracting cleanly is not proof the payload is usable."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/README", b"docs only"))),
        )
        assert update._download_and_install("0.6.0", "darwin-arm64") is False

    def test_no_dangling_symlink_after_a_bad_archive(self, sandbox, monkeypatch):
        """The worst outcome: ``blade-ai`` on PATH resolving to nothing."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/README", b"docs only"))),
        )
        update._download_and_install("0.6.0", "darwin-arm64")
        link = sandbox / ".local" / "bin" / "blade-ai"
        assert not link.is_symlink()

    def test_existing_install_survives_a_bad_archive(self, sandbox, monkeypatch):
        """A failed update must leave the working version in place."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"good"))),
        )
        assert update._download_and_install("0.6.0", "darwin-arm64") is True

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/README", b"broken"))),
        )
        assert update._download_and_install("0.6.0", "darwin-arm64") is False

        binary = (sandbox / ".blade-ai" / "versions"
                  / "blade-ai-v0.6.0" / "blade-ai")
        assert binary.read_text() == "good"

    def test_download_uses_a_timeout(self, sandbox, monkeypatch):
        """``urlretrieve`` had none, so a stalled peer hung the command."""
        seen = {}

        def capture(url, timeout=None):
            seen["timeout"] = timeout
            return _Resp(_archive_bytes(("top/blade-ai", b"bin")))

        monkeypatch.setattr("urllib.request.urlopen", capture)
        update._download_and_install("0.6.0", "darwin-arm64")
        assert seen["timeout"] == update._DOWNLOAD_TIMEOUT_S

    def test_binary_is_executable(self, sandbox, monkeypatch):
        """A mirror may serve the archive with the mode bits stripped."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"bin"))),
        )
        update._download_and_install("0.6.0", "darwin-arm64")
        binary = (sandbox / ".blade-ai" / "versions"
                  / "blade-ai-v0.6.0" / "blade-ai")
        assert os.access(binary, os.X_OK)


class TestCheckExplainsWhatWouldHappen:
    """``--check`` answers a person, not a script.

    So it says what the situation IS and what to type next, rather than encoding
    the result in an exit status nobody reads. It also verifies a requested
    version exists — promising "would install v99" and only 404-ing on the real
    run is not a preview.
    """

    RELEASES = [
        {"tag_name": "blade-ai-v0.7.0", "draft": False, "prerelease": False},
        {"tag_name": "blade-ai-v0.6.1", "draft": False, "prerelease": False},
        {"tag_name": "blade-ai-v0.5.2", "draft": False, "prerelease": False},
    ]

    @pytest.fixture
    def cli(self):
        app = typer.Typer()
        app.command()(update.update_command)
        return app

    def _run(self, cli, args, *, current="0.6.1", api=True):
        def fake(target, timeout=None):
            if not api:
                raise urllib.error.URLError("offline")
            return _Resp(json.dumps(self.RELEASES).encode())

        with patch.object(update, "_is_standalone_install", return_value=True), \
             patch.object(update, "_current_version", return_value=current), \
             patch("urllib.request.urlopen", side_effect=fake):
            return CliRunner().invoke(cli, args)

    def test_update_available_names_the_command_to_run(self, cli):
        out = self._run(cli, ["--check"]).output
        assert "v0.6.1 → v0.7.0" in out
        assert "Run: blade-ai update" in out

    def test_already_latest_says_nothing_to_do(self, cli):
        out = self._run(cli, ["--check"], current="0.7.0").output
        assert "Already on the latest release" in out
        assert "Run:" not in out          # no action to offer

    def test_downgrade_is_called_out(self, cli):
        """Installing an older version on purpose is fine — but say so."""
        out = self._run(cli, ["--check", "--version", "0.5.2"]).output
        assert "DOWNGRADE" in out
        assert "Run: blade-ai update --version 0.5.2" in out

    def test_same_version_is_described_as_a_repair(self, cli):
        out = self._run(cli, ["--check", "--version", "0.6.1"]).output
        assert "reinstall the running version" in out

    def test_unpublished_version_is_caught_before_downloading(self, cli):
        """The whole point: find out now, not during the real run."""
        result = self._run(cli, ["--check", "--version", "99.0.0"])
        assert "not published" in result.output
        assert "v0.7.0" in result.output and "v0.5.2" in result.output

    def test_no_direction_is_claimed_when_the_list_is_unavailable(self, cli):
        """Without the release list, "newer or older" is unknowable — stay quiet."""
        out = self._run(cli, ["--check", "--version", "0.5.2"], api=False).output
        assert "DOWNGRADE" not in out
        assert "would install" in out

    def test_unreachable_list_without_version_explains_the_fix(self, cli):
        result = self._run(cli, ["--check"], api=False)
        assert "Could not reach the release list" in result.output
        assert "BLADE_AI_MIRROR_API" in result.output


class TestVersionArgumentIsValidated:
    """A version string reaches both a URL and a filesystem path.

    ``mkdtemp`` runs BEFORE the try block, so a value containing a slash raised
    FileNotFoundError from outside any handler and dumped a traceback. The swap
    that follows would also have resolved ``versions/blade-ai-v<value>`` out of
    the versions directory. Nothing about a version needs a separator, so the
    pattern is the whole fix.
    """

    @pytest.mark.parametrize("bad", [
        "0.6.0/../../memory",
        "../x",
        "$(id)",
        "0.6.0;rm -rf /",
        "",
        "latest",
    ])
    def test_download_refuses_malformed_versions(self, sandbox, monkeypatch, bad):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"bin"))),
        )
        # Must return False, not raise: the traceback was the original symptom.
        assert update._download_and_install(bad, "darwin-arm64") is False

    def test_no_temp_directory_is_created_for_a_bad_version(
        self, sandbox, monkeypatch,
    ):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"bin"))),
        )
        update._download_and_install("0.6.0/../..", "darwin-arm64")
        assert list((sandbox / ".blade-ai" / "versions").iterdir()) == []

    @pytest.mark.parametrize("good", ["0.6.0", "1", "1.0", "0.10.0"])
    def test_plain_versions_still_install(self, sandbox, monkeypatch, good):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"bin"))),
        )
        assert update._download_and_install(good, "darwin-arm64") is True

    def test_check_reports_a_typo_as_a_typo(self, monkeypatch):
        """``--check`` never reaches the download, so it validated nothing and
        reported "not published" for what is really malformed input."""
        app = typer.Typer()
        app.command()(update.update_command)
        releases = [{"tag_name": "blade-ai-v0.6.0",
                     "draft": False, "prerelease": False}]
        monkeypatch.setattr(update, "_is_standalone_install", lambda: True)
        monkeypatch.setattr(update, "_current_version", lambda: "0.6.0")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(json.dumps(releases).encode()),
        )
        result = CliRunner().invoke(app, ["--check", "--version", "../etc"])
        assert result.exit_code == 1
        assert "Invalid version" in result.output
        assert "not published" not in result.output


class TestChecksumEntryIsMatchedByFilename:
    """The manifest is matched on its FILENAME field, not by substring.

    ``asset in line`` also matched derived names. A ``checksums.txt`` that lists
    ``blade-ai-darwin-arm64.tar.gz.sig`` before the archive itself handed back the
    signature's hash and reported a MISMATCH for a perfectly good download —
    which stops the update, so a release adding signature files would have broken
    self-update for everyone.
    """

    ASSET = "blade-ai-darwin-arm64.tar.gz"

    @pytest.fixture
    def archive(self, tmp_path):
        path = tmp_path / "a.tar.gz"
        path.write_bytes(b"PAYLOAD")
        return path

    def _digest(self) -> str:
        import hashlib
        return hashlib.sha256(b"PAYLOAD").hexdigest()

    def _verify(self, archive, manifest, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(manifest.encode()),
        )
        return update._verify_checksum(archive, "base", "tag", self.ASSET)

    def test_a_sig_entry_listed_first_is_skipped(self, archive, monkeypatch):
        manifest = (f"{'0' * 64}  {self.ASSET}.sig\n"
                    f"{self._digest()}  {self.ASSET}\n")
        assert self._verify(archive, manifest, monkeypatch) is True

    @pytest.mark.parametrize("sep", ["  ", " ", " *"])
    def test_both_sha256sum_output_modes_parse(self, archive, monkeypatch, sep):
        """Text mode writes two spaces; binary mode prefixes the name with '*'."""
        manifest = f"{self._digest()}{sep}{self.ASSET}\n"
        assert self._verify(archive, manifest, monkeypatch) is True

    def test_a_real_mismatch_still_fails(self, archive, monkeypatch):
        manifest = f"{'0' * 64}  {self.ASSET}\n"
        assert self._verify(archive, manifest, monkeypatch) is False

    def test_another_platform_entry_is_not_borrowed(self, archive, monkeypatch):
        """No entry for this asset means "unverified", never "use that hash"."""
        manifest = f"{'0' * 64}  blade-ai-linux-amd64.tar.gz\n"
        assert self._verify(archive, manifest, monkeypatch) is True

    def test_malformed_lines_are_ignored(self, archive, monkeypatch):
        manifest = ("\n"
                    "# a comment\n"
                    "onlyonefield\n"
                    f"{self._digest()}  {self.ASSET}\n")
        assert self._verify(archive, manifest, monkeypatch) is True


class TestSymlinkIsRepointedAtomically:
    """A failed symlink update must leave the previous command on PATH.

    ``unlink`` then ``symlink_to`` has a window where nothing is on PATH. When the
    second call failed the user was left with a working binary on disk, no
    ``blade-ai`` command at all, and an "Update failed" message implying nothing
    had changed. Staging the new link and ``os.replace``-ing it keeps the old one
    until the new one exists.
    """

    def _break_staged_symlink(self, monkeypatch):
        real = Path.symlink_to

        def flaky(self, target, target_is_directory=False):
            if ".blade-ai.new-" in str(self):
                raise OSError("simulated symlink failure")
            return real(self, target, target_is_directory)

        monkeypatch.setattr(Path, "symlink_to", flaky)

    def test_old_symlink_survives_a_failure(self, sandbox, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"old"))),
        )
        assert update._download_and_install("0.5.2", "darwin-arm64") is True
        link = sandbox / ".local" / "bin" / "blade-ai"
        assert link.exists()

        self._break_staged_symlink(monkeypatch)
        assert update._download_and_install("0.6.0", "darwin-arm64") is False

        assert link.is_symlink(), "the command must not vanish from PATH"
        assert link.exists(), "and it must still resolve"
        assert "blade-ai-v0.5.2" in os.readlink(link)

    def test_no_staged_link_is_left_behind(self, sandbox, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"old"))),
        )
        update._download_and_install("0.5.2", "darwin-arm64")
        self._break_staged_symlink(monkeypatch)
        update._download_and_install("0.6.0", "darwin-arm64")

        strays = [p.name for p in (sandbox / ".local" / "bin").iterdir()
                  if p.name.startswith(".blade-ai.new-")]
        assert strays == [], strays

    def test_the_happy_path_still_repoints(self, sandbox, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"bin"))),
        )
        update._download_and_install("0.5.2", "darwin-arm64")
        update._download_and_install("0.6.0", "darwin-arm64")
        link = sandbox / ".local" / "bin" / "blade-ai"
        assert "blade-ai-v0.6.0" in os.readlink(link)
        assert link.exists()


class TestReplacingTheRunningVersion:
    """Reinstalling the version this process runs from is the repair case.

    The binary ships in PyInstaller ``--onedir`` layout, so the running process
    keeps lazily loading modules and data out of ``<version dir>/_internal/``.
    Renaming that directory aside and deleting it — which is what the swap did —
    makes every later import fail; measured directly: ``ModuleNotFoundError`` for
    a module first imported after the delete.

    Refusing the operation was not an option (it is how a corrupted install gets
    fixed), so the displaced copy stays on disk for the current process and the
    next run reclaims it.
    """

    def _as_frozen_at(self, monkeypatch, version_dir: Path):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(version_dir / "blade-ai"))

    def _serve(self, monkeypatch, payload: bytes = b"new"):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", payload))),
        )

    def test_displaced_copy_is_kept_for_the_running_process(
        self, sandbox, monkeypatch,
    ):
        self._serve(monkeypatch, b"old")
        assert update._download_and_install("0.6.0", "darwin-arm64") is True
        version_dir = sandbox / ".blade-ai" / "versions" / "blade-ai-v0.6.0"

        self._as_frozen_at(monkeypatch, version_dir)
        self._serve(monkeypatch, b"new")
        assert update._download_and_install("0.6.0", "darwin-arm64") is True

        assert (version_dir / "blade-ai").read_text() == "new"
        kept = [p for p in version_dir.parent.iterdir() if ".replaced-" in p.name]
        assert len(kept) == 1, "the running process's files must survive"
        assert (kept[0] / "blade-ai").read_text() == "old"

    def test_the_user_is_told_to_restart(self, sandbox, monkeypatch, capsys):
        self._serve(monkeypatch)
        update._download_and_install("0.6.0", "darwin-arm64")
        version_dir = sandbox / ".blade-ai" / "versions" / "blade-ai-v0.6.0"
        self._as_frozen_at(monkeypatch, version_dir)
        capsys.readouterr()

        update._download_and_install("0.6.0", "darwin-arm64")
        assert "replaced the running binary" in capsys.readouterr().out

    def test_replacing_a_different_version_still_deletes_the_old_copy(
        self, sandbox, monkeypatch,
    ):
        """The keep-it behaviour is scoped to self-replacement only."""
        self._serve(monkeypatch)
        update._download_and_install("0.6.0", "darwin-arm64")
        # Frozen, but running from 0.6.0 while replacing 0.7.0.
        self._as_frozen_at(
            monkeypatch, sandbox / ".blade-ai" / "versions" / "blade-ai-v0.6.0")
        update._download_and_install("0.7.0", "darwin-arm64")
        update._download_and_install("0.7.0", "darwin-arm64")

        versions = sandbox / ".blade-ai" / "versions"
        assert [p.name for p in versions.iterdir() if ".replaced-" in p.name] == []

    def test_the_kept_copy_is_reclaimed_once_it_is_old(self, sandbox, monkeypatch):
        """Otherwise every self-repair would leak a full copy of the bundle.

        Reclamation waits for the age threshold rather than happening on the very
        next run: a fresh work directory may belong to a concurrent process, and
        deleting one of those broke that process's download.
        """
        self._serve(monkeypatch)
        update._download_and_install("0.6.0", "darwin-arm64")
        versions = sandbox / ".blade-ai" / "versions"
        self._as_frozen_at(monkeypatch, versions / "blade-ai-v0.6.0")
        update._download_and_install("0.6.0", "darwin-arm64")

        kept = [p for p in versions.iterdir() if ".replaced-" in p.name]
        assert len(kept) == 1

        # An immediate re-run leaves it: it could still be in use.
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        update._download_and_install("0.7.0", "darwin-arm64")
        assert kept[0].exists()

        # Once past the threshold, a later run reclaims it.
        stamp = time.time() - update._STALE_WORKDIR_AGE_S * 2
        os.utime(kept[0], (stamp, stamp))
        update._download_and_install("0.8.0", "darwin-arm64")

        leftovers = [p.name for p in versions.iterdir()
                     if ".replaced-" in p.name or p.name.startswith(".tmp-")]
        assert leftovers == [], leftovers


class TestUnsupportedOperatingSystem:
    """An OS with no build is not a usage error.

    ``typer.BadParameter`` printed "Invalid value" plus a usage block, which reads
    as "you passed a bad argument" when nothing the user typed was wrong.
    """

    def test_it_reports_the_os_not_a_usage_error(self, monkeypatch):
        app = typer.Typer()
        app.command()(update.update_command)
        releases = [{"tag_name": "blade-ai-v0.7.0",
                     "draft": False, "prerelease": False}]
        monkeypatch.setattr(update, "_is_standalone_install", lambda: True)
        monkeypatch.setattr(update, "_current_version", lambda: "0.6.0")
        monkeypatch.setattr("platform.system", lambda: "FreeBSD")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(json.dumps(releases).encode()),
        )
        result = CliRunner().invoke(app, [])
        assert result.exit_code == 1
        assert "No blade-ai build exists for freebsd" in result.output
        assert "Usage:" not in result.output
        assert "Invalid value" not in result.output


class TestSweepDoesNotDisturbConcurrentRuns:
    """Reclaiming leftovers must not delete a download that is still happening.

    The sweep was introduced to stop ``.replaced-*`` copies accumulating, and it
    deleted every ``.tmp-*`` and ``*.replaced-*`` it found. Names carry no owner
    — ``mkdtemp``'s suffix is random — so "abandoned" and "in use right now" were
    indistinguishable, and it took the second case with the first.

    Measured end to end before the fix: a sweep during another run's download
    removed the half-written archive, and that run died with
    ``tar: ...tar.gz: No such file or directory``. ``install.sh`` uses the same
    ``versions/.tmp-*`` location, so the two implementations could break each
    other too. Age is the discriminator: an hour is far longer than any real
    download.
    """

    def _age(self, path: Path, seconds: int) -> None:
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_a_fresh_temp_dir_is_left_alone(self, sandbox):
        versions = sandbox / ".blade-ai" / "versions"
        live = versions / ".tmp-blade-ai-v0.9.0-live"
        live.mkdir()
        (live / "blade-ai.tar.gz").write_bytes(b"partial")

        update._sweep_stale_workdirs(versions)
        assert live.exists(), "another process may still be downloading into it"

    def test_an_old_temp_dir_is_reclaimed(self, sandbox):
        versions = sandbox / ".blade-ai" / "versions"
        dead = versions / ".tmp-blade-ai-v0.8.0-dead"
        dead.mkdir()
        (dead / "blade-ai.tar.gz").write_bytes(b"junk")
        self._age(dead, update._STALE_WORKDIR_AGE_S * 2)

        update._sweep_stale_workdirs(versions)
        assert not dead.exists()

    def test_an_old_replaced_copy_is_reclaimed(self, sandbox):
        """Otherwise every self-repair leaks a full copy of the bundle."""
        versions = sandbox / ".blade-ai" / "versions"
        kept = versions / "blade-ai-v0.5.0.replaced-1234"
        kept.mkdir()
        (kept / "blade-ai").write_text("old")
        self._age(kept, update._STALE_WORKDIR_AGE_S * 2)

        update._sweep_stale_workdirs(versions)
        assert not kept.exists()

    def test_a_concurrent_download_survives_a_real_update(self, sandbox, monkeypatch):
        """The end-to-end shape of the original failure."""
        versions = sandbox / ".blade-ai" / "versions"
        live = versions / ".tmp-blade-ai-v0.9.0-live"
        live.mkdir()
        (live / "blade-ai.tar.gz").write_bytes(b"partial")

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Resp(_archive_bytes(("top/blade-ai", b"bin"))),
        )
        assert update._download_and_install("0.7.0", "darwin-arm64") is True
        assert (live / "blade-ai.tar.gz").read_bytes() == b"partial"

    def test_an_unreadable_entry_is_skipped_not_fatal(self, sandbox, monkeypatch):
        """stat() can fail on a directory another process just removed."""
        versions = sandbox / ".blade-ai" / "versions"
        (versions / ".tmp-blade-ai-v0.1.0-x").mkdir()

        real_stat = Path.stat

        def flaky(self, *a, **kw):
            if ".tmp-blade-ai-v0.1.0-x" in str(self):
                raise OSError("vanished")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", flaky)
        update._sweep_stale_workdirs(versions)   # must not raise


class TestCheckWorksFromAnyInstall:
    """``--check`` is read-only, so the standalone gate must not block it.

    The gate used to sit at the top of the command and refused everything from a
    pip or source install — including ``--check``, which mutates nothing. Such a
    user could not even find out whether a newer release existed. The gate now
    guards only the download; ``--check`` runs regardless and, when it finds an
    update, points a non-standalone install at the right upgrade path instead of
    ``blade-ai update`` (which they cannot use).
    """

    RELEASES = [
        {"tag_name": "blade-ai-v0.7.0", "draft": False, "prerelease": False},
        {"tag_name": "blade-ai-v0.6.0", "draft": False, "prerelease": False},
    ]

    @pytest.fixture
    def cli(self):
        app = typer.Typer()
        app.command()(update.update_command)
        return app

    def _run(self, cli, args, *, standalone, current="0.6.0"):
        with patch.object(update, "_is_standalone_install", return_value=standalone), \
             patch.object(update, "_current_version", return_value=current), \
             patch("urllib.request.urlopen",
                   lambda *_a, **_k: _Resp(json.dumps(self.RELEASES).encode())):
            return CliRunner().invoke(cli, args)

    def test_check_reports_an_update_on_a_non_standalone_install(self, cli):
        result = self._run(cli, ["--check"], standalone=False)
        assert result.exit_code == 0, result.output
        assert "v0.6.0 → v0.7.0" in result.output
        # and points at the upgrade paths it CAN use, not "blade-ai update"
        assert "pip install --upgrade blade-ai" in result.output
        assert "Run: blade-ai update" not in result.output

    def test_check_says_nothing_to_do_when_current(self, cli):
        result = self._run(cli, ["--check"], standalone=False, current="0.7.0")
        assert result.exit_code == 0
        assert "Already on the latest release" in result.output

    def test_check_with_version_on_non_standalone(self, cli):
        result = self._run(cli, ["--check", "--version", "0.7.0"], standalone=False)
        assert result.exit_code == 0, result.output
        assert "pip install --upgrade blade-ai" in result.output
        assert "Run: blade-ai update" not in result.output

    def test_actual_update_is_still_refused_on_non_standalone(self, cli):
        """The mutation gate stayed — only its position moved."""
        result = self._run(cli, [], standalone=False)
        assert result.exit_code == 1
        assert "cannot self-update" in result.output

    def test_standalone_check_still_says_run_blade_ai_update(self, cli):
        result = self._run(cli, ["--check"], standalone=True)
        assert result.exit_code == 0
        assert "Run: blade-ai update" in result.output
        assert "cannot self-update" not in result.output
