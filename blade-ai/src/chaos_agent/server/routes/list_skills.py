"""GET /api/v1/skills - List supported fault capabilities."""

import logging

from fastapi import Request

from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope
from chaos_agent.server.routes import skills_router

logger = logging.getLogger(__name__)


@skills_router.get("/skills")
async def list_skills(req: Request):
    """List all supported fault capabilities (from last capabilities-sync).

    Reads the pre-generated skill_capabilities.json. Fast, no LLM/blade calls.
    Run `blade-ai capabilities-sync` to regenerate.
    """
    import json as _json
    from chaos_agent.skills.loader import get_skills_dir

    primary = settings.resolved_memory_dir / "skill_capabilities.json"
    default = get_skills_dir() / "k8s-chaos-skills" / "references" / "skill_capabilities.default.json"
    src = primary if primary.exists() else default

    if not src.exists():
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
        cats.setdefault(cat, {"category": cat, "description": f"{cat} 故障注入用例", "faults": []})
        cats[cat]["faults"].append(c)

    return JSONEnvelope.ok(
        data={
            "total": len(cases),
            "blade_version": catalog.get("blade_version", ""),
            "categories": list(cats.values()),
        },
        request_id=getattr(req.state, "request_id", ""),
    )
