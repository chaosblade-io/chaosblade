"""Contract tests for skills/capabilities.py — the runtime registry reader.

Pinned behaviors (single source of truth, NO fallback fabrication):
1. Explicit path loads directly (test/CLI injection point).
2. Local sync output overrides the bundled registry ONLY when it is v2 AND
   its lang matches the requested locale (v1 files / other langs fall through).
3. Local override is staleness-checked against the distillation-input
   fingerprint — which covers exactly what a sync run reads, so editing
   anything else must NOT invalidate the registry.
4. Bundled registry serves when no applicable local file exists.
5. Both sources missing → CapabilitiesMissingError (never silent empty data).
6. Wrong schema_version → CapabilitiesError.
7. Local registries are per language; the pre-split single-name file is still
   honoured, and the two languages no longer evict each other.
8. The distillation cache is per language too, and the CLI derives both the
   accepted ``--lang`` set and the "refresh the other language" hint from the
   same ``LANG_TAG_BY_RUN`` mapping that picks the files.
9. The language-agnostic readers (``blade-ai list``, ``GET /skills``) name the
   language and file they read, since "the last sync" stopped being unique.
10. The CLI declares the generator (model + endpoint) to ``sync_capabilities``,
    which is what makes the cache identity cover a model switch.
11. The skills fingerprint covers exactly what a sync run reads: a skill with no
    case markdown under ``catalogue/<category>/`` contributes nothing, whether
    it is newly authored or has had its cases removed. Adding one must not judge
    existing registries stale; adding a skill WITH cases must.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from chaos_agent.skills import capabilities as caps


def _use_local(monkeypatch, *paths) -> None:
    """Point the local-override lookup at ``paths`` (missing ones are fine).

    ``load_capabilities`` resolves through ``local_registry_candidates``, so
    patching that one hook is what keeps the real ``~/.blade-ai/memory`` —
    which on a developer machine does hold a registry — out of the test.
    """
    monkeypatch.setattr(caps, "local_registry_candidates", lambda locale: [Path(p) for p in paths])


def _registry(lang: str, fingerprint: str = "", total: int = 1,
              generated_at: str = "2026-08-05T00:00:00+00:00") -> dict:
    return {
        "schema_version": 2,
        "lang": lang,
        "generated_at": generated_at,
        "skills_fingerprint": fingerprint,
        "total": total,
        "cases": [
            {
                "id": "host.host.cpu.fullload",
                "family": "host", "profile": "host",
                "scope": "host", "target": "cpu", "action": "fullload",
                "cluster_scoped": True,
                "title": "进程CPU满载" if lang == "zh" else "CPU fullload",
                "category": "Host_CPU使用率过高",
                "symptom": "s", "case_path": "references/catalogue/x.md",
                "nl_cmd": "blade-ai inject -i \"...\"",
                "params": [
                    {"name": "host", "kind": "target_resource", "resolved_from": "environment",
                     "required": True, "default": "", "description": "target host"},
                ],
                "executable": True,
            }
        ],
    }


def _write(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestExplicitPath:
    def test_load_from_explicit_path(self, tmp_path):
        p = tmp_path / "reg.json"
        _write(p, _registry("zh"))
        reg = caps.load_capabilities("zh", path=p)
        assert reg.total == 1
        case = reg.cases[0]
        assert case.id == "host.host.cpu.fullload"
        assert case.params[0].resolved_from == "environment"
        assert reg.families() == ["host"]
        assert reg.case_by_id("host.host.cpu.fullload") is case
        assert reg.case_by_id("nope") is None

    def test_wrong_schema_version_raises(self, tmp_path):
        p = tmp_path / "reg.json"
        data = _registry("zh")
        data["schema_version"] = 1
        _write(p, data)
        with pytest.raises(caps.CapabilitiesError):
            caps.load_capabilities("zh", path=p)


class TestResolutionOrder:
    def test_local_v2_matching_lang_overrides_bundled(self, tmp_path, monkeypatch):
        local = tmp_path / "local.json"
        bundled = tmp_path / "bundled.json"
        _write(local, _registry("zh"))
        _write(bundled, _registry("zh", total=2))
        _use_local(monkeypatch, local)
        monkeypatch.setattr(caps, "bundled_registry_path", lambda locale: bundled)

        reg = caps.load_capabilities("zh")
        assert reg.source == str(local)

    def test_local_other_lang_falls_through_to_bundled(self, tmp_path, monkeypatch):
        local = tmp_path / "local.json"
        bundled = tmp_path / "bundled.json"
        _write(local, _registry("zh"))          # 本地只有中文版
        _write(bundled, _registry("en"))
        _use_local(monkeypatch, local)
        monkeypatch.setattr(caps, "bundled_registry_path", lambda locale: bundled)

        reg = caps.load_capabilities("en")
        assert reg.source == str(bundled)

    def test_legacy_v1_local_is_ignored(self, tmp_path, monkeypatch):
        local = tmp_path / "local.json"
        bundled = tmp_path / "bundled.json"
        _write(local, {"generated_at": "x", "total": 1, "cases": []})  # v1 shape
        _write(bundled, _registry("zh"))
        _use_local(monkeypatch, local)
        monkeypatch.setattr(caps, "bundled_registry_path", lambda locale: bundled)

        reg = caps.load_capabilities("zh")
        assert reg.source == str(bundled)

    def test_both_missing_raises_missing(self, tmp_path, monkeypatch):
        _use_local(monkeypatch, tmp_path / "nope.json")
        monkeypatch.setattr(caps, "bundled_registry_path",
                            lambda locale: tmp_path / "also_nope.json")
        with pytest.raises(caps.CapabilitiesMissingError):
            caps.load_capabilities("zh")


class TestStaleness:
    def test_stale_local_registry_raises(self, tmp_path, monkeypatch):
        local = tmp_path / "local.json"
        _write(local, _registry("zh", fingerprint="stale-fingerprint"))
        _use_local(monkeypatch, local)
        monkeypatch.setattr(caps, "compute_skills_fingerprint", lambda: "current-fingerprint")
        bundled = tmp_path / "bundled.json"
        monkeypatch.setattr(caps, "bundled_registry_path", lambda locale: bundled)

        with pytest.raises(caps.CapabilitiesStaleError):
            caps.load_capabilities("zh")

    def test_matching_fingerprint_passes(self, tmp_path, monkeypatch):
        local = tmp_path / "local.json"
        _write(local, _registry("zh", fingerprint="fp-1"))
        _use_local(monkeypatch, local)
        monkeypatch.setattr(caps, "compute_skills_fingerprint", lambda: "fp-1")

        reg = caps.load_capabilities("zh")
        assert reg.source == str(local)

    def test_stale_error_names_both_fingerprints(self, tmp_path, monkeypatch):
        """The operator has to be able to see the mismatch, not just be told
        to re-run a minutes-long command."""
        local = tmp_path / "local.json"
        _write(local, _registry("zh", fingerprint="fp-registry"))
        _use_local(monkeypatch, local)
        monkeypatch.setattr(caps, "compute_skills_fingerprint", lambda: "fp-current")
        monkeypatch.setattr(caps, "bundled_registry_path",
                            lambda locale: tmp_path / "bundled.json")

        with pytest.raises(caps.CapabilitiesStaleError) as exc:
            caps.load_capabilities("zh")
        assert "fp-registry" in str(exc.value)
        assert "fp-current" in str(exc.value)

    def test_stale_error_names_both_possible_causes(self, tmp_path, monkeypatch):
        """A hash mismatch cannot distinguish "the case docs moved" from "blade-ai
        was upgraded and now fingerprints differently". Blaming only the first
        sends the operator hunting for an edit that never happened — which is
        what every install hits once, because the fingerprint algorithm itself
        changed in this release."""
        local = tmp_path / "local.json"
        _write(local, _registry("zh", fingerprint="fp-registry",
                                generated_at="2026-10-26T06:37:43+00:00"))
        _use_local(monkeypatch, local)
        monkeypatch.setattr(caps, "compute_skills_fingerprint", lambda: "fp-current")
        monkeypatch.setattr(caps, "bundled_registry_path",
                            lambda locale: tmp_path / "bundled.json")

        with pytest.raises(caps.CapabilitiesStaleError) as exc:
            caps.load_capabilities("zh")
        msg = str(exc.value)
        assert "2026-10-26T06:37:43+00:00" in msg   # when the artifact was built
        assert "upgrade" in msg                       # cause 2 is named, not implied
        assert "capabilities-sync" in msg             # and the way out is stated


# ---------------------------------------------------------------------------
# Per-language local registries
# ---------------------------------------------------------------------------

class TestPerLanguageLocalFiles:
    """Regression: a single ``skill_capabilities.json`` made the two languages
    evict each other. Syncing ``--lang en`` overwrote the zh override, and a zh
    consumer then silently fell through to the older bundled artifact — the
    platform loads BOTH locales, so one run always left one of them behind."""

    def test_local_filename_carries_the_locale(self):
        assert caps.local_registry_path("zh").name == "skill_capabilities_zh.json"
        assert caps.local_registry_path("en").name == "skill_capabilities_en.json"
        assert caps.legacy_local_registry_path().name == "skill_capabilities.json"

    def test_candidates_try_language_specific_then_legacy(self):
        specific, legacy = caps.local_registry_candidates("zh")
        assert specific == caps.local_registry_path("zh")
        assert legacy == caps.legacy_local_registry_path()

    def test_legacy_file_still_overrides_bundled(self, tmp_path, monkeypatch):
        """An override generated before the split keeps working after upgrade."""
        legacy = tmp_path / "skill_capabilities.json"
        bundled = tmp_path / "bundled.json"
        _write(legacy, _registry("zh", total=2))
        _write(bundled, _registry("zh"))
        _use_local(monkeypatch, tmp_path / "skill_capabilities_zh.json", legacy)
        monkeypatch.setattr(caps, "bundled_registry_path", lambda locale: bundled)

        reg = caps.load_capabilities("zh")
        assert reg.source == str(legacy)
        assert reg.total == 2

    def test_language_specific_wins_over_legacy(self, tmp_path, monkeypatch):
        specific = tmp_path / "skill_capabilities_zh.json"
        legacy = tmp_path / "skill_capabilities.json"
        _write(specific, _registry("zh", total=3))
        _write(legacy, _registry("zh", total=2))
        _use_local(monkeypatch, specific, legacy)
        monkeypatch.setattr(caps, "bundled_registry_path",
                            lambda locale: tmp_path / "bundled.json")

        assert caps.load_capabilities("zh").total == 3

    def test_zh_and_en_overrides_coexist(self, tmp_path, monkeypatch):
        zh = tmp_path / "skill_capabilities_zh.json"
        en = tmp_path / "skill_capabilities_en.json"
        _write(zh, _registry("zh"))
        _write(en, _registry("en"))
        monkeypatch.setattr(caps, "local_registry_candidates",
                            lambda locale: [tmp_path / f"skill_capabilities_{locale}.json"])
        monkeypatch.setattr(caps, "bundled_registry_path",
                            lambda locale: tmp_path / f"bundled_{locale}.json")

        assert caps.load_capabilities("zh").source == str(zh)
        assert caps.load_capabilities("en").source == str(en)

    def test_existing_local_registries_dedupes_legacy(self, tmp_path, monkeypatch):
        """The legacy path appears in both locales' candidate lists; a
        language-agnostic consumer must not see it twice."""
        legacy = tmp_path / "skill_capabilities.json"
        _write(legacy, _registry("zh"))
        monkeypatch.setattr(caps, "local_registry_candidates",
                            lambda locale: [tmp_path / f"skill_capabilities_{locale}.json", legacy])

        assert caps.existing_local_registries() == [legacy]

    def test_newer_other_lang_local_override_is_reported(self, tmp_path, monkeypatch, caplog):
        """The remaining silent outcome, made visible: this locale has no local
        override, and the bundled artifact being served is OLDER than the
        override sitting right next to it."""
        zh = tmp_path / "skill_capabilities_zh.json"
        bundled = tmp_path / "bundled.json"
        _write(zh, _registry("zh", generated_at="2026-10-30T00:00:00+00:00"))
        _write(bundled, _registry("en", generated_at="2026-10-26T00:00:00+00:00"))
        _use_local(monkeypatch, zh)
        monkeypatch.setattr(caps, "bundled_registry_path", lambda locale: bundled)

        with caplog.at_level("WARNING", logger="chaos_agent.skills.capabilities"):
            reg = caps.load_capabilities("en")

        assert reg.source == str(bundled)
        assert "NEWER local" in caplog.text
        assert "--lang en" in caplog.text  # the hint names the run that fixes it

    def test_older_other_lang_local_override_stays_quiet(self, tmp_path, monkeypatch, caplog):
        """Falling back to a NEWER bundled artifact is normal, not a warning."""
        zh = tmp_path / "skill_capabilities_zh.json"
        bundled = tmp_path / "bundled.json"
        _write(zh, _registry("zh", generated_at="2026-08-01T00:00:00+00:00"))
        _write(bundled, _registry("en", generated_at="2026-10-26T00:00:00+00:00"))
        _use_local(monkeypatch, zh)
        monkeypatch.setattr(caps, "bundled_registry_path", lambda locale: bundled)

        with caplog.at_level("WARNING", logger="chaos_agent.skills.capabilities"):
            reg = caps.load_capabilities("en")

        assert reg.source == str(bundled)
        assert "NEWER local" not in caplog.text


# ---------------------------------------------------------------------------
# Distillation-input fingerprint
# ---------------------------------------------------------------------------

def _make_skill(root: Path, name: str = "demo-chaos-skills", *,
                cases: dict[str, str] | None = None,
                skill_type: str = "fault-injection",
                with_catalogue: bool = True) -> Path:
    """A skill dir laid out the way loader / sync expect."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo\nskill_type: {skill_type}\n---\n\n"
        "# Body\n\nprose the distiller never reads\n",
        encoding="utf-8",
    )
    if with_catalogue:
        cat = d / "references" / "catalogue" / "CPU"
        cat.mkdir(parents=True)
        for stem, body in (cases or {"caseA": "# a"}).items():
            (cat / f"CPU_{stem}.md").write_text(body, encoding="utf-8")
    return d


