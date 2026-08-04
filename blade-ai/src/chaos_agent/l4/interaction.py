"""Interactive clarify, connection update, and card resolution APIs."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from chaos_agent.l4.adapter import state_to_task_result
from chaos_agent.l4.error_mapping import map_to_agent_error
from chaos_agent.l4.events import (
    _conn_to_state_patch,
    _forward_progress_event,
    _last_ai_message_text,
)
from chaos_agent.l4.pool import _ChaosAgentPool
from chaos_agent.l4.schemas import ClarifyResult, L4TaskResult, StepResult

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _conn_state_fields(conn: dict) -> dict:
    """Map the L4 ``conn`` dict onto the graph state's connection fields.

    Single source for **all** connection fields so the three ``graph_input``
    branches below cannot drift apart. They previously carried a copy-pasted
    list of only the four Kubernetes fields, which silently dropped every
    host-channel field — the intent graph then had ``kube_connection_mode =
    None`` and the model could not tell a host environment from a K8s one,
    so it loaded ``k8s-chaos-skills`` for SSH/KubeWiz-host environments.

    Both ends were already correct: the caller sends these fields (see the
    platform's ``env_to_conn_dict``) and ``AgentState`` declares them — only
    this hand-off was incomplete.

    ``ssh_port`` is numeric, so it degrades to ``None`` rather than ``""``.
    """
    return {
        "kubeconfig": conn.get("kubeconfig", "") or "",
        "kube_context": conn.get("kube_context", "") or "",
        "kubewiz_cluster_uuid": conn.get("kubewiz_cluster_uuid", "") or "",
        "kubewiz_profile": conn.get("kubewiz_profile", "") or "",
        # Host channel (kubewiz_host / ssh). ``kube_connection_mode`` is the
        # authoritative channel marker used to resolve the capability profile.
        "kube_connection_mode": conn.get("kube_connection_mode", "") or "",
        "host_name": conn.get("host_name", "") or "",
        "ssh_host": conn.get("ssh_host", "") or "",
        "ssh_user": conn.get("ssh_user", "") or "",
        "ssh_port": conn.get("ssh_port") or None,
    }


class _L4InteractionMixin:
    def clarify(
        self,
        thread_id: str,
        user_message: str,
        *,
        tui_session_id: str | None = None,
        conn: dict | None = None,
        on_event: "Callable[[dict], None] | None" = None,
    ) -> ClarifyResult:
        """Run one round of intent clarification.

        Drives the inject graph through ``intent_clarification`` (and
        possibly ``intent_confirm``) until either:
          - graph stops at ``intent_confirm`` interrupt → ``pending_card``
            is populated and ``confirmed_intent`` is None
          - LLM produces a follow-up question → ``last_ai_message`` is
            populated, ``pending_card`` is None
          - graph reaches END (rejected branch) → all fields None except
            ``last_ai_message``

        First call uses ``confirmed_intent=None`` initial_state; subsequent
        calls re-invoke from scratch with accumulated messages + new one
        (resetting confirmed_intent/fault_spec to None so the graph always
        enters intent_clarification).

        Args:
            thread_id: LangGraph checkpointer key (process-memory scope).
            user_message: latest human turn.
            tui_session_id: filename of the on-disk session JSON used by
                TuiSessionStore for cross-process replay. The platform
                typically passes the chat session UUID so ``inject``/
                ``recover`` task files share the same session folder.
                When None/empty, falls back to ``""`` (legacy behaviour,
                no on-disk persistence).
            conn: optional connection params injected into the graph state
                on first turn or refreshed on subsequent turns (for env
                switching). Recognised keys::

                    {
                      "kubeconfig": str,           # raw kubeconfig YAML
                      "kube_context": str,         # optional context name
                      "kubewiz_cluster_uuid": str, # for kubewiz mode
                      "kubewiz_profile": str,      # for kubewiz mode
                    }

                Note: ``settings`` (kubewiz_url/token, model creds) are
                injected separately by the caller via
                ``with blade_ai_context(...):``. ``conn`` only carries
                the per-state fields consumed by pipeline nodes
                (baseline_capture / verifier / debug_pod) via
                ``state.get("kubeconfig")``.
            on_event: optional callback for intermediate progress events.
                Invoked synchronously with a dict containing event info
                (tool calls, node transitions). Used by the platform to
                relay progress to the frontend via SSE.
        """
        pool = self._ensure_pool()
        return asyncio.run(
            self._async_clarify(
                pool, thread_id, user_message,
                tui_session_id=tui_session_id or "",
                conn=conn or {},
                on_event=on_event,
            )
        )

    async def async_clarify(
        self,
        thread_id: str,
        user_message: str,
        *,
        tui_session_id: str | None = None,
        conn: dict | None = None,
        on_event: "Callable[[dict], None] | None" = None,
    ) -> ClarifyResult:
        """Async public entry for intent clarification (stays in caller's loop)."""
        pool = await self._async_ensure_pool()
        return await self._async_clarify(
            pool, thread_id, user_message,
            tui_session_id=tui_session_id or "",
            conn=conn or {},
            on_event=on_event,
        )

    async def _async_clarify(
        self,
        pool: _ChaosAgentPool,
        thread_id: str,
        user_message: str,
        *,
        tui_session_id: str = "",
        conn: dict | None = None,
        on_event: "Callable[[dict], None] | None" = None,
    ) -> ClarifyResult:
        from langchain_core.messages import HumanMessage
        from chaos_agent.config.settings import settings as _settings

        # _NO_TASK_ID_RATIONALE
        # ---------------------
        # None of the ``graph_input`` dicts below carries a ``task_id``, and
        # that omission is load-bearing.
        #
        # ``thread_id`` is a *conversation* key (the LangGraph checkpointer
        # key; the platform passes ``chaos-<session>``). Intent
        # clarification / chat / capability Q&A are dialogue turns, not
        # tasks — only the inject and recover pipelines own the concept of a
        # task, and the real ``task-<hex>`` is minted at the transition point
        # by ``_allocate_operation_task_id``.
        #
        # Passing ``thread_id`` as ``task_id`` used to make ``load_memory``'s
        # ``sync_to_store`` persist a bogus ``tasks`` row per conversation.
        # Those "ghost" experiments then ranked newest-first in the recover
        # flow and were mis-selected, failing with "no injection state found
        # for this task" because nothing had ever been injected.
        #
        # The CLI's ``intent_input`` (cli/runner.py ``converse_stream``)
        # likewise carries no ``task_id`` — keeping this path identical is
        # what makes platform / CLI / TUI behave the same.
        #
        # The persistence layer independently rejects non-task ids
        # (``persistence.task_identity.is_real_task_id``); that is
        # defence-in-depth, not a licence to re-add the field here.

        # Read tenant_id from ContextVar (set by blade_ai_context in platform
        # tools_chaos.py) so it propagates into LangGraph state for
        # load_memory / recover_handler tenant-scoped queries.
        _tenant_id = getattr(_settings, "tenant_id", "") or ""

        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 150,
        }
        conn = conn or {}

        # If thread already has state, push a new HumanMessage and let
        # the graph re-route from intent_clarification. Otherwise build
        # a minimal initial state.
        #
        # CRITICAL: use pool.intent_graph (Intent Graph = dialogue layer:
        # load_memory → intent_clarification → intent_confirm → END).
        # The previous code mistakenly used pool.inject_graph (Pipeline
        # Graph) which has NO intent_clarification node — causing the
        # graph to route straight into agent_loop/finish_planning.
        intent_graph = pool.intent_graph
        try:
            existing = await intent_graph.aget_state(config)
        except ValueError:
            # "No checkpointer set"：图根本没有持久化。此时降级为全新会话
            # = 多轮对话必然失忆（2026-08-04 平台线上事故），fail-closed
            # 把配置错误暴露给调用方，而不是伪装成新会话。
            logger.error(
                "clarify: intent_graph has no checkpointer — refusing to "
                "start a stateless session thread_id=%s", thread_id,
            )
            raise
        except Exception:
            # 瞬时读失败（如 PG 抖动）：记录后降级。注意这会让本轮丢失
            # 历史上下文，若频繁出现必须排查 checkpointer/连接池。
            logger.error(
                "clarify: aget_state failed, degrading to fresh session "
                "thread_id=%s", thread_id, exc_info=True,
            )
            existing = None

        has_existing = bool(existing and existing.values)

        if has_existing and existing.next:
            # Graph is at an interrupt (e.g. intent_confirm).
            #
            # IMPORTANT: Command(update=...) on an interrupted graph does NOT
            # re-execute the interrupted node in a consistent way across
            # LangGraph versions. In some versions it skips the node entirely
            # and continues to the downstream router — which, with
            # confirmed_intent="inject" still in state, routes to agent_loop
            # (causing finish_planning to run inside a clarify() call).
            #
            # Safe approach: treat the new user message as an implicit
            # rejection of the pending intent confirm. Reset the graph state
            # (clear confirmed_intent/fault_spec/batch_submit_args) and
            # re-invoke from scratch so the graph enters
            # intent_clarification cleanly.
            #
            # NOTE: dialogue history is preserved intentionally — the user
            # may want to refine the previous intent (e.g. "remove first 5
            # nodes"), not abandon it entirely. The LLM decides based on
            # the new message content.
            prev_messages = list(existing.values.get("messages") or [])
            prev_messages.append(HumanMessage(content=user_message))
            graph_input = {
                # NOTE: deliberately NO ``task_id`` — see _NO_TASK_ID_RATIONALE.
                "tui_session_id": tui_session_id,
                "confirmed_intent": None,
                "fault_spec": None,
                "batch_submit_args": None,
                "dry_run": False,
                "interaction_mode": "tui",
                "tenant_id": _tenant_id,
                **_conn_state_fields(conn),
                "messages": prev_messages,
            }
        elif has_existing and not existing.next:
            # Graph reached END on previous turn (pure-text follow-up).
            # Re-invoke from scratch with accumulated messages + new one.
            # This is the standard LangGraph pattern for multi-turn where
            # each turn ends the graph (should_continue returns END).
            prev_messages = list(existing.values.get("messages") or [])
            prev_messages.append(HumanMessage(content=user_message))
            graph_input = {
                # NOTE: deliberately NO ``task_id`` — see _NO_TASK_ID_RATIONALE.
                "tui_session_id": tui_session_id,
                "confirmed_intent": None,
                "fault_spec": None,
                "batch_submit_args": None,
                "dry_run": False,
                "interaction_mode": "tui",
                "tenant_id": _tenant_id,
                **_conn_state_fields(conn),
                "messages": prev_messages,
            }
        else:
            graph_input = {
                # NOTE: deliberately NO ``task_id`` — see _NO_TASK_ID_RATIONALE.
                "tui_session_id": tui_session_id,
                # Crucial: None → intent_clarification will run from scratch
                "confirmed_intent": None,
                "fault_spec": None,
                "batch_submit_args": None,
                "dry_run": False,
                "interaction_mode": "tui",
                "tenant_id": _tenant_id,
                **_conn_state_fields(conn),
                "messages": [HumanMessage(content=user_message)],
            }

        values, pending_card, token_usage = await self._drive_until_interrupt(
            intent_graph, graph_input, config, on_event=on_event
        )

        # Extract fault_intent from IntentState's fault_spec (dict form of
        # FaultSpec). IntentState stores the converged intent as fault_spec,
        # not as a separate fault_intent field.
        fault_intent = None
        _spec_dict = values.get("fault_spec")
        if _spec_dict and isinstance(_spec_dict, dict):
            try:
                from chaos_agent.agent.spec.fault_spec import FaultSpec
                fault_intent = FaultSpec.from_dict(_spec_dict).to_intent_dict()
            except Exception:
                fault_intent = _spec_dict

        return ClarifyResult(
            thread_id=thread_id,
            last_ai_message=_last_ai_message_text(values),
            fault_intent=fault_intent,
            confirmed_intent=values.get("confirmed_intent"),
            pending_card=pending_card,
            token_usage=token_usage,
            recover_task_id=values.get("recover_task_id", "") or "",
        )

    def update_connection(
        self,
        thread_id: str,
        conn: dict,
    ) -> None:
        """Refresh the kubeconfig / kubewiz fields on an existing thread.

        Used when the platform wants to switch the bound drill environment
        mid-conversation while preserving messages/fault_spec/confirmed_intent.

        ``conn`` shape is identical to ``clarify(conn=...)``:

            {"kubeconfig": ..., "kube_context": ...,
             "kubewiz_cluster_uuid": ..., "kubewiz_profile": ...}

        If the thread has no existing state yet, this is a no-op (the
        next ``clarify`` call will pick up the same conn via its own
        parameter).
        """
        pool = self._ensure_pool()
        asyncio.run(self._async_update_connection(pool, thread_id, conn or {}))

    async def async_update_connection(
        self,
        thread_id: str,
        conn: dict,
    ) -> None:
        """Async public entry for connection refresh (stays in caller's loop)."""
        pool = await self._async_ensure_pool()
        await self._async_update_connection(pool, thread_id, conn or {})

    async def _async_update_connection(
        self,
        pool: _ChaosAgentPool,
        thread_id: str,
        conn: dict,
    ) -> None:
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 150,
        }
        # Clarify threads live on the Intent Graph
        graph = pool.intent_graph
        try:
            existing = await graph.aget_state(config)
        except Exception:
            existing = None
        if not (existing and existing.values):
            # No checkpoint yet — the connection will be set on the next
            # clarify(conn=...) call. Silently skip.
            return
        await graph.aupdate_state(
            config,
            values=_conn_to_state_patch(conn),
        )

    def step(
        self,
        thread_id: str,
        command: dict,
    ) -> StepResult:
        """Resume a paused graph with a card decision.

        Args:
            thread_id: same thread_id passed to clarify / execute
            command: ``{"card_id": str, "decision": "approved"|"rejected",
                "answer": str | None}``. ``answer`` is reserved for the
                upper layer's ``request_modify`` path; SDK ignores it
                and treats ``decision`` as the only authoritative input.

        ``decision`` MUST be ``"approved"`` or ``"rejected"``. SDK does
        not accept ``request_modify``: the platform layer translates
        ``request_modify`` into ``step(rejected)`` followed by
        ``clarify(user_feedback)``, so the main graph nodes need zero
        changes.
        """
        decision = (command or {}).get("decision")
        if decision not in ("approved", "rejected"):
            raise ValueError(
                f"step.decision must be 'approved' or 'rejected', got {decision!r}. "
                "request_modify is handled by the platform layer, not the SDK."
            )
        pool = self._ensure_pool()
        return asyncio.run(self._async_step(pool, thread_id, decision))

    async def async_step(
        self,
        thread_id: str,
        command: dict,
    ) -> StepResult:
        """Async public entry for resuming a paused graph (stays in caller's loop)."""
        decision = (command or {}).get("decision")
        if decision not in ("approved", "rejected"):
            raise ValueError(
                f"step.decision must be 'approved' or 'rejected', got {decision!r}. "
                "request_modify is handled by the platform layer, not the SDK."
            )
        pool = await self._async_ensure_pool()
        return await self._async_step(pool, thread_id, decision)

    async def _async_step(
        self,
        pool: _ChaosAgentPool,
        thread_id: str,
        decision: str,
    ) -> StepResult:
        from langgraph.types import Command

        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 150,
        }

        # Determine which graph owns this thread.
        # Clarify threads (chaos-<session_id>) live on intent_graph;
        # execution threads (task-<hex>) live on inject_graph (pipeline).
        graph = pool.intent_graph if thread_id.startswith("chaos-") else pool.inject_graph

        try:
            values, pending_card, _token_usage = await self._drive_until_interrupt(
                graph,
                Command(resume=decision),
                config,
            )
        except Exception as exc:
            return StepResult(
                thread_id=thread_id,
                status="failed",
                pending_card=None,
                task_result=L4TaskResult(
                    task_id=thread_id,
                    status="failed",
                    trajectory_id="",
                    error=map_to_agent_error(exc),
                ),
            )

        if pending_card is not None:
            return StepResult(
                thread_id=thread_id,
                status="interrupted",
                pending_card=pending_card,
                task_result=None,
            )

        # Graph ended — synthesise a TaskResult from final state
        result = state_to_task_result(values, thread_id, trajectory_id="")
        return StepResult(
            thread_id=thread_id,
            status="completed",
            pending_card=None,
            task_result=result,
        )
