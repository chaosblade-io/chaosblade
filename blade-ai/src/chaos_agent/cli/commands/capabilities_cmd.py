"""CLI command: blade-ai capabilities-sync"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from chaos_agent.cli.output import OutputFormat, format_output
from chaos_agent.preflight import LIST_CHECKS, run_command

logger = logging.getLogger(__name__)


def _get_output_path(lang_key: str) -> Path:
    """Where this run's registry lands — one file per language.

    ``--lang`` names the run language (en|cn); the registry records the locale
    tag (en|zh) that ``capabilities.load_capabilities`` matches on. Writing to
    the tag-specific file is what stops two languages from evicting each other,
    which used to leave the other locale silently reading the bundled artifact.
    """
    from chaos_agent.skills.capabilities import local_registry_path
    from chaos_agent.skills.case_sync import LANG_TAG_BY_RUN

    key = (lang_key or "en").strip().lower()
    tag = LANG_TAG_BY_RUN.get(key)
    if tag is None:
        # No silent default: writing to the wrong locale's file would leave
        # that locale's consumers on the bundled artifact with no signal.
        raise ValueError(f"Unsupported --lang {lang_key!r}; expected one of {sorted(LANG_TAG_BY_RUN)}")
    return local_registry_path(tag)


def _discover_catalogue_roots() -> dict[str, Path]:
    """Find every fault-injection skill that ships a case catalogue.

    Thin CLI adapter over ``capabilities.discover_fault_injection_skills``,
    which is the single source of truth for that set: the fingerprint stamped
    into the artifact is computed over exactly the same skills, and the two
    must agree or stale-detection misfires. Data-driven discovery (scan +
    metadata filter) instead of a hardcoded skill name, so newly added fault
    skills (host / python-app / future families) are picked up without
    touching this command.
    """
    from chaos_agent.skills.capabilities import discover_fault_injection_skills

    return {
        name: skill_dir / "references" / "catalogue"
        for name, skill_dir in discover_fault_injection_skills(verbose=True)
    }


def capabilities_sync(
    output: OutputFormat = typer.Option(OutputFormat.json, "--output", "-o", help="Output format: json|yaml"),
    lang: str = typer.Option(
        "en", "--lang", "-l",
        help="Description language for nl_cmd/fault_symptom: en (default) | cn. Each language "
             "gets its own local registry file, so consumers of the other language are unaffected "
             "— run the command once per language you need fresh.",
    ),
    reference: Path | None = typer.Option(
        None, "--reference", "-r",
        help="Reference v2 registry (usually the other language's artifact) to anchor "
             "structural fields (triple/params schema), keeping both languages id-isomorphic",
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache",
        help="Re-run the LLM for every case, ignoring the distillation cache; the cache "
             "is then rebuilt from this run's results",
    ),
):
    """Sync skill capabilities: LLM derives commands from each skill case.

    This is a slow command — it calls the LLM for every catalogue case whose
    markdown changed since the last run (unchanged cases are reused from the
    per-language cache under ``~/.blade-ai/memory/``; pass --no-cache to force a
    full re-distillation). The case library is the generation source; no blade
    binary probing happens. Run it when you add/remove/edit skill cases. When
    generating the second language, pass --reference <first-language artifact>
    so the structural fields stay anchored and both registries keep identical
    case ids.
    """
    from chaos_agent.skills.case_sync import LANG_TAG_BY_RUN

    lang_key = lang.strip().lower()
    if lang_key not in LANG_TAG_BY_RUN:
        # Validated against the same mapping that picks the output file, so the
        # accepted set and the written set can never drift apart.
        raise typer.BadParameter(
            f"Unsupported --lang {lang!r}; expected one of {sorted(LANG_TAG_BY_RUN)}"
        )
    if reference is not None and not reference.exists():
        raise typer.BadParameter(f"--reference file not found: {reference}")

    # The registry records the locale tag (en|zh) that load_capabilities matches
    # on, and each tag has its own local file — so this run leaves the other
    # language's override untouched. Say so, because "sync succeeded" used to
    # look like it covered both.
    catalog_lang = LANG_TAG_BY_RUN[lang_key]
    # Every language this run does NOT cover, each with the command that would.
    # Derived from the mapping rather than hardcoded to "the other one of two",
    # so adding a language extends the hint instead of silently dropping it.
    others = [(k, tag) for k, tag in LANG_TAG_BY_RUN.items() if k != lang_key]

    async def _local(backend):
        from chaos_agent.agent.factory import make_llm
        from chaos_agent.config.settings import settings
        from chaos_agent.skills.capabilities import distill_cache_path, superseded_local_registry
        from chaos_agent.skills.case_sync import sync_capabilities

        catalogue_roots = _discover_catalogue_roots()

        if not catalogue_roots:
            return {
                "status": "error",
                "code": 1,
                "message": "No fault-injection skill with references/catalogue/ found",
            }

        # Thinking stays ON here (unlike display-catalog / postmortem
        # generation): this pass derives the actual inject/verify/recover
        # commands from each case doc, and those commands feed the
        # injection chain directly — derivation quality beats latency.
        llm = make_llm(read_timeout=120, enable_thinking=True)
        # make_llm takes no model argument: the generator is exactly these two
        # settings, so they are what the distillation cache must key on. Same
        # model name behind a different endpoint is a different generator and
        # its answers are not interchangeable. The api key is NOT part of the
        # tag — rotating a credential must not invalidate a cache, and the tag
        # is only ever hashed, never written out.
        model_tag = f"{settings.model_name}@{settings.api_base_url}"
        out_path = _get_output_path(lang)
        # The cache is per language too: a shared file made alternating
        # --lang cn / --lang en runs wipe each other's cache, so the bilingual
        # refresh the platform needs always paid the full LLM cost twice.
        cache_path = distill_cache_path(catalog_lang)

        typer.echo(
            f"Syncing capabilities for {len(catalogue_roots)} skill(s): "
            f"{', '.join(sorted(catalogue_roots))} → lang={catalog_lang} "
            f"model={settings.model_name} "
            f"({'cache ignored, will be rebuilt' if no_cache else 'unchanged cases reused'})...",
            err=True,
        )
        catalog = await sync_capabilities(
            catalogue_roots, llm, out_path, lang=lang, reference_registry=reference,
            cache_path=cache_path, use_cache=not no_cache, model_tag=model_tag,
        )

        blade_count = sum(1 for c in catalog["cases"] if c["inject_kind"] == "blade")
        kubectl_count = sum(1 for c in catalog["cases"] if c["inject_kind"] == "kubectl")
        mixed_count = sum(1 for c in catalog["cases"] if c["inject_kind"] == "mixed")

        typer.echo(
            f"\n✓ Sync complete: {catalog['total']} cases "
            f"(blade={blade_count}, kubectl={kubectl_count}, mixed={mixed_count})\n"
            f"  Written to: {out_path} (lang={catalog['lang']})\n"
            f"  That refreshed lang={catalog_lang} ONLY. Consumers of "
            f"{', '.join(tag for _, tag in others)} read their own file — refresh it with:\n"
            + "".join(
                f"    blade-ai capabilities-sync --lang {k} --reference {out_path}\n"
                for k, _ in others
            )
            + "  (--reference anchors scope/target/action and the params schema to this\n"
              "   artifact so both languages keep identical case ids. Skip it and the two\n"
              "   runs derive those fields independently; a consumer that merges the two\n"
              "   registries by id then silently loses whichever cases drifted.)",
            err=True,
        )
        superseded = superseded_local_registry(catalog_lang)
        if superseded is not None:
            typer.echo(
                f"  Note: {superseded.name} is the pre-per-language file and is now\n"
                f"  shadowed by {out_path.name}; it is no longer read and you can delete it.",
                err=True,
            )

        return {
            "status": "success",
            "code": 0,
            "message": "success",
            "data": {
                "total": catalog["total"],
                "skills": sorted(catalogue_roots),
                "lang": catalog["lang"],
                "blade_count": blade_count,
                "kubectl_count": kubectl_count,
                "mixed_count": mixed_count,
                "output_path": str(out_path),
                "cache_path": str(cache_path),
                "cache_used": not no_cache,
                "other_lang_commands": [
                    f"blade-ai capabilities-sync --lang {k} --reference {out_path}"
                    for k, _ in others
                ],
                "superseded_local_registry": str(superseded) if superseded else None,
            },
        }

    async def _server(backend):
        return await backend.post("/api/v1/capabilities/sync", {})

    result = run_command(LIST_CHECKS, _local, _server)
    typer.echo(format_output(result, output))