class TestDistillationInputsFingerprint:
    """The fingerprint must change iff a re-sync could change the artifact.

    Regression: it used to hash the WHOLE skill dir (everything except
    __pycache__/.DS_Store/scripts), so archiving a drill record or editing a
    reference doc invalidated a registry whose re-distillation was a
    byte-identical no-op. Consumers got CapabilitiesStaleError — a 503 on the
    platform's capability table — until someone paid for a full 115-case run.
    """

    def test_files_the_distiller_never_reads_are_ignored(self, tmp_path):
        d = _make_skill(tmp_path)
        before = caps.distillation_inputs_fingerprint(d)

        (d / "CASE_实测记录.md").write_text("# 归档的实测记录", encoding="utf-8")
        (d / "references" / "carrier").mkdir(parents=True)
        (d / "references" / "carrier" / "recovery-carrier.md").write_text("carrier", encoding="utf-8")
        (d / "scripts").mkdir()
        (d / "scripts" / "list.py").write_text("print(1)", encoding="utf-8")
        # SKILL.md prose: load_skill_metadata is Tier 1 and never reads the body
        (d / "SKILL.md").write_text(
            "---\nname: demo-chaos-skills\ndescription: demo\nskill_type: fault-injection\n---\n\n"
            "# Body\n\ncompletely rewritten prose\n",
            encoding="utf-8",
        )

        assert caps.distillation_inputs_fingerprint(d) == before

    def test_case_body_change_changes_fingerprint(self, tmp_path):
        d = _make_skill(tmp_path)
        before = caps.distillation_inputs_fingerprint(d)
        (d / "references" / "catalogue" / "CPU" / "CPU_caseA.md").write_text("# a changed", encoding="utf-8")
        assert caps.distillation_inputs_fingerprint(d) != before

    def test_new_case_file_changes_fingerprint(self, tmp_path):
        d = _make_skill(tmp_path)
        before = caps.distillation_inputs_fingerprint(d)
        (d / "references" / "catalogue" / "CPU" / "CPU_caseB.md").write_text("# b", encoding="utf-8")
        assert caps.distillation_inputs_fingerprint(d) != before

    def test_metadata_identity_change_changes_fingerprint(self, tmp_path):
        """``name`` drives family resolution and ``skill_type`` drives discovery,
        so both are real inputs even though the body is not."""
        d = _make_skill(tmp_path)
        before = caps.distillation_inputs_fingerprint(d)
        (d / "SKILL.md").write_text(
            "---\nname: renamed-chaos-skills\ndescription: demo\n"
            "skill_type: fault-injection\n---\n\n# Body\n",
            encoding="utf-8",
        )
        assert caps.distillation_inputs_fingerprint(d) != before

    def test_description_rewrite_does_not_change_fingerprint(self, tmp_path):
        """The description is the most frequently edited field in a SKILL.md and
        no sync code reads it — re-distilling after such an edit reproduces the
        artifact byte-for-byte, so it must not cost a 115-case LLM run.

        Regression: the first shape of this fix hashed the whole front matter
        block, which kept firing on exactly the edits the user makes daily.
        """
        d = _make_skill(tmp_path)
        before = caps.distillation_inputs_fingerprint(d)
        (d / "SKILL.md").write_text(
            "---\nname: demo-chaos-skills\n"
            "description: |\n  A completely rewritten, much longer description\n"
            "  spanning several lines.\nskill_type: fault-injection\n"
            "scripts:\n  - name: new_script.py\n    description: added\n---\n\n# Body\n",
            encoding="utf-8",
        )
        assert caps.distillation_inputs_fingerprint(d) == before

    def test_unparseable_metadata_contributes_nothing(self, tmp_path):
        """Mirrors discovery: a skill whose front matter cannot be parsed is
        skipped, so a sync run reads none of it — not even its catalogue."""
        d = _make_skill(tmp_path)
        (d / "SKILL.md").write_text("no front matter here\n", encoding="utf-8")
        assert caps.distillation_inputs_fingerprint(d) == ""

    def test_deeper_and_non_md_catalogue_files_are_ignored(self, tmp_path):
        """sync walks catalogue/<category>/*.md — one level, md only — so a
        nested file or a sibling asset cannot reach the artifact."""
        d = _make_skill(tmp_path)
        before = caps.distillation_inputs_fingerprint(d)
        deep = d / "references" / "catalogue" / "CPU" / "sub"
        deep.mkdir()
        (deep / "CPU_caseC.md").write_text("# c", encoding="utf-8")
        (d / "references" / "catalogue" / "CPU" / "asset.json").write_text("{}", encoding="utf-8")
        assert caps.distillation_inputs_fingerprint(d) == before

    def test_dir_without_inputs_returns_empty(self, tmp_path):
        d = tmp_path / "bare"
        d.mkdir()
        assert caps.distillation_inputs_fingerprint(d) == ""

    def test_discoverable_skill_without_case_files_contributes_nothing(self, tmp_path):
        """Discovery keeps a skill whose ``references/catalogue/`` exists, but
        sync walks ``catalogue/<category>/*.md`` and would read nothing from an
        empty one — so it is not a distillation input.

        Regression: the identity was hashed unconditionally, so a half-authored
        skill (catalogue dir created, first case doc not yet written) moved the
        fingerprint and judged every existing registry stale.
        """
        d = _make_skill(tmp_path, with_catalogue=False)
        (d / "references" / "catalogue" / "CPU").mkdir(parents=True)
        assert caps.distillation_inputs_fingerprint(d) == ""

        # Same for a catalogue with no category dir at all.
        bare = _make_skill(tmp_path, "bare-catalogue", with_catalogue=False)
        (bare / "references" / "catalogue").mkdir(parents=True)
        assert caps.distillation_inputs_fingerprint(bare) == ""

    def test_first_case_file_makes_the_skill_an_input(self, tmp_path):
        """The empty skill is invisible only while it is empty — writing its
        first case doc must bring it into the fingerprint."""
        d = _make_skill(tmp_path, with_catalogue=False)
        cat = d / "references" / "catalogue" / "CPU"
        cat.mkdir(parents=True)
        assert caps.distillation_inputs_fingerprint(d) == ""

        (cat / "CPU_caseA.md").write_text("# a", encoding="utf-8")
        assert caps.distillation_inputs_fingerprint(d) != ""


