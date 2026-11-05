"""Tests for SKILL.md loader and parser."""

from pathlib import Path

import pytest

from chaos_agent.skills import loader
from chaos_agent.skills.loader import (
    _parse_parameters,
    get_skill_roots,
    get_skills_dir,
    list_skill_resources,
    load_skill_instructions,
    load_skill_metadata,
    load_skill_resource,
    parse_frontmatter,
    resolve_skill_dir,
)


class TestParseFrontmatter:
    """Test YAML frontmatter extraction."""

    def test_valid_frontmatter(self):
        content = """---
name: test-skill
description: A test
category: network
---
## Instructions
Do stuff.
"""
        result = parse_frontmatter(content)
        assert result is not None
        assert result["name"] == "test-skill"
        assert result["description"] == "A test"
        assert result["category"] == "network"

    def test_missing_separator(self):
        content = "No frontmatter here\nJust content"
        result = parse_frontmatter(content)
        assert result is None

    def test_only_one_separator(self):
        content = "---\nname: test\nNo closing separator"
        result = parse_frontmatter(content)
        assert result is None

    def test_invalid_yaml(self):
        content = "---\n: invalid yaml: [\n---\nContent"
        result = parse_frontmatter(content)
        assert result is None

    def test_empty_frontmatter(self):
        content = "---\n---\nContent"
        result = parse_frontmatter(content)
        # Empty YAML between separators parses as None
        assert result is None or result == {}

    def test_frontmatter_with_parameters(self):
        content = """---
name: skill
description: desc
parameters:
  - name: time
    type: int
    required: true
---
Body
"""
        result = parse_frontmatter(content)
        assert result is not None
        assert len(result["parameters"]) == 1
        assert result["parameters"][0]["name"] == "time"


