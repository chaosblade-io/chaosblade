"""Agent factory: creates compiled graphs with checkpointer and tools."""

import logging

from chaos_agent.agent.graph import build_recover_graph
from chaos_agent.config.settings import settings
from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.tools import (
    blade_create,
    blade_help,
    blade_status,
    blade_query_k8s,
    kubectl,
    kubectl_ro,
    safe_read_file,
    safe_write_file,
    read_knowledge_resource,
)

# blade_destroy intentionally not imported here. Phase 1 must not see
# it (post-task-ce9647931ce1 mutation lockdown) and Phase 2 also
# doesn't bind it (destruction is framework-controlled by the recover
# graph or by inject/inject_stream auto-cleanup paths, which import
# blade_destroy directly from chaos_agent.tools.blade).

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Patch langchain-openai to preserve reasoning_content from thinking models
# (e.g., Qwen enable_thinking). ChatOpenAI explicitly does NOT extract
# reasoning_content — this patch adds it to additional_kwargs so that
# downstream code can access it via message.additional_kwargs["reasoning_content"].
# ---------------------------------------------------------------------------
_REASONING_PATCH_APPLIED = False


def _patch_langchain_for_reasoning_content() -> None:
    """Monkey-patch langchain-openai message conversion to preserve reasoning_content.

    Idempotent: safe to call multiple times — only patches once.
    """
    global _REASONING_PATCH_APPLIED
    if _REASONING_PATCH_APPLIED:
        return
    _REASONING_PATCH_APPLIED = True

    from langchain_openai.chat_models import base as _lc_base
    from langchain_core.messages import AIMessage, AIMessageChunk

    # --- Patch 1: non-streaming _convert_dict_to_message ---
    _orig_convert_dict = _lc_base._convert_dict_to_message

    def _patched_convert_dict_to_message(_dict):
        msg = _orig_convert_dict(_dict)
        # For assistant messages, extract reasoning_content into additional_kwargs
        if isinstance(msg, AIMessage) and _dict.get("reasoning_content"):
            msg.additional_kwargs["reasoning_content"] = _dict["reasoning_content"]
        return msg

    _lc_base._convert_dict_to_message = _patched_convert_dict_to_message

    # --- Patch 2: streaming _convert_delta_to_message_chunk ---
    _orig_convert_delta = _lc_base._convert_delta_to_message_chunk

    def _patched_convert_delta_to_message_chunk(_dict, default_class):
        msg = _orig_convert_delta(_dict, default_class)
        # For assistant chunks, extract reasoning_content into additional_kwargs
        if isinstance(msg, AIMessageChunk) and _dict.get("reasoning_content"):
            msg.additional_kwargs["reasoning_content"] = _dict["reasoning_content"]
        return msg

    _lc_base._convert_delta_to_message_chunk = _patched_convert_delta_to_message_chunk

    logger.debug("Patched langchain-openai to preserve reasoning_content")


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
    from langchain_openai import ChatOpenAI

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
    return ChatOpenAI(**llm_kwargs)


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

        Constraints (MUST READ before calling):
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
        """Phase 1 / Phase 2 read-only. Read a resource file from an activated skill.

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

        Constraints (MUST READ before calling):
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

        Constraints (MUST READ before calling):
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

        Constraints (MUST READ before calling):
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
    def save_fault_plan(plan_content: str, task_id: str, skill_case_resource: str = "") -> str:
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
    def propose_plan_change(reason: str, scope: str, target: str, action: str) -> str:
        """Replan ONLY. Propose changing the fault type when the current approach failed.

        When to use:
          - During replan (after Phase 2 failure), when you have determined the
            original fault type cannot be injected but an alternative exists.
          - Do NOT use during initial planning — complete your plan with
            finish_planning instead.

        Inputs:
          - reason: why the original fault type failed and why the alternative
            should work (1-2 sentences).
          - scope: proposed ChaosBlade scope (pod/node/container).
          - target: proposed ChaosBlade target (cpu/mem/network/disk/...).
          - action: proposed ChaosBlade action (fullload/burn/drop/delay/...).

        Output: confirmation of proposal submission. The user will be asked
                to approve or reject the change.
        """
        return (
            f"Plan change proposed: {scope}-{target}-{action}. "
            f"Reason: {reason}"
        )

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

        Constraints (MUST READ before calling):
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
    # classify_intent is the only function calling schema bound to the LLM
    # directly — NOT a ToolNode tool. submit_fault_intent is now a real @tool
    # processed by ToolNode (produces ToolMessage feedback for the model).
    # Multi-invocation model: ask_human removed; conversation turns are handled
    # by graph termination + TUI REPL loop. ToolNode processes: kubectl_ro
    # (read-only target verification), activate_skill + read_skill_resource
    # (browse fault types), submit_fault_intent (signal intent convergence).
    #
    # ``kubectl_ro`` (NOT full ``kubectl``) — same rationale as phase1_tools:
    # intent_clarification is a pre-planning stage; only read-only inspection
    # is appropriate. Full kubectl was the bypass vector in sess_1e39e8f4dcce
    # where the LLM called `kubectl scale` to directly mutate kube-system.
    from chaos_agent.agent.nodes.intent_clarification import submit_fault_intent, submit_batch_intent, query_active_experiments

    clarification_tools = [
        kubectl_ro,
        _activate_skill,
        _read_skill_resource,
        submit_fault_intent,
        submit_batch_intent,
        query_active_experiments,
    ]
    if mcp_manager is not None:
        clarification_tools = clarification_tools + mcp_manager.tools_for_phase("clarification")

    # P1-1: Phase 1 (planning / agent_loop) — tightened tool surface.
    # Phase 1 (agent_loop / planning) tool surface.
    #
    # ``blade_create`` is REMOVED from this list — that was the original
    # safety bug. ChaosBlade has no dry-run mode, so any call to
    # ``blade_create`` with real parameters performs the actual fault
    # injection. The whole point of the
    # ``agent_loop → safety_check → confirmation_gate → execute_loop``
    # pipeline is that the user sees the final plan + safety_status
    # BEFORE any destructive action. Binding ``blade_create`` to the
    # planner handed it a direct path past Layer 2 — caught in session
    # sess_dd91ed7271b2 where the planner attempted ``blade_create``
    # four times during ``agent_loop``, before ``confirmation_gate``
    # fired; only a transient K8s API connection error prevented an
    # unauthorised injection.
    #
    # ``blade_destroy`` REMOVED from phase1_tools (post-task-ce9647931ce1):
    # it mutates cluster state, so leaving it in the schema only to let
    # the phase1_screener reject it at runtime would burn LLM turns on a
    # tool the LLM "sees" but can't actually use. Per the user goal of
    # "have the LLM go right on the first try, not via error-recovery",
    # the only consistent choice is to hide it from Phase 1 entirely.
    # Orphan cleanup is deferred to a future ``pre_execute_cleanup`` node
    # that runs deterministically (no LLM dispatch) between
    # confirmation_gate and execute_loop.
    #
    # ``blade_status`` STAYS — read-only: lists current experiments,
    # confirms ChaosBlade is installed.
    # ``kubectl_ro`` (NOT full ``kubectl``) — read-only target inspection
    # only (get / describe / top / logs / version / cluster-info /
    # api-resources / explain / auth). The full ``kubectl`` was the
    # bypass vector in task-ce9647931ce1: with full kubectl bound here,
    # the LLM that撞 the blade_create blacklist pivoted to
    # ``kubectl exec <chaosblade-controller-pod> -- blade create ...``
    # and injected anyway. ``kubectl_ro``'s ``Literal`` type
    # constraint on ``subcommand`` makes that bypass impossible at the
    # tool-schema level.
    #
    # Excludes ``write_file`` / ``search_files`` /
    # ``execute_skill_script`` for the same "planning is read-only +
    # save_fault_plan" reason.
    phase1_tools = [
        _activate_skill,
        _read_skill_resource,
        _read_file,
        _save_fault_plan,
        _finish_planning,
        _propose_plan_change,
        blade_help,
        blade_status,
        # blade_destroy intentionally absent — see comment above
        kubectl_ro,                # ← was: kubectl (full surface)
        read_knowledge_resource,
    ]
    if mcp_manager is not None:
        phase1_tools = phase1_tools + mcp_manager.tools_for_phase("phase1")

    # P1-1: Phase 2 (execution / execute_loop) — tightened tool surface.
    # Excludes blade_destroy and read_skill_resource:
    #   - blade_destroy: destruction is framework-controlled (recover graph
    #     or replan), the executor must not abort experiments mid-run.
    #   - read_skill_resource: skill content was loaded in Phase 1 and is
    #     already embedded in the execution prompt; re-reading wastes tokens.
    from chaos_agent.tools.wait import time_wait
    phase2_tools = [
        blade_create,
        blade_help,
        blade_status,
        blade_query_k8s,
        kubectl,
        _execute_skill_script,
        read_knowledge_resource,
        time_wait,
    ]
    if mcp_manager is not None:
        phase2_tools = phase2_tools + mcp_manager.tools_for_phase("phase2")

    # submit_verification (Scheme B): control-signal tool the verifier LLM
    # calls to submit a structured verdict and end verification.
    # route_after_verifier_tools routes its execution to finalize_verification.
    from chaos_agent.agent.nodes._verifier_submit import submit_verification
    from chaos_agent.tools.kubectl import kubectl_verify
    verifier_tools = [
        kubectl_verify,
        _read_skill_resource,
        _execute_skill_script,
        read_knowledge_resource,
        submit_verification,
        time_wait,
    ]
    if mcp_manager is not None:
        verifier_tools = verifier_tools + mcp_manager.tools_for_phase("verifier")

    from chaos_agent.agent.nodes._verifier_submit import submit_recover_verification
    recover_verifier_tools = [
        kubectl,
        kubectl_verify,
        _read_skill_resource,
        _execute_skill_script,
        read_knowledge_resource,
        submit_recover_verification,
        time_wait,
    ]
    if mcp_manager is not None:
        # Recover verifier shares the same MCP attach_to as the inject
        # verifier phase — both are read-only verification work.
        recover_verifier_tools = recover_verifier_tools + mcp_manager.tools_for_phase("verifier")

    # Set up checkpointer
    conn = None  # aiosqlite connection ref for cleanup
    if checkpointer is None:
        conn = None
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            checkpoint_path = settings.resolved_checkpoint_db_path
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            # Use aiosqlite.connect() directly for a persistent connection.
            # AsyncSqliteSaver.from_conn_string() returns an async context manager
            # that closes the connection on __aexit__, making it unsuitable for
            # long-lived checkpointer instances. Direct aiosqlite connection stays
            # open as long as we hold the reference.
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
            serde = JsonPlusSerializer(
                allowed_msgpack_modules=[
                    ("chaos_agent.agent.verdict", "Layer1Status"),
                    ("chaos_agent.agent.verdict", "FailureCategory"),
                ],
            )
            conn = await aiosqlite.connect(str(checkpoint_path))
            checkpointer = AsyncSqliteSaver(conn=conn, serde=serde)
            await checkpointer.setup()
            logger.info(f"Checkpointer initialized at {checkpoint_path}")
        except ImportError:
            logger.warning("langgraph-checkpoint-sqlite not available, running without checkpointer")
            checkpointer = None
        except Exception as e:
            # Close aiosqlite connection if it was opened but setup failed
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
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