class TestSkillDiscovery:
    """``discover_fault_injection_skills`` is the single source of truth shared
    by the fingerprint and the CLI's input list — the two used to be separate
    scans whose agreement was only a docstring promise."""

    def _patch_roots(self, monkeypatch, roots):
        from chaos_agent.skills import loader
        monkeypatch.setattr(loader, "get_skill_roots", lambda: list(roots))

    def test_discovers_only_fault_injection_skills_with_catalogue(self, tmp_path, monkeypatch):
        root = tmp_path / "user"
        keep = _make_skill(root, "k8s-chaos-skills")
        _make_skill(root, "no-catalogue", with_catalogue=False)
        _make_skill(root, "docs-skill", skill_type="documentation")
        self._patch_roots(monkeypatch, [root])

        assert caps.discover_fault_injection_skills() == [("k8s-chaos-skills", keep)]

    def test_earlier_root_shadows_same_name(self, tmp_path, monkeypatch):
        user_root, builtin_root = tmp_path / "user", tmp_path / "builtin"
        user_copy = _make_skill(user_root, "k8s-chaos-skills", cases={"caseU": "# u"})
        _make_skill(builtin_root, "k8s-chaos-skills", cases={"caseB": "# b"})
        self._patch_roots(monkeypatch, [user_root, builtin_root])

        assert caps.discover_fault_injection_skills() == [("k8s-chaos-skills", user_copy)]

    def test_fingerprint_ignores_archived_drill_record(self, tmp_path, monkeypatch):
        """The exact failure behind the platform 503."""
        root = tmp_path / "skills"
        _make_skill(root, "k8s-chaos-skills")
        self._patch_roots(monkeypatch, [root])
        before = caps.compute_skills_fingerprint()
        assert before  # a discovered skill holding case docs always yields a value

        (root / "k8s-chaos-skills" / "CASE_实测记录.md").write_text("# 记录", encoding="utf-8")
        assert caps.compute_skills_fingerprint() == before

    def test_new_skill_without_cases_does_not_invalidate_registries(self, tmp_path, monkeypatch):
        """A half-authored skill must not judge every registry stale.

        Regression: ``compute_skills_fingerprint`` listed every DISCOVERED skill,
        and discovery only checks that ``references/catalogue/`` exists — so a
        new skill whose first case doc was not written yet moved the combined
        fingerprint. Consumers then got CapabilitiesStaleError (a 503 on the
        platform's capability table) for a re-distillation that would have
        reproduced the artifact byte-for-byte.
        """
        root = tmp_path / "skills"
        _make_skill(root, "k8s-chaos-skills")
        self._patch_roots(monkeypatch, [root])
        before = caps.compute_skills_fingerprint()

        wip = _make_skill(root, "wip-chaos-skills", with_catalogue=False)
        (wip / "references" / "catalogue" / "CPU").mkdir(parents=True)

        # It IS discovered — the fix is that it contributes no input, not that
        # it is hidden from the scan the CLI feeds to sync.
        assert [n for n, _ in caps.discover_fault_injection_skills()] == [
            "k8s-chaos-skills", "wip-chaos-skills"]
        assert caps.compute_skills_fingerprint() == before

        # …and writing its first case doc does invalidate, as it must.
        (wip / "references" / "catalogue" / "CPU" / "CPU_caseW.md").write_text(
            "# w", encoding="utf-8")
        assert caps.compute_skills_fingerprint() != before

    def test_new_skill_with_cases_still_invalidates(self, tmp_path, monkeypatch):
        """The other half of the invariant: the fingerprint covers what sync
        reads. A new skill with a real case doc adds capabilities to the
        artifact, so it MUST invalidate — otherwise the registry would keep
        serving a table that is missing them."""
        root = tmp_path / "skills"
        _make_skill(root, "k8s-chaos-skills")
        self._patch_roots(monkeypatch, [root])
        before = caps.compute_skills_fingerprint()

        _make_skill(root, "new-chaos-skills", cases={"caseN": "# n"})

        assert caps.compute_skills_fingerprint() != before

    def test_fingerprint_is_independent_of_root_layout(self, tmp_path, monkeypatch):
        """Same case content, different skill roots → same fingerprint.

        Regression (measured on the platform 503): ``compute_skills_fingerprint``
        joined ``name:fp`` parts in ``discover_fault_injection_skills`` scan
        order, which is roots order × ``sorted()`` within a root. md5 is
        order-sensitive, so removing a stale user-root copy of one skill — an
        edit that only changes WHERE that skill is read from — also reordered
        the parts and moved the combined value (2ac5d49a… → f614c98b… on the
        identical 3-skill content set). An artifact generated on one machine was
        then judged stale on another holding byte-identical skills, and
        CapabilitiesStaleError blamed a markdown edit that never happened.

        The two layouts below hold the same per-skill content; only the scan
        order differs. Asserting the orders actually differ is what keeps this
        from passing vacuously when discovery happens to normalise them.
        """
        # Layout 1: one root, alphabetical → k8s scanned before zzz.
        single = tmp_path / "single"
        _make_skill(single, "k8s-chaos-skills", cases={"caseK": "# k"})
        _make_skill(single, "zzz-chaos-skills", cases={"caseZ": "# z"})
        self._patch_roots(monkeypatch, [single])
        assert [n for n, _ in caps.discover_fault_injection_skills()] == [
            "k8s-chaos-skills", "zzz-chaos-skills"]
        fp_single = caps.compute_skills_fingerprint()

        # Layout 2: zzz lives in the user root, which is scanned first → zzz
        # before k8s. Content per skill is unchanged.
        user_root, builtin_root = tmp_path / "user", tmp_path / "builtin"
        _make_skill(user_root, "zzz-chaos-skills", cases={"caseZ": "# z"})
        _make_skill(builtin_root, "k8s-chaos-skills", cases={"caseK": "# k"})
        self._patch_roots(monkeypatch, [user_root, builtin_root])
        assert [n for n, _ in caps.discover_fault_injection_skills()] == [
            "zzz-chaos-skills", "k8s-chaos-skills"]

        assert caps.compute_skills_fingerprint() == fp_single

    def test_skill_whose_cases_all_disappeared_stops_being_an_input(self, tmp_path, monkeypatch):
        """Deleting the last case doc is the mirror of adding an empty skill:
        sync would now read nothing from it, so it must leave the fingerprint —
        and the remaining skills' contribution must be unchanged."""
        root = tmp_path / "skills"
        _make_skill(root, "k8s-chaos-skills")
        gone = _make_skill(root, "retired-chaos-skills", cases={"caseR": "# r"})
        self._patch_roots(monkeypatch, [root])
        with_both = caps.compute_skills_fingerprint()

        (gone / "references" / "catalogue" / "CPU" / "CPU_caseR.md").unlink()
        after = caps.compute_skills_fingerprint()

        assert after != with_both
        # Same value as if the retired skill had never been there at all.
        (root / "retired-chaos-skills").rename(tmp_path / "parked")
        assert caps.compute_skills_fingerprint() == after

    def test_fingerprint_changes_when_a_case_changes(self, tmp_path, monkeypatch):
        root = tmp_path / "skills"
        d = _make_skill(root, "k8s-chaos-skills")
        self._patch_roots(monkeypatch, [root])
        before = caps.compute_skills_fingerprint()

        (d / "references" / "catalogue" / "CPU" / "CPU_caseA.md").write_text("# a v2", encoding="utf-8")
        assert caps.compute_skills_fingerprint() != before

    def test_fingerprint_is_keyed_by_skill_name_not_dir_name(self, tmp_path, monkeypatch):
        """``resource_path`` is relative to the skill dir and family comes from
        the skill NAME, so renaming the directory cannot change the artifact —
        and must not change the fingerprint either."""
        root = tmp_path / "skills"
        d = _make_skill(root, "k8s-chaos-skills")
        self._patch_roots(monkeypatch, [root])
        before = caps.compute_skills_fingerprint()

        d.rename(root / "k8s-renamed")
        assert caps.compute_skills_fingerprint() == before

    def test_fingerprint_empty_when_no_skill_qualifies(self, tmp_path, monkeypatch):
        self._patch_roots(monkeypatch, [tmp_path / "nothing"])
        assert caps.compute_skills_fingerprint() == ""