class TestLoadSkillMetadata:
    """Test Tier 1 metadata loading."""

    def test_load_from_valid_skill(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        meta = load_skill_metadata(skill_dir)
        assert meta.name == "test-skill"
        assert meta.description == "A test skill for unit testing"
        assert meta.category == "test"
        assert meta.target == "pod"
        assert "blade" in meta.required_tools

    def test_load_with_parameters(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        meta = load_skill_metadata(skill_dir)
        assert len(meta.parameters) == 1
        assert meta.parameters[0].name == "time"
        assert meta.parameters[0].type == "int"
        assert meta.parameters[0].required is True

    def test_missing_frontmatter_raises(self, tmp_path):
        skill_dir = tmp_path / "bad-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("No frontmatter here", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid or missing"):
            load_skill_metadata(skill_dir)


class TestLoadSkillInstructions:
    """Test Tier 2 instructions loading."""

    def test_extracts_markdown_body(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        instructions = load_skill_instructions(skill_dir)
        assert "Pre-checks" in instructions
        assert "Injection Procedure" in instructions

    def test_empty_body(self, tmp_path):
        skill_dir = tmp_path / "empty-body"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\ndescription: x\n---", encoding="utf-8")
        result = load_skill_instructions(skill_dir)
        assert result == ""


class TestLoadSkillResource:
    """Test Tier 3 resource loading."""

    def test_read_existing_resource(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        content = load_skill_resource(skill_dir, "scripts/verify.py")
        assert "verify" in content

    def test_read_reference_file(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        content = load_skill_resource(skill_dir, "references/troubleshooting.md")
        assert "Troubleshooting" in content

    def test_nonexistent_resource_raises(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        with pytest.raises(FileNotFoundError):
            load_skill_resource(skill_dir, "nonexistent.txt")

    def test_parent_traversal_is_blocked(self, tmp_skills_dir):
        """read_skill_resource is LLM-callable, so resource_path is untrusted.

        A ``..`` sequence that escapes the skill directory must be rejected
        before any file read — verified this actually leaked the repo's
        pyproject.toml prior to the containment guard.
        """
        skill_dir = tmp_skills_dir / "test-skill"
        # Plant a secret OUTSIDE the skill dir, then try to reach it.
        secret = tmp_skills_dir / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        with pytest.raises(ValueError, match="escapes skill directory"):
            load_skill_resource(skill_dir, "../secret.txt")

    def test_absolute_path_escape_is_blocked(self, tmp_skills_dir):
        """``skill_dir / "/etc/hosts"`` collapses to ``/etc/hosts`` in pathlib,
        so an absolute path is an escape vector distinct from ``..``."""
        skill_dir = tmp_skills_dir / "test-skill"
        with pytest.raises(ValueError, match="escapes skill directory"):
            load_skill_resource(skill_dir, "/etc/hosts")

    def test_symlink_escape_is_blocked(self, tmp_skills_dir):
        """A symlink inside the skill dir pointing outside must not be a
        read primitive: .resolve() collapses it and the containment check
        rejects the real target."""
        skill_dir = tmp_skills_dir / "test-skill"
        outside = tmp_skills_dir / "outside.txt"
        outside.write_text("OUTSIDE", encoding="utf-8")
        link = skill_dir / "references" / "escape.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        with pytest.raises(ValueError, match="escapes skill directory"):
            load_skill_resource(skill_dir, "references/escape.md")


class TestListSkillResources:
    """Test resource file enumeration."""

    def test_lists_all_resources(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        resources = list_skill_resources(skill_dir)
        assert any("scripts" in r for r in resources)
        assert any("verify.py" in r for r in resources)
        assert any("references" in r for r in resources)

    def test_no_resources(self, tmp_path):
        skill_dir = tmp_path / "bare-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\ndescription: x\n---", encoding="utf-8")
        resources = list_skill_resources(skill_dir)
        assert resources == []


class TestGetSkillsDir:
    """Test skills directory resolution."""

    @staticmethod
    def _make_skill_dir(parent: Path, name: str = "test-skill") -> Path:
        """Create a directory with a valid SKILL.md inside."""
        skill_dir = parent / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: test\n---\nbody", encoding="utf-8"
        )
        return parent

    def test_env_var_used_when_no_config_override(self, monkeypatch, tmp_path):
        """When config.json skills_dir is empty, env var takes effect."""
        env_skills = self._make_skill_dir(tmp_path / "env_skills")
        monkeypatch.setattr("chaos_agent.config.settings.settings.skills_dir", Path("/nonexistent/skills"))
        monkeypatch.setenv("BLADE_AI_SKILLS_DIR", str(env_skills))
        result = get_skills_dir()
        assert result == env_skills

    def test_config_overrides_env_var(self, monkeypatch, tmp_path):
        """When config.json sets a skills_dir with skills, it overrides env var."""
        config_skills = self._make_skill_dir(tmp_path / "config_skills")
        monkeypatch.setattr("chaos_agent.config.settings.settings.skills_dir", config_skills)
        monkeypatch.setenv("BLADE_AI_SKILLS_DIR", "/should/not/be/used")
        result = get_skills_dir()
        assert result == config_skills

    def test_empty_dir_skipped(self, monkeypatch, tmp_path):
        """An empty skills_dir should be skipped, falling through to lower priority."""
        empty_dir = tmp_path / "empty_skills"
        empty_dir.mkdir()
        monkeypatch.setattr("chaos_agent.config.settings.settings.skills_dir", empty_dir)
        result = get_skills_dir()
        # Should NOT return the empty dir, should fall through to dev path or fallback
        assert result != empty_dir


class TestParseParameters:
    """Test parameter list parsing."""

    def test_valid_parameters(self):
        data = [
            {"name": "time", "type": "int", "required": True},
            {"name": "offset", "type": "int", "required": False, "default": "0"},
        ]
        params = _parse_parameters(data)
        assert len(params) == 2
        assert params[0].name == "time"
        assert params[0].required is True
        assert params[1].default == "0"

    def test_empty_list(self):
        assert _parse_parameters([]) == []

    def test_none_input(self):
        assert _parse_parameters(None) == []

    def test_non_dict_entry_skipped(self):
        params = _parse_parameters(["not a dict"])
        assert params == []

    def test_partial_dict(self):
        params = _parse_parameters([{"name": "x"}])
        assert len(params) == 1
        assert params[0].name == "x"
        assert params[0].type == "string"


def _mk_skill(parent: Path, dir_name: str, fm_name: str | None = None) -> Path:
    """Create ``<parent>/<dir_name>/SKILL.md`` (frontmatter name = fm_name or dir_name)."""
    d = parent / dir_name
    d.mkdir(parents=True, exist_ok=True)
    name = fm_name or dir_name
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test {name}\ncategory: test\n---\nbody",
        encoding="utf-8",
    )
    return d


class TestGetSkillRoots:
    """Merged-root resolution: user root first, built-in second, de-duped."""

    def test_user_root_first_then_builtin(self, monkeypatch, tmp_path):
        user = tmp_path / "user"
        user.mkdir()
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        monkeypatch.setattr(loader, "get_skills_dir", lambda: user)
        monkeypatch.setattr(loader, "get_builtin_skills_dir", lambda: builtin)
        assert get_skill_roots() == [user, builtin]

    def test_dedup_when_both_same_dir(self, monkeypatch, tmp_path):
        """Dev/frozen case: user root == built-in root → returned once."""
        same = tmp_path / "skills"
        same.mkdir()
        monkeypatch.setattr(loader, "get_skills_dir", lambda: same)
        monkeypatch.setattr(loader, "get_builtin_skills_dir", lambda: same)
        assert get_skill_roots() == [same]

    def test_builtin_none_yields_user_only(self, monkeypatch, tmp_path):
        user = tmp_path / "user"
        user.mkdir()
        monkeypatch.setattr(loader, "get_skills_dir", lambda: user)
        monkeypatch.setattr(loader, "get_builtin_skills_dir", lambda: None)
        assert get_skill_roots() == [user]


class TestResolveSkillDir:
    """Cross-root skill-dir resolution used by safety-critical readers."""

    def test_finds_in_user_root(self, monkeypatch, tmp_path):
        user = tmp_path / "user"
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        _mk_skill(user, "my-skill")
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [user, builtin])
        assert resolve_skill_dir("my-skill") == user / "my-skill"

    def test_user_root_wins_on_name_clash(self, monkeypatch, tmp_path):
        user = tmp_path / "user"
        builtin = tmp_path / "builtin"
        _mk_skill(user, "k8s-chaos-skills")
        _mk_skill(builtin, "k8s-chaos-skills")
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [user, builtin])
        assert resolve_skill_dir("k8s-chaos-skills") == user / "k8s-chaos-skills"

    def test_falls_back_to_builtin(self, monkeypatch, tmp_path):
        user = tmp_path / "user"
        user.mkdir()
        builtin = tmp_path / "builtin"
        _mk_skill(builtin, "only-builtin")
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [user, builtin])
        assert resolve_skill_dir("only-builtin") == builtin / "only-builtin"

    def test_missing_returns_none(self, monkeypatch, tmp_path):
        user = tmp_path / "user"
        user.mkdir()
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [user])
        assert resolve_skill_dir("nope") is None

    def test_traversal_and_empty_rejected(self, monkeypatch, tmp_path):
        """Authorization path: skill_name may be LLM-influenced — reject escapes."""
        user = tmp_path / "user"
        user.mkdir()
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [user])
        assert resolve_skill_dir("../etc") is None
        assert resolve_skill_dir("a/b") is None
        assert resolve_skill_dir("") is None
        assert resolve_skill_dir(".") is None


