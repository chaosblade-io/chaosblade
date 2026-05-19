"""Phase 3b — skill catalog management endpoints.

Surface mirrors Python TUI's ``/skills`` family beyond ``list``:
  - ``GET    /api/v1/skills/{name}``        → metadata + SKILL.md instructions.
  - ``POST   /api/v1/skills/reload``        → re-scan ``skills_dir``.
  - ``POST   /api/v1/skills/install``       → ``{source}`` body; copies a
                                               local dir / git URL into
                                               ``skills_dir`` (no setup
                                               scripts run).
  - ``POST   /api/v1/skills/{name}/enable`` → remove from
                                               ``settings.disabled_skills``.
  - ``POST   /api/v1/skills/{name}/disable``→ add to
                                               ``settings.disabled_skills``;
                                               drop from the live registry.

Why these are split out from ``list_skills.py``:
  ``list_skills.py`` runs the LLM-heavy catalog generator; admin ops
  here are filesystem / config writes. Keeping them in their own file
  avoids bloating that route's import surface and signals which
  endpoints are read-only vs mutating.

Skill name path validation: every ``{name}`` segment goes through the
same ``[A-Za-z0-9_\\-.]{1,128}`` gate so a crafted name can't escape
``skills_dir`` when the registry composes filesystem paths internally.
The ``.`` is allowed (some skills use ``v1.2``-style names) but the
gate still rejects ``..`` since it requires segments of >= 1 char and
disallows path separators.
"""

from __future__ import annotations

import logging
import re

from fastapi import Body, Request

from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import skills_router

logger = logging.getLogger(__name__)


# Skill names: alnum + dash + underscore + dot. Dots allow versioned
# names (``cpu-burner.v2``) without permitting traversal — ``..`` is
# blocked by FastAPI's URL normalisation BEFORE the handler, and any
# traversal that did reach us couldn't sneak through this regex
# because it requires the whole string to match (no embedded
# slashes / spaces / NULs).
_SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-.]{1,128}$")


def _validate_skill_name(name: str, req_id: str):
    """Return None when ``name`` is safe, otherwise a fail envelope.
    Single-shot helper so each handler keeps the gate literal in
    one place — same pattern as ``memory._validate_session_id``."""
    if not _SKILL_NAME_PATTERN.match(name):
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message=(
                f"invalid skill name '{name}' — must be 1–128 chars of "
                "[A-Za-z0-9_-.]"
            ),
            request_id=req_id,
        )
    return None


def _registry(req: Request):
    """Pull the live SkillRegistry off ``app.state``. Returns None
    when the server hasn't initialised it yet (test fixtures, early
    boot)."""
    return getattr(req.app.state, "skill_registry", None)


def _meta_dict(meta) -> dict:
    """Serialise a ``SkillMetadata`` dataclass into a JSON-safe dict.
    Pulled out so the various ``GET`` shapes (single skill / future
    bulk) stay in lockstep on field naming."""
    return {
        "name": meta.name,
        "description": meta.description,
        "version": meta.version,
        "category": meta.category,
        "target": meta.target,
        "required_tools": list(meta.required_tools or []),
        "tags": list(meta.tags or []),
        "parameters": [
            {"name": p.name, "description": getattr(p, "description", "")}
            for p in (meta.parameters or [])
        ],
        "scripts": [
            {
                "name": s.name,
                "description": getattr(s, "description", ""),
            }
            for s in (meta.scripts or [])
        ],
    }


