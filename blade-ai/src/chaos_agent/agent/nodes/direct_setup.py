"""Direct setup node: deterministic skill activation (no LLM)."""

import logging
from typing import Callable

from langchain_core.messages import HumanMessage

from chaos_agent.agent.node_names import DIRECT_SETUP
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

# The only skill used for direct blade injection
_DIRECT_SKILL_NAME = "k8s-chaos-skills"

# Max skill content length injected into messages (avoid oversized context)
_MAX_SKILL_CONTENT_LEN = 2000


async def _collect_context(state: AgentState) -> dict:
    """Collect target_metadata for FCAT adaptation rules.

    Gathers pod memory limit and active same-action experiments so that
    downstream nodes (safety_check, baseline_capture, direct_execute)
    can query FCAT without re-fetching.
    """
    from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
    from chaos_agent.agent.nodes.direct_execute import _fetch_pod_memory_limit_mb

    metadata: dict = {}

    # Pod memory limit (for P0 param safety check) — read from FaultSpec
    spec = read_fault_spec(state) or FaultSpec()
    scope = spec.scope
    blade_target = spec.blade_target.lower()
    kubeconfig = state.get("kubeconfig") or ""
    ns = spec.namespace
    names = list(spec.names)
    labels = dict(spec.labels)
    task_id = state.get("task_id", "unknown")

    # Gate by ``blade_target == "mem"`` — every downstream consumer of
    # ``pod_memory_limit_mb`` is memory-burn specific:
    #   - direct_execute.py FCAT P0 param_override only matches when
    #     ``param_overrides.size == "auto"`` (a memory-burn key)
    #   - direct_execute.py OOMKill risk warning compares burn ``size``
    #     against the limit; non-memory faults have no ``size`` param
    #     so the comparison is meaningless
    #   - utils/fault_context.py compute_safe_burn_size / lookup_adaptations
    #     mem rules
    # For cpu / network / io / disk faults this prefetch was pure waste:
    # one extra kubectl roundtrip per drill, a "Pod memory limit: ..."
    # log line that confused users into thinking we cared about memory,
    # AND it set up the OOMKill block to fire a misleading warning
    # downstream. Skipping the fetch here also removes those.
    if scope == "pod" and blade_target == "mem" and kubeconfig:
        # Obtain session store before try block so exception path can write
        from chaos_agent.memory.session_store import get_global_session_store
        _store = get_global_session_store()
        _tid = state.get("task_id", "")
        try:
            mem_limit = await _fetch_pod_memory_limit_mb(
                namespace=ns, names=names, labels=labels,
                kubeconfig=kubeconfig, task_id=task_id,
            )
            if mem_limit is not None:
                metadata["pod_memory_limit_mb"] = mem_limit
            # Persist pod memory limit result to session store for full audit trail
            if _store and _tid:
                _msg = (
                    f"[FCAT Context] Pod memory limit: "
                    + (f"{mem_limit}MB" if mem_limit is not None else "not available / query failed")
                )
                _store.append_messages(_tid, [HumanMessage(content=_msg)], node_name=DIRECT_SETUP)
        except Exception:
            logger.debug("Failed to fetch pod memory limit for FCAT", exc_info=True)
            if _store and _tid:
                _store.append_messages(_tid, [HumanMessage(
                    content="[FCAT Context] Pod memory limit: query failed (exception)"
                )], node_name=DIRECT_SETUP)

    # Active same-action experiments (for P1 conflict escalation)
    # This will be populated by safety_check's conflict_check; here we
    # just prepare the field so downstream nodes can read it.
    # safety_check writes active_same_action_experiments into target_metadata
    # after its own conflict analysis.

    return metadata


