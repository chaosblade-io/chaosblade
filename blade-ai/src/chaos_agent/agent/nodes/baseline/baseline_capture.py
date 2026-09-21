"""Baseline capture node: pre-injection metric collection.

Collects baseline metrics before fault injection so the verifier can perform
before/after comparison instead of relying solely on absolute thresholds.

Runs for every fault injection flow: baseline_capture runs after
safety_check/confirmation_gate, then the graph continues to execute_loop.

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
import json
import logging
import re
from dataclasses import dataclass, replace

from langchain_core.messages import HumanMessage

from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.evidence import EvidenceProfile
from chaos_agent.agent.node_names import BASELINE_CAPTURE
from chaos_agent.tools.pod_discovery import (  # noqa: F401 — re-exported (imported by tests)
    TOOL_POD_NAMESPACE as _TOOL_POD_NAMESPACE,
)
from chaos_agent.agent.nodes.execute._kubeconfig_inject import sync_kubewiz_runtime
from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
    _find_saved_plan,
)
from chaos_agent.agent.nodes.store._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.target_guard.classifier import canonicalise_kind
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.memory.session_store import get_global_session_store
from chaos_agent.observability.status_tracker import (
    elided_preview,
    get_tracker,
    StatusCategory,
)
from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
from chaos_agent.transports import (
    PROFILE_HOST,
    PROFILE_K8S,
    TransportTarget,
    execute_via_transport,
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
    _is_empty_observation as _is_empty_observation,
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
    # Validity split (#16 fix C — the Validity axiom): a successful
    # execution is not necessarily a valid observation. An exit-0
    # "No resources found" / empty-items observation anchored on nothing;
    # counting it as coverage and quality is how a 1-valid + 3-empty
    # baseline sailed through as "4/4 succeeded, high confidence" in the
    # R10 live-cluster replay. Recompute via the predicate rather than
    # trusting the executor stamp so legacy / hand-built observation
    # lists (tests, hydrated old tasks) classify identically to live ones.
    # ``expected_absence`` observations keep their #31 semantics: the
    # absence IS the value, so they count as usable (valid) — but stay
    # outside ``success_count`` exactly as before.
    valid_observations = [
        observation for observation in successful_observations
        if not _is_empty_observation(observation)
        and not observation.get("expected_absence")
    ]
    empty_observations = [
        observation for observation in successful_observations
        if _is_empty_observation(observation)
        and not observation.get("expected_absence")
    ]
    absent_observations = [
        observation for observation in observations
        if observation.get("expected_absence")
    ]
    # Coverage measures what was OBSERVED, so only value-carrying
    # observations count: an empty-spinning selector query does not cover
    # the pod-status dimension no matter how cleanly it exited. Expected
    # absences DO cover their dimension — the absence is the observed
    # value — so coverage receives the usable set (valid + absent).
    usable_observations = valid_observations + absent_observations
    evidence_coverage = evidence_profile.coverage(usable_observations)
    target_coverage = _target_coverage(
        spec, resolved, usable_observations,
    )
    result = {
        "baseline_data": {
            "captured_at": now_iso(),
            "source": source,
            "observations": observations,
            "success_count": len(successful_observations),
            "valid_count": len(usable_observations),
            "empty_count": len(empty_observations),
            # Denominator must ship WITH the counts: persistent-layer
            # consumers render "N/M succeeded" from this dict alone
            # (verify/_verifier_messages, recover/_recover_layer1).
            # Without it they fall back and the receipt reads "N/0" —
            # the cosmetic defect pinned in the #13/#10 case audits
            # (inject-a19d3807 "4/0", inject-9c9f659b "5/0").
            "total_count": len(observations),
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
    # ``extracted_metadata`` would clobber whatever an upstream node wrote
    # earlier (e.g. ``pod_memory_limit_mb``). Empty-dict short-circuit avoids
    # writing back an unchanged value for the common case.
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
    _valid = result["baseline_data"]["valid_count"]
    _empty = result["baseline_data"]["empty_count"]
    _total = len(observations)
    # Build output previews for detail dict. Failure previews keep BOTH
    # ends (elided_preview): kubectl puts the causal error LAST, and the
    # transport wrapper merges stderr into stdout, so a head-only cut can
    # hide the root cause (#31) — and a failure whose merged output sits in
    # stdout used to yield an EMPTY preview here. Success keeps the head.
    _obs_previews = []
    for obs in observations:
        _preview = ""
        if obs.get("exit_code") == 0 and obs.get("stdout"):
            _preview = obs["stdout"][:200]
        elif obs.get("stderr"):
            _preview = elided_preview(obs["stderr"], 60, 140)
        elif obs.get("stdout"):
            _preview = elided_preview(obs["stdout"], 60, 140)
        _obs_previews.append({
            "description": obs["description"],
            "exit_code": obs.get("exit_code", -1),
            "stdout_preview": _preview,
        })
    # Honest counting (#16 fix C): "{_success}/{_total} succeeded" reads as a
    # quality claim, but 4/4 with 3 of them empty-spinning is a degraded
    # baseline, not a healthy one. Append the valid/empty split whenever
    # unexplained empties exist so every consumer of the receipt (tracker,
    # session status, message history) sees the same honest number.
    _counts = (
        f"{_success}/{_total} commands succeeded"
        if _empty <= 0
        else f"{_success}/{_total} commands succeeded "
        f"({_valid} valid + {_empty} empty — empty observations captured "
        f"no value and cannot serve as comparison baselines)"
    )
    tracker.complete(
        f"Baseline capture done: {source} strategy, {_counts}",
        detail={
            "source": source,
            "success_count": _success,
            "valid_count": _valid,
            "empty_count": _empty,
            "total_count": _total,
            "observations": _obs_previews,
        },
    )

    # ── Observability: session status ──
    sync_node_status_to_session(
        state, BASELINE_CAPTURE,
        f"Baseline collected ({source}): {_counts}",
        detail={
            "source": source,
            "success_count": _success,
            "valid_count": _valid,
            "empty_count": _empty,
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
                f"({source} strategy, {_counts})"
            )),
        ]
        for obs in observations:
            _obs_parts = [
                f"### {obs['description']}",
                f"Command: `{obs.get('command', '')}`",
            ]
            if obs.get("exit_code") is not None:
                _obs_parts.append(f"Exit code: {obs['exit_code']}")
            if obs.get("expected_absence"):
                # Judged (LLM retry, #31) or machine-marked (planned
                # creation, #16 fix B) pre-injection absence: the absence
                # IS the baseline value — rendered as an existence
                # baseline, distinct from an empty-spin observation.
                _obs_parts.append(
                    f"Note: expected pre-injection ABSENCE — "
                    f"{obs['expected_absence']}"
                )
            elif obs.get("empty_observation") or (
                _is_empty_observation(obs) and _is_observation_success(obs)
            ):
                _obs_parts.append(
                    "Note: EMPTY observation — captured no value; nothing "
                    "matched the query, so this is not a usable comparison "
                    "baseline"
                )
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
    # #16 fix A (Identity axiom): the authoritative pod selector discovered
    # from the workload's own spec (None on every non-applicable route).
    # Consumed by the LLM derive/retry target context; deliberately NOT
    # written into ``spec.labels`` — that field's ~15 downstream consumers
    # (safety_check conflict fingerprints, tool_screener drift correction,
    # ...) all assume spec-scope identity, and a pod selector is cross-kind
    # identity. Writing it there would pollute the whole chain.
    pod_selector: dict[str, str] | None = None


def _build_baseline_ctx(state: AgentState, llm, task_id: str, tracker) -> _BaselineCtx:
    """Phase 1: extract fault params from the spec + resolve channel/profile.

    Preserves the original ordering: read spec fields → ``sync_kubewiz_runtime``
    → resolve channel/profile (the sync MUST run before channel resolution).
    ``read_fault_spec`` returns a typed FaultSpec so we read
    scope/fault_target/fault_action directly instead of from 3 state fields.
    """
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    spec = read_fault_spec(state)
    scope = spec.scope if spec else ""
    target = spec.fault_target if spec else ""
    action = spec.fault_action if spec else ""
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


# jsonpath for the pod-owning selector of each workload-kind scope. Service
# selectors live at ``.spec.selector`` directly (no matchLabels wrapper);
# workload selectors at ``.spec.selector.matchLabels``. A selectorless
# service returns an empty/None jsonpath and discovery fail-opens to None.
_POD_OWNER_SELECTOR_JSONPATH = {
    "deployment": "{.spec.selector.matchLabels}",
    "statefulset": "{.spec.selector.matchLabels}",
    "daemonset": "{.spec.selector.matchLabels}",
    "service": "{.spec.selector}",
}


async def _discover_pod_selector(ctx: _BaselineCtx) -> dict[str, str] | None:
    """#16 fix A (Identity axiom): authoritative pod-identity discovery.

    The baseline derive LLM emits cross-kind queries — pod-level state
    under a workload-scope fault — and needs the target's pod selector.
    On this route ``spec.labels`` is structurally empty (the B31
    scope/name guards both reject a pod probe's labels under a
    deployment-scope spec; R10 Part3 proved the CORRECT label was in the
    message track and got dropped), so the LLM had nothing authoritative
    to anchor on and invented one (``app=drill-pvc-target`` — the
    deployment name as a label value).

    Rather than widening the B31 write (mixing pod-selector identity into
    ``spec.labels`` would pollute its ~15 downstream consumers), ask the
    API server directly: ``kubectl get <kind> <name> -o jsonpath=<sel>``
    returns the workload's own selector as a JSON object (verified live
    in R10: ``{"app":"drill-pvc"}``). Fail-open: any error, non-zero
    exit, unparseable or empty output → ``None`` and the derive context
    simply lacks the selector line, exactly as before.
    """
    spec = ctx.spec
    if (
        ctx.profile != PROFILE_K8S
        or spec is None
        or ctx.scope not in _POD_OWNER_SELECTOR_JSONPATH
        or not spec.names
        or spec.labels
        # Discovery only pays off when the LLM strategy can consume it;
        # registry/scope_fallback templates read spec.labels, which stays
        # untouched by design.
        or not ctx.llm
        or not ctx.skill_case
    ):
        return None
    namespace = spec.namespace or ""
    name = spec.names[0]
    v_args = [ctx.scope, name]
    if namespace:
        v_args += ["-n", namespace]
    v_args += ["-o", f"jsonpath={_POD_OWNER_SELECTOR_JSONPATH[ctx.scope]}"]
    try:
        cmd = build_kubectl_cmd("get", v_args, kubeconfig=ctx.kubeconfig)
        result = await execute_via_transport(
            cmd, TransportTarget.from_state({}),
            timeout=settings.timeout_kubectl, task_id=ctx.task_id,
            expect_profile=PROFILE_K8S,
        )
    except Exception as e:
        logger.info(
            "Pod selector discovery for %s %s failed: %s", ctx.scope, name, e,
        )
        return None
    raw = (result.stdout or "").strip()
    if result.exit_code != 0 or not raw:
        return None
    try:
        selector = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(selector, dict):
        return None
    selector = {
        str(k): str(v)
        for k, v in selector.items()
        if v is not None and str(v) != ""
    }
    if not selector:
        return None
    selector_str = ", ".join(f"{k}={v}" for k, v in selector.items())
    logger.info(
        "Discovered authoritative pod selector for %s %s: %s",
        ctx.scope, name, selector_str,
    )
    ctx.tracker.update(
        f"Pod identity: {ctx.scope} {name} selector {selector_str} "
        "(authoritative, from workload spec)",
        {"step": "pod_selector_discovery", "kind": ctx.scope,
         "name": name, "selector": selector},
    )
    return selector


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
            "baseline_capture", "Deriving baseline-capture commands via the LLM...\n\n",
        )
        try:
            return await asyncio.wait_for(
                _llm_derive_baseline_commands(
                    ctx.llm, ctx.skill_case, ctx.scope, ctx.target, ctx.action,
                    channel=ctx.channel, profile=ctx.profile,
                    namespace=ctx.spec.namespace if ctx.spec else "",
                    names=ctx.spec.names if ctx.spec else (),
                    labels=dict(ctx.spec.labels) if ctx.spec and ctx.spec.labels else None,
                    pod_selector=ctx.pod_selector,
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
                f"Strategy {strategy_name} matched ({viable_count}/{total_count} commands usable)\n\n",
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
            f"Strategy {partial_source} matched (partially usable, {partial_viable}/{partial_total} commands)\n\n",
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


# #16 fix B (Temporal axiom): assets the approved plan CREATES during execute.
# The graph wires baseline_capture BEFORE execute_loop, yet the #16 plan's
# own baseline section declares "(after step 3 completes, ...)" — the plan
# already encodes the temporal contract; baseline_capture just never read
# it. For such assets the pre-injection absence is not an error but the
# ONLY POSSIBLE baseline at this point in the lifecycle, and the asset-level
# baseline is established by the first post-creation observation instead.
_PLANNED_CREATE_RE = re.compile(
    r"kubectl\s+create\s+(?P<kind>[a-z][a-z0-9.-]*)\s+"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)",
    re.IGNORECASE,
)
_PLANNED_RUN_RE = re.compile(
    r"kubectl\s+run\s+(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)",
    re.IGNORECASE,
)


def _extract_planned_creations(plan_content: str) -> dict[str, str]:
    """Map ``name -> canonical kind`` for assets the plan creates during execute.

    Scans the approved plan's command lines for ``kubectl create <kind>
    <name>`` and ``kubectl run <name>`` (a run creates a pod). File-based
    forms (``create -f``/``apply -f``) carry no extractable name and are
    skipped — fail-open, they simply don't get machine marking (the retry
    LLM can still judge them). Rollback/delete lines never match.
    """
    creations: dict[str, str] = {}
    if not plan_content:
        return creations
    for line in plan_content.splitlines():
        # Markdown plans wrap commands in backticks/emphasis; strip the
        # wrappers so the regex anchors on the command text itself.
        text = line.strip().strip("`*")
        m = _PLANNED_CREATE_RE.search(text)
        if m:
            kind = canonicalise_kind(m.group("kind"))
            name = m.group("name")
            if kind and name:
                creations[name] = kind
            continue
        m = _PLANNED_RUN_RE.search(text)
        if m:
            creations[m.group("name")] = "pod"
    return creations


def _mark_planned_creation_absence(
    observations: list, plan_creations: dict[str, str], tracker,
) -> int:
    """Machine-mark absence-shaped observations on planned-creation assets.

    An observation querying a planned-creation asset and finding nothing
    (non-zero NotFound OR an empty success) gets ``expected_absence`` with
    a reason quoting the plan — BEFORE the LLM retry loop, so the retry
    never burns rounds on a command that can never converge (the asset is
    created later, by design; #31's lesson generalized to the temporal
    axis). Marked observations count as valid baseline values (the
    absence IS the value) and stay out of ``empty_count``.

    Matching is by asset NAME: planned-creation names are drill-scoped
    and globally unique, and only absence-shaped observations qualify,
    so a name collision with an unrelated query is not a real risk.
    """
    marked = 0
    for obs in observations:
        if obs.get("expected_absence"):
            continue
        cmd = obs.get("command", "") or ""
        hit = next((n for n in plan_creations if n and n in cmd), None)
        if not hit:
            continue
        if _is_observation_success(obs) and not _is_empty_observation(obs):
            # The planned asset already exists with a value — temporal
            # mismatch of the other kind; leave it unmarked.
            continue
        obs["expected_absence"] = (
            f"planned creation: the approved plan creates "
            f"{plan_creations[hit]} '{hit}' during execute, so its "
            f"pre-injection absence IS the baseline value; the asset-level "
            f"baseline is established by the first post-creation "
            f"observation"
        )
        marked += 1
    if marked and tracker:
        tracker.update(
            f"{marked} observation(s) match planned-creation assets — "
            "marked expected pre-injection absence (asset-level baseline "
            "is established after execute creates them)",
            {"step": "planned_creation_absence", "marked": marked,
             "assets": sorted(plan_creations)},
        )
    return marked


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
        f"Running {len(resolved)} baseline-capture command(s)...\n\n",
    )
    observations = await _execute_observations(resolved, kubeconfig, task_id)

    # 4.0.4 #16 fix B (Temporal axiom): read the approved plan's temporal
    # contract and machine-mark absence observations on assets the plan
    # itself creates during execute. Runs BEFORE the LLM retry loop so a
    # planned-creation query never enters judgment — the retry can never
    # converge on an asset that does not exist yet by design.
    _plan_content, _ = _find_saved_plan(state.get("messages", []) or [])
    _plan_creations = _extract_planned_creations(_plan_content)
    if _plan_creations:
        _mark_planned_creation_absence(observations, _plan_creations, tracker)

    # 4.0.5 LLM self-correcting retry: when LLM-generated commands
    # exit non-zero, feed the evidence back to the LLM for a SEMANTIC
    # verdict per command (expected pre-injection absence vs. true
    # failure) and let it self-correct the true failures (up to
    # _LLM_BASELINE_MAX_RETRIES attempts). Runs BEFORE the strategy-level
    # fallback (4.0.7) so that the LLM is given a chance to fix itself
    # before we abandon the primary strategy and reach for registry /
    # scope_fallback.
    #
    # First principles (#31): a non-zero exit is channel signal, not a
    # semantic verdict. `ls /etc/hosts.bak` exit 2 and `systemctl status
    # blade-restore-hosts.timer` exit 4 were correct pre-checks whose
    # non-zero exits ARE the baseline value — the old loop fed them back
    # as "FAILED" and burned all 3 retries re-deriving byte-identical
    # commands (~35s: retry cannot converge on an already-correct
    # command). Observations the retry LLM judges "expected absence" get
    # marked and excluded from this loop, and counted as valid baseline
    # values by the strategy-fallback gate below.
    if source == "llm" and llm:
        all_pairs = list(zip(resolved, observations))
        # Commands earlier retries already produced and that still failed. Each
        # retry is a fresh LLM call with no memory of the last one, so without
        # this the prompt is byte-identical every round and the model can only
        # resample — task-fc64c982 spent two of its three retries re-emitting
        # the same debug-pod command against a node that had none.
        tried_commands: list[str] = []

        for retry_num in range(1, _LLM_BASELINE_MAX_RETRIES + 1):
            # Single source of truth for "needs retry judgment": the same
            # predicate the final filtering uses (_is_observation_success),
            # NOT the bare exit_code — and marked expected-absence
            # observations are valid baseline values that must not re-enter
            # judgment. #16 fix C extends the set with EMPTY successes
            # (exit 0, nothing observed): emptiness is either a wrong
            # identity (wrong selector / wrong name — repairable, the R10
            # replay's core finding) or an expected pre-injection state
            # only the retry LLM can confirm from the skill case (an asset
            # the approved plan creates during execute).
            failed_obs = [o for _, o in all_pairs
                          if (not _is_observation_success(o)
                              or _is_empty_observation(o))
                          and not o.get("expected_absence")]
            if not failed_obs:
                break

            logger.info(
                "LLM baseline retry %d/%d: %d command(s) non-zero-or-empty",
                retry_num, _LLM_BASELINE_MAX_RETRIES, len(failed_obs),
            )
            tracker.update(
                f"LLM retry {retry_num}/{_LLM_BASELINE_MAX_RETRIES}: "
                f"{len(failed_obs)} command(s) non-zero or empty, "
                f"judging semantics with error feedback...",
                {"step": "llm_retry", "attempt": retry_num,
                 "failed_count": len(failed_obs)},
            )
            await dispatch_node_message(
                "baseline_capture",
                f"LLM self-correction retry {retry_num}/{_LLM_BASELINE_MAX_RETRIES}: "
                f"{len(failed_obs)} command(s) non-zero or empty, judging semantics...\n\n",
            )

            try:
                retry_decisions = await asyncio.wait_for(
                    _llm_retry_failed_commands(
                        llm, skill_case, scope, target, action,
                        failed_obs,
                        channel=channel, profile=profile,
                        namespace=spec.namespace if spec else "",
                        names=spec.names if spec else (),
                        labels=dict(spec.labels) if spec and spec.labels else None,
                        pod_selector=ctx.pod_selector,
                        task_id=task_id,
                        already_tried=tuple(tried_commands),
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
                    f"LLM self-correction retry {retry_num} timed out, giving up\n\n",
                )
                break

            # Semantic verdicts first: mark observations judged as the
            # expected pre-injection absence form. Their non-zero exit IS
            # the baseline value; the marks steer the loop condition above
            # and the strategy-fallback gate below.
            expected_pairs = retry_decisions.get("expected") or []
            for _obs, _reason in expected_pairs:
                _obs["expected_absence"] = _reason or (
                    "judged expected pre-injection absence by baseline retry"
                )
            retry_commands = retry_decisions.get("replace") or []

            if not retry_commands:
                if not expected_pairs:
                    logger.info(
                        "LLM retry %d: no corrected commands returned",
                        retry_num,
                    )
                    await dispatch_node_message(
                        "baseline_capture",
                        f"LLM self-correction retry {retry_num} returned no valid command, giving up\n\n",
                    )
                    break
                # All judged expected-absence this round: nothing to re-run.
                # The marks make the next loop check converge.
                await dispatch_node_message(
                    "baseline_capture",
                    f"LLM self-correction retry {retry_num}: non-zero command(s) "
                    "judged expected pre-injection absence — kept as baseline values\n\n",
                )
                continue

            # Record what this retry produced BEFORE judging it: an unresolved
            # template never runs, so it would otherwise leave no trace and the
            # next round could emit it again. ``retry_commands`` holds the LLM's
            # own text (``{debug_pod}`` still in place) — that is the form worth
            # showing back, not the resolved one.
            for _c in retry_commands:
                _text = (getattr(_c, "command", "") or "").strip()
                if _text and _text not in tried_commands:
                    tried_commands.append(_text)

            retry_resolved = _resolve_templates(retry_commands, state, profile)
            retry_viable = [
                c for c in retry_resolved if not c.get("_unresolved")
            ]
            if not retry_viable:
                if not expected_pairs:
                    break
                # Only absence verdicts survived this round; marks already
                # applied — continue so the loop check converges.
                continue

            retry_obs = await _execute_observations(
                retry_resolved, kubeconfig, task_id,
            )

            # Keep original VALID successes, keep judged-expected-absence
            # pairs, replace true failures AND empty successes with retry
            # results.
            # Use _is_observation_success so kubectl partial failures
            # (exit_code=0 + 'Error from server' in stdout) are treated
            # as failures and properly retried; exclude empty successes
            # (#16 fix C) — they entered this retry round precisely so a
            # replacement could anchor on the right identity, so keeping
            # them here would double-count the dimension alongside the
            # retry result.
            success_pairs = [
                (r, o) for r, o in all_pairs
                if _is_observation_success(o)
                and not _is_empty_observation(o)
                and not o.get("expected_absence")
            ]
            # Judged-expected-absence observations keep their ORIGINAL pair
            # — the observation IS the baseline value the verifier
            # compares against (e.g. "hosts.bak absent pre-injection" via a
            # non-zero exit, or a planned-creation asset's empty query via
            # exit 0), and the original resolved dict stays with it
            # verbatim: downstream consumers read per-target bookkeeping
            # fields off it (``_target_name``/``_target_sampled`` for
            # coverage, ``_extractors`` for metadata merging — non-zero
            # observations are skipped there, but coverage still needs the
            # name). Rebuilding a minimal dict here would silently drop
            # those fields and skew ``_target_coverage`` for multi-target
            # drills. The MARK (not the exit code) is the retention signal:
            # #16 fix C lets empty successes carry it too.
            absence_pairs = [
                (r, o) for r, o in all_pairs
                if o.get("expected_absence")
            ]
            all_pairs = success_pairs + absence_pairs + list(
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
    #
    # 判定 "全部跑挂" 时 expected_absence 观测计为有效采集值：
    # 纯预检型 baseline（全部观测都是 "注入前不存在" 形态）不触发
    # 策略回落，否则会白白重跑 registry/scope_fallback 链。
    if (
        observations
        and not any(
            _is_observation_success(o) or o.get("expected_absence")
            for o in observations
        )
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
                f"Strategy {source} failed entirely, falling back to {_fb_name}...\n\n",
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
            # Phase 1.5 (#16 fix A — Identity axiom): discover the target's
            # authoritative pod selector (workload-scope routes) so the
            # derive/retry prompts never have to guess one.
            ctx = replace(
                ctx, pod_selector=await _discover_pod_selector(ctx),
            )
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