# ---------------------------------------------------------------------------
# CLI agreement (the sync side of the same contract)
# ---------------------------------------------------------------------------

class TestSyncCliAgreement:
    """The CLI's input set, output filename and cache must match what the
    fingerprint and the reader assume. That agreement used to be a docstring
    promise between two independently written scans."""

    def test_cli_distills_exactly_what_the_fingerprint_covers(self, tmp_path, monkeypatch):
        from chaos_agent.cli.commands import capabilities_cmd
        from chaos_agent.skills import loader

        root = tmp_path / "skills"
        keep = _make_skill(root, "k8s-chaos-skills")
        _make_skill(root, "docs-skill", skill_type="documentation")
        _make_skill(root, "no-catalogue", with_catalogue=False)
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [root])

        roots = capabilities_cmd._discover_catalogue_roots()

        assert roots == {"k8s-chaos-skills": keep / "references" / "catalogue"}
        assert list(roots) == [name for name, _ in caps.discover_fault_injection_skills()]

    def test_output_path_is_per_language(self):
        from chaos_agent.cli.commands import capabilities_cmd

        assert capabilities_cmd._get_output_path("cn").name == "skill_capabilities_zh.json"
        assert capabilities_cmd._get_output_path("en").name == "skill_capabilities_en.json"
        assert capabilities_cmd._get_output_path(" CN ").name == "skill_capabilities_zh.json"

    def test_output_path_refuses_an_unknown_language(self):
        from chaos_agent.cli.commands import capabilities_cmd

        with pytest.raises(ValueError, match="Unsupported --lang"):
            capabilities_cmd._get_output_path("fr")

    def test_distill_cache_is_per_language_beside_the_registries(self):
        """One cache file per language, next to the registry it produces.

        Regression: a single shared ``skill_distill_cache.json`` made
        alternating ``--lang cn`` / ``--lang en`` runs wipe each other's
        cache — the prompt fingerprint embeds the language section, so every
        switch invalidated the whole file. The bilingual refresh the platform
        needs then always paid the full LLM cost twice, which is exactly the
        eviction bug the per-language registries were split to fix.
        """
        assert caps.distill_cache_path("zh").name == "skill_distill_cache_zh.json"
        assert caps.distill_cache_path("en").name == "skill_distill_cache_en.json"
        assert caps.distill_cache_path("zh").parent == caps.local_registry_path("zh").parent

    def test_distill_cache_path_requires_a_locale(self):
        """No shared default: the whole point is that there is no single file."""
        with pytest.raises(TypeError):
            caps.distill_cache_path()  # type: ignore[call-arg]

    def _invoke_sync(self, extra_args: list[str]) -> tuple[dict, dict]:
        """Run ``blade-ai capabilities-sync`` with every slow/IO dependency
        stubbed; return (what it handed to ``sync_capabilities``, its result
        payload)."""
        from unittest.mock import patch

        from typer.testing import CliRunner

        from chaos_agent.cli.main import app

        captured: dict = {}
        payload: dict = {}

        async def _fake_sync(roots, llm, out_path, **kwargs):
            captured.update(kwargs)
            captured["roots"] = roots
            captured["out_path"] = out_path
            return {"total": 0, "lang": "en", "cases": []}

        def _run_local(checks, local_fn, server_fn):
            # run_command is sync and owns the loop; skip the real preflight
            # (network probes) and go straight to the local branch.
            return asyncio.run(local_fn(None))

        def _fake_format(result, output):
            # CliRunner mixes the err=True echoes into stdout, so grab the
            # structured result here instead of parsing it back out.
            payload.update(result)
            return ""

        with patch("chaos_agent.cli.commands.capabilities_cmd.run_command", _run_local), \
                patch("chaos_agent.cli.commands.capabilities_cmd.format_output", _fake_format), \
                patch("chaos_agent.cli.commands.capabilities_cmd._discover_catalogue_roots",
                      return_value={"s1": Path("/nonexistent/references/catalogue")}), \
                patch("chaos_agent.agent.factory.make_llm", return_value=object()), \
                patch("chaos_agent.skills.case_sync.sync_capabilities", _fake_sync):
            result = CliRunner().invoke(app, ["capabilities-sync", *extra_args])

        assert result.exit_code == 0, result.output
        return captured, payload

    def test_default_run_reads_the_language_cache(self):
        captured, payload = self._invoke_sync([])
        assert Path(captured["cache_path"]).name == "skill_distill_cache_en.json"
        assert captured["use_cache"] is True
        assert payload["data"]["cache_used"] is True

    def test_no_cache_ignores_but_still_rebuilds_the_cache(self):
        """``--no-cache`` is "don't read", not "don't write".

        Regression: it used to pass ``cache_path=None``, which disabled both
        directions. A forced re-distillation produces the freshest answers
        there are; throwing them away left the previous generation on disk, so
        the next ordinary run reused exactly the stale entries the caller had
        just paid to replace — and left a fresh registry beside an old cache.
        """
        captured, payload = self._invoke_sync(["--no-cache"])
        assert Path(captured["cache_path"]).name == "skill_distill_cache_en.json"
        assert captured["use_cache"] is False
        assert payload["data"]["cache_used"] is False

    def test_lang_selects_the_cache_file(self):
        assert Path(self._invoke_sync(["--lang", "cn"])[0]["cache_path"]).name == \
            "skill_distill_cache_zh.json"

    def test_cli_declares_the_generator_it_used(self):
        """The cache identity has to reach ``sync_capabilities`` from the only
        place that knows it.

        ``make_llm`` takes no model argument — it reads ``settings.model_name``
        and ``settings.api_base_url`` — so the CLI is the caller that must
        declare them. Without this the cache keys on the prompt alone and a
        model switch silently replays the old model's answers.

        Asserting the exact composition also pins that ``llm_api_key`` is not in
        it: the tag is hashed into a cache file under ``~/.blade-ai``, where a
        credential would outlive the run that used it.
        """
        from chaos_agent.config.settings import settings

        captured, _ = self._invoke_sync([])

        assert captured["model_tag"] == f"{settings.model_name}@{settings.api_base_url}"

    def test_lang_selects_the_output_file(self):
        assert Path(self._invoke_sync(["--lang", "cn"])[0]["out_path"]).name == "skill_capabilities_zh.json"
        assert Path(self._invoke_sync([])[0]["out_path"]).name == "skill_capabilities_en.json"

    def test_cli_rejects_a_language_the_mapping_does_not_know(self):
        from unittest.mock import patch

        from typer.testing import CliRunner

        from chaos_agent.cli.main import app

        # Validated against LANG_TAG_BY_RUN, the same mapping that picks the
        # output file — so the accepted set and the written set cannot drift.
        with patch("chaos_agent.cli.commands.capabilities_cmd.run_command",
                   side_effect=AssertionError("must fail before preflight")):
            result = CliRunner().invoke(app, ["capabilities-sync", "--lang", "fr"])

        assert result.exit_code != 0
        assert "Unsupported --lang" in result.output

    def test_result_tells_how_to_refresh_the_other_language(self):
        """The hint must carry ``--reference``, not just the other ``--lang``.

        The platform merges the two registries by ``case.id``. Two independent
        runs derive scope/target/action separately; when those drift the merge
        silently loses the mismatched cases and the English column goes blank.
        """
        _, payload = self._invoke_sync(["--lang", "cn"])
        commands = payload["data"]["other_lang_commands"]
        assert len(commands) == 1
        assert "--lang en" in commands[0]
        assert "--reference" in commands[0]
        assert payload["data"]["output_path"] in commands[0]


