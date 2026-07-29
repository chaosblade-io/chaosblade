"""ChaosBlade Python-application execution backend provider.

Backend semantics: faults are injected INSIDE a running Python process with
``blade create python <target> <action>`` and undone with ``blade destroy
<uid>``. The ChaosBlade Python executor (``chaosblade-exec-python``) runs as an
in-process agent that intercepts library calls via MonkeyPatch, so the fault is
a *method-level* application fault (Redis GET latency, MySQL query exception,
HTTP client return-value tampering) rather than an OS / container resource
fault.

Why a separate carrier from :class:`ChaosbladeProvider`
------------------------------------------------------
Same binary (``blade``), different *fault domain*: the target vocabulary is
middleware clients (``redis`` / ``mysql`` / ``http`` / ...) and the action
vocabulary is method-level verbs (``delay`` / ``throwCustomException`` /
``returnValue``) — disjoint from the OS-subsystem vocabulary the ChaosBlade OS
executor owns. Declaring them on a separate carrier keeps
``INTENT_TARGETS`` / ``INTENT_ACTIONS`` free of illegal combinations (a
``pod-redis fullload`` would otherwise become expressible).

The backend also uses its OWN injection tool (``blade_python_create``) rather
than ``blade_create``. That is what keeps the two ChaosBlade carriers from
fighting over attribution: ``_detection.scan_blade_evidence_index`` only
recognises ``blade_create`` / ``kubectl`` ToolMessages, so a Python-agent
injection is invisible to :meth:`ChaosbladeProvider.detect` and cannot be
mis-attributed to ``host_blade`` (which would route recovery to the wrong
backend).

Prerequisite (NOT performed by this backend)
--------------------------------------------
An in-process agent must already be LISTENING inside the target application.
Getting there is two steps, both verified against chaosblade 1.9.0-alpha:
``blade prepare python --port P --target-script S`` writes a ``sitecustomize.py``
hook into the directory of ``S`` (it bundles its own agent library, so no extra
``pip install``), and the application must then be (re)started with that
directory on ``PYTHONPATH``. Prepare succeeding does not mean an agent is
running. Because the restart cannot happen mid-drill, this is an environment
precondition rather than an injection step, and each experiment stays a single
``create`` → UID → ``destroy`` cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from chaos_agent.agent.providers.base import ProviderPrompts, RecoverResult
from chaos_agent.transports import PROFILE_HOST

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from chaos_agent.agent.result.verdict import Layer1Result


class ChaosbladePythonProvider:
    """ChaosBlade Python-agent backend (in-process method faults; blade_destroy
    recovery)."""

    carrier = "chaosblade_python"
    injection_methods = ("python_agent",)
    has_experiment_uid = True
    # One ``blade create python`` command per experiment — no multi-step
    # injection, so no step self-check is needed before a text-only exit.
    is_multi_step = False
    # This backend IS detected by tool name (unlike ChaosbladeProvider, which is
    # detected by a UID scan over blade_create/kubectl). Declaring the tool here
    # is what isolates the two ChaosBlade carriers' attribution.
    inject_tool_names = frozenset({"blade_python_create"})
    inject_kubectl_subcommands = frozenset()
    # Intent vocabulary this carrier contributes to the FaultFamily aggregate:
    # middleware clients the in-process agent can intercept, and the
    # method-level fault verbs it can apply.
    supported_targets = (
        "redis", "mysql", "http", "httpx", "grpc", "kafka", "sqlalchemy",
    )
    supported_actions = ("delay", "throwCustomException", "returnValue")
    # Binaries this backend runs, contributed to the tool guard's Gate-① binary
    # whitelist. Injection and recovery both go through ``blade``.
    injection_binaries = frozenset({"blade"})

    def matches_channel(self, profile: str) -> bool:
        # ``blade create python`` talks to the in-process agent over
        # ``http://127.0.0.1:<port>``, so it MUST run on the machine hosting the
        # target application — i.e. through a host channel.
        return profile == PROFILE_HOST

    def required_params(self, scope: str) -> list[str]:
        from chaos_agent.agent.spec.fault_registry import required_intent_params

        return required_intent_params(scope)

    def tools(self, phase: str) -> list["BaseTool"]:
        """Tools contributed to the factory tool union per phase.

        - PLAN → ``blade_help`` / ``blade_status`` only (read-only: help text /
          list experiments). ``blade_python_create`` is intentionally ABSENT —
          there is no dry-run, so binding it in planning would hand the planner
          a path past the confirmation gate.
        - EXECUTE → the injection surface plus ``blade_destroy`` for ReAct
          cleanup of a partial/failed create, and the prepare/revoke pair for
          the agent-port precondition.
        - VERIFY / RECOVER_VERIFY → nothing. Layer 1 is deterministic
          (``blade_status``); application-side observation uses ``host_read``,
          which the host-shell backend already contributes to those phases (the
          factory unions every provider's tools).
        """
        from chaos_agent.agent.providers.base import EXECUTE, PLAN
        from chaos_agent.tools import (
            blade_destroy,
            blade_help,
            blade_python_create,
            blade_python_prepare,
            blade_python_revoke,
            blade_status,
        )

        if phase == PLAN:
            return [blade_help, blade_status]
        if phase == EXECUTE:
            return [
                blade_python_create,
                blade_python_prepare,
                blade_python_revoke,
                blade_destroy,
                blade_help,
                blade_status,
            ]
        return []

    def _scan_index(self, messages: list) -> int:
        """Message index of the most-recent live Python-agent injection, or ``-1``.

        Reverse-scans this backend's own injection tool for a parseable
        experiment UID that has NOT been ``blade_destroy``'d, mirroring
        :func:`~chaos_agent.agent.providers._detection.scan_blade_evidence_index`
        but keyed on ``inject_tool_names`` instead of the ChaosBlade OS
        carrier's tool names. Excluding destroyed UIDs stops a cleaned-up failed
        create from re-claiming the task.
        """
        from langchain_core.messages import ToolMessage

        from chaos_agent.agent.providers._detection import scan_destroyed_uids
        from chaos_agent.utils.blade_uid import extract_blade_uid

        destroyed = scan_destroyed_uids(messages)
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if not isinstance(msg, ToolMessage):
                continue
            if getattr(msg, "name", "") not in self.inject_tool_names:
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            uid = extract_blade_uid(content)
            if uid and uid not in destroyed:
                return i
        return -1

    def detect(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> Optional[str]:
        """Return ``python_agent`` when a live Python-agent experiment is attested."""
        if not is_host:
            return None
        return "python_agent" if self._scan_index(messages) >= 0 else None

    def injection_recency(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> int:
        """Message index of this backend's injection evidence, or ``-1``."""
        if not is_host:
            return -1
        return self._scan_index(messages)

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """Deterministic Layer-1 verification: poll ``blade_status`` for the UID.

        Reuses the host-blade Layer 1 path — ``blade_status`` already routes to
        the host channel and queries that host's local experiment DB, which is
        exactly where a ``blade create python`` experiment is recorded.
        """
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            _run_host_blade_layer1,
        )

        return await _run_host_blade_layer1(
            kwargs.get("blade_uid", "") or "",
            kwargs.get("kubeconfig", "") or "",
            task_id=kwargs.get("task_id", ""),
            messages=state.get("messages", []),
        )

    def verify_prompt_note(
        self, injection_method: str, *, injection_pod_name: str | None = None
    ) -> str:
        """Post-injection verifier note: verify at the APPLICATION layer."""
        if injection_method != "python_agent":
            return ""
        return (
            "\n### Injection Method Note\n"
            "The fault was injected INSIDE the target Python process by the "
            "ChaosBlade Python agent (runtime method interception). Nothing "
            "changed at the OS, container or Kubernetes layer, so system-level "
            "metrics and cluster object state are EXPECTED to look normal — "
            "their being normal is NOT evidence the injection failed.\n"
            "Verify at the application layer instead, matching the injected "
            "action:\n"
            "- delay → the intercepted call's latency rises by roughly the "
            "configured time (compare against the pre-injection baseline)\n"
            "- throwCustomException → the intercepted call raises the "
            "configured exception type (visible in application logs / error "
            "rate)\n"
            "- returnValue → the intercepted call returns the configured value "
            "instead of the real one\n"
            "Only calls matching the experiment's matchers (e.g. a specific "
            "Redis command or SQL type) are affected; unmatched calls stay "
            "normal by design.\n"
        )

    def recover_layer2_context(
        self, state: dict, layer1, *, is_deterministic: bool, blade_uid: str,
        is_host_scope: bool,
    ) -> tuple[str, str]:
        """Recover Layer-2 framing: confirm the application behaviour is normal
        again after ``blade_destroy`` removed the in-process interception."""
        layer1_context = (
            f"## Layer 1 Result (already completed)\n"
            f"blade_destroy for UID {blade_uid}: {layer1.status}\n"
            f"Details: {layer1.details}\n"
            f"Raw output: {layer1.raw_output[:500]}\n\n"
        )
        layer2_instruction = (
            "PHASE TRANSITION: Layer 1 (recovery execution) is COMPLETE. "
            "You are now in Layer 2 (VERIFICATION). "
            "DO NOT execute more recovery actions — only VERIFY that the "
            "in-process interception is gone: the previously affected "
            "application calls behave normally again (latency back to baseline, "
            "no injected exception, real return values). Observe from the "
            "application side; OS / cluster state was never modified. "
            "Output RECOVERY_VERIFICATION_RESULT format, NOT "
            "RECOVERY_EXECUTION_RESULT.\n"
        )
        return layer1_context, layer2_instruction

    async def recover(self, state: dict, handle: Optional[dict], **kwargs) -> RecoverResult:
        """Deterministic recovery: ``blade destroy <uid>`` + ``blade_status``.

        A Python-agent experiment is recorded in the local DB of the host the
        command ran on, so the canonical host Layer-1 recovery reaches it
        directly — there is no CRD / kubectl-exec variant to dispatch on.
        """
        from chaos_agent.agent.nodes.recover._recover_layer1 import (
            _recover_layer1_to_dict,
            _run_recover_layer1,
        )
        from chaos_agent.agent.result.verdict import FailureCategory

        blade_uid = kwargs.get("blade_uid", "") or ""
        kubeconfig = kwargs.get("kubeconfig", "") or ""
        messages = kwargs.get("messages", []) or []

        layer1 = await _run_recover_layer1(blade_uid, kubeconfig, messages=messages)
        recovered = layer1.is_passed()
        layer2 = {
            "status": "skipped",
            "details": "No LLM available for application-side verification",
        }
        warnings = (
            (
                "Layer 2 (application-side) recovery verification was skipped. "
                "Only blade_destroy + blade_status verification was performed.",
            )
            if recovered
            else ()
        )
        return RecoverResult(
            recovered=recovered,
            level="recovered" if recovered else "unrecovered",
            layer1=_recover_layer1_to_dict(layer1), layer2=layer2,
            warnings=warnings, blade_uid=blade_uid,
            failure=None if recovered else (
                FailureCategory.RECOVERY_FAILED,
                f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}",
            ),
        )

    def prompt_fragments(self) -> ProviderPrompts:
        return ProviderPrompts()


__all__ = ["ChaosbladePythonProvider"]