class TestResolveSkillDirKeysOnFrontmatterName:
    """``resolve_skill_dir`` keys on frontmatter ``name`` — the SAME authority
    the registry uses — NOT on the directory basename.

    Before the fix these were two independent name→dir authorities that only
    agreed under the unenforced invariant ``dirname == frontmatter name``.
    The measured divergence: an override dir ``my-override`` declaring
    ``name: k8s-chaos-skills`` shadowed the built-in in the registry (which
    keys by frontmatter name), yet basename matching fell through to the
    BUILT-IN ``k8s-chaos-skills`` dir — so the guard read the built-in case
    while the model ran the override. Identity is now guaranteed by
    DELEGATION (``resolve_skill_dir`` calls ``SkillRegistry.resolve_dir``,
    which runs the registry's own ``_scan_directory``), so the two cannot
    drift on basename OR on any load filter (see the matrix class below).
    """

    def test_agrees_with_registry_when_dir_name_differs_from_frontmatter_name(
        self, monkeypatch, tmp_path
    ):
        """The exact divergence scenario: user override dir basename != name."""
        from chaos_agent.skills.registry import SkillRegistry

        user = tmp_path / "user"
        builtin = tmp_path / "builtin"
        # User override lives in a dir whose basename ("my-override") differs
        # from the frontmatter name it declares ("k8s-chaos-skills").
        _mk_skill(user, "my-override", fm_name="k8s-chaos-skills")
        _mk_skill(builtin, "k8s-chaos-skills")
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [user, builtin])

        registry = SkillRegistry()
        registry.load_from_roots([user, builtin])
        loaded = registry.get_skill_dir("k8s-chaos-skills")

        # Registry loads the user override (first-wins by frontmatter name).
        assert loaded == user / "my-override"
        # THE invariant: resolve_skill_dir returns the SAME dir the registry
        # loaded — no second, disagreeing authority. Before A′ this returned
        # builtin/"k8s-chaos-skills" (basename match) and the assert failed.
        assert resolve_skill_dir("k8s-chaos-skills") == loaded

    def test_resolves_by_frontmatter_name_not_basename(self, monkeypatch, tmp_path):
        """A dir is found by its declared name; its basename is NOT a key."""
        user = tmp_path / "user"
        _mk_skill(user, "weird-dirname", fm_name="real-name")
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [user])

        assert resolve_skill_dir("real-name") == user / "weird-dirname"
        # The directory's basename is no longer an alias for the skill.
        assert resolve_skill_dir("weird-dirname") is None