class TestSupersededLocalRegistry:
    """The pre-split single-name override is the user's file: report, never touch."""

    def _patch(self, monkeypatch, tmp_path, *, new: bool, legacy: bool):
        new_p = tmp_path / "skill_capabilities_zh.json"
        legacy_p = tmp_path / "skill_capabilities.json"
        if new:
            new_p.write_text("{}", encoding="utf-8")
        if legacy:
            legacy_p.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(caps, "local_registry_candidates", lambda locale: [new_p, legacy_p])
        return new_p, legacy_p

    def test_reported_only_when_actually_shadowed(self, tmp_path, monkeypatch):
        _, legacy_p = self._patch(monkeypatch, tmp_path, new=True, legacy=True)
        assert caps.superseded_local_registry("zh") == legacy_p

    def test_silent_when_the_new_file_does_not_exist_yet(self, tmp_path, monkeypatch):
        """Before the first per-language sync the legacy file is still the one
        being read, so calling it superseded would be wrong."""
        self._patch(monkeypatch, tmp_path, new=False, legacy=True)
        assert caps.superseded_local_registry("zh") is None

    def test_silent_when_there_is_nothing_to_supersede(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path, new=True, legacy=False)
        assert caps.superseded_local_registry("zh") is None


# ---------------------------------------------------------------------------
# Language-agnostic readers must say which registry they read
# ---------------------------------------------------------------------------

