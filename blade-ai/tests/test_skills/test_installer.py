"""Tests for the skill installer's on-disk naming contract.

B write-site #1: ``install_skill`` / ``_copy_skill`` must name the installed
directory after the skill's canonical frontmatter ``name`` — NOT the source
directory's basename — so the installed layout upholds the system-wide
invariant ``dirname == frontmatter name``. That invariant is what lets the
registry (keyed by frontmatter name) and ``resolve_skill_dir`` (A′: also
keyed by frontmatter name) agree on which directory a skill lives in.
"""

import asyncio
from pathlib import Path

import pytest

from chaos_agent.skills import installer as installer_mod
from chaos_agent.skills.installer import SkillInstallError, install_skill


def _mk_skill(parent: Path, dir_name: str, fm_name: str) -> Path:
    """Create ``<parent>/<dir_name>/SKILL.md`` declaring frontmatter name ``fm_name``."""
    d = parent / dir_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {fm_name}\ndescription: test {fm_name}\ncategory: test\n---\nbody",
        encoding="utf-8",
    )
    return d


class TestInstallNamesDirAfterFrontmatterName:
    async def test_installed_dir_uses_frontmatter_name_not_source_basename(self, tmp_path):
        """Source dir basename != frontmatter name → installed under the name."""
        src_root = tmp_path / "src"
        src = _mk_skill(src_root, "src-dirname", "canonical-name")
        target = tmp_path / "installed"

        installed = await install_skill(str(src), skills_dir=target)

        assert len(installed) == 1
        assert installed[0].name == "canonical-name"
        assert installed[0].target_dir == target / "canonical-name"
        assert (target / "canonical-name" / "SKILL.md").is_file()
        # The source basename must NOT leak into the installed layout.
        assert not (target / "src-dirname").exists()

    async def test_common_path_dirname_equals_name_still_installs(self, tmp_path):
        """Regression guard: the normal case (basename == name) is unchanged."""
        src_root = tmp_path / "src"
        src = _mk_skill(src_root, "my-skill", "my-skill")
        target = tmp_path / "installed"

        installed = await install_skill(str(src), skills_dir=target)

        assert len(installed) == 1
        assert installed[0].target_dir == target / "my-skill"
        assert (target / "my-skill" / "SKILL.md").is_file()


class TestGitCloneTimeoutReap:
    """``_git_clone``'s timeout branch must ``kill()`` AND ``await wait()`` the
    child — the deterministic-reap shape shared by the other six asyncio spawn
    sites (shell / env_info / preflight / wizard_validators). Non-payload under
    ThreadedChildWatcher (the watcher thread reaps the zombie and the loop
    closes the pipes on EOF regardless — probe:
    ``.b4tmp/_gitclone_reap_probe.py``), but pinned so this lone former
    asymmetry cannot silently regress back to ``kill()`` alone."""

    async def test_timeout_kills_then_awaits_child(self, monkeypatch):
        calls: list[str] = []

        class _FakeProc:
            returncode = None

            async def communicate(self, *a, **k):  # pragma: no cover - never completes
                await asyncio.sleep(3600)

            def kill(self):
                calls.append("kill")

            async def wait(self):
                calls.append("wait")
                return 0

        async def _fake_exec(*a, **k):
            return _FakeProc()

        async def _fast_timeout(awaitable, timeout):
            # Trigger the timeout branch without sleeping the real 120s; close
            # the pending communicate() coroutine so no RuntimeWarning leaks.
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(
            installer_mod.asyncio, "create_subprocess_exec", _fake_exec
        )
        monkeypatch.setattr(installer_mod.asyncio, "wait_for", _fast_timeout)

        with pytest.raises(SkillInstallError, match="timed out"):
            await installer_mod._git_clone(
                "https://example.com/repo.git", Path("/tmp/never-written")
            )

        assert calls == ["kill", "wait"], (
            f"timeout branch must kill then await the child; got {calls}"
        )
