"""Agent factory: creates compiled graphs with checkpointer and tools."""

import logging

from chaos_agent.agent.graph import build_recover_graph
from chaos_agent.config.settings import settings
from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.utils.reasoning_replay import (
    reasoning_model_key,
    replayed_reasoning,
)
from chaos_agent.tools import (
    safe_read_file,
    safe_write_file,
    read_knowledge_resource,
)

# Backend execution tools (blade_* / kubectl / kubectl_read) are no longer
# hardcoded here — each is contributed by its FaultProvider via
# ``_append_provider_tools`` (see chaosblade.py / k8s_native.py / host_shell.py
# ``tools(phase)``). Environment discovery belongs to the provider-backed
# planning phase, not to semantic intent clarification.

logger = logging.getLogger(__name__)


def _append_provider_tools(base: list, phase: str) -> list:
    """Union the built-in FaultProviders' phase tools onto a static base list.

    Tools bind at graph-*build* time — the factory unions every provider's
    ``tools(phase)`` onto the phase's static base (see
    :meth:`FaultProvider.tools`). This is the seam that lets a new execution
    backend surface its LLM tools by *registering a provider* instead of
    editing these five hardcoded lists.

    Dedup by tool name so a provider that re-declares a statically-listed tool
    (e.g. ``kubectl``) never double-binds it. Self-bootstraps the built-ins on
    an empty registry, mirroring ``FaultProviderRegistry.detect_method``; an
    already-populated registry (test fixtures) is respected as-is.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    if not FaultProviderRegistry.all_providers():
        FaultProviderRegistry.register_builtins()
    seen = {getattr(t, "name", None) for t in base}
    out = list(base)
    for provider in FaultProviderRegistry.all_providers():
        for tool in provider.tools(phase):
            name = getattr(tool, "name", None)
            if name not in seen:
                seen.add(name)
                out.append(tool)
    return out


# ---------------------------------------------------------------------------
# Patch langchain-openai to preserve reasoning_content from thinking models
# (e.g., Qwen enable_thinking). ChatOpenAI explicitly does NOT extract
# reasoning_content — this patch adds it to additional_kwargs so that
# downstream code can access it via message.additional_kwargs["reasoning_content"].
#
# It must also travel back OUT. A thinking model writes "why I'm doing this /
# what I already did" into reasoning_content and often leaves ``content``
# empty; ``_convert_message_to_dict`` only emits content/role/tool_calls/name,
# so history degenerates into bare tool calls with no rationale and the model
# re-derives its intent from the original input every turn. Re-derivation
# always yields step 1 — harmless for single-step work (step 1 *is* the whole
# job), non-convergent for multi-step work.
# ---------------------------------------------------------------------------
_REASONING_PATCH_APPLIED = False

# Labels of patches that could not be applied. Non-empty means reasoning replay
# is degraded — surfaced via the WARNING logs emitted at patch time, and asserted
# by tests/test_agent/test_reasoning_patch_resilience.py.
_REASONING_PATCH_FAILURES: list[str] = []

# Model provenance and the replay decision itself live in
# ``utils.reasoning_replay`` because the TOKEN COUNTER has to reach the same
# verdict: every context decision (auto-compact, strip-vs-compress, keep/drop)
# is computed from ``count_tokens_messages``, and text that goes on the wire
# unaccounted made a 10-turn history measure 83 tokens against a real 7,547.
# Re-implementing the conditions here would let the two sides drift silently.
# The private alias below is a forwarder kept for existing call sites here and
# in the reasoning-replay tests; new code should import from the utils module.
_reasoning_model_key = reasoning_model_key


def _patch_langchain_for_reasoning_content() -> None:
    """Monkey-patch langchain-openai message conversion to preserve reasoning_content.

    Idempotent: safe to call multiple times — only patches once.

    Every patch is applied under its own guard. The targets are PRIVATE
    langchain functions and ``pyproject.toml`` pins only ``langchain-openai>=1.0``
    (no upper bound), so a routine ``uv sync`` can rename or reshape them. This
    used to raise at import time, which takes the whole Agent down — a
    reasoning-replay optimisation must never do that. On failure the Agent keeps
    running with replay disabled (degraded: multi-step drills fall back to the
    slow path) and logs a WARNING naming the patch that failed.

    Wrappers forward ``*args/**kwargs`` rather than restating the upstream
    signature, so adding or removing a parameter (e.g. Patch 3's ``api``, which
    only exists in newer versions) does not break the call either.
    """
    global _REASONING_PATCH_APPLIED
    if _REASONING_PATCH_APPLIED:
        return
    _REASONING_PATCH_APPLIED = True

    from langchain_openai.chat_models import base as _lc_base
    from langchain_core.messages import AIMessage, AIMessageChunk

    def _apply(label: str, attr: str, make_wrapper) -> bool:
        """Patch one target, degrading to a WARNING if it cannot be applied."""
        try:
            original = getattr(_lc_base, attr)
            setattr(_lc_base, attr, make_wrapper(original))
            return True
        except Exception as e:
            _REASONING_PATCH_FAILURES.append(label)
            logger.warning(
                "reasoning_content %s could not be applied (%s: %s). "
                "The Agent continues to run, but thinking-channel intent is "
                "%s. Check the installed langchain-openai version.",
                label, type(e).__name__, e,
                "not replayed to the model — multi-step tasks may re-derive "
                "their plan every turn" if attr == "_convert_message_to_dict"
                else "not captured for logs/TUI",
            )
            return False

    # --- Patch 1: non-streaming _convert_dict_to_message (inbound) ---
    def _make_convert_dict(orig):
        def _patched(*args, **kwargs):
            msg = orig(*args, **kwargs)
            _dict = args[0] if args else kwargs.get("_dict") or {}
            # For assistant messages, extract reasoning_content into additional_kwargs
            if isinstance(msg, AIMessage) and _dict.get("reasoning_content"):
                msg.additional_kwargs["reasoning_content"] = _dict["reasoning_content"]
                msg.additional_kwargs[_reasoning_model_key(settings.model_name)] = True
            return msg
        return _patched

    _apply("inbound patch (non-streaming)", "_convert_dict_to_message", _make_convert_dict)

    # --- Patch 2: streaming _convert_delta_to_message_chunk (inbound) ---
    def _make_convert_delta(orig):
        def _patched(*args, **kwargs):
            msg = orig(*args, **kwargs)
            _dict = args[0] if args else kwargs.get("_dict") or {}
            # For assistant chunks, extract reasoning_content into additional_kwargs
            if isinstance(msg, AIMessageChunk) and _dict.get("reasoning_content"):
                msg.additional_kwargs["reasoning_content"] = _dict["reasoning_content"]
                msg.additional_kwargs[_reasoning_model_key(settings.model_name)] = True
            return msg
        return _patched

    _apply("inbound patch (streaming)", "_convert_delta_to_message_chunk", _make_convert_delta)

    # --- Patch 3: outbound _convert_message_to_dict — replay reasoning_content ---
    def _make_message_to_dict(orig):
        def _patched(message, *args, **kwargs):
            d = orig(message, *args, **kwargs)
            api = kwargs.get("api", args[0] if args else "chat/completions")
            # Only the Chat Completions shape is verified to accept the field.
            # The Responses API uses a different reasoning representation
            # entirely. This is the one condition NOT delegated below, because
            # it is a property of the call, not of the message.
            if api != "chat/completions":
                return d
            # Everything else — assistant-only, non-blank trace, model
            # provenance, tail truncation — comes from the shared decision so
            # ``count_tokens_messages`` accounts for exactly this text.
            reasoning = replayed_reasoning(message)
            if reasoning:
                d["reasoning_content"] = reasoning
            return d
        return _patched

    _apply("outbound replay patch", "_convert_message_to_dict", _make_message_to_dict)

    if _REASONING_PATCH_FAILURES:
        logger.warning(
            "reasoning_content patching incomplete: %d of 3 failed (%s)",
            len(_REASONING_PATCH_FAILURES), ", ".join(_REASONING_PATCH_FAILURES),
        )
    else:
        logger.debug("Patched langchain-openai to preserve and replay reasoning_content")


def reasoning_patch_failures() -> tuple[str, ...]:
    """Patches that could not be applied.

    Empty means thinking-channel intent continuity is fully active. Failures are
    already reported as WARNING logs at patch time; this accessor exists so the
    degraded state stays inspectable and testable rather than log-only.
    """
    return tuple(_REASONING_PATCH_FAILURES)


# Apply the patch at import time so all ChatOpenAI instances benefit
if settings.llm_enable_thinking:
    _patch_langchain_for_reasoning_content()


def make_llm(
    *,
    temperature: float | None = None,
    max_retries: int | None = None,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
    callbacks: list | None = None,
    enable_thinking: bool | None = None,
):
    """Create a ChatOpenAI instance with project-standard configuration.

    All parameters default to ``settings`` values.  Explicit overrides
    (e.g. ``temperature=0.3`` for skill catalog generation) replace
    the defaults.

    ``enable_thinking`` / ``extra_body`` is handled automatically based
    on ``settings.llm_enable_thinking``, eliminating the need to repeat
    the conditional injection at every construction site.

    Parameters
    ----------
    temperature : float | None
        LLM sampling temperature.  Defaults to ``settings.llm_temperature``.
    max_retries : int | None
        Maximum retry count for API calls.  Defaults to ``settings.llm_max_retries``.
    connect_timeout : float | None
        TCP/TLS connection-establishment timeout (s). Defaults to
        ``settings.llm_connect_timeout``. Keep short so connectivity
        failures surface fast.
    read_timeout : float | None
        Response read timeout (s) — time-to-first-token + between-chunk
        gaps when streaming, or whole-body wait when non-streaming.
        Defaults to ``settings.llm_read_timeout``. Keep generous so
        slow thinking models aren't cut off mid-inference.
    callbacks : list | None
        LangChain callbacks list (e.g. tracing).  Defaults to ``None``.
    """
    import httpx

    from chaos_agent.agent.resilient_llm import ResilientChatOpenAI

    _connect = connect_timeout if connect_timeout is not None else settings.llm_connect_timeout
    _read = read_timeout if read_timeout is not None else settings.llm_read_timeout

    llm_kwargs = dict(
        model=settings.model_name,
        api_key=settings.llm_api_key,
        base_url=settings.api_base_url,
        temperature=temperature if temperature is not None else settings.llm_temperature,
        max_retries=max_retries if max_retries is not None else settings.llm_max_retries,
        # streaming=True 让 astream_events 产生 on_chat_model_stream 事件，
        # 支持前端逐 token 流式展示 LLM 思考过程。
        streaming=True,
        # Split connect vs read: connection failures fail fast (_connect),
        # slow inference gets a generous response budget (_read). A scalar
        # timeout would apply one value to both, forcing a bad tradeoff.
        timeout=httpx.Timeout(timeout=float(_read), connect=float(_connect)),
        # Forward ``stream_options.include_usage=true`` to the OpenAI-
        # compatible API so the final stream chunk carries token usage.
        # Without this, LangChain assembles the streamed AIMessage with
        # ``usage_metadata=None`` — which makes
        # ``parse_stream_event(on_chat_model_end)`` skip the ``usage``
        # SSE event entirely (``_extract_token_usage`` returns 0/0),
        # so the TUI never accumulates per-turn tokens and neither
        # the LoadingIndicator's ``↓ N tokens`` tail nor the
        # ``⚡ turn used N tokens`` summary line ever populate. Both
        # OpenAI proper and DashScope (Qwen) honour ``include_usage``.
        stream_usage=True,
    )
    if callbacks:
        llm_kwargs["callbacks"] = callbacks
    _thinking = enable_thinking if enable_thinking is not None else settings.llm_enable_thinking
    if _thinking:
        llm_kwargs["extra_body"] = {"enable_thinking": True}
    # ResilientChatOpenAI adds application-level retry for transient transport
    # failures (mid-stream ReadError / RemoteProtocolError from sleep, network
    # handoff, gateway reset) that the OpenAI SDK's own max_retries can't cover
    # once streaming has begun. See resilient_llm.py.
    return ResilientChatOpenAI(**llm_kwargs)


def _build_skill_tools(registry: SkillRegistry):
    """Build skill-related tools with dynamic catalog from registry."""
    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def activate_skill(skill_name: str) -> str:
        """Phase 1 ONLY. Activate a chaos-engineering skill and load its full instructions.

        When to use:
          - Phase 1 planning, ONCE per task before reading skill resources.
          - Re-activate when switching fault types within the same task.
          - Do NOT skip — every fault injection MUST be backed by an activated skill.

        Inputs:
          - skill_name: an available skill name. If unsure which names
            exist, call with any name — the error response lists all
            currently available skills.

        Output: the activated skill's full markdown content (SKILL.md body
                including safety rules, decision flow, use-case catalogue).
                Errors start with "Error:".

        Side effects: marks the skill as the current active context for this task.

        Constraints:
          - Unknown names return an error listing the available choices.
            Do NOT guess — use a name from that list or from the system
            prompt's Skill Index (when present).
        """
        try:
            return registry.activate(skill_name)
        except KeyError:
            return f"Error: Skill '{skill_name}' not found. Available skills: {registry.list_skills()}"
        except Exception as e:
            return f"Error activating skill '{skill_name}': {e}"

    @lc_tool
    def read_skill_resource(skill_name: str, resource_path: str) -> str:
        """Phase 1 / Phase 2 read-only. Read a resource file from a skill.

        **PREREQUISITE**: You MUST call `activate_skill` first. This tool
        only works on an already-activated skill. If you haven't activated
        a skill yet, call `activate_skill` now — do NOT call this tool
        before activation.

        Templates inside (blade/kubectl command snippets) are EXECUTION
        templates Phase 2 runs automatically — do NOT execute them yourself
        in Phase 1. Use them to understand WHAT will happen and decide IF
        the plan is safe.

        When to use:
          - You need a reference (commands.md, examples.yaml, ...) bundled
            with the active skill but not embedded in its top-level case.
          - Verifier follow-up reading after activate_skill.
          - Do NOT use to access arbitrary filesystem paths — use read_file
            for that.

        Inputs:
          - skill_name: name of an activated skill.
          - resource_path: path relative to the skill directory
            (e.g. "references/commands.md", "scripts/verify.sh").

        Output: file content, or a directory listing if the path is a dir.
                Errors start with "Error:" and include available resources.

        Side effects: None (read-only).

        Constraints:
          - Skill must be activated first (activate_skill); otherwise returns
            "Skill not found" error.
        """
        try:
            result = registry.read_resource(skill_name, resource_path)
            if not result or not result.strip():
                return f"Resource '{resource_path}' in skill '{skill_name}' is empty or contains no content."
            # Phase-aware wrapper. Every skill use-case markdown contains
            # `blade create k8s pod-cpu fullload ...` style EXECUTION
            # TEMPLATES that Phase 2 runs automatically. Without this
            # header, an LLM in Phase 1 reading a template tends to
            # mimic it via whatever tools it has (kubectl exec ... blade
            # create) — caught in task-ce9647931ce1 where the LLM read
            # Pod_CPU_应用资源争抢.md, saw the blade-create template, and
            # immediately ran the equivalent via kubectl exec. The header
            # sets the right frame ("this is a recipe you're reading,
            # not following") so the LLM treats the commands as plan
            # input rather than imperatives.
            wrapped = (
                "[Skill resource — REFERENCE for planning]\n"
                "The injection / verification commands shown below are\n"
                "EXECUTION TEMPLATES that Phase 2 will run automatically\n"
                "once your plan is approved. In Phase 1 (current), use them\n"
                "to understand WHAT will happen and decide IF the plan is\n"
                "safe. DO NOT execute them yourself in this phase.\n"
                "─────────────────────────────────────────────────────\n\n"
                f"{result}"
            )
            return wrapped
        except FileNotFoundError:
            available = registry.list_resources(skill_name)
            available_str = "\n".join(f"  - {r}" for r in available) if available else "  (none found)"
            return f"Error: Resource '{resource_path}' not found in skill '{skill_name}'.\nAvailable resources:\n{available_str}"
        except KeyError:
            return f"Error: Skill '{skill_name}' not found. Activate it first with activate_skill."
        except Exception as e:
            return f"Error reading resource '{resource_path}' from skill '{skill_name}': {e}"

    @lc_tool
    def read_file(file_path: str) -> str:
        """Phase 1 / Phase 2 read-only. Read a file from the local filesystem.

        When to use:
          - The user referenced a file path in their request.
          - You need to inspect a config, log, or report file outside of
            skill resources.
          - Do NOT use for skill resources — use read_skill_resource.

        Inputs:
          - file_path: absolute or working-dir-relative path. Directories
            return a listing instead of content.

        Output: file content / directory listing, or "Error:" prefix.

        Side effects: None (read-only).

        Constraints:
          - Sensitive paths (SSH keys, private keys, system credentials)
            are blocked by safe_read_file.
        """
        try:
            return safe_read_file(file_path)
        except FileNotFoundError as e:
            return f"Error: {e}"
        except PermissionError as e:
            return f"Error: Access denied - {e}"
        except Exception as e:
            return f"Error reading file '{file_path}': {e}"

    @lc_tool
    def write_file(file_path: str, content: str) -> str:
        """Write content to a file on the local filesystem.

        When to use:
          - Generate experiment reports or scratch artifacts requested by
            the user.
          - Do NOT use to save fault plans — use save_fault_plan, which
            stores them in the canonical plan directory.

        Inputs:
          - file_path: target path (parent dirs are created as needed).
          - content: full text to write (overwrites existing files).

        Output: confirmation string, or "Error:" prefix.

        Side effects: writes to disk; overwrites existing content.

        Constraints:
          - System directories and sensitive paths are blocked by
            safe_write_file.
        """
        try:
            return safe_write_file(file_path, content)
        except PermissionError as e:
            return f"Error: Access denied - {e}"
        except IsADirectoryError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file '{file_path}': {e}"

    @lc_tool
    def save_fault_plan(
        plan_content: str,
        task_id: str,
        skill_case_resource: str = "",
    ) -> str:
        """Phase 1 ONLY. Save a fault injection plan as `<task_id>.md` in the plan directory.

        Writes to the local plan dir (does NOT touch the cluster). After
        calling this, your next message should be your final summary text
        WITHOUT tool_calls — the system advances to Phase 2.

        When to use:
          - End of Phase 1, after the plan is finalized and before the
            confirmation gate.
          - Do NOT call mid-planning — saving a partial plan replaces any
            previous version.

        Inputs:
          - plan_content: full plan in Markdown (target / parameters /
            verification methods / recovery / blast radius).
          - task_id: task identifier used as the filename.
          - skill_case_resource: The resource_path of the chosen skill case file
            (e.g. "references/catalogue/Pod_镜像拉取失败/Pod_镜像拉取失败_镜像不存在或标签错误.md").
            Required when multiple skill cases were read during planning.

        Output: confirmation including the saved path, or "Error:" prefix.

        Side effects: writes (or overwrites) `<plan_dir>/<task_id>.md`.
        """
        try:
            from chaos_agent.config.settings import settings as _s
            plan_dir = _s.resolved_memory_dir / "plan"
            plan_dir.mkdir(parents=True, exist_ok=True)
            plan_file = plan_dir / f"{task_id}.md"
            plan_file.write_text(plan_content, encoding="utf-8")
            return f"Plan saved to {plan_file}\n\n{plan_content}"
        except Exception as e:
            return f"Error saving plan: {e}"

    @lc_tool
    def finish_planning(
        summary: str,
        rejected: bool = False,
        rejection_reason: str = "",
        alternatives: str = "",
        blast_radius_scope: str = "",
        blast_radius_detail: str = "",
        skill_case_resource: str = "",
    ) -> str:
        """Signal that Phase 1 is complete — either proceed to execution or reject the request.

        This is the REQUIRED way to end Phase 1. Two modes:
        - Normal (default): proceed to safety check → user confirmation → execution.
        - Rejection: rejected=True. The request cannot be fulfilled. The system will
          end the run cleanly without any cluster changes.

        Inputs:
          - summary: Brief summary of the plan (normal) or the rejection decision.
          - rejected: Set to True to reject the request. Default False.
          - rejection_reason: Why the request is rejected (only when rejected=True).
          - alternatives: When rejected=True, provide 2-4 feasible fault scenarios
            that CAN be executed against the same target. Each must be a concrete,
            actionable scenario based on the catalogue, ChaosBlade, or kubectl-native
            injection methods. Format as a numbered list.
          - blast_radius_scope: Scope of the execution's actual impact on the cluster.
            Must be one of: "target-only" (only the target resource is mutated),
            "namespace-wide" (mutations affect other resources in the target namespace),
            "cluster-wide" (mutations affect resources outside the target namespace,
            e.g. tainting all nodes, modifying cluster-level resources).
            The system uses this to assess safety — cluster-wide scope triggers
            elevated safety review.
          - blast_radius_detail: One-line description of what cluster resources
            the execution will mutate beyond the target (e.g. "Will taint 30 nodes
            cluster-wide to block scheduling").
          - skill_case_resource: The resource_path of the chosen skill case file
            (e.g. "references/catalogue/Pod_镜像拉取失败/Pod_镜像拉取失败_镜像不存在或标签错误.md").
            Required when multiple skill cases were read during planning —
            tells the system which one to use for verification.

        Output: Confirmation message.

        Side effects: None (control signal only — the system handles the transition).
        """
        if rejected:
            parts = [f"Planning rejected. Reason: {rejection_reason or summary}"]
            if alternatives:
                parts.append(f"\nAlternatives:\n{alternatives}")
            return "".join(parts)
        return f"Planning finalized. Summary: {summary}"

    @lc_tool
    def propose_plan_change(
        reason: str,
        proposed_fault: dict,
        fault_revision: int | None = None,
    ) -> str:
        """Propose a change to the semantic fault identity for user confirmation.

        A plan change is a change to WHAT is attacked. The system diffs the
        proposed FaultSpec against the reviewed one and asks the user to approve
        or reject before the contract is replaced.

        When to use:
          - The TARGET identity must change (e.g. target A does not exist or
            cannot be injected, and a different resource C must be used).
          - The FAULT TYPE must change (scope / target / action — e.g. switching
            from a network fault to a similar but different fault).

        Inputs:
          - reason: why the target or fault type cannot be preserved and why the
            alternative should work (1-2 sentences).
          - proposed_fault: the COMPLETE FaultSpec object — ALL 13 fields are
            mandatory: scope, target, action, namespace, names, labels, params,
            params_flags, duration_seconds, objective, boundaries, constraints,
            assumptions. The proposal REPLACES the reviewed contract wholesale;
            a partial submission is refused with the list of missing fields
            (resource name lists go in "names", not "name").
          - fault_revision: copy the revision of the Reviewed FaultSpec that
            this proposal replaces.

        Output: confirmation of proposal submission, or "Error:" prefix.

        Side effects: None (proposal only — the contract changes only after the
        user approves).

        Constraints:
          - Do NOT use this to switch the injection METHOD / CHANNEL / TOOL while
            keeping the same target and fault type (e.g. ChaosBlade DaemonSet →
            kubectl-native). That is HOW to attack, not what — just re-plan and
            call finish_planning.
          - Do NOT use it for method-specific parameter differences. FaultSpec
            captures the semantic intent, not one tool's flags.
        """
        # Task-5193538b (question 3): validate the full contract HERE, at the
        # tool surface. The old check only required scope/target/action and
        # returned "Plan change proposed." for anything else — the router then
        # dropped the partial proposal silently (no card, no error), so the
        # model believed the change was submitted. Failing loudly with the
        # exact missing fields gives the model one actionable retry.
        from chaos_agent.agent.spec.fault_spec import missing_full_proposal_fields

        missing = missing_full_proposal_fields(proposed_fault)
        if missing:
            hint = ""
            if (
                isinstance(proposed_fault, dict)
                and "name" in proposed_fault
                and "names" not in proposed_fault
            ):
                hint = " Note: the resource name list goes in 'names', not 'name'."
            return (
                "Error: proposed_fault is a partial contract; a plan change "
                "replaces the reviewed FaultSpec wholesale and must carry "
                "every field. Missing or empty: "
                f"{', '.join(missing)}.{hint}"
            )
        return f"Plan change proposed. Reason: {reason}"

    def _fuzzy_match_script(requested: str, available: list[str]) -> str | None:
        """Suggest a similar script name using simple prefix/suffix matching."""
        requested_lower = requested.lower()
        # Strip common prefixes/suffixes for comparison
        for avail in available:
            avail_lower = avail.lower()
            if avail_lower == requested_lower:
                return avail
            # Prefix match: "list_scenarios" matches "list_scenarios.py"
            if avail_lower.startswith(requested_lower.rsplit(".", 1)[0]) or \
               requested_lower.startswith(avail_lower.rsplit(".", 1)[0]):
                return avail
            # Suffix match: "get_pods" loosely matches "list_pods"
            req_stem = requested_lower.replace("_", "").replace(".py", "").replace(".sh", "")
            avl_stem = avail_lower.replace("_", "").replace(".py", "").replace(".sh", "")
            if req_stem and avl_stem and (req_stem in avl_stem or avl_stem in req_stem):
                return avail
        return None

    @lc_tool
    async def execute_skill_script(
        skill_name: str,
        script_name: str,
        params: str = "",
        timeout: int = 0,
    ) -> str:
        """Phase 2 / verifier ONLY. Execute a script from a skill's scripts/ directory.

        Side effects depend on the script. NOT available in Phase 1
        planning (scripts may perform mutating operations).

        When to use:
          - Phase 2 / verifier needs a side-effect-free probe that the skill
            author bundled (list_scenarios, check_health, etc.).
          - Do NOT invent script names — if unsure which scripts exist,
            call with any name and the error response lists all available
            scripts for that skill.

        Inputs:
          - skill_name: owning skill of the script.
          - script_name: filename exactly as listed (e.g. "list_scenarios.py").
          - params: CLI arg string (e.g. "--namespace default").
          - timeout: seconds; 0 = use default 60s.

        Output: stdout from the script, or "Error:" with available names +
                a fuzzy-match suggestion when the script is unknown.

        Side effects: runs the script under the skill's working dir.

        Constraints:
          - Only .py and .sh are supported.
          - Unknown script names return an error listing available scripts;
            do not retry until you choose one from that list.
        """
        # Pre-validate: check if script exists before attempting execution
        try:
            available_scripts = registry.list_scripts(skill_name)
        except KeyError:
            return f"Error: Skill '{skill_name}' not found. Available skills: {registry.list_skills()}"

        available_names = [s["name"] for s in available_scripts if isinstance(s, dict) and "name" in s]
        if script_name not in available_names:
            available_str = "\n".join(f"  - {n}" for n in available_names) if available_names else "  (none)"
            suggestion = _fuzzy_match_script(script_name, available_names)
            msg = (
                f"Error: Script '{script_name}' does not exist in skill '{skill_name}'.\n"
                f"Available scripts:\n{available_str}\n"
                f"Only use scripts listed above. Do NOT invent script names."
            )
            if suggestion:
                msg += f"\nDid you mean '{suggestion}'?"
            return msg

        try:
            return await registry.execute_script(
                skill_name, script_name, params, timeout or None
            )
        except Exception as e:
            return f"Error executing script '{script_name}' from skill '{skill_name}': {e}"

    return [activate_skill, read_skill_resource, read_file, write_file, save_fault_plan, finish_planning, propose_plan_change, execute_skill_script]


async def create_agent(
    registry: SkillRegistry,
    checkpointer=None,
    *,
    mcp_manager=None,
) -> dict:
    """Create compiled graph instances for inject and recover.

    Args:
        registry: SkillRegistry with skills loaded
        checkpointer: LangGraph checkpointer for state persistence.
                      If None, uses AsyncSqliteSaver.
        mcp_manager: Optional ``McpManager`` (E9). When provided, per-phase
                     external MCP tools are appended to the corresponding
                     built-in tool list. Filtering by attach_to is
                     enforced inside ``McpManager.tools_for_phase``.
                     ``None`` (default) → built-in tools only, zero
                     behavioural change from pre-E9 builds.

    Returns:
        Dict with compiled graph instances: {"inject": ..., "recover": ...}
    """
    # Build tool lists
    skill_tools = _build_skill_tools(registry)
    _skill_tools_by_name = {t.name: t for t in skill_tools}
    _activate_skill = _skill_tools_by_name["activate_skill"]
    _read_skill_resource = _skill_tools_by_name["read_skill_resource"]
    _read_file = _skill_tools_by_name["read_file"]
    _save_fault_plan = _skill_tools_by_name["save_fault_plan"]
    _finish_planning = _skill_tools_by_name["finish_planning"]
    _propose_plan_change = _skill_tools_by_name["propose_plan_change"]
    _execute_skill_script = _skill_tools_by_name["execute_skill_script"]

    # Clarification tools: only available in intent_clarification node (TUI mode).
    # All tools are real @tool functions processed by ToolNode. The LLM's
    # action IS the intent — no separate classification tool needed:
    #   - Pure text response = chat/Q&A (graph ends, TUI waits for next input)
    #   - submit_fault_intent = inject flow
    #   - submit_batch_intent = batch inject flow
    #   - recover_task = recover flow
    #   - activate_skill / read_skill_resource = semantic catalog lookup
    #   - provider PLAN tools = read-only discovery of the current environment
    #
    # Intent always receives the complete skill catalog. Its discovery tool
    # binding is selected from the active transport only, so it can inspect
    # targets without making the supported fault vocabulary transport-dependent.
    from chaos_agent.agent.nodes.planning.intent_clarification import submit_fault_intent, submit_batch_intent, query_active_experiments, recover_task

    clarification_tools = [
        _activate_skill,
        _read_skill_resource,
        submit_fault_intent,
        submit_batch_intent,
        query_active_experiments,
        recover_task,
    ]
    from chaos_agent.agent.providers import EXECUTE, PLAN, RECOVER_VERIFY, VERIFY
    clarification_tools = _append_provider_tools(clarification_tools, PLAN)
    if mcp_manager is not None:
        clarification_tools = clarification_tools + mcp_manager.tools_for_phase("clarification")

    # P1-1: Phase 1 (planning / agent_loop) — tightened tool surface.
    #
    # The backend planning tools (ChaosBlade ``blade_help`` / ``blade_status``,
    # kubectl-native ``kubectl_read``) are contributed by the PLAN-phase provider
    # union below, NOT hardcoded here — see ChaosbladeProvider.tools /
    # K8sNativeProvider.tools for the per-tool safety rationale. The critical
    # Phase-1 invariants those docstrings enforce:
    #   - ``blade_create`` ABSENT from planning — ChaosBlade has no dry-run mode,
    #     so binding it here handed the planner a path past the confirmation gate
    #     (caught in sess_dd91ed7271b2: four ``blade_create`` attempts during
    #     ``agent_loop`` before ``confirmation_gate`` fired).
    #   - ``blade_destroy`` ABSENT — it mutates cluster state; partial-create
    #     cleanup belongs to Phase 2 ReAct (UID-provenance guarded).
    #   - ``kubectl_read`` (NOT full ``kubectl``) — full kubectl was the bypass
    #     vector in task-ce9647931ce1 (``kubectl exec <pod> -- blade create``);
    #     ``kubectl_read``'s ``Literal`` subcommand constraint + read-only exec
    #     gating block it.
    #
    # This static base excludes ``write_file`` / ``search_files`` /
    # ``execute_skill_script`` for the "planning is read-only + save_fault_plan"
    # reason.
    from chaos_agent.tools.progress import update_progress  # progress ledger (all ReAct phases)
    phase1_tools = [
        _activate_skill,
        _read_skill_resource,
        _read_file,
        _save_fault_plan,
        _finish_planning,
        _propose_plan_change,
        read_knowledge_resource,
        update_progress,
    ]
    if mcp_manager is not None:
        phase1_tools = phase1_tools + mcp_manager.tools_for_phase("phase1")
    # Provider tool union (plan phase). Contributes the read-only backend
    # planning tools: ChaosBlade's ``blade_help`` / ``blade_status`` and
    # kubectl-native's ``kubectl_read`` (NOT full kubectl — see K8sNativeProvider.
    # tools docstring). ``blade_create`` / ``blade_destroy`` / full ``kubectl``
    # are execute-only and never surface here.
    phase1_tools = _append_provider_tools(phase1_tools, PLAN)

    # P1-1: Phase 2 (execution / execute_loop) — tightened tool surface.
    # Excludes read_skill_resource. ``blade_destroy`` is available for ReAct
    # cleanup of a partial/failed create, but the screener only permits UIDs
    # observed in this task's own blade_create ToolMessages.
    #   - read_skill_resource: NOT bound here. Phase 2 executes the APPROVED PLAN
    #     produced in Phase 1 (injected into the execute prompt via ``plan`` /
    #     ``plan_path``); planning already distilled the skill use-case into that
    #     plan, so re-reading the raw case during execution is unnecessary.
    from chaos_agent.tools.wait import time_wait
    from chaos_agent.agent.replan import request_replan
    phase2_tools = [
        _execute_skill_script,
        read_knowledge_resource,
        time_wait,
        request_replan,
        update_progress,
    ]
    if mcp_manager is not None:
        phase2_tools = phase2_tools + mcp_manager.tools_for_phase("phase2")
    # Provider tool union (execute phase). Contributes the injection carriers:
    # ChaosBlade's ``blade_create`` / ``blade_destroy`` / ``blade_help`` /
    # ``blade_status`` / ``blade_query_k8s``, kubectl-native's full ``kubectl``,
    # and host-shell's ``host_inject`` (superset of ``host_read``).
    phase2_tools = _append_provider_tools(phase2_tools, EXECUTE)

    # submit_verification (Scheme B): control-signal tool the verifier LLM
    # calls to submit a structured verdict and end verification.
    # route_after_verifier_tools routes its execution to finalize_verification.
    from chaos_agent.agent.nodes.verify._verifier_submit import submit_verification
    verifier_tools = [
        _read_skill_resource,
        _execute_skill_script,
        read_knowledge_resource,
        submit_verification,
        time_wait,
        update_progress,
    ]
    if mcp_manager is not None:
        verifier_tools = verifier_tools + mcp_manager.tools_for_phase("verifier")
    # Provider tool union (verify phase). Contributes kubectl-native's read-only
    # ``kubectl_read`` and host-shell's read-only ``host_read``.
    verifier_tools = _append_provider_tools(verifier_tools, VERIFY)

    from chaos_agent.agent.nodes.verify._verifier_submit import submit_recover_verification
    recover_verifier_tools = [
        _read_skill_resource,
        _execute_skill_script,
        read_knowledge_resource,
        submit_recover_verification,
        time_wait,
        update_progress,
    ]
    if mcp_manager is not None:
        # Recover verifier shares the same MCP attach_to as the inject
        # verifier phase — both are read-only verification work.
        recover_verifier_tools = recover_verifier_tools + mcp_manager.tools_for_phase("verifier")
    # Provider tool union (recover-verify phase). Contributes kubectl-native's
    # full ``kubectl`` (run the reverse op; superset of ``kubectl_read``) and
    # host-shell's ``host_inject`` (superset of ``host_read``).
    recover_verifier_tools = _append_provider_tools(recover_verifier_tools, RECOVER_VERIFY)

    # Set up checkpointer
    conn = None  # connection/pool ref for cleanup
    if checkpointer is None:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        serde = JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("chaos_agent.agent.result.verdict", "Layer1Status"),
                ("chaos_agent.agent.result.verdict", "FailureCategory"),
            ],
        )

        backend = settings.checkpoint_backend
        if backend == "postgresql":
            try:
                from psycopg_pool import AsyncConnectionPool
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                if not settings.checkpoint_pg_dsn:
                    raise ValueError(
                        "checkpoint_pg_dsn must be set when checkpoint_backend=postgresql"
                    )

                pool = AsyncConnectionPool(
                    settings.checkpoint_pg_dsn,
                    open=False,
                    min_size=1,
                    max_size=5,
                    timeout=30,
                    max_idle=300,
                    max_lifetime=1800,
                    check=AsyncConnectionPool.check_connection,
                    kwargs={
                        "autocommit": True,
                        "prepare_threshold": 0,
                        "connect_timeout": 10,
                        "options": "-c statement_timeout=30000",
                        "keepalives": 1,
                        "keepalives_idle": 10,
                        "keepalives_interval": 5,
                        "keepalives_count": 3,
                    },
                )
                await pool.open()
                checkpointer = AsyncPostgresSaver(pool, serde=serde)
                await checkpointer.setup()
                conn = pool  # shutdown 时调 pool.close()
                logger.info("Checkpointer initialized (PostgreSQL)")
            except ImportError:
                logger.warning(
                    "langgraph-checkpoint-postgres not available, falling back to SQLite"
                )
                backend = "sqlite"  # fall through to SQLite below
            except Exception as e:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    conn = None
                # fail-fast：调用方显式要求 postgresql 后端时，初始化失败绝不
                # 能静默降级。chaos pool 是进程级单例，checkpointer 在首次
                # create_agent 时定型、之后永不改变；若此处静默置 None，图会
                # 零持久化运行：aget_state 抛 "No checkpointer set" 被
                # interaction 层吞掉 → 多轮会话退化为失忆开场白（2026-08-04
                # 平台线上事故）。让异常传播到调用方，暴露真实故障。
                logger.error(
                    "Failed to initialize PostgreSQL checkpointer "
                    "(checkpoint_backend=postgresql): %s", e,
                )
                raise RuntimeError(
                    "checkpoint_backend=postgresql but checkpointer "
                    f"initialization failed: {e}"
                ) from e

        if backend != "postgresql" and checkpointer is None:
            # SQLite path (original logic)
            try:
                import aiosqlite
                from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                checkpoint_path = settings.resolved_checkpoint_db_path
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(checkpoint_path))
                checkpointer = AsyncSqliteSaver(conn=conn, serde=serde)
                await checkpointer.setup()
                logger.info(f"Checkpointer initialized (SQLite: {checkpoint_path})")
            except ImportError:
                logger.warning(
                    "langgraph-checkpoint-sqlite not available, running without checkpointer"
                )
                checkpointer = None
            except Exception as e:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    conn = None
                logger.warning(f"Failed to initialize checkpointer: {e}")
                checkpointer = None

    # Set up LLM with tracing callback for token usage tracking
    from chaos_agent.observability.tracer import TracingCallback, TaskTrace, _traces

    # Create a shared tracing callback that routes to the correct TaskTrace
    # per task_id. Since LLM is shared across tasks, we use a dynamic callback
    # that looks up the current task's trace at callback time.
    class _DynamicTracingCallback(TracingCallback):
        """TracingCallback that dynamically resolves the current task's trace.

        LangChain callbacks don't have per-request context, so we track the
        current task_id via a thread-local-like mechanism set by track_status.

        The ``trace`` property directly reads the in-memory ``_traces`` dict
        instead of calling ``await get_trace()`` because LangChain callbacks
        are synchronous. This is safe because ``track_status`` (async) always
        calls ``await get_trace()`` before the LLM callback accesses the trace,
        ensuring the trace is loaded into memory first.
        """

        def __init__(self):
            # Don't pass a trace to parent; we resolve dynamically
            self._current_task_id = None

        def set_task_id(self, task_id: str):
            self._current_task_id = task_id

        @property
        def trace(self):
            if self._current_task_id and self._current_task_id in _traces:
                return _traces[self._current_task_id]
            return TaskTrace()

        def on_llm_end(self, response, **kwargs) -> None:
            """Record token usage from LLM response."""
            trace = self.trace
            trace.total_llm_calls += 1
            from chaos_agent.observability.tracer import _extract_token_usage
            prompt, completion = _extract_token_usage(response)
            trace.total_token_input += prompt
            trace.total_token_output += completion
            # Diagnostic: log routing and extraction result
            is_dummy = self._current_task_id is None or self._current_task_id not in _traces
            if is_dummy or (not prompt and not completion):
                logger.warning(
                    "_DynamicTracingCallback.on_llm_end: task_id=%r, is_dummy_trace=%s, "
                    "extracted prompt=%d completion=%d",
                    self._current_task_id, is_dummy, prompt, completion,
                )

    _tracing_callback = _DynamicTracingCallback()

    # Register the tracing callback so status_tracker can set task_id
    from chaos_agent.observability import status_tracker as _st_mod
    _st_mod._tracing_callback = _tracing_callback

    # Initialize OTel GenAI parallel export (no-op if not installed/enabled)
    from chaos_agent.observability.otel_genai import (
        init_otel_genai, OTelGenAICallback, is_otel_available,
    )
    init_otel_genai()
    llm_callbacks: list = [_tracing_callback]
    if is_otel_available():
        _otel_callback = OTelGenAICallback()
        _st_mod._otel_callback = _otel_callback
        llm_callbacks.append(_otel_callback)

    llm = make_llm(callbacks=llm_callbacks)
    thinking_status = "enabled" if settings.llm_enable_thinking else "disabled"
    logger.info(f"LLM initialized: {settings.model_name} (thinking {thinking_status}, with tracing callback)")

    # Set up PreReasoningHook for memory compaction
    pre_reason_hook = None
    session_store = None
    try:
        from chaos_agent.memory import (
            ContextManager,
            ToolResultCompactor,
            SessionStore,
            PreReasoningHook,
        )
        from chaos_agent.memory.tui_session_store import get_global_tui_session_store

        memory_base = settings.resolved_memory_dir
        ctx_max_tokens, ctx_compact_ratio = settings.resolve_context_budget(
            settings.model_name
        )
        context_manager = ContextManager(
            max_tokens=ctx_max_tokens,
            compact_ratio=ctx_compact_ratio,
        )
        tool_compactor = ToolResultCompactor(cache_dir=memory_base / "tool_cache")
        session_store = SessionStore(task_dir=memory_base / "tasks")
        from chaos_agent.memory.session_store import set_global_session_store
        set_global_session_store(session_store)
        tui_session_store = get_global_tui_session_store()
        pre_reason_hook = PreReasoningHook(
            context_manager=context_manager,
            tool_compactor=tool_compactor,
            session_store=session_store,
            llm=llm,
            tui_session_store=tui_session_store,
        )
        logger.info("PreReasoningHook initialized for memory compaction")
    except Exception as e:
        logger.warning(f"Failed to initialize PreReasoningHook: {e}")

    # Initialize trace persistence (so metric command can query across process restarts)
    from chaos_agent.observability.tracer import init_tracer
    await init_tracer()

    # Build and compile Intent Graph (dialogue layer)
    from chaos_agent.agent.graph import build_intent_graph
    intent_graph = build_intent_graph(
        clarification_tools=clarification_tools,
        llm=llm,
        registry=registry,
        pre_reason_hook=pre_reason_hook,
    )
    intent_compiled = intent_graph.compile(checkpointer=checkpointer)

    # Build and compile Pipeline Graph (execution layer)
    from chaos_agent.agent.graph import build_pipeline_graph
    pipeline_graph = build_pipeline_graph(
        phase1_tools, phase2_tools,
        verifier_tools=verifier_tools,
        clarification_tools=clarification_tools,
        pre_reason_hook=pre_reason_hook, llm=llm,
        registry=registry,
    )
    pipeline_compiled = pipeline_graph.compile(checkpointer=checkpointer)

    # Build and compile recover graph
    recover_graph = build_recover_graph(
        verifier_tools=recover_verifier_tools,
        pre_reason_hook=pre_reason_hook,
        llm=llm,
        registry=registry,
    )
    recover_compiled = recover_graph.compile(
        checkpointer=checkpointer,
    )

    return {
        "intent": intent_compiled,
        "pipeline": pipeline_compiled,
        "recover": recover_compiled,
        "checkpointer": checkpointer,
        "checkpointer_conn": conn,
        "session_store": session_store,
        "skill_registry": registry,
        "llm": llm,
        "pre_reason_hook": pre_reason_hook,
    }