class TestLanguageAgnosticReaders:
    """``blade-ai list`` and ``GET /skills`` take whichever local registry
    exists, zh first. That was unambiguous while there was one shared file; now
    that registries are per language it is a silent choice, so both readers
    report the language and the file they actually read."""

    def _files(self, tmp_path):
        zh = tmp_path / "skill_capabilities_zh.json"
        en = tmp_path / "skill_capabilities_en.json"
        _write(zh, _registry("zh"))
        _write(en, _registry("en"))
        return zh, en

    def test_list_command_names_the_registry_it_read(self, tmp_path, monkeypatch, capsys):
        """An en-only sync: zh-first precedence must not hide that the file
        being reported is the English one."""
        from chaos_agent.cli.commands import list_cmd
        from chaos_agent.cli.output import OutputFormat

        _, en = self._files(tmp_path)
        monkeypatch.setattr(list_cmd, "existing_local_registries", lambda: [en])
        monkeypatch.setattr(list_cmd, "resolve_skill_dir", lambda name: None)

        list_cmd.list_command(output=OutputFormat.json)

        data = json.loads(capsys.readouterr().out)["data"]
        assert data["lang"] == "en"
        assert data["source"] == str(en)

    def test_list_command_reports_zh_when_both_exist(self, tmp_path, monkeypatch, capsys):
        """Documents the precedence rather than leaving a caller to guess."""
        from chaos_agent.cli.commands import list_cmd
        from chaos_agent.cli.output import OutputFormat

        zh, en = self._files(tmp_path)
        monkeypatch.setattr(list_cmd, "existing_local_registries", lambda: [zh, en])
        monkeypatch.setattr(list_cmd, "resolve_skill_dir", lambda name: None)

        list_cmd.list_command(output=OutputFormat.json)

        data = json.loads(capsys.readouterr().out)["data"]
        assert data["lang"] == "zh"
        assert data["source"] == str(zh)

    def test_skills_endpoint_names_the_registry_it_read(self, tmp_path):
        from types import SimpleNamespace
        from unittest.mock import patch

        from chaos_agent.server.routes import list_skills as route

        _, en = self._files(tmp_path)
        req = SimpleNamespace(state=SimpleNamespace(request_id="r1"))
        with patch("chaos_agent.skills.capabilities.existing_local_registries", lambda: [en]), \
                patch("chaos_agent.skills.loader.resolve_skill_dir", lambda name: None):
            payload = asyncio.run(route.list_skills(req))

        assert payload["data"]["lang"] == "en"
        assert payload["data"]["source"] == str(en)