class TestGetSkillsDirDoesNotMaterialize:
    """Root-fix: ``get_skills_dir()`` must NOT copy the built-in pack into the
    writable user root. Materialization was removed — a fresh install's user
    root stays EMPTY and the built-in pack is merge-loaded read-only via
    ``get_builtin_skills_dir()``. The old copy never refreshed and, being
    same-named, shadowed the fresh built-in pack in the merge (the
    stale-shadow bug this removal fixes)."""

    def test_empty_user_root_created_but_not_populated(self, monkeypatch, tmp_path):
        """An absent/empty config dir is created and returned, but NOT filled
        with a copy of the built-in pack — even when a built-in pack exists."""
        config_dir = tmp_path / "user_root"  # does not exist yet
        builtin = tmp_path / "builtin"
        _mk_skill(builtin, "k8s-chaos-skills")
        monkeypatch.setattr(
            "chaos_agent.config.settings.settings.skills_dir", config_dir
        )
        monkeypatch.delenv("BLADE_AI_SKILLS_DIR", raising=False)
        # Only the built-in dir "has skills"; the config dir and the dev repo
        # path report False, forcing fall-through to the create-empty branch
        # (priority 5) instead of the dev path (priority 4).
        monkeypatch.setattr(loader, "_has_skills", lambda p: p == builtin)

        result = get_skills_dir()

        assert result == config_dir
        assert config_dir.is_dir()               # created
        assert list(config_dir.iterdir()) == []  # but EMPTY — nothing copied out

    def test_builtin_still_loads_via_merge_when_user_root_empty(self, tmp_path):
        """The built-in pack is reachable through the merge even though the
        user root is empty — proving materialization was never load-bearing."""
        from chaos_agent.skills.registry import SkillRegistry

        user = tmp_path / "user"
        user.mkdir()  # empty user root
        builtin = tmp_path / "builtin"
        _mk_skill(builtin, "k8s-chaos-skills")

        registry = SkillRegistry()
        registry.load_from_roots([user, builtin])

        assert (
            registry.get_skill_dir("k8s-chaos-skills")
            == builtin / "k8s-chaos-skills"
        )


