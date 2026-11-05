"""Runtime reader for the compiled capabilities registry — the SINGLE source
of truth for what blade-ai can inject.

The registry is produced by ``blade-ai capabilities-sync`` (LLM distills each
skill case's markdown into an executable capability definition) and shipped
inside the package at ``chaos_agent/_capabilities/capabilities_{locale}.json``
— installing a package version gives you exactly that version's capabilities.

Resolution order in :func:`load_capabilities`:

1. explicit ``path`` argument (tests / callers with a custom location)
2. locally generated ``~/.blade-ai/memory/skill_capabilities_{locale}.json`` —
   one file per language, only when it is v2 format and holds the requested
   locale (developer override; validated against the current distillation-input
   fingerprint, stale → :class:`CapabilitiesStaleError`). The pre-per-language
   filename ``skill_capabilities.json`` is still honoured on read.
3. package-bundled ``capabilities_{locale}.json``

There is NO fallback that fabricates capabilities: both sources missing →
:class:`CapabilitiesMissingError`. Consumers (platform, TUI, agent) must
surface that as "registry unavailable", never as an empty-but-ok catalog.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Local override written by ``blade-ai capabilities-sync`` — one file per
# language. The registry's prose fields (title/symptom/nl_cmd/params[].
# description) are generated in the run's language, so a single shared
# filename made the two languages evict each other: syncing ``--lang en``
# overwrote the zh override, and a consumer asking for zh then silently fell
# through to the *bundled* (older) artifact with no signal at all.
_LOCAL_FILENAME_FMT = "skill_capabilities_{lang}.json"
# Pre-per-language filename. Still honoured on read so an existing local
# override keeps working after an upgrade; sync no longer writes it.
_LOCAL_FILENAME_LEGACY = "skill_capabilities.json"
# Per-case LLM distillation results, keyed by (skill, case path) and guarded
# by the prompt fingerprint. One file per language, living next to the
# registry it produces: the guard is the prompt's fingerprint and the prompt
# embeds the run's language section, so a shared file made ``--lang cn`` and
# ``--lang en`` drop each other's cache on every alternating run.
_DISTILL_CACHE_FILENAME_FMT = "skill_distill_cache_{lang}.json"
# Package-bundled registries, one per language.
_BUNDLED_DIRNAME = "_capabilities"


class CapabilitiesError(RuntimeError):
    """Base error for capabilities registry problems."""


class CapabilitiesMissingError(CapabilitiesError):
    """No registry found anywhere — run ``blade-ai capabilities-sync`` or
    upgrade/reinstall the blade-ai package."""


class CapabilitiesStaleError(CapabilitiesError):
    """A locally generated registry exists but the skill cases changed
    after it was generated — re-run ``blade-ai capabilities-sync``."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityParam:
    """One parameter slot of a capability.

    kind:          target_resource (addresses the victim) | fault_param (tunes the fault)
    resolved_from: environment (filled from the environment config at drill
                   time) | user (filled by the operator, default pre-filled)
    """

    name: str
    kind: str = "fault_param"
    resolved_from: str = "user"
    required: bool = False
    default: str = ""
    description: str = ""


@dataclass(frozen=True)
class CapabilityCase:
    """One executable fault capability distilled from a skill case md."""

    id: str
    family: str                       # k8s | host | python
    profile: str                      # required environment channel profile
    scope: str
    target: str
    action: str
    cluster_scoped: bool
    title: str
    category: str
    symptom: str
    case_path: str
    nl_cmd: str
    params: tuple[CapabilityParam, ...] = field(default_factory=tuple)
    executable: bool = True
    # Back-compat fields for ``blade-ai list`` and other existing consumers.
    use_case_name: str = ""
    inject_kind: str = "blade"
    structured_cmd: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "family": self.family,
            "profile": self.profile,
            "scope": self.scope,
            "target": self.target,
            "action": self.action,
            "cluster_scoped": self.cluster_scoped,
            "title": self.title,
            "category": self.category,
            "symptom": self.symptom,
            "case_path": self.case_path,
            "nl_cmd": self.nl_cmd,
            "params": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "resolved_from": p.resolved_from,
                    "required": p.required,
                    "default": p.default,
                    "description": p.description,
                }
                for p in self.params
            ],
            "executable": self.executable,
            "use_case_name": self.use_case_name,
            "inject_kind": self.inject_kind,
            "structured_cmd": self.structured_cmd,
        }


