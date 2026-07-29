"""Host-shell execution backend provider.

Backend semantics: the fault is created by a raw host command (``iptables`` /
``stress-ng`` / ``dd`` / ``fallocate`` …) delivered over a host transport
channel (ssh / kubewiz_host), with no ChaosBlade experiment and no kubectl
object. There is no UID to poll, so Layer-1 is ``skipped``; recovery is
LLM-driven Layer 2 — the *reverse* command is domain knowledge that lives in
the skill case, which the recover graph reads and executes via ``host_inject``.

Backend != environment: "host" here is the execution *backend* (raw shell),
distinct from the transport *channel* that also happens to be host-scoped.
ChaosBlade can target a host too (``host_blade`` delivery), so a host channel
does not by itself imply this backend — the ``is_host`` gate only tells host
shell that a bare command *may* be the injection carrier.

This provider fully owns every per-backend behaviour for the host-shell
carrier: tool binding (``tools``), detection (``detect``), Layer-1 (``skipped``),
the post-injection verifier note (``verify_prompt_note``) and the recover Layer-2
framing (``recover_layer2_context``). Recovery is LLM-driven: there is no
code-side reverse-command derivation — the reverse command is authored per
scenario in the skill case, so recovery goes through the same LLM Layer-1
(execute the reverse via ``host_inject``) / Layer-2 (verify via ``host_read``)
flow as the kubectl-native backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from chaos_agent.agent.providers.base import ProviderPrompts, RecoverResult
from chaos_agent.transports import PROFILE_HOST

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from chaos_agent.agent.result.verdict import Layer1Result


class HostShellProvider:
    """Host raw-shell backend (native-command faults; reverse-command recovery)."""

    carrier = "host_shell"
    injection_methods = ("host_native",)
    has_experiment_uid = False
    # Host-native injection is a UID-less raw-command backend that may span
    # multiple injection steps (like kubectl_native), so it opts into the
    # multi-step injection step self-check before a text-only exit.
    is_multi_step = True
    # Raw host commands that, when run successfully, mark a host-native
    # injection. Single source of truth for the host-native carrier vocabulary;
    # ``detect`` (below) scans these via ``scan_host_native_injection``.
    inject_tool_names = frozenset({"host_inject", "exec_host_command", "shell"})
    inject_kubectl_subcommands = frozenset()
    # Intent vocabulary this carrier contributes to the FaultFamily aggregate.
    # Host raw-shell faults cover the same OS subsystems / action verbs as the
    # ChaosBlade OS executor; declared here so the ``host`` family derives its
    # vocabulary from its carriers rather than re-listing a flat tuple. (These
    # overlap the chaosblade set and dedup away in the aggregate — see
    # fault_registry aggregation.)
    supported_targets = ("cpu", "mem", "network", "disk", "process")
    supported_actions = (
        "fullload", "load", "delay", "loss", "drop", "fill", "kill", "burn",
    )
    # Raw host binaries this backend runs, contributed to the tool guard's
    # Gate-① binary whitelist. Groups mirror the guard's former inline tiers:
    #   - fault injection primitives (net / stress / disk write),
    #   - Tier-1 recovery / low-risk primitives (bounded, reversible),
    #   - Tier-2 service / time control (admitted only WITH the guard's extra
    #     per-binary guards — systemctl verb whitelist, kill PID / chmod
    #     recursion checks, which stay in the guard, NOT here).
    # SECURITY: never includes interpreters / shells (sh/bash/python/perl); the
    # guard admits the binary, its per-binary guards still narrow the form.
    injection_binaries = frozenset({
        # fault injection
        "iptables", "ip6tables", "nft", "tc",
        "stress", "stress-ng", "dd", "fallocate", "fio",
        # Tier-1 recovery / low-risk primitives
        "truncate", "chmod", "cp", "kill", "ntpdate", "chronyc",
        # Tier-2 service / time control (guarded per-binary)
        "systemctl", "date", "timedatectl", "mv",
        # Tier-2 single-resource fault primitives (guarded per-binary).
        # Admitted because a fallback plan has to WORK: not every cluster runs
        # ChaosBlade, and for those the 降级方案 section is the only path, not a
        # decoration. Each is the sole way to express its fault:
        #   nc     — occupy a port (Host_网络故障_端口占用)
        #   fuser  — kill whoever holds a port (Host_进程异常_进程被杀死)
        #   strace — attach to a PID and slow its syscalls
        #            (Host_系统调用异常_调用延迟)
        # Each also has a genuinely dangerous form, so each is narrowed by its
        # own guard in ToolGuard (_check_nc / _check_fuser / _check_strace):
        # listen-only for nc, port-spec-only for fuser, attach-only for strace.
        # Admitting the bare binary without those checks would hand over
        # arbitrary command execution (`nc -e /bin/sh`, `strace <cmd>`).
        "nc", "fuser", "strace",
    })

    def matches_channel(self, profile: str) -> bool:
        # Raw-shell faults only make sense against a bare host.
        return profile == PROFILE_HOST

    def required_params(self, scope: str) -> list[str]:
        from chaos_agent.agent.spec.fault_registry import required_intent_params

        return required_intent_params(scope)

    def tools(self, phase: str) -> list["BaseTool"]:
        # host_read (read-only) is the single observation tool for the read-only
        # phases. host_inject (mutating) injects (EXECUTE) and runs the
        # skill-case reverse command during recovery (RECOVER_VERIFY); it is the
        # SUPERSET of host_read (admits read-only diagnostics via skip_guard),
        # so the write phases bind it alone.
        from chaos_agent.agent.providers.base import (
            EXECUTE,
            PLAN,
            RECOVER_VERIFY,
            VERIFY,
        )
        from chaos_agent.tools.host_cmd import host_inject, host_read

        if phase == PLAN:
            return [host_read]
        if phase == EXECUTE:
            return [host_inject]
        if phase == RECOVER_VERIFY:
            return [host_inject]
        if phase == VERIFY:
            return [host_read]
        return []

    def detect(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> Optional[str]:
        """Classify as ``host_native`` on a resolved host channel with a
        successful raw-command carrier. Does NOT bail on a non-empty
        ``blade_uid``: a failed host blade attempt that fell back to a raw host
        command still leaves a stale UID, so ownership is decided by RECENCY at
        the registry (:meth:`injection_recency`), not by UID presence."""
        if not is_host:
            return None
        from chaos_agent.agent.providers._detection import scan_host_native_index

        return "host_native" if scan_host_native_index(
            messages, self.inject_tool_names,
        ) >= 0 else None

    def injection_recency(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> int:
        """Message index of the latest host-native carrier, or ``-1``."""
        if not is_host:
            return -1
        from chaos_agent.agent.providers._detection import scan_host_native_index

        return scan_host_native_index(messages, self.inject_tool_names)

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """A host-native fault applies a raw host command (iptables / stress-ng /
        dd …), not a ChaosBlade experiment — there is no ``blade_status`` to poll,
        so Layer 1 is not applicable (the host effect is checked in Layer 2)."""
        from chaos_agent.agent.result.verdict import Layer1Result

        return Layer1Result(
            status="skipped",
            details="host-native injection (no blade experiment), Layer 1 not applicable",
        )

    def verify_prompt_note(
        self, injection_method: str, *, injection_pod_name: str | None = None
    ) -> str:
        """Post-injection verifier note for a host-native fault."""
        if injection_method != "host_native":
            return ""
        return (
            "\n### Injection Method Note\n"
            "The fault was injected via a native command directly on the target host "
            "(no ChaosBlade experiment, no cluster tool pod). This means:\n"
            "1. There is NO `blade_uid` / ChaosBlade status to consult — verify the fault "
            "effect by observing the host directly (filesystem, processes, network, I/O).\n"
            "2. Run verification commands ON THE HOST through the configured host transport. "
            "Do not assume any other execution backend is available.\n"
            "3. Recovery is performed by reversing the native command (e.g. reclaiming a "
            "fill file, resuming a process), so confirm the effect is present before "
            "concluding the fault is in effect.\n"
        )

    def recover_layer2_context(
        self, state: dict, layer1, *, is_deterministic: bool, blade_uid: str,
        is_host_scope: bool,
    ) -> tuple[str, str]:
        """Recover Layer-2 framing for a host-native (raw-shell) fault.

        Recovery is LLM-driven: Layer 1 executed the reverse command sourced
        from the skill case via ``host_inject``; Layer 2 confirms the fault
        effect is gone on the host via ``host_read``.
        """
        if layer1.status == "skipped":
            layer1_context = (
                "## Layer 1 Result\n"
                "Layer 1 skipped: host-native fault with no recovery actions in skill files. "
                "Proceed directly to Layer 2 recovery verification.\n\n"
            )
            layer2_instruction = (
                "This is a host-native fault recovery. "
                "Verify the fault effect has been removed on the host using the host diagnostic tool.\n"
            )
            return layer1_context, layer2_instruction

        layer1_context = (
            f"## Layer 1 Result (Recovery Execution)\n"
            f"This is a host-native fault. Recovery actions executed on the host: {layer1.status}\n"
            f"Details: {layer1.details}\n\n"
        )
        layer2_instruction = (
            "PHASE TRANSITION: Layer 1 (recovery execution) is COMPLETE. "
            "You are now in Layer 2 (VERIFICATION). "
            "DO NOT execute more recovery actions — only VERIFY the fault effect is removed. "
            "Use the host diagnostic tool only to CHECK host state, not to modify it. "
            "Output RECOVERY_VERIFICATION_RESULT format, NOT RECOVERY_EXECUTION_RESULT.\n"
        )
        return layer1_context, layer2_instruction

    async def recover(self, state: dict, handle: Optional[dict], **kwargs) -> RecoverResult:
        """No-LLM verdict for the host-native / no-UID case (mirrors
        :meth:`K8sNativeProvider.recover`).

        There is no ``blade_uid`` experiment to destroy and no code-side reverse
        command (the reverse lives in the skill case, executed by the LLM recover
        loop). Without an LLM this cannot be verified — report
        ``skipped``/unrecovered with the historical warning.
        """
        from chaos_agent.agent.nodes.recover._recover_layer1 import _recover_layer1_to_dict
        from chaos_agent.agent.result.verdict import FailureCategory, Layer1Result

        blade_uid = kwargs.get("blade_uid", "") or ""
        layer1 = Layer1Result(
            status="skipped",
            details="Host-native fault (no blade_uid), Layer 1 recovery not applicable",
        )
        return RecoverResult(
            recovered=False, level="unrecovered",
            layer1=_recover_layer1_to_dict(layer1),
            layer2={"status": "skipped", "details": "No LLM available for host recovery verification"},
            warnings=(
                "Host-native fault: Layer 1 not applicable, Layer 2 skipped (no LLM). "
                "Recovery could NOT be verified — the fault may still be active.",
            ),
            blade_uid=blade_uid,
            failure=(
                FailureCategory.RECOVERY_FAILED,
                f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}",
            ),
        )

    def prompt_fragments(self) -> ProviderPrompts:
        return ProviderPrompts()


__all__ = ["HostShellProvider"]
