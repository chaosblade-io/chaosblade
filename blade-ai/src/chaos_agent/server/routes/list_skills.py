"""GET /api/v1/skills - List supported fault capabilities."""

import logging
from collections import defaultdict

from fastapi import FastAPI, Request

from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope
from chaos_agent.server.routes import skills_router
from chaos_agent.utils.fault_type import extract_fault_type

logger = logging.getLogger(__name__)


def _get_or_create_catalog_llm(app: FastAPI):
    """Lazy process-singleton for the catalog-generator LLM client.

    Construction is cheap (no network), but every fresh ``ChatOpenAI``
    spins up its own ``openai.AsyncOpenAI`` with a dedicated ``httpx``
    connection pool — a few sockets / file descriptors held until GC
    runs. Rebuilding on every ``/api/v1/skills`` request quietly racks
    those up; once the user hits cache HITs (no actual API traffic)
    the client is also pure waste.

    Strategy: build on the first request that reaches here, cache on
    ``app.state.catalog_llm``, reuse for the lifetime of the process.
    The lifespan shutdown hook (``server/app.py``) calls into the
    underlying httpx pool to close the sockets cleanly.

    Stored on ``app.state`` (not a module global) so a multi-app test
    setup can't cross-contaminate, and so the close-on-shutdown hook
    has a known well-typed handle to reach.
    """
    cached = getattr(app.state, "catalog_llm", None)
    if cached is not None:
        return cached
    from chaos_agent.agent.factory import make_llm

    llm = make_llm(max_retries=1, timeout=30)
    app.state.catalog_llm = llm
    logger.info("Catalog LLM client created (lazy)")
    return llm


@skills_router.get("/skills")
async def list_skills(
    req: Request,
    category: str = "",
    target_type: str = "",
    no_cache: bool = False,
):
    """List all supported fault capabilities with use-case examples.

    Uses LLM to analyze each skill's content and generate injectable fault
    scenarios. Results are cached; use no_cache=true to force regeneration.
    """
    from chaos_agent.skills.catalog_generator import generate_skill_catalog

    registry = req.app.state.skill_registry

    categories_dict = defaultdict(lambda: {"category": "", "description": "", "faults": []})

    llm = _get_or_create_catalog_llm(req.app)

    for name, meta in registry.metadata.items():
        # Apply filters
        if category and meta.category != category:
            continue
        if target_type and meta.target != target_type:
            continue

        cat = meta.category or "other"

        # Read skill content (SKILL.md body)
        try:
            skill_content = registry.activate(name)
        except Exception:
            skill_content = meta.description or ""

        # Get skill directory for fingerprint computation
        skill_dir = registry.get_skill_dir(name)

        # Generate use-case catalog via LLM (cached)
        use_cases = await generate_skill_catalog(
            skill_name=name,
            skill_content=skill_content,
            skill_dir=skill_dir,
            llm=llm,
            work_dir=settings.working_dir,
            no_cache=no_cache,
        )

        if use_cases:
            for uc in use_cases:
                uc_cat = uc.get("category") or cat
                categories_dict[uc_cat]["category"] = uc_cat
                categories_dict[uc_cat]["description"] = f"{uc_cat} 故障注入用例"
                categories_dict[uc_cat]["faults"].append({
                    "fault_type": extract_fault_type(uc_cat),
                    "use_case_name": uc["use_case_name"],
                    "fault_symptom": uc["fault_symptom"],
                    "resource_path": uc["resource_path"],
                    "example_cmd": uc["example_cmd"],
                })
        else:
            # Fallback
            categories_dict[cat]["category"] = cat
            categories_dict[cat]["description"] = f"{cat} related faults"
            categories_dict[cat]["faults"].append({
                "fault_type": extract_fault_type(cat),
                "name": name.replace("-", " ").title(),
                "description": meta.description.split(".")[0] if meta.description else "",
                "example_cmd": (
                    f'blade-ai inject -i "帮我注入'
                    f'{meta.description.split(chr(46))[0] if meta.description else name}故障，'
                    f'命名空间为<namespace>，目标为<name>，'
                    f'kubeconfig路径为<kubeconfig>"'
                ),
            })

    categories = list(categories_dict.values())
    total_use_cases = sum(len(c["faults"]) for c in categories)

    return JSONEnvelope.ok(
        data={
            "total": total_use_cases,
            "categories": categories,
        },
        request_id=getattr(req.state, "request_id", ""),
    )