@skills_router.get("/skills_dir")
async def skills_dir(req: Request):
    """Phase 3 finishing — ``GET /api/v1/skills_dir``.

    Mirror of Python TUI's ``_cmd_skills_path`` (``tui/controllers/
    commands.py:1358``). Returns the resolved skills directory plus
    the priority list of candidates the loader checks. Lightweight —
    no LLM call, no filesystem scan beyond what ``get_skills_dir()``
    already does at startup.

    Why a flat ``/skills_dir`` path instead of nesting under
    ``/skills/path``: the latter would shadow any future skill named
    ``path`` (FastAPI matches the literal segment first, then the
    ``{name}`` capture). Keep paths concrete to avoid that silent
    collision class.
    """
    from chaos_agent.config.settings import settings as s
    from chaos_agent.skills.loader import get_skills_dir as _get_dir
    import os

    req_id = getattr(req.state, "request_id", "")
    resolved = _get_dir()
    candidates = [
        {"label": "config.json", "value": str(s.skills_dir or "")},
        {
            "label": "env BLADE_AI_SKILLS_DIR",
            "value": os.environ.get("BLADE_AI_SKILLS_DIR", "") or "",
        },
        # Dev path matches the loader's step-4 fallback.
        {
            "label": "dev path",
            "value": str(
                (
                    __import__("pathlib").Path(__file__).resolve()
                    .parents[3]
                    / "skills"
                )
            ),
        },
    ]
    return JSONEnvelope.ok(
        data={"resolved": str(resolved), "candidates": candidates},
        request_id=req_id,
    )


@skills_router.get("/skills/{name}")
async def show_skill(name: str, req: Request):
    """Return metadata + SKILL.md body for one skill."""
    req_id = getattr(req.state, "request_id", "")
    bad = _validate_skill_name(name, req_id)
    if bad is not None:
        return bad
    registry = _registry(req)
    if registry is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message="skill registry is not initialised",
            request_id=req_id,
        )
    meta = registry.get_metadata(name)
    if meta is None:
        return JSONEnvelope.fail(
            code=ResponseCode.TASK_NOT_FOUND,
            message=f"skill '{name}' is not loaded",
            request_id=req_id,
        )
    # ``activate`` returns the SKILL.md body (Tier 2). Cached, so
    # repeated /skills show calls don't re-read disk.
    instructions = ""
    try:
        instructions = registry.activate(name) or ""
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("skills/show: activate failed for %s: %s", name, e)
    skill_dir = registry.get_skill_dir(name)
    return JSONEnvelope.ok(
        data={
            "name": name,
            "metadata": _meta_dict(meta),
            "instructions": instructions,
            "skill_dir": str(skill_dir) if skill_dir else "",
        },
        request_id=req_id,
    )


@skills_router.post("/skills/reload")
async def reload_skills(req: Request):
    """Re-scan ``skills_dir`` and surface the diff vs. the previous
    set. Mirrors Python TUI's ``_cmd_skills_reload`` so the user
    sees ``added`` / ``removed`` / ``total`` after the rescan.
    """
    from chaos_agent.skills.loader import get_skills_dir

    req_id = getattr(req.state, "request_id", "")
    registry = _registry(req)
    if registry is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message="skill registry is not initialised",
            request_id=req_id,
        )
    before = set(registry.list_skills())
    try:
        registry.reload(get_skills_dir())
    except Exception as e:
        logger.exception("skills reload failed")
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to reload skills: {e}",
            request_id=req_id,
        )
    after = set(registry.list_skills())
    return JSONEnvelope.ok(
        data={
            "skills_dir": str(get_skills_dir()),
            "total": len(after),
            "added": sorted(after - before),
            "removed": sorted(before - after),
        },
        request_id=req_id,
    )


@skills_router.post("/skills/install")
async def install_skill_endpoint(req: Request, payload: dict = Body(...)):
    """Install one or more skills from a git URL or local path.

    Body shape: ``{"source": "<git url or path>"}``. We never run
    the skill's setup scripts — the installer only copies files and
    re-validates ``SKILL.md``. That guarantee is what lets us safely
    expose this over HTTP.
    """
    from chaos_agent.skills.installer import install_skill, SkillInstallError

    req_id = getattr(req.state, "request_id", "")
    source = (payload.get("source") or "").strip()
    if not source:
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message="body must include a non-empty 'source' field",
            request_id=req_id,
        )
    try:
        installed = await install_skill(source, overwrite=False)
    except SkillInstallError as e:
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message=f"install failed: {e}",
            request_id=req_id,
        )
    except Exception as e:  # pragma: no cover — disk / network surprises
        logger.exception("skills install failed")
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"install failed: {e}",
            request_id=req_id,
        )
    return JSONEnvelope.ok(
        data={
            "source": source,
            "installed": [
                {
                    "name": sk.name,
                    "target_dir": str(sk.target_dir),
                    "skill_md_sha256": sk.skill_md_sha256,
                }
                for sk in installed
            ],
            # The TS handler tells the user to follow up with /skills
            # reload; surfacing the literal hint here keeps the API
            # self-describing for non-TUI callers too.
            "next_action": "POST /api/v1/skills/reload to make installed skills active",
        },
        request_id=req_id,
    )