@dataclass
class CapabilitiesRegistry:
    """In-memory view of one language's registry."""

    lang: str
    total: int
    cases: list[CapabilityCase]
    source: str  # file path the registry was read from
    skills_fingerprint: str = ""
    generated_at: str = ""

    def families(self) -> list[str]:
        """Ordered unique family names."""
        seen: dict[str, None] = {}
        for c in self.cases:
            seen.setdefault(c.family, None)
        return list(seen)

    def cases_by_family(self) -> dict[str, list[CapabilityCase]]:
        grouped: dict[str, list[CapabilityCase]] = {}
        for c in self.cases:
            grouped.setdefault(c.family, []).append(c)
        return grouped

    def case_by_id(self, case_id: str) -> CapabilityCase | None:
        for c in self.cases:
            if c.id == case_id:
                return c
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_param(raw: dict) -> CapabilityParam:
    return CapabilityParam(
        name=str(raw.get("name", "")),
        kind=str(raw.get("kind", "fault_param")),
        resolved_from=str(raw.get("resolved_from", "user")),
        required=bool(raw.get("required", False)),
        default=str(raw.get("default", "")),
        description=str(raw.get("description", "")),
    )


def _parse_case(raw: dict) -> CapabilityCase:
    params = [_parse_param(p) for p in raw.get("params", []) if isinstance(p, dict) and p.get("name")]
    return CapabilityCase(
        id=str(raw.get("id", "")),
        family=str(raw.get("family", "")),
        profile=str(raw.get("profile", "")),
        scope=str(raw.get("scope", "")),
        target=str(raw.get("target", "")),
        action=str(raw.get("action", "")),
        cluster_scoped=bool(raw.get("cluster_scoped", False)),
        title=str(raw.get("title", "")),
        category=str(raw.get("category", "")),
        symptom=str(raw.get("symptom", raw.get("fault_symptom", ""))),
        case_path=str(raw.get("case_path", raw.get("resource_path", ""))),
        nl_cmd=str(raw.get("nl_cmd", "")),
        params=tuple(params),
        executable=bool(raw.get("executable", True)),
        use_case_name=str(raw.get("use_case_name", "")),
        inject_kind=str(raw.get("inject_kind", "blade")),
        structured_cmd=str(raw.get("structured_cmd", "")),
    )


def parse_registry(data: dict, source: str) -> CapabilitiesRegistry:
    """Parse a v2 registry dict. Raises CapabilitiesError on invalid shape."""
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise CapabilitiesError(
            f"Unsupported capabilities schema_version {version!r} in {source}; "
            f"expected {SCHEMA_VERSION}. Re-run `blade-ai capabilities-sync`."
        )
    raw_cases = data.get("cases", [])
    cases = [_parse_case(c) for c in raw_cases if isinstance(c, dict) and c.get("id")]
    return CapabilitiesRegistry(
        lang=str(data.get("lang", "")),
        total=int(data.get("total", len(cases))),
        cases=cases,
        source=source,
        skills_fingerprint=str(data.get("skills_fingerprint", "")),
        generated_at=str(data.get("generated_at", "")),
    )