def make_direct_setup(registry: SkillRegistry) -> Callable:
    """Create direct_setup node with registry injection.

    This node replaces agent_loop in direct mode — it deterministically
    activates the k8s-chaos-skills skill, generates a plan summary,
    and injects skill instructions into messages for the verifier.
    """

    async def direct_setup(state: AgentState) -> dict:
        task_id = state.get("task_id", "unknown")

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "direct_setup",
            f"Direct setup: activating skill '{_DIRECT_SKILL_NAME}'",
            {},
        )

        # 1. Activate skill (deterministic, no LLM)
        try:
            skill_content = registry.activate(_DIRECT_SKILL_NAME)
        except KeyError:
            logger.error(f"Skill '{_DIRECT_SKILL_NAME}' not found in registry")
            from chaos_agent.agent.state_helpers import fail_state
            from chaos_agent.agent.verdict import FailureCategory
            result = {
                **fail_state(FailureCategory.PREREQUISITE_FAILED, f"skill={_DIRECT_SKILL_NAME} not found"),
                "skill_name": "",
            }
            tracker.fail(f"Skill '{_DIRECT_SKILL_NAME}' not found")
            sync_node_status_to_session(state, DIRECT_SETUP,
                f"Skill '{_DIRECT_SKILL_NAME}' not found",
                detail={"safety_status": "rejected", "reason": "skill_not_found"})
            await sync_to_store(state, result)
            return result

        # 2. Build plan summary (for confirmation_gate and verifier)
        from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
        spec = read_fault_spec(state) or FaultSpec()
        scope = spec.scope
        target = spec.blade_target
        action = spec.blade_action
        ns = spec.namespace
        names = list(spec.names)

        param_str = ", ".join(f"{k}={v}" for k, v in spec.params.items() if v)
        flags_str = " ".join(spec.params_flags)

        plan = (
            f"Direct blade injection: blade create k8s {scope}-{target} {action}\n"
            f"Namespace: {ns}, Names: {','.join(names) if names else ''}\n"
            + (f"Parameters: {param_str}\n" if param_str else "")
            + (f"Flags: {flags_str}\n" if flags_str else "")
            + f"Scope: {scope}, Target: {target}, Action: {action}"
        )

        # 3. Read use-case specific content for verifier Layer 2.
        # match_use_cases returns ALL matching skill cases (not just the
        # first). When multiple cases share the same (scope, target, action)
        # — e.g. pod-cpu-fullload matches both "Pod CPU 使用率过高" AND
        # "HPA 副本达到上限" — load all of them so the verifier LLM can
        # pick the verification steps most relevant to the actual situation.
        skill_case_content = ""
        use_case_path = None
        try:
            all_matches = registry.match_use_cases(
                scope=scope, target=target, action=action
            )
            if all_matches:
                use_case_path = all_matches[0]
                if len(all_matches) == 1:
                    skill_case_content = registry.read_resource(
                        _DIRECT_SKILL_NAME, all_matches[0]
                    )
                    logger.info(
                        f"Direct setup: loaded use-case from {all_matches[0]} "
                        f"({len(skill_case_content)} chars)"
                    )
                else:
                    # Multiple candidates: load all and let verifier LLM
                    # choose the right verification methodology.
                    parts = []
                    for i, path in enumerate(all_matches):
                        try:
                            content = registry.read_resource(_DIRECT_SKILL_NAME, path)
                            label = path.split("/")[-1].replace(".md", "")
                            parts.append(
                                f"--- Candidate {i+1}: {label} ---\n{content}"
                            )
                        except Exception:
                            pass
                    skill_case_content = (
                        f"Multiple skill cases match this injection ({len(all_matches)} candidates). "
                        f"Read ALL candidates below and choose the verification steps "
                        f"most relevant to the ACTUAL cluster state (e.g. check if HPA "
                        f"exists before using HPA verification steps).\n\n"
                        + "\n\n".join(parts)
                    )
                    logger.info(
                        f"Direct setup: loaded {len(all_matches)} candidate use-cases: "
                        f"{all_matches}"
                    )
        except Exception as e:
            logger.warning(f"Failed to read use-case content for direct mode: {e}")

        # 4. Inject skill instructions into messages (verifier Layer2 reads these)
        #    Truncate to avoid oversized context
        messages = [
            HumanMessage(
                content=f"[Skill Instructions]\n{skill_content[:_MAX_SKILL_CONTENT_LEN]}"
            )
        ]

        result = {
            "skill_name": _DIRECT_SKILL_NAME,
            "plan": plan,
            "messages": messages,
            "skill_case_content": skill_case_content or None,
        }

        # 5. Collect target_metadata (FCAT context) for downstream nodes
        #    Must happen before safety_check (P1) and baseline_capture (P3).
        target_metadata = await _collect_context(state)
        if target_metadata is not None:
            result["target_metadata"] = target_metadata
            logger.info(f"FCAT: collected target_metadata: {list(target_metadata.keys())}")

        tracker.complete(f"Direct setup done: skill '{_DIRECT_SKILL_NAME}' activated")
        sync_node_status_to_session(state, DIRECT_SETUP, "Skill activated, use-case loaded",
            detail={"skill_name": _DIRECT_SKILL_NAME, "use_case_loaded": bool(skill_case_content)})
        await sync_to_store(state, result)

        # Record messages to session store immediately so they appear
        # in correct chronological order (before direct_execute's ToolMessages).
        from chaos_agent.memory.session_store import get_global_session_store
        _store = get_global_session_store()
        _tid = state.get("task_id", "")
        if _store and _tid:
            _store.append_messages(_tid, messages, node_name=DIRECT_SETUP)

        return result

    return direct_setup
