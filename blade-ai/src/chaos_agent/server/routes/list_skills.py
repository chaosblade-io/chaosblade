"""GET /api/v1/skills - List supported fault capabilities."""

import logging

from fastapi import Request

from chaos_agent.models.schemas import JSONEnvelope
from chaos_agent.server.routes import skills_router

logger = logging.getLogger(__name__)


@skills_router.get("/skills")
async def list_skills(req: Request):
    """List all supported fault capabilities (from last capabilities-sync).

    Reads the registry the last sync produced. Fast, no LLM/blade calls.
    Run `blade-ai capabilities-sync` to regenerate.
    """
    import json as _json
    from chaos_agent.skills.capabilities import existing_local_registries
    from chaos_agent.skills.loader import resolve_skill_dir

    # Language-agnostic endpoint: whichever local registry exists (zh first),
    # then the checked-in default so it still answers before any sync has run.
    local_files = existing_local_registries()
    k8s_dir = resolve_skill_dir("k8s-chaos-skills")
    default = (
        k8s_dir / "references" / "skill_capabilities.default.json"
        if k8s_dir else None
    )
    src = local_files[0] if local_files else default

    if src is None or not src.exists():
        return JSONEnvelope.ok(
            data={"total": 0, "categories": [],
                  "hint": "No capabilities data. Run `blade-ai capabilities-sync` to generate."},
            request_id=getattr(req.state, "request_id", ""),
        )

    try:
        catalog = _json.loads(src.read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read capabilities file %s: %s", src, e)
        return JSONEnvelope.ok(
            data={"total": 0, "categories": [],
                  "hint": f"Capabilities file corrupted: {e}. Run `blade-ai capabilities-sync` to regenerate."},
            request_id=getattr(req.state, "request_id", ""),
        )
    cases = catalog.get("cases", [])

    cats: dict = {}
    for c in cases:
        cat = c.get("category", "unknown")
        cats.setdefault(cat, {"category": cat, "description": f"{cat} fault-injection use cases", "faults": []})
        cats[cat]["faults"].append(c)

    return JSONEnvelope.ok(
        data={
            "total": len(cases),
            "blade_version": catalog.get("blade_version", ""),
            # Registries are per language now, so "the last sync" is ambiguous
            # the moment two files exist. This endpoint takes zh first; naming
            # the language and the file lets a caller detect that instead of
            # silently rendering one language's prose as if it were the only
            # registry. Same fields as `blade-ai list`.
            "lang": catalog.get("lang", ""),
            "source": str(src),
            "categories": list(cats.values()),
        },
        request_id=getattr(req.state, "request_id", ""),
    )
