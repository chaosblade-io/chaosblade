"""Baseline capture node: pre-injection metric collection for direct mode.

Collects baseline metrics before fault injection so the verifier can perform
before/after comparison instead of relying solely on absolute thresholds.

Shared across ALL execution modes (direct and NL) — baseline_capture runs
after safety_check/confirmation_gate for every fault injection flow, then
route_after_baseline dispatches to direct_execute or execute_loop.

Strategy priority (matches the actual chain in ``make_baseline_capture``):
  1. LLM-driven (parse full skill_case_content to derive commands)
  2. Python Registry three-level lookup (scope,target,action) -> (scope,target) -> (scope,)
  3. Scope fallback

Each strategy is gated by a *full-viability* check: only a strategy whose
commands are all executable after template resolution short-circuits the
chain. Partially-viable strategies are remembered as a best-effort
fallback that is used if no later strategy produces a complete set.

Design principle: best-effort — any failure does NOT block injection.
"""

import asyncio
import inspect
import logging
from dataclasses import dataclass

from langchain_core.messages import HumanMessage

from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.evidence import EvidenceProfile
from chaos_agent.agent.node_names import BASELINE_CAPTURE
from chaos_agent.agent.nodes.execute._injection_detection import (
    _TOOL_POD_NAMESPACE as _TOOL_POD_NAMESPACE,  # re-exported (imported by tests)
)
from chaos_agent.agent.nodes.execute._kubeconfig_inject import sync_kubewiz_runtime
from chaos_agent.agent.nodes.store._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.memory.session_store import get_global_session_store
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.transports import (
    PROFILE_HOST,
    profile_of,
    resolve_channel_name,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BaselineCommand, the registry tables, the pure lookup/normalization helpers,
# and the observation-success judgement now live in ``_commands.py`` (Phase 2
# module split). Re-exported here so existing import paths (tests /
# plan_generator) keep working unchanged.
# ---------------------------------------------------------------------------
from chaos_agent.agent.nodes.baseline._commands import (  # noqa: E402
    BASELINE_COMMANDS as BASELINE_COMMANDS,
    BaselineCommand as BaselineCommand,
    _is_observation_success as _is_observation_success,
    _FCAT_DIMENSION_COMMANDS as _FCAT_DIMENSION_COMMANDS,
    _HOST_BASELINE_COMMANDS as _HOST_BASELINE_COMMANDS,
    _HOST_FALLBACK as _HOST_FALLBACK,
    _HOST_FALLBACK_CHAIN as _HOST_FALLBACK_CHAIN,
    _IOSTAT_FALLBACK_CHAIN as _IOSTAT_FALLBACK_CHAIN,
    _SCOPE_FALLBACK as _SCOPE_FALLBACK,
    _get_iostat_fallback_chain as _get_iostat_fallback_chain,
    _lookup_baseline_commands as _lookup_baseline_commands,
    _normalize_debug_namespace as _normalize_debug_namespace,
)


# ---------------------------------------------------------------------------
# Template variable resolution + coverage / evidence-supplement helpers now
# live in ``_templates.py`` (Phase 2 module split). Re-exported here so existing
# import paths (tests) keep working unchanged.
# ---------------------------------------------------------------------------
from chaos_agent.agent.nodes.baseline._templates import (  # noqa: E402
    _evidence_supplement_commands as _evidence_supplement_commands,
    _resolve_templates as _resolve_templates,
    _target_coverage as _target_coverage,
)



# ---------------------------------------------------------------------------
# Command execution (run resolved baseline commands → observation dicts) now
# lives in ``_executors.py`` (Phase 2 module split). Re-exported here so
# existing import paths (tests) keep working unchanged.
# ---------------------------------------------------------------------------
from chaos_agent.agent.nodes.baseline._executors import (  # noqa: E402
    _DEBUG_CONTAINER_NAME as _DEBUG_CONTAINER_NAME,
    _create_and_wait_debug_pod as _create_and_wait_debug_pod,
    _delete_debug_pod as _delete_debug_pod,
    _exec_debug_two_step as _exec_debug_two_step,
    _exec_host_simple as _exec_host_simple,
    _exec_in_debug_pod as _exec_in_debug_pod,
    _exec_in_tool_pod as _exec_in_tool_pod,
    _exec_simple as _exec_simple,
    _execute_observations as _execute_observations,
    _parse_debug_pod_name as _parse_debug_pod_name,
    _wait_for_debug_pod_ready as _wait_for_debug_pod_ready,
)



# ---------------------------------------------------------------------------
# LLM-driven baseline derivation (primary strategy) now lives in
# ``_llm_derive.py`` (Phase 2 module split). Re-exported here so existing
# import paths (tests) keep working unchanged.
# ---------------------------------------------------------------------------
from chaos_agent.agent.nodes.baseline._llm_derive import (  # noqa: E402
    _LLM_BASELINE_MAX_RETRIES as _LLM_BASELINE_MAX_RETRIES,
    _llm_derive_baseline_commands as _llm_derive_baseline_commands,
    _llm_retry_failed_commands as _llm_retry_failed_commands,
    _parse_llm_json_output as _parse_llm_json_output,
    _validate_and_filter_commands as _validate_and_filter_commands,
)


# ---------------------------------------------------------------------------
# baseline_capture node function
# ---------------------------------------------------------------------------

def _run_baseline_extractors(
    resolved: list, observations: list, state: AgentState,
) -> dict:
    """Phase 4.5: run per-command extractors over successful observations.

    Pure extraction from ``baseline_capture`` (behaviour unchanged). Parses the
    stdout already captured into structured fields downstream nodes can consume,
    instead of letting them re-issue the same kubectl call. Failure of any
    extractor is non-fatal: log debug, skip that field, the consumer falls back
    to its own fetch. A buggy extractor returning a non-dict is ignored rather
    than crashing baseline_capture.
    """
    extracted_metadata: dict = {}
    for cmd_info, obs in zip(resolved, observations):
        if obs.get("exit_code") != 0:
            continue  # don't try to parse error output
        for extractor in cmd_info.get("_extractors") or []:
            try:
                fields = extractor(obs.get("stdout", "") or "", state)
            except Exception:
                logger.debug(
                    "baseline extractor %s raised on %s (non-fatal)",
                    getattr(extractor, "__name__", repr(extractor)),
                    cmd_info.get("description", "?"),
                    exc_info=True,
                )
                continue
            # Defensive: contract says extractors return a dict (possibly empty).
            # A buggy extractor returning the wrong type (None / list / int)
            # would crash the .update() below and take baseline_capture down
            # with it. ``isinstance`` keeps the runner robust against future
            # extractor authors who break the contract.
            if not isinstance(fields, dict):
                logger.debug(
                    "baseline extractor %s returned non-dict %r "
                    "(contract violation, ignored)",
                    getattr(extractor, "__name__", repr(extractor)),
                    type(fields).__name__,
                )
                continue
            if fields:
                extracted_metadata.update(fields)
    return extracted_metadata


def _assemble_baseline_result(
    spec, profile: str, source: str, resolved: list, observations: list,
    extracted_metadata: dict, state: AgentState,
) -> dict:
    """Phase 5: assemble the ``baseline_data`` result dict + merge extractor fields.

    Pure extraction from ``baseline_capture`` (behaviour unchanged).
    """
    evidence_profile = EvidenceProfile.for_fault(spec, profile)
    successful_observations = [
        observation for observation in observations
        if _is_observation_success(observation)
    ]
    evidence_coverage = evidence_profile.coverage(successful_observations)
    target_coverage = _target_coverage(
        spec, resolved, successful_observations,
    )
    result = {
        "baseline_data": {
            "captured_at": now_iso(),
            "source": source,
            "observations": observations,
            "success_count": len(successful_observations),
            "evidence_coverage": evidence_coverage.as_dict(),
            "target_coverage": target_coverage,
        }
    }
    if evidence_coverage.missing:
        logger.warning(
            "Baseline evidence profile %s is incomplete: %s",
            evidence_coverage.profile_id,
            ", ".join(evidence_coverage.missing),
        )
    if target_coverage["applicable"] and not target_coverage["complete"]:
        logger.warning(
            "Baseline target coverage is partial: %d/%d observed (%s)",
            target_coverage["observed_count"],
            target_coverage["requested_count"],
            target_coverage["collection_mode"],
        )

    # Merge extracted fields into target_metadata. ``AgentState`` has no reducer
    # for this field, so we MUST do the merge here — returning just
    # ``extracted_metadata`` would clobber whatever direct_setup wrote earlier
    # (e.g. ``pod_memory_limit_mb``). Empty-dict short-circuit avoids writing
    # back an unchanged value for the common case.
    if extracted_metadata:
        existing_metadata = state.get("target_metadata") or {}
        merged = {**existing_metadata, **extracted_metadata}
        result["target_metadata"] = merged
        logger.info(
            "baseline extractors produced: %s",
            sorted(extracted_metadata.keys()),
        )
    return result


async def _emit_baseline_observability(
    state: AgentState, result: dict, source: str, observations: list, tracker,
) -> None:
    """Phase 6: emit tracker / session / TaskStore / message-history observability.

    Pure extraction from ``baseline_capture`` (behaviour unchanged).
    """
    _success = result["baseline_data"]["success_count"]
    _total = len(observations)
    # Build output previews for detail dict (standard [:200] truncation)
    _obs_previews = []
    for obs in observations:
        _preview = ""
        if obs.get("exit_code") == 0 and obs.get("stdout"):
            _preview = obs["stdout"][:200]
        elif obs.get("stderr"):
            _preview = obs["stderr"][:200]
        _obs_previews.append({
            "description": obs["description"],
            "exit_code": obs.get("exit_code", -1),
            "stdout_preview": _preview,
        })
    tracker.complete(
        f"Baseline capture done: {source} strategy, "
        f"{_success}/{_total} commands succeeded",
        detail={
            "source": source,
            "success_count": _success,
            "total_count": _total,
            "observations": _obs_previews,
        },
    )

    # ── Observability: session status ──
    sync_node_status_to_session(
        state, BASELINE_CAPTURE,
        f"Baseline collected ({source}): {_success}/{_total} succeeded",
        detail={
            "source": source,
            "success_count": _success,
            "total_count": _total,
        },
    )

    # ── Observability: TaskStore persistence ──
    await sync_to_store(state, result)

    # ── Observability: message history (full content, no truncation) ──
    _store = get_global_session_store()
    _tid = state.get("task_id", "")
    if _store and _tid:
        _session_msgs = [
            HumanMessage(content=(
                f"[Baseline Capture] Collected pre-injection metrics "
                f"({source} strategy, {_success}/{_total} succeeded)"
            )),
        ]
        for obs in observations:
            _obs_parts = [
                f"### {obs['description']}",
                f"Command: `{obs.get('command', '')}`",
            ]
            if obs.get("exit_code") is not None:
                _obs_parts.append(f"Exit code: {obs['exit_code']}")
            if obs.get("stdout"):
                _obs_parts.append(f"```\n{obs['stdout']}\n```")
            if obs.get("stderr"):
                _obs_parts.append(f"stderr:\n```\n{obs['stderr']}\n```")
            _session_msgs.append(HumanMessage(content="\n".join(_obs_parts)))
        _store.append_messages(_tid, _session_msgs, node_name=BASELINE_CAPTURE)


@dataclass(frozen=True)
class _BaselineCtx:
    """Static per-invocation inputs shared by the baseline_capture phase helpers.

    Groups the values every phase (strategy chain / selection / collection)
    reads, so helpers take ``ctx`` instead of 12+ positional params. Pure
    plumbing — no behaviour.
    """

    llm: object
    state: AgentState
    task_id: str
    tracker: object
    spec: object
    scope: str
    target: str
    action: str
    skill_case: str
    kubeconfig: str
    channel: str
    profile: str


def _build_baseline_ctx(state: AgentState, llm, task_id: str, tracker) -> _BaselineCtx:
    """Phase 1: extract fault params from the spec + resolve channel/profile.

    Preserves the original ordering: read spec fields → ``sync_kubewiz_runtime``
    → resolve channel/profile (the sync MUST run before channel resolution).
    ``read_fault_spec`` returns a typed FaultSpec so we read
    scope/blade_target/blade_action directly instead of from 3 state fields.
    """
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    spec = read_fault_spec(state)
    scope = spec.scope if spec else ""
    target = spec.blade_target if spec else ""
    action = spec.blade_action if spec else ""
    skill_case = state.get("skill_case_content", "")
    kubeconfig = state.get("kubeconfig", "")
    sync_kubewiz_runtime(state)

    # Connection-channel capability profile drives prompt assembly, registry
    # table selection, fallback set, and execution dispatch (k8s = kubectl,
    # host = plain shell diagnostics). Host baseline runs the SAME strategy
    # chain as k8s.
    channel = resolve_channel_name(state)
    profile = profile_of(channel)
    return _BaselineCtx(
        llm=llm, state=state, task_id=task_id, tracker=tracker,
        spec=spec, scope=scope, target=target, action=action,
        skill_case=skill_case, kubeconfig=kubeconfig,
        channel=channel, profile=profile,
    )


def _build_strategy_chain(ctx: _BaselineCtx) -> list:
    """Phase 2a: build the lazy ``(name, factory)`` baseline strategy chain.

    The three strategies (llm → registry → scope_fallback) close over ``ctx``.
    Returned as a list so both selection (Phase 2b) and the execution-level
    fallback (Phase 4.0.7) share the same chain. Pure extraction.
    """
    async def _llm_strategy():
        if not ctx.llm or not ctx.skill_case:
            return []
        ctx.tracker.update(
            "Strategy: LLM-driven baseline derivation...",
            {"step": "strategy", "strategy": "llm"},
        )
        await dispatch_node_message(
            "baseline_capture", "正在通过 LLM 推导基线采集命令...\n\n",
        )
        try:
            return await asyncio.wait_for(
                _llm_derive_baseline_commands(
                    ctx.llm, ctx.skill_case, ctx.scope, ctx.target, ctx.action,
                    channel=ctx.channel, profile=ctx.profile,
                    namespace=ctx.spec.namespace if ctx.spec else "",
                    names=ctx.spec.names if ctx.spec else (),
                    labels=dict(ctx.spec.labels) if ctx.spec and ctx.spec.labels else None,
                    task_id=ctx.task_id,
                ),
                timeout=settings.timeout_baseline_llm,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "LLM baseline derivation timed out after %ds, "
                "falling back to registry",
                settings.timeout_baseline_llm,
            )
            return []

    def _registry_strategy():
        return _lookup_baseline_commands(ctx.profile, ctx.scope, ctx.target, ctx.action)

    def _scope_fallback_strategy():
        if ctx.profile == PROFILE_HOST:
            return list(_HOST_FALLBACK)
        return _SCOPE_FALLBACK.get(ctx.scope, [])

    return [
        ("llm", _llm_strategy),
        ("registry", _registry_strategy),
        ("scope_fallback", _scope_fallback_strategy),
    ]


async def _select_baseline_strategy(
    ctx: _BaselineCtx, strategy_chain: list,
) -> tuple[list, str]:
    """Phase 2b: run the viability-gated strategy chain + P3 FCAT supplement.

    Returns ``(commands, source)``. Pure extraction from baseline_capture: try
    each strategy in order, lock in the first fully-viable one, keep the first
    partial as a best-effort fallback, then enrich with FCAT P3
    baseline_supplement dimensions (k8s only). Behaviour unchanged.

    Each strategy is tried in priority order (llm → registry → scope_fallback).
    A strategy's output is accepted only if it is *fully viable* (every command
    is executable after template resolution). A *partially viable* strategy is
    remembered as a fallback but does NOT short-circuit the chain — we keep
    trying later strategies for a complete set. If none is fully viable, the
    first partial we saw is used as best-effort. (Rationale: task 23ee60d retro
    — the previous "any-viable wins" rule let a half-broken strategy lock in and
    silently dropped the ``kubectl top`` baseline.)
    """
    state, profile = ctx.state, ctx.profile
    scope, target, action, tracker = ctx.scope, ctx.target, ctx.action, ctx.tracker
    commands = []
    source = "none"
    partial_commands: list = []
    partial_source = ""
    partial_viable = 0
    partial_total = 0

    for strategy_name, strategy_fn in strategy_chain:
        try:
            strategy_commands = await strategy_fn() \
                if inspect.iscoroutinefunction(strategy_fn) \
                else strategy_fn()
        except Exception as e:
            logger.warning("Strategy '%s' raised exception: %s", strategy_name, e)
            continue

        if not strategy_commands:
            continue

        # Viability Gate: how many commands survive template resolution
        resolved_preview = _resolve_templates(strategy_commands, state, profile)
        viable_count = sum(1 for c in resolved_preview if not c.get("_unresolved"))
        total_count = len(strategy_commands)

        if viable_count == 0:
            logger.warning(
                "Strategy '%s' produced %d command(s) but 0 viable "
                "(all unresolved after template resolution), trying next",
                strategy_name, total_count,
            )
            tracker.update(
                f"Strategy {strategy_name}: 0 viable, falling back",
                {"step": "strategy", "source": strategy_name,
                 "viable": 0, "total": total_count},
            )
            continue

        if viable_count == total_count:
            # Fully viable — lock in this strategy.
            commands = strategy_commands
            source = strategy_name
            tracker.update(
                f"Strategy selected: {strategy_name} "
                f"({viable_count}/{total_count} viable, complete)",
                {"step": "strategy", "source": strategy_name,
                 "viable": viable_count, "total": total_count},
            )
            await dispatch_node_message(
                "baseline_capture",
                f"策略 {strategy_name} 命中（{viable_count}/{total_count} 条命令可用）\n\n",
            )
            break

        # Partial: keep first partial as fallback, but continue trying
        # later strategies (e.g. LLM) for a complete set.
        if not partial_commands:
            partial_commands = strategy_commands
            partial_source = strategy_name
            partial_viable = viable_count
            partial_total = total_count
        logger.info(
            "Strategy '%s' is partial (%d/%d viable), retained as "
            "fallback; continuing strategy chain",
            strategy_name, viable_count, total_count,
        )
        tracker.update(
            f"Strategy {strategy_name}: partial "
            f"({viable_count}/{total_count}), keep trying",
            {"step": "strategy", "source": strategy_name,
             "viable": viable_count, "total": total_count,
             "partial": True},
        )

    # No fully-viable strategy — fall back to the first partial we saw.
    if not commands and partial_commands:
        commands = partial_commands
        source = partial_source
        logger.warning(
            "No fully-viable baseline strategy; using partial '%s' "
            "(%d/%d viable) as best-effort fallback",
            partial_source, partial_viable, partial_total,
        )
        tracker.update(
            f"Strategy selected: {partial_source} (partial fallback, "
            f"{partial_viable}/{partial_total} viable)",
            {"step": "strategy", "source": partial_source,
             "viable": partial_viable, "total": partial_total,
             "partial_fallback": True},
        )
        await dispatch_node_message(
            "baseline_capture",
            f"策略 {partial_source} 命中（部分可用，{partial_viable}/{partial_total} 条命令）\n\n",
        )

    # P3: FCAT baseline_supplement — enrich with dimensions from knowledge docs
    # (k8s-only: the dimension→command map is kubectl-based; host
    # baseline relies on its own registry/fallback diagnostics).
    target_metadata = state.get("target_metadata") or {}
    _p3_added_dims = []
    if profile != PROFILE_HOST and (target_metadata or (scope and target and action)):
        from chaos_agent.utils.fault_context import lookup_adaptations
        supplements = lookup_adaptations(
            scope, target, action, target_metadata or {},
            rule_type="baseline_supplement",
        )
        for supp in supplements:
            dimensions = supp.action.get("dimensions", [])
            if not dimensions:
                continue
            for dim in dimensions:
                # Map dimension names to scope-aware BaselineCommand entries
                # (dimension → scope → command — P3 knowledge-driven enrichment)
                dim_cmds = _FCAT_DIMENSION_COMMANDS.get(dim, {})
                dim_cmd = dim_cmds.get(scope) or dim_cmds.get("pod")
                if dim_cmd:
                    # Deduplicate by description
                    if not any(c.description == dim_cmd.description for c in commands):
                        commands.append(dim_cmd)
                        _p3_added_dims.append(dim)
                        logger.info(
                            "FCAT P3: added baseline command for dimension '%s': %s",
                            dim, dim_cmd.description,
                        )
                else:
                    logger.warning(
                        "FCAT P3: no command mapping for dimension '%s', skipping", dim,
                    )
            # Write P3 session event after processing each supplement
            if _p3_added_dims:
                sync_node_status_to_session(state, BASELINE_CAPTURE,
                    f"P3 baseline supplement: added {', '.join(_p3_added_dims)} dimensions",
                    detail={"dimensions": _p3_added_dims, "rule_id": supp.id})
                if settings.is_debug and tracker:
                    tracker.update(
                        f"[P3] baseline supplement: added {', '.join(_p3_added_dims)} dimensions"[:200],
                        {"debug": True, "fcat": True},
                    )

    tracker.update(
        f"Strategy selected: {source} ({len(commands)} command(s))",
        {"step": "strategy", "source": source, "command_count": len(commands)},
    )
    return commands, source


async def _collect_observations(
    ctx: _BaselineCtx, commands: list, source: str, strategy_chain: list,
) -> tuple[list, list, str]:
    """Phases 3–4: resolve templates, execute, LLM self-correct retry, and the
    execution-level strategy fallback.

    Returns ``(resolved, observations, source)``. Pure extraction — behaviour
    unchanged (see the inline 4.0.5 / 4.0.7 rationale comments).
    """
    state, profile, spec = ctx.state, ctx.profile, ctx.spec
    kubeconfig, task_id = ctx.kubeconfig, ctx.task_id
    llm, skill_case, tracker = ctx.llm, ctx.skill_case, ctx.tracker
    scope, target, action, channel = ctx.scope, ctx.target, ctx.action, ctx.channel

    # 3. Resolve template variables
    resolved = _resolve_templates(commands, state, profile)
    evidence_supplements = _evidence_supplement_commands(profile, spec, resolved)
    if evidence_supplements:
        commands = [*commands, *evidence_supplements]
        resolved = _resolve_templates(commands, state, profile)
        logger.info(
            "Added %d baseline evidence supplement(s) for profile %s",
            len(evidence_supplements), profile,
        )

    # 4. Execute collection (best-effort)
    tracker.update(
        f"Executing {len(resolved)} baseline command(s)...",
        {"step": "execute", "command_count": len(resolved)},
    )
    await dispatch_node_message(
        "baseline_capture",
        f"正在执行 {len(resolved)} 条基线采集命令...\n\n",
    )
    observations = await _execute_observations(resolved, kubeconfig, task_id)

    # 4.0.5 LLM self-correcting retry: when LLM-generated commands
    # fail execution, feed errors back to the LLM and let it
    # self-correct (up to _LLM_BASELINE_MAX_RETRIES attempts).
    # Runs BEFORE the strategy-level fallback (4.0.7) so that the
    # LLM is given a chance to fix itself before we abandon the
    # primary strategy and reach for registry / scope_fallback.
    if source == "llm" and llm:
        all_pairs = list(zip(resolved, observations))

        for retry_num in range(1, _LLM_BASELINE_MAX_RETRIES + 1):
            failed_obs = [o for _, o in all_pairs
                          if o.get("exit_code") != 0]
            if not failed_obs:
                break

            logger.info(
                "LLM baseline retry %d/%d: %d command(s) failed",
                retry_num, _LLM_BASELINE_MAX_RETRIES, len(failed_obs),
            )
            tracker.update(
                f"LLM retry {retry_num}/{_LLM_BASELINE_MAX_RETRIES}: "
                f"{len(failed_obs)} command(s) failed, "
                f"regenerating with error feedback...",
                {"step": "llm_retry", "attempt": retry_num,
                 "failed_count": len(failed_obs)},
            )
            await dispatch_node_message(
                "baseline_capture",
                f"LLM 自纠错重试 {retry_num}/{_LLM_BASELINE_MAX_RETRIES}: "
                f"{len(failed_obs)} 条命令失败，正在重新生成...\n\n",
            )

            try:
                retry_commands = await asyncio.wait_for(
                    _llm_retry_failed_commands(
                        llm, skill_case, scope, target, action,
                        failed_obs,
                        channel=channel, profile=profile,
                        namespace=spec.namespace if spec else "",
                        names=spec.names if spec else (),
                        labels=dict(spec.labels) if spec and spec.labels else None,
                        task_id=task_id,
                    ),
                    timeout=settings.timeout_baseline_llm,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "LLM baseline retry %d timed out after %ds",
                    retry_num, settings.timeout_baseline_llm,
                )
                await dispatch_node_message(
                    "baseline_capture",
                    f"LLM 自纠错重试 {retry_num} 超时，放弃重试\n\n",
                )
                break
            if not retry_commands:
                logger.info(
                    "LLM retry %d: no corrected commands returned",
                    retry_num,
                )
                await dispatch_node_message(
                    "baseline_capture",
                    f"LLM 自纠错重试 {retry_num} 未返回有效命令，放弃重试\n\n",
                )
                break

            retry_resolved = _resolve_templates(retry_commands, state, profile)
            retry_viable = [
                c for c in retry_resolved if not c.get("_unresolved")
            ]
            if not retry_viable:
                break

            retry_obs = await _execute_observations(
                retry_resolved, kubeconfig, task_id,
            )

            # Keep original successes, replace failures with retry.
            # Use _is_observation_success so kubectl partial failures
            # (exit_code=0 + 'Error from server' in stdout) are treated
            # as failures and properly retried.
            success_pairs = [
                (r, o) for r, o in all_pairs
                if _is_observation_success(o)
            ]
            all_pairs = success_pairs + list(
                zip(retry_resolved, retry_obs)
            )

        if all_pairs:
            resolved, observations = (
                [r for r, _ in all_pairs],
                [o for _, o in all_pairs],
            )
        else:
            resolved, observations = [], []

    # 4.0.7 Execution-level strategy fallback：
    # 当前 strategy 的 Viability Gate 仅校验 "模板 placeholder 是否填得上"，
    # 不保证命令真的能跑通（典型反例：模板拼接缺 ``-l`` 前缀时
    # ``label_selector`` 字符串非空 → viable_count > 0 → 锁定该策略 →
    # 执行全部失败 → 没机会回落到下一级策略）。
    #
    # 设计意图（LLM 优先链：llm → registry → scope_fallback）：
    #   首选策略命中且至少 1 条跑通 → 沿用首选
    #   首选策略命中但全部跑挂      → 自动回落到链中其他未尝试的策略
    #   首选策略完全没给           → 直接走下一级（已由 viable gate 处理）
    #
    # 注意：source == "llm" 时同样会进入此段，因为 4.0.5 已经给过
    # LLM 最多 3 次 self-correcting retry，retry 仍救不回来才会走
    # 到这里。``_attempted = {source}`` 保证 LLM 不会被再调一次，
    # 也就杜绝了"LLM 已经退化失败 → 再调 LLM"的死循环风险。
    if (
        observations
        and not any(_is_observation_success(o) for o in observations)
    ):
        _attempted = {source}
        for _fb_name, _fb_fn in strategy_chain:
            if _fb_name in _attempted:
                continue
            try:
                _fb_commands = await _fb_fn() \
                    if inspect.iscoroutinefunction(_fb_fn) \
                    else _fb_fn()
            except Exception as e:
                logger.warning(
                    "Fallback strategy '%s' raised exception: %s",
                    _fb_name, e,
                )
                _attempted.add(_fb_name)
                continue
            if not _fb_commands:
                _attempted.add(_fb_name)
                continue
            _fb_resolved_preview = _resolve_templates(_fb_commands, state, profile)
            _fb_viable = sum(
                1 for c in _fb_resolved_preview
                if not c.get("_unresolved")
            )
            if _fb_viable == 0:
                _attempted.add(_fb_name)
                continue

            logger.warning(
                "Strategy '%s' executed 0/%d succeeded, "
                "falling through to '%s' (%d/%d viable)",
                source, len(observations),
                _fb_name, _fb_viable, len(_fb_commands),
            )
            tracker.update(
                f"Strategy {source}: 0/{len(observations)} succeeded, "
                f"falling through to {_fb_name}",
                {"step": "strategy_fallback",
                 "from": source, "to": _fb_name,
                 "from_total": len(observations)},
            )
            await dispatch_node_message(
                "baseline_capture",
                f"策略 {source} 全部失败，回退到 {_fb_name}...\n\n",
            )

            commands = list(_fb_commands)
            source = _fb_name
            resolved = _resolve_templates(commands, state, profile)
            _fb_supplements = _evidence_supplement_commands(
                profile, spec, resolved,
            )
            if _fb_supplements:
                commands.extend(_fb_supplements)
                resolved = _resolve_templates(commands, state, profile)
            observations = await _execute_observations(
                resolved, kubeconfig, task_id,
            )
            _attempted.add(_fb_name)

            if any(_is_observation_success(o) for o in observations):
                break
            # 否则继续遍历下一个 strategy

    return resolved, observations, source


def make_baseline_capture(llm=None, registry=None):
    """Factory: create baseline_capture node with LLM and SkillRegistry injection."""

    async def baseline_capture(state: AgentState) -> dict:
        task_id = state.get("task_id", "") or ""

        # ── Observability: tracker event ──
        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "baseline_capture",
            "Baseline capture: collecting pre-injection metrics",
            {},
        )

        try:
            # Phases 1–2 extracted to _build_baseline_ctx / _build_strategy_chain.
            ctx = _build_baseline_ctx(state, llm, task_id, tracker)
            strategy_chain = _build_strategy_chain(ctx)

            # Phase 2b extracted to _select_baseline_strategy.
            commands, source = await _select_baseline_strategy(ctx, strategy_chain)

            # Phases 3–4 extracted to _collect_observations.
            resolved, observations, source = await _collect_observations(
                ctx, commands, source, strategy_chain,
            )

            # 4.5 Run per-command extractors → merge structured fields into
            # target_metadata (Phase 4.5 extracted to _run_baseline_extractors).
            extracted_metadata = _run_baseline_extractors(
                resolved, observations, state,
            )

            # 5. Assemble baseline_data (Phase 5 extracted to _assemble_baseline_result)
            result = _assemble_baseline_result(
                ctx.spec, ctx.profile, source, resolved, observations,
                extracted_metadata, state,
            )

            # ── Observability: tracker / session / store / history ──
            # (Phase 6 extracted to _emit_baseline_observability)
            await _emit_baseline_observability(
                state, result, source, observations, tracker,
            )

            return result

        except Exception as e:
            logger.error(f"baseline_capture unexpected error: {e}", exc_info=True)
            # Exception safety: never block injection
            result = {
                "baseline_data": {
                    "captured_at": now_iso(),
                    "source": "error",
                    "observations": [],
                    "success_count": 0,
                }
            }
            tracker.fail(f"Baseline capture failed: {e}")
            sync_node_status_to_session(
                state, BASELINE_CAPTURE,
                f"Baseline capture failed: {e}",
                detail={"source": "error", "error": str(e)},
            )
            await sync_to_store(state, result)
            return result

    return baseline_capture
