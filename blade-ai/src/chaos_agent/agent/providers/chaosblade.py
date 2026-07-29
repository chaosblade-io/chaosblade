"""ChaosBlade execution backend provider.

Backend semantics: faults are injected with ``blade create`` and undone with
``blade destroy <uid>``. Two *delivery* variants share this backend (and this
provider):

- ``host_blade`` — the local (host/agent-installed) blade binary runs
  ``blade create k8s ...`` against the API server. The "host" in the method
  name is the delivery *location*, not the fault target: it still creates a
  K8s experiment.
- ``kubectl_exec`` — fallback used when the local blade binary is unavailable /
  incompatible: ``kubectl exec <tool-pod> -- blade create ...``. Recovery for
  this variant must also go through ``kubectl exec`` (a host ``blade destroy``
  cannot find the CRD-created experiment record).

This provider fully owns every per-backend behaviour for the ChaosBlade
carrier: tool binding (``tools``), injection detection (``detect``), Layer-1
verification (``layer1_verify`` — host-blade vs kubectl-exec dispatch), the
post-injection verifier note (``verify_prompt_note``), the recover Layer-2
framing (``recover_layer2_context``), and deterministic recovery (``recover``).
There are no ChaosBlade branches left anywhere outside this class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from chaos_agent.agent.providers.base import ProviderPrompts, RecoverResult
from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from chaos_agent.agent.result.verdict import Layer1Result


class ChaosbladeProvider:
    """ChaosBlade backend (k8s + host modes; blade_destroy recovery)."""

    carrier = "chaosblade"
    injection_methods = ("host_blade", "kubectl_exec")
    has_experiment_uid = True
    is_multi_step = False
    # ChaosBlade is detected by experiment-UID scan, not by tool-name / kubectl
    # subcommand, so both injection-vocabulary sets are empty.
    inject_tool_names = frozenset()
    inject_kubectl_subcommands = frozenset()
    # Intent vocabulary this carrier contributes to the FaultFamily aggregate —
    # the OS-subsystem targets / ChaosBlade action verbs. Single source of the
    # per-carrier vocabulary (the family no longer re-declares a flat tuple).
    supported_targets = ("cpu", "mem", "network", "disk", "process")
    supported_actions = (
        "fullload", "load", "delay", "loss", "drop", "fill", "kill", "burn",
        "stop",
    )
    # Binaries this backend runs, contributed to the tool guard's Gate-① binary
    # whitelist. ChaosBlade injects and recovers exclusively through ``blade``.
    injection_binaries = frozenset({"blade"})

    def matches_channel(self, profile: str) -> bool:
        # ChaosBlade operates on both cluster and bare-host targets.
        return profile in (PROFILE_K8S, PROFILE_HOST)

    def required_params(self, scope: str) -> list[str]:
        from chaos_agent.agent.spec.fault_registry import required_intent_params

        return required_intent_params(scope)

    def tools(self, phase: str) -> list["BaseTool"]:
        """ChaosBlade tools contributed to the factory tool union per phase.

        - PLAN → ``blade_help`` / ``blade_status`` only. Both are read-only
          (help text / list experiments + confirm blade is installed).
          ``blade_create`` is intentionally ABSENT from planning: ChaosBlade
          has no dry-run mode, so binding it here would hand the planner a
          direct path past the confirmation gate. ``blade_destroy`` is likewise
          hidden from Phase 1 (it mutates cluster state).
        - EXECUTE → the full injection surface. ``blade_destroy`` is available
          for ReAct cleanup of a partial/failed create; the target guard
          validates UID provenance before ToolNode can execute it.
        - VERIFY / RECOVER_VERIFY → nothing (ChaosBlade verification is
          deterministic via Layer 1, not LLM tools).
        """
        from chaos_agent.agent.providers.base import EXECUTE, PLAN
        from chaos_agent.tools import (
            blade_create,
            blade_destroy,
            blade_help,
            blade_query_k8s,
            blade_status,
        )

        if phase == PLAN:
            return [blade_help, blade_status]
        if phase == EXECUTE:
            return [blade_create, blade_destroy, blade_help, blade_status, blade_query_k8s]
        return []

    def detect(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> Optional[str]:
        """Reverse-scan for a live (NON-destroyed) ChaosBlade injection.

        Attributes the task to ChaosBlade only when the most recent parseable
        blade UID has NOT been ``blade_destroy``'d. A failed ``blade_create``
        that was subsequently cleaned up leaves a residual ``UID:`` in history;
        excluding destroyed UIDs (parity with the execute node's
        ``_extract_blade_uid_from_messages``) stops it re-claiming an experiment
        that no longer exists (task-76c59364). Recency vs a later native
        injection is arbitrated by the registry using :meth:`injection_recency`.
        """
        from chaos_agent.agent.providers._detection import (
            scan_blade_evidence_index,
            scan_destroyed_uids,
        )

        _idx, method = scan_blade_evidence_index(
            messages, destroyed=scan_destroyed_uids(messages),
        )
        return method

    def injection_recency(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> int:
        """Message index of the live blade evidence, or ``-1``. See registry."""
        from chaos_agent.agent.providers._detection import (
            scan_blade_evidence_index,
            scan_destroyed_uids,
        )

        idx, _method = scan_blade_evidence_index(
            messages, destroyed=scan_destroyed_uids(messages),
        )
        return idx

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """ChaosBlade Layer-1 verification.

        Dispatches between the two delivery variants this backend owns:
        ``kubectl_exec`` uses ``kubectl exec`` into a tool pod (host blade may be
        incompatible); ``host_blade`` (and the UID-only fallback) polls
        ``blade_status`` / ``blade_query_k8s`` locally.
        """
        from chaos_agent.agent.nodes.verify._verifier_layer1 import (
            _run_host_blade_layer1,
            _run_layer1_via_kubectl_exec,
        )

        blade_uid = kwargs.get("blade_uid", "") or ""
        kubeconfig = kwargs.get("kubeconfig", "") or ""
        task_id = kwargs.get("task_id", "")

        if state.get("injection_method") == "kubectl_exec":
            return await _run_layer1_via_kubectl_exec(
                blade_uid, kubeconfig, task_id=task_id,
                injection_pod_name=state.get("kubectl_exec_pod_name"),
            )
        return await _run_host_blade_layer1(
            blade_uid, kubeconfig, task_id=task_id,
            messages=state.get("messages", []),
        )

    def verify_prompt_note(
        self, injection_method: str, *, injection_pod_name: str | None = None
    ) -> str:
        """Post-injection verifier note for the ``kubectl_exec`` delivery.

        The ``host_blade`` delivery needs no special note (standard kubectl
        verification), so it returns ``""`` and the verifier falls back to its
        default minimal-container note."""
        if injection_method != "kubectl_exec":
            return ""

        note = (
            "\n### Injection Method Note\n"
            "The fault was injected via `kubectl exec` (the standard `blade_create` tool "
            "was unavailable). This means the injection method may differ from the skill "
            "case's recommended approach. You MUST:\n"
            "1. Check whether the ACTUAL injection method produces the same fault effects "
            "described in the skill case's verification steps\n"
            "2. If the expected fault effect differs, note this as a WARNING "
            "and adapt your verification accordingly\n\n"
            "### BusyBox Quick Reference (commands via kubectl exec run in a BusyBox container)\n"
            "- iostat: NO -x flag. Use `iostat -d -k 1 3` (device stats) + `iostat -c 1 3` (CPU/iowait)\n"
            "  NOTE: cumulative counters may overflow (values near 9e18); use interval deltas, NOT absolute values\n"
            "- ps: NO -w flag. Use `ps` (bare) or `ps -o pid,args`, NOT `ps -w` or `ps -o PID,USER,TIME,COMMAND`\n"
            "- grep: NO -E flag (no extended regex). Use `grep -e pattern1 -e pattern2` or basic regex\n"
            "- mount: output differs; use `cat /proc/mounts` as alternative\n"
            "- top: may not exist. Use `top -bn1` (batch mode) or `cat /proc/stat`\n"
            "- df: `df -h` works normally on BusyBox\n"
            "- find: limited but functional. Avoid complex predicates\n"
            "- awk/sed: BusyBox versions have fewer features; prefer simple grep/cut\n"
        )
        if injection_pod_name:
            note += (
                f"\nTool pod `{injection_pod_name}` in `chaosblade` is available for:\n"
                f"  - ChaosBlade commands (blade status, blade destroy)\n"
                f"  - kubectl API checks (describe node, top node, get events)\n"
                f"LIMITATION: This pod does NOT mount /host. `df -h` inside it shows "
                f"the overlay filesystem, not the host disk.\n"
            )
        note += (
            "\n### BusyBox Compatibility (MANDATORY)\n"
            "You are running verification commands inside a BusyBox container (via kubectl exec on a tool pod). "
            "Common Linux flags/commands may NOT be available — check the BusyBox Quick Reference above BEFORE "
            "issuing any command. Do NOT guess flags. If a command returns \"unrecognized option\" or "
            "\"bad usage\", do NOT retry similar commands — switch to the BusyBox alternative immediately.\n"
            "If kubectl exec commands consistently fail, fall back to `kubectl describe` for Pod-level "
            "metrics (restart count, conditions, events) as an alternative.\n"
        )
        return note

    def recover_layer2_context(
        self, state: dict, layer1, *, is_deterministic: bool, blade_uid: str,
        is_host_scope: bool,
    ) -> tuple[str, str]:
        """Recover Layer-2 framing for the ChaosBlade backend.

        Three shapes: kubectl-exec experiment with no deterministic Layer 1
        (``skipped``), LLM-driven recovery execution (kubectl-exec), and the
        deterministic ``blade_destroy`` case."""
        if layer1.status == "skipped":
            layer1_context = (
                "## Layer 1 Result\n"
                "Layer 1 skipped: ChaosBlade experiment was created via kubectl exec and "
                "recovery is being handled through the LLM-driven recovery flow.\n\n"
            )
            layer2_instruction = (
                "This is a ChaosBlade fault that was injected via kubectl exec. "
                "Verify the fault effect has been removed using kubectl tools.\n"
            )
            return layer1_context, layer2_instruction

        if not is_deterministic:
            layer1_context = (
                f"## Layer 1 Result (Recovery Execution)\n"
                f"This is a ChaosBlade fault (injected via kubectl exec). "
                f"Recovery actions executed: {layer1.status}\n"
                f"Details: {layer1.details}\n\n"
            )
            layer2_instruction = (
                "PHASE TRANSITION: Layer 1 (recovery execution) is COMPLETE. "
                "You are now in Layer 2 (VERIFICATION). "
                "DO NOT execute more recovery actions — only VERIFY the fault effect is removed. "
                "Use kubectl only to CHECK status, not to modify resources. "
                "Output RECOVERY_VERIFICATION_RESULT format, NOT RECOVERY_EXECUTION_RESULT.\n"
            )
            return layer1_context, layer2_instruction

        layer1_context = (
            f"## Layer 1 Result (already completed)\n"
            f"blade_destroy for UID {blade_uid}: {layer1.status}\n"
            f"Details: {layer1.details}\n"
            f"Raw output: {layer1.raw_output[:500]}\n\n"
        )
        layer2_instruction = (
            "PHASE TRANSITION: Layer 1 PASSED (blade_destroy reported success and blade_status confirms Destroyed). "
            "You are now in Layer 2 (VERIFICATION). "
            "Verify the fault effect has ACTUALLY been removed from the target's runtime state. "
            + (
                "Use the host diagnostic tool to check the host's runtime state directly. "
                if is_host_scope
                else "Use kubectl tools to check the target resource. "
            )
            + "Output RECOVERY_VERIFICATION_RESULT format, NOT RECOVERY_EXECUTION_RESULT.\n"
        )
        return layer1_context, layer2_instruction

    async def recover(self, state: dict, handle: Optional[dict], **kwargs) -> RecoverResult:
        """Deterministic ChaosBlade recovery verdict (no LLM).

        Two sub-variants keyed on whether the experiment was created via
        ``kubectl exec`` (message-scanned here, not passed in):

        - ``kubectl_exec`` delivery — a host ``blade destroy`` cannot reach a
          CRD-created experiment; without an LLM to run ``kubectl exec`` we
          cannot destroy it. Report ``skipped``/unrecovered with the guidance
          warning.
        - local ``blade_destroy`` — run the canonical Layer-1 recovery
          (blade_destroy + blade_status) and map its verdict.
        """
        from chaos_agent.agent.nodes.recover._recover_layer1 import (
            _recover_layer1_to_dict,
            _run_recover_layer1,
        )
        from chaos_agent.agent.providers._detection import scan_kubectl_blade_success
        from chaos_agent.agent.result.verdict import FailureCategory, Layer1Result

        blade_uid = kwargs.get("blade_uid", "") or ""
        kubeconfig = kwargs.get("kubeconfig", "") or ""
        messages = kwargs.get("messages", []) or []
        is_kubectl_exec = scan_kubectl_blade_success(messages)

        layer2 = {"status": "skipped", "details": "No LLM available for specific verification"}

        if is_kubectl_exec:
            layer1 = Layer1Result(
                status="skipped",
                details=f"ChaosBlade experiment (uid={blade_uid}) was created via kubectl exec, "
                        f"host blade_destroy cannot destroy it — LLM-based recovery required",
            )
            warnings = (
                f"ChaosBlade experiment (uid={blade_uid}) created via kubectl exec cannot be "
                f"destroyed from host (blade_destroy). Use LLM-based recovery "
                f"(blade-ai recover with LLM) to destroy via kubectl exec: "
                f"kubectl exec <pod> -n chaosblade -- blade destroy {blade_uid}",
            )
            return RecoverResult(
                recovered=False, level="unrecovered",
                layer1=_recover_layer1_to_dict(layer1), layer2=layer2,
                warnings=warnings, blade_uid=blade_uid,
                failure=(
                    FailureCategory.RECOVERY_FAILED,
                    f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}",
                ),
            )

        layer1 = await _run_recover_layer1(blade_uid, kubeconfig, messages=messages)
        recovered = layer1.is_passed()
        warnings = (
            (
                "Layer 2 (fault-specific) recovery verification was skipped. "
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


__all__ = ["ChaosbladeProvider"]