def _disabled_list() -> list[str]:
    """Read the current ``settings.disabled_skills`` defensively. Pydantic
    sometimes hands us a tuple; coerce to list every time so callers
    don't have to."""
    from chaos_agent.config.settings import settings as s

    return list(s.disabled_skills or [])


def _write_disabled_list(updated: list[str]) -> None:
    """Persist via ConfigStore.set_many — the same path the Python TUI
    uses (``tui/controllers/commands.py:1324``). Triggers
    settings.reload() automatically."""
    from chaos_agent.tui.config_store import ConfigStore

    ConfigStore().set_many({"disabled_skills": updated})


@skills_router.post("/skills/{name}/enable")
async def enable_skill(name: str, req: Request):
    """Remove ``name`` from ``settings.disabled_skills``. Idempotent —
    the response says whether the skill was actually previously
    disabled so the TS handler can show the right message
    ("enabled" vs "already enabled")."""
    req_id = getattr(req.state, "request_id", "")
    bad = _validate_skill_name(name, req_id)
    if bad is not None:
        return bad
    current = _disabled_list()
    if name not in current:
        return JSONEnvelope.ok(
            data={"name": name, "was_disabled": False, "disabled_skills": current},
            request_id=req_id,
        )
    updated = [n for n in current if n != name]
    try:
        _write_disabled_list(updated)
    except Exception as e:  # pragma: no cover
        logger.exception("enable_skill: write failed")
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to update disabled_skills: {e}",
            request_id=req_id,
        )
    return JSONEnvelope.ok(
        data={
            "name": name,
            "was_disabled": True,
            "disabled_skills": updated,
            "next_action": "POST /api/v1/skills/reload to apply",
        },
        request_id=req_id,
    )


@skills_router.post("/skills/{name}/disable")
async def disable_skill(name: str, req: Request):
    """Add ``name`` to ``settings.disabled_skills`` and drop it from
    the live registry so subsequent operations don't pick it up.
    Idempotent on already-disabled."""
    req_id = getattr(req.state, "request_id", "")
    bad = _validate_skill_name(name, req_id)
    if bad is not None:
        return bad
    current = _disabled_list()
    if name in current:
        return JSONEnvelope.ok(
            data={"name": name, "was_enabled": False, "disabled_skills": current},
            request_id=req_id,
        )
    updated = list(current) + [name]
    try:
        _write_disabled_list(updated)
    except Exception as e:  # pragma: no cover
        logger.exception("disable_skill: write failed")
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to update disabled_skills: {e}",
            request_id=req_id,
        )
    # Drop from the live registry so the next call doesn't see it.
    # Python TUI does the same surgery in ``_cmd_skills_disable``
    # (``tui/controllers/commands.py:1349``). Defensive ``getattr`` /
    # ``pop`` so a registry whose internals diverge later doesn't
    # crash this endpoint.
    registry = _registry(req)
    if registry is not None:
        try:
            getattr(registry, "_metadata", {}).pop(name, None)
            getattr(registry, "_skill_dirs", {}).pop(name, None)
            getattr(registry, "_instructions_cache", {}).pop(name, None)
        except Exception as e:  # pragma: no cover
            logger.warning("disable_skill: registry surgery failed: %s", e)
    return JSONEnvelope.ok(
        data={
            "name": name,
            "was_enabled": True,
            "disabled_skills": updated,
            "next_action": "POST /api/v1/skills/reload to refresh dynamic commands",
        },
        request_id=req_id,
    )