# ---------------------------------------------------------------------------
# Release freshness guard (the check the load path deliberately omits)
# ---------------------------------------------------------------------------

class TestBundledRegistryFreshness:
    """The committed bundled registry must match the built-in skill pack.

    ``load_capabilities`` reads the bundled registry with
    ``validate_staleness=False``: a fresh install has no local override to
    compare against, so the bundled copy is *trusted* to ship in sync with the
    skills it was distilled from. That trust is a release invariant, not a
    runtime check — nothing at import or load time notices when someone edits a
    case markdown under ``skills/**/references/catalogue/`` and forgets to
    regenerate + re-commit ``_capabilities/capabilities_{zh,en}.json``. The
    bundled table then serves stale inject/verify/recover commands with no error
    and no warning, on every machine that installs the wheel.

    This test is that missing check. It recomputes the fingerprint from the
    built-in pack and asserts both committed bundled registries still carry it,
    turning "edited a case, forgot to re-sync" from a silent drift into a red
    test naming the exact remediation.

    Discovery is pinned to ``get_builtin_skills_dir()`` — NOT the merged
    ``get_skill_roots()`` — on purpose: a developer's writable
    ``~/.blade-ai/skills`` may hold custom skills that are not part of the
    shipped pack, and folding them into the fingerprint would make this guard
    fire on machine-local state that has nothing to do with the release
    artifact. The invariant is "bundled JSON == built-in pack", so the
    fingerprint must be computed over exactly the built-in pack.
    """

    @staticmethod
    def _builtin_fingerprint(monkeypatch) -> str:
        from chaos_agent.skills import loader

        builtin = loader.get_builtin_skills_dir()
        if builtin is None or not builtin.exists():
            pytest.skip("no built-in skill pack present (stripped install)")
        # Pin discovery to the built-in root so a machine-local user skill dir
        # cannot pollute the fingerprint this guard compares against.
        monkeypatch.setattr(loader, "get_skill_roots", lambda: [builtin])
        return caps.compute_skills_fingerprint()

    @pytest.mark.parametrize("locale", ["zh", "en"])
    def test_bundled_registry_matches_builtin_pack(self, monkeypatch, locale):
        expected = self._builtin_fingerprint(monkeypatch)
        assert expected, (
            "built-in pack produced an empty fingerprint — no fault-injection "
            "skill with case markdown under references/catalogue/?"
        )

        path = caps.bundled_registry_path(locale)
        assert path.exists(), f"bundled registry missing: {path}"
        actual = json.loads(path.read_text(encoding="utf-8")).get("skills_fingerprint")

        assert actual == expected, (
            f"Bundled {path.name} is stale: its skills_fingerprint ({actual}) no "
            f"longer matches the built-in skill pack ({expected}). A case markdown "
            f"under skills/**/references/catalogue/ changed without the bundled "
            f"registry being regenerated. Re-sync both languages and re-commit:\n"
            f"    blade-ai capabilities-sync --lang cn\n"
            f"    blade-ai capabilities-sync --lang en --reference <the cn output path>\n"
            f"then copy the two generated registries over "
            f"src/chaos_agent/_capabilities/capabilities_zh.json and "
            f"capabilities_en.json."
        )