def probe_registry_file(path: Path) -> tuple[bool, str]:
    """Cheap probe: is this file a v2 registry, and which lang does it hold?

    Returns ``(is_v2, lang)``. Used to skip legacy v1 files and to honour
    locale when deciding whether a local override applies.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ""
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return False, ""
    return True, str(data.get("lang", ""))


# ---------------------------------------------------------------------------
# Skill discovery + distillation-input fingerprint (staleness check)
# ---------------------------------------------------------------------------

def discover_fault_injection_skills(*, verbose: bool = False) -> list[tuple[str, Path]]:
    """Every fault-injection skill dir that ``capabilities-sync`` distills.

    Returns ``[(skill_name, skill_dir), ...]`` in scan order. This is the
    SINGLE source of truth for that set: both the fingerprint stamped into a
    registry (:func:`compute_skills_fingerprint`) and the CLI's input list
    (``capabilities_cmd._discover_catalogue_roots``) derive from it. Two
    independent scans that disagree make stale-detection misfire — the
    artifact would be validated against a different input set than the one
    that produced it.

    Scans the merged roots (user root first, then the built-in pack) and
    de-duplicates by skill ``name`` BEFORE the catalogue/type filter, so a
    user skill that shadows a built-in one of the same name suppresses the
    built-in copy here exactly as it does in the registry.

    ``verbose`` raises skip/shadow notices from DEBUG to INFO — the CLI passes
    it (a human is waiting on a slow run), the runtime reader does not (this
    executes on every ``load_capabilities`` call).
    """
    from chaos_agent.skills.loader import get_skill_roots, load_skill_metadata
    from chaos_agent.skills.models import SKILL_TYPE_FAULT_INJECTION

    def _note(msg: str, *args) -> None:
        (logger.info if verbose else logger.debug)(msg, *args)

    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for skills_dir in get_skill_roots():
        if not skills_dir.exists():
            continue
        for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            if not (skill_dir / "SKILL.md").exists():
                continue
            try:
                meta = load_skill_metadata(skill_dir)
            except Exception as e:  # noqa: BLE001 — front matter may be malformed
                _note("Skipping skill dir %s: %s", skill_dir.name, e)
                continue
            if meta.name in seen:
                _note("Skill '%s' shadowed by an earlier root; skipping %s", meta.name, skill_dir)
                continue  # earlier (user) root wins — mirrors registry merge
            seen.add(meta.name)
            if not (skill_dir / "references" / "catalogue").exists():
                _note("Skipping skill '%s': no references/catalogue/", meta.name)
                continue
            if meta.skill_type != SKILL_TYPE_FAULT_INJECTION:
                _note("Skipping skill '%s' (skill_type=%s, not fault-injection)", meta.name, meta.skill_type)
                continue
            found.append((meta.name, skill_dir))
    return found


def _skill_identity(skill_dir: Path) -> str | None:
    """The metadata fields a sync run actually consumes, as a stable string.

    ``case_sync`` never opens ``SKILL.md``. It is handed ``{skill_name:
    catalogue_root}`` by the CLI, that name comes from
    ``load_skill_metadata(...).name``, and ``_family_of`` maps it to a family;
    ``skill_type`` is what discovery filters on. Those two fields are therefore
    the entire metadata surface that can reach the artifact — ``description``
    and every other front matter key are prose as far as staleness goes.
    Hashing the raw front matter block (the previous shape of this fix) still
    invalidated the registry on a routine SKILL.md wording edit that
    re-distillation would have reproduced byte-for-byte.

    Returns None when the metadata is absent or unparseable, which is exactly
    when :func:`discover_fault_injection_skills` skips the skill — so a None
    here means "this dir contributes no distillation input at all".

    Maintenance contract: if the sync path ever starts reading another metadata
    field, add it here, or edits to that field will be missed.
    """
    from chaos_agent.skills.loader import load_skill_metadata

    try:
        meta = load_skill_metadata(skill_dir)
    except Exception as e:  # noqa: BLE001 — missing file or malformed front matter
        logger.debug("No usable metadata in %s: %s", skill_dir, e)
        return None
    return f"{meta.name}\0{meta.skill_type}"


def distillation_inputs_fingerprint(skill_dir: Path) -> str:
    """Content fingerprint of exactly what the distiller reads for one skill.

    Deliberately NOT a whole-directory hash. A sync run consumes only the
    skill's parsed identity (:func:`_skill_identity`) and the case markdown
    under ``references/catalogue/<cat>/`` — it walks precisely that two-level
    layout. Everything else in a skill dir (SKILL.md prose and description,
    sibling reference docs, archived drill records, scripts) cannot alter a
    single byte of the registry, so hashing it made staleness detection fire on
    edits whose re-distillation is a no-op: the local override was rejected
    with :class:`CapabilitiesStaleError`, consumers saw "registry unavailable",
    and the only way out was a full LLM pass over every case.

    Returns "" when the dir holds no distillation input at all — unparseable
    metadata (discovery skips the skill) or no case markdown under the two-level
    layout sync walks (discovery keeps it, but sync reads nothing from it).
    """
    identity = _skill_identity(skill_dir)
    if identity is None:
        return ""  # discovery skips this skill, so a sync run reads none of it

    catalogue = skill_dir / "references" / "catalogue"
    case_files: list[Path] = []
    if catalogue.is_dir():
        for cat_dir in sorted(p for p in catalogue.iterdir() if p.is_dir()):
            case_files.extend(sorted(cat_dir.glob("*.md")))
    if not case_files:
        # Discoverable but empty: a new skill whose catalogue dir exists before
        # its first case doc, or one whose cases were all moved out. Sync walks
        # exactly this layout and would read nothing, so the artifact cannot
        # change — hashing the identity here would invalidate every registry on
        # a change that re-distillation reproduces byte-for-byte, and the
        # consumer's reward for that is a CapabilitiesStaleError (a 503 on the
        # platform) until someone re-runs a sync.
        return ""

    h = hashlib.md5()
    h.update(b"SKILL.md#identity\0")
    h.update(identity.encode("utf-8"))
    h.update(b"\0")

    for md_file in case_files:
        try:
            data = md_file.read_bytes()
        except OSError as e:
            logger.debug("Skipping unreadable case %s: %s", md_file, e)
            continue
        h.update(str(md_file.relative_to(skill_dir)).encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")

    # At least one case file contributed, so the identity above always does too:
    # a skill with distillation input never fingerprints to "".
    return h.hexdigest()


def compute_skills_fingerprint() -> str:
    """Combined fingerprint of every fault-injection skill's distillation input.

    Changes iff re-running ``capabilities-sync`` could produce a different
    artifact. Keyed by skill ``name`` (not directory name) because that is the
    identity the registry records — ``case_sync._family_of`` maps it to a
    family and every case's ``resource_path`` is relative to the skill dir, so
    renaming the directory leaves the artifact untouched and must leave the
    fingerprint untouched too.

    Skills that contribute no distillation input ("" — see
    :func:`distillation_inputs_fingerprint`) are left out entirely rather than
    listed with an empty hash: their name in the joined string would move the
    combined value while the artifact stays identical.

    Parts are SORTED before joining, so the value depends only on which skills
    contribute input and what each contains — never on the order they were
    scanned in. Discovery order is roots order (user root first) × ``sorted()``
    within a root, and md5 is order-sensitive: without sorting, deleting a stale
    user-root copy of one skill reorders the parts and moves the combined value
    even though every other skill's content is untouched. An artifact generated
    on one machine was then rejected as stale on another holding byte-identical
    skills, with :class:`CapabilitiesStaleError` blaming a markdown edit that
    never happened. Sorting also upholds the directory-rename freedom stated
    above: a rename can no longer move a skill's position in the scan.
    """
    parts = sorted(
        f"{name}:{fp}"
        for name, skill_dir in discover_fault_injection_skills()
        if (fp := distillation_inputs_fingerprint(skill_dir))
    )
    return hashlib.md5("|".join(parts).encode()).hexdigest() if parts else ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def bundled_registry_path(locale: str) -> Path:
    """Path of the package-bundled registry for ``locale``."""
    return Path(__file__).resolve().parent.parent / _BUNDLED_DIRNAME / f"capabilities_{locale}.json"


def local_registry_path(locale: str = "zh") -> Path:
    """Path where ``blade-ai capabilities-sync`` writes ``locale``'s registry.

    One file per language: the registry's prose fields are generated in the
    run's language, so a shared filename made two languages evict each other.
    """
    from chaos_agent.config.settings import settings
    return settings.resolved_memory_dir / _LOCAL_FILENAME_FMT.format(lang=locale)


def legacy_local_registry_path() -> Path:
    """Pre-per-language local override path. Read-only: sync no longer writes it."""
    from chaos_agent.config.settings import settings
    return settings.resolved_memory_dir / _LOCAL_FILENAME_LEGACY


def local_registry_candidates(locale: str) -> list[Path]:
    """Local override paths to try for ``locale``, in priority order.

    The language-specific file wins; the legacy single-name file is still
    honoured so an override generated before the split keeps working after an
    upgrade — its recorded ``lang`` decides whether it applies, exactly as it
    did when the filename was shared.
    """
    return [local_registry_path(locale), legacy_local_registry_path()]


def existing_local_registries() -> list[Path]:
    """Every local override present on disk, zh before en, specific before legacy.

    For language-agnostic consumers (``blade-ai list``, ``GET /skills``) that
    want "the registry the last sync produced" without picking a locale.
    """
    found: list[Path] = []
    for locale in ("zh", "en"):
        for p in local_registry_candidates(locale):
            if p.exists() and p not in found:
                found.append(p)
    return found


def distill_cache_path(locale: str) -> Path:
    """Where ``capabilities-sync`` keeps ``locale``'s per-case LLM cache.

    ``locale`` is required — there is deliberately no shared default, because a
    single file across languages is exactly the eviction bug the per-language
    registries were split to fix. The prompt fingerprint would still keep the
    two languages from reading each other's answers, but each run would wipe
    the other's cache, so a bilingual refresh (what the platform needs) would
    always pay the full LLM cost twice.
    """
    from chaos_agent.config.settings import settings
    return settings.resolved_memory_dir / _DISTILL_CACHE_FILENAME_FMT.format(lang=locale)


def superseded_local_registry(locale: str) -> Path | None:
    """The legacy single-name override this ``locale``'s new file replaces.

    Returned only when both exist, i.e. only when the leftover is actually
    shadowed. ``load_capabilities`` prefers the per-language file, so the
    legacy one is dead weight after a sync — but it is the user's file, so the
    CLI reports it instead of touching it.
    """
    new, legacy = local_registry_candidates(locale)
    return legacy if new.exists() and legacy.exists() else None


def _read_registry_file(path: Path, validate_staleness: bool) -> CapabilitiesRegistry:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CapabilitiesMissingError(f"Cannot read capabilities registry {path}: {e}") from e
    registry = parse_registry(data, source=str(path))
    logger.debug(
        "Capabilities registry: %d cases lang=%s generated=%s from %s",
        registry.total, registry.lang, registry.generated_at or "?", path,
    )
    if validate_staleness and registry.skills_fingerprint:
        current = compute_skills_fingerprint()
        if current and current != registry.skills_fingerprint:
            # Two causes are indistinguishable from the hashes alone, and saying
            # "the markdown changed" when the real cause is an upgrade sends the
            # operator hunting for an edit that never happened.
            raise CapabilitiesStaleError(
                f"Capabilities registry {path} (generated {registry.generated_at or '?'}) "
                f"no longer matches the current distillation inputs (registry "
                f"{registry.skills_fingerprint[:12]}, current {current[:12]}). Cause is "
                f"either a skill case markdown / SKILL.md identity edited since it was "
                f"generated, or a blade-ai upgrade that computes the fingerprint "
                f"differently; re-run `blade-ai capabilities-sync`."
            )
    return registry


def _warn_if_local_is_newer(served: CapabilitiesRegistry, skipped: list[tuple[Path, str]]) -> None:
    """Surface the one silent outcome the resolution chain can still produce.

    Skipping a local override that holds another language is normal and needs
    no noise. It becomes a trap only when the skipped file is NEWER than the
    bundled artifact actually served: the operator ran a sync, watched it
    succeed, and consumers of the other locale keep reading older data with
    nothing anywhere to explain it.
    """
    for path, lang in skipped:
        try:
            generated_at = str(json.loads(path.read_text(encoding="utf-8")).get("generated_at", ""))
        except (OSError, json.JSONDecodeError):
            continue
        # Both stamps come from datetime.now(timezone.utc).isoformat(), so
        # lexicographic order is chronological order.
        if generated_at and generated_at > served.generated_at:
            logger.warning(
                "Serving the bundled %s registry (generated %s) while a NEWER local "
                "override for lang=%r exists at %s (generated %s) — this locale has no "
                "local override of its own. Run `blade-ai capabilities-sync --lang %s`.",
                served.lang, served.generated_at or "?", lang, path, generated_at,
                "cn" if served.lang == "zh" else served.lang,
            )
            return


def load_capabilities(locale: str = "zh", *, path: str | Path | None = None) -> CapabilitiesRegistry:
    """Load the capabilities registry — the single source of capability truth.

    Resolution: explicit path > local sync output (v2 + matching lang only,
    staleness-checked) > package-bundled ``capabilities_{locale}.json``
    > CapabilitiesMissingError. Never fabricates data.
    """
    if path is not None:
        return _read_registry_file(Path(path), validate_staleness=False)

    skipped: list[tuple[Path, str]] = []
    for local in local_registry_candidates(locale):
        if not local.exists():
            continue
        is_v2, lang = probe_registry_file(local)
        if not is_v2:
            continue  # legacy v1 shape: the bundled artifact is the newer truth
        # A locally generated registry is language-specific: it only counts as
        # the override for the locale it was generated for.
        if lang != locale:
            skipped.append((local, lang))
            continue
        try:
            return _read_registry_file(local, validate_staleness=True)
        except CapabilitiesStaleError:
            raise  # a stale local override must NOT silently degrade to bundled
        except CapabilitiesError as e:
            logger.warning("Local capabilities file unusable (%s); trying bundled registry", e)

    bundled = bundled_registry_path(locale)
    if bundled.exists():
        registry = _read_registry_file(bundled, validate_staleness=False)
        _warn_if_local_is_newer(registry, skipped)
        return registry

    raise CapabilitiesMissingError(
        "No capabilities registry found: no usable local override in "
        f"{[str(p) for p in local_registry_candidates(locale)]} and no bundled {bundled}. "
        "Run `blade-ai capabilities-sync` or upgrade/reinstall the blade-ai package."
    )