class TestResolveSkillDirDelegatesToRegistryAuthority:
    """Root-fix invariant: ``resolve_skill_dir`` and the registry are ONE
    authority (delegation via ``SkillRegistry.resolve_dir`` → the registry's
    own ``_scan_directory``), so they agree under EVERY load filter — not just
    frontmatter-name keying.

    Each scenario below is a case a hand-rolled copy of the scan (matching the
    name but omitting validation / disabled / dup handling) resolves
    DIFFERENTLY from the registry. The equality assertion is the anti-drift
    net: it reddens if ``resolve_skill_dir`` ever stops delegating and grows a
    second, diverging implementation again.
    """

    def _invalid_skill(self, parent: Path, dir_name: str, fm_name: str) -> Path:
        """A dir declaring ``fm_name`` but MISSING required ``description`` —
        the validator rejects it, so the registry never loads it."""
        d = parent / dir_name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {fm_name}\ncategory: test\n---\nbody", encoding="utf-8"
        )
        return d

    def _assert_agrees(self, monkeypatch, roots, name):
        from chaos_agent.skills.registry import SkillRegistry

        monkeypatch.setattr(loader, "get_skill_roots", lambda: list(roots))
        registry = SkillRegistry()
        registry.load_from_roots(list(roots))
        expected = registry.get_skill_dir(name)
        assert resolve_skill_dir(name) == expected
        return expected

    def test_invalid_user_shadow_falls_through_to_valid_builtin(
        self, monkeypatch, tmp_path
    ):
        """The proven divergence: an INVALID user-root dir sharing the name
        must NOT win — the registry skips it and loads the valid built-in, and
        resolve_skill_dir must land on the SAME valid built-in. A name-only
        copy returned the invalid user dir (guard read the wrong case)."""
        user = tmp_path / "user"
        builtin = tmp_path / "builtin"
        self._invalid_skill(user, "foo", "foo")  # missing description -> invalid
        _mk_skill(builtin, "foo")  # valid
        assert self._assert_agrees(monkeypatch, [user, builtin], "foo") == builtin / "foo"

    def test_disabled_skill_resolves_to_none_like_registry(self, monkeypatch, tmp_path):
        """A disabled skill is loaded by NO authority: the registry returns
        None and resolve_skill_dir must too (a name-only copy returned the
        dir, contradicting its 'the dir load_from_roots loaded' contract)."""
        from chaos_agent.config.settings import settings
        from chaos_agent.skills.registry import SkillRegistry

        root = tmp_path / "root"
        _mk_skill(root, "bar")
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [root])
        old = settings.disabled_skills
        try:
            settings.disabled_skills = ["bar"]
            registry = SkillRegistry()
            registry.load_from_roots([root])
            assert registry.get_skill_dir("bar") is None
            assert resolve_skill_dir("bar") is None
        finally:
            settings.disabled_skills = old

    def test_duplicate_name_same_root_first_sorted_wins(self, monkeypatch, tmp_path):
        """Two valid dirs in ONE root declaring the same name: the registry
        keeps the first in ``sorted()`` order; resolve_skill_dir must match."""
        root = tmp_path / "root"
        _mk_skill(root, "zzz-dup", fm_name="dup")
        first = _mk_skill(root, "aaa-dup", fm_name="dup")  # sorts first -> wins
        assert self._assert_agrees(monkeypatch, [root], "dup") == first

    def test_valid_cross_root_user_wins(self, monkeypatch, tmp_path):
        """Baseline: a valid user override shadows a valid built-in
        (first-root wins) and both authorities agree on the user dir."""
        user = tmp_path / "user"
        builtin = tmp_path / "builtin"
        u = _mk_skill(user, "k8s", fm_name="k8s-chaos-skills")
        _mk_skill(builtin, "k8s-chaos-skills")
        assert self._assert_agrees(monkeypatch, [user, builtin], "k8s-chaos-skills") == u
