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

import re
from typing import TYPE_CHECKING, Any, Optional

from langchain_core.messages import ToolMessage

from .declaration import (
    CARRIER_ID,
    HOST_INJECT_TOOL_NAMES,
    SUPPORTED_ACTIONS,
    SUPPORTED_TARGETS,
)
from chaos_agent.agent.providers.base import (
    DestroyOutcome,
    ProviderPrompts,
    RecoverResult,
    StepActionScan,
    coerce_tool_args_dict,
)
from chaos_agent.agent.providers.message_scanning import (
    build_tool_call_args_lookup,
    reached_target,
)
from chaos_agent.transports import PROFILE_HOST

# NOTE: module level here imports ONLY providers-internal + stdlib modules —
# target_guard symbols are imported lazily inside the functions that use them
# (fault_registry builds its vocabulary from this class at import time, so a
# module-level import reaching target_guard/__init__ → freeze → fault_registry
# would be circular; see the NOTE in chaosblade.py).

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from chaos_agent.tools.request_identity import RequestFingerprint
    from chaos_agent.agent.result.verdict import Layer1Result

    from chaos_agent.agent.target_guard.types import EffectiveTarget


def _classify_host_inject(args: dict, raw_command: str) -> EffectiveTarget:
    """Classify a ``host_inject`` tool_call.

    ``host_inject`` runs ONE raw host-native fault command (``iptables`` /
    ``tc`` / ``stress-ng`` / ``dd`` / ``kill`` …) over the host transport.
    Identity is the host itself, not a k8s namespace/selector, so
    ``scope="host"``. The fault family is derived from the command via
    ``classify_host_operation`` so the guard's ``fault_target`` lock still
    pins the fault TYPE (``network`` → ``process`` is drift), while the
    k8s namespace/names/labels checks are skipped for host scope.

    Guard-side target classification migrated from
    ``target_guard/classifier.py`` (phase-7 T5) — pure move, behaviour is
    byte-identical.
    """
    # Lazy imports — target_guard symbols must not be imported at module level
    # from a provider module (see the NOTE near the top of this file).
    from chaos_agent.agent.target_guard.types import ConfidenceLevel, EffectiveTarget

    from chaos_agent.agent.target_guard.carriers import (
        classify_host_operation,
        find_banned_host_verbs,
    )

    command = str(args.get("command") or "")
    family = classify_host_operation(command)
    reject_detail = ""
    if not command:
        reject_detail = (
            "host_inject was called with an empty 'command', so there is "
            "no host operation to classify"
        )
    elif not family:
        # An empty family with a banned verb on board is NOT "no family
        # verb" — say which verb voided it (same fix as the carrier gate;
        # see carriers._resolve_carrier_from_artifact / inject-055c86cc).
        banned = find_banned_host_verbs(command)
        if banned:
            verbs = ", ".join(f"'{verb}'" for verb in banned)
            reject_detail = (
                f"the command contains banned verb(s) {verbs} — a banned "
                "verb never maps to a fault family, so the fault type "
                "cannot be pinned; remove the banned verb and re-express "
                "that step with the fault family's own binaries"
            )
    return EffectiveTarget(
        scope="host",
        namespace="",
        host_name="",
        fault_target=family,
        confidence=ConfidenceLevel.HIGH if command else ConfidenceLevel.UNKNOWN,
        raw_command=raw_command,
        # Say WHICH argument is missing — see the note in the python-app
        # classifier (chaosblade_python.py).
        reject_detail=reject_detail or (
            ""
            if command
            else "host_inject was called with an empty 'command', so there is "
            "no host operation to classify"
        ),
    )


class HostShellProvider:
    """Host raw-shell backend (native-command faults; reverse-command recovery)."""

    carrier = CARRIER_ID
    injection_methods = ("host_native",)
    has_experiment_uid = False
    # Not the verdict-default carrier (that role belongs to the kubectl-native
    # backend on the K8s profile) — host-channel dispatch always carries an
    # attributed method.
    uid_less_verdict_default = False
    handle_kind = "native"
    # Host-native injection is a UID-less raw-command backend that may span
    # multiple injection steps (like kubectl_native), so it opts into the
    # multi-step injection step self-check before a text-only exit.
    is_multi_step = True
    # UID-less carrier: no programmatic Layer-1 recovery exists — the
    # LLM-driven undo flow IS the Layer 1.
    has_deterministic_recover = False
    # Raw host commands that, when run successfully, mark a host-native
    # injection. Single source of truth for the host-native carrier
    # vocabulary (declaration.HOST_INJECT_TOOL_NAMES — R23/G-7: the
    # machinery≠mutation HOST face consumes the SAME set through the
    # declaration seam); ``detect`` (below) scans these via
    # ``scan_host_native_injection``.
    inject_tool_names = HOST_INJECT_TOOL_NAMES
    inject_kubectl_subcommands = frozenset()
    # Intent vocabulary this carrier contributes to the FaultFamily aggregate.
    # Host raw-shell faults cover the same OS subsystems / action verbs as the
    # ChaosBlade OS executor; declared here so the ``host`` family derives its
    # vocabulary from its carriers rather than re-listing a flat tuple. (These
    # overlap the chaosblade set and dedup away in the aggregate — see
    # fault_registry aggregation.)
    supported_targets = SUPPORTED_TARGETS
    supported_actions = SUPPORTED_ACTIONS
    # Raw host binaries this backend runs, contributed to the tool guard's
    # Gate-① binary whitelist. Groups mirror the guard's former inline tiers:
    #   - fault injection primitives (net / stress / disk write),
    #   - Tier-1 recovery / low-risk primitives (bounded, reversible),
    #   - Tier-2 service / time control (admitted only WITH the guard's extra
    #     per-binary guards — systemctl verb whitelist, kill PID / chmod
    #     recursion checks, which stay in the guard, NOT here).
    # SECURITY: never includes interpreters / shells (sh/bash/python/perl); the
    # guard admits the binary, its per-binary guards still narrow the form.
    injection_binaries = frozenset(
        {
            # fault injection
            "iptables",
            "ip6tables",
            "nft",
            "tc",
            "stress",
            "stress-ng",
            "dd",
            "fallocate",
            "fio",
            # Tier-1 recovery / low-risk primitives
            "truncate",
            "chmod",
            "cp",
            "kill",
            "ntpdate",
            "chronyc",
            # Tier-2 service / time control (guarded per-binary)
            "systemctl",
            "date",
            "timedatectl",
            "mv",
            # Tier-2 self-recovery timer carrier (guarded per-binary). The skill
            # 降级方案 pattern "先武装定时恢复，再注入" arms a systemd transient
            # timer (`systemd-run --on-active=<N>s --unit=... <inverse-cmd>`) so a
            # native fault self-reverses at the deadline even if the session dies.
            # Admitted ONLY in that timer form — `_check_systemd_run` rejects every
            # non-`--on-active` form, because a bare `systemd-run <cmd>` runs the
            # payload IMMEDIATELY and would be an arbitrary-execution bypass of
            # Gate ① (`systemd-run nginx` runs nginx, which no whitelist admits).
            "systemd-run",
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
            "nc",
            "fuser",
            "strace",
            # Tier-2 drill-artifact cleanup tail (guarded per-binary): the host
            # twin of `kubectl delete <debug-pod>` — every 降级方案 ends its
            # manual early-recovery with `rm -f <file>.bak` (backup / fill file
            # the drill itself created). Narrowed to that exact single-file form
            # by `_check_rm`; recursive forms have no drill boundary.
            "rm",
        }
    )
    # Raw-shell faults run on the host itself — no injection-infrastructure
    # pods anywhere, so the Tier-1 tool-pod-namespace exemption set is empty
    # (the protocol default, made explicit).
    tool_pod_namespaces = frozenset()
    # Phase-7 T2 per-tool pass sets. Host tools never take a kubeconfig
    # (raw-shell channel, no cluster), and their runs are not shipped to L4;
    # both ``host_inject`` and ``host_read`` declare ``task_id``.
    kubeconfig_scoped_tool_names = frozenset()
    audit_scoped_tool_names = frozenset({"host_inject", "host_read"})
    log_shipping_tool_names = frozenset()
    # Create-reconcile gate (D6): the gate arms on the uncertain-outcome
    # marker, which only create tools emit — host tools are not create
    # tools and never emit it, so the gate has nothing to arm on (the
    # protocol defaults, made explicit). R58 correction: the earlier claim
    # that a raw-shell command has a "deterministic exit" was falsified by
    # the R57 shared-transport measurement (the local wait can be killed
    # while the command keeps running server-side) — host_inject surfaces
    # that edge via its ToolTimeoutError branch. The empty sets stay
    # correct: the gate is marker-driven, not exit-driven, and host
    # returns never carry the marker.
    reconcile_create_tool_names = frozenset()
    reconcile_read_tool_names = frozenset()
    # Result-shape verdict (agent/tool_verdicts.py): the host tools render
    # failures as text (the ``Error:`` prefix / ToolTimeoutError branch), so
    # the generic verdict already reads them and this carrier has no result
    # shape of its own to declare (the protocol default, made explicit).
    result_shape_tool_names = frozenset()

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
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> Optional[str]:
        """Classify as ``host_native`` on a resolved host channel with a
        successful raw-command carrier. Does NOT bail on a non-empty
        ``experiment_uid``: a failed host blade attempt that fell back to a raw host
        command still leaves a stale UID, so ownership is decided by RECENCY at
        the registry (:meth:`injection_recency`), not by UID presence.

        ``is_teardown`` threads the machinery≠mutation exemption into the
        scan (P3, R23/G-7): an arm-first systemd-run timer is a recovery
        registration, not native takeover evidence — the same matcher the
        kubectl channel's scans consume.
        """
        if not is_host:
            return None
        from chaos_agent.agent.providers.message_scanning import scan_host_native_index

        return (
            "host_native"
            if scan_host_native_index(
                messages,
                self.inject_tool_names,
                is_teardown=is_teardown,
            )
            >= 0
            else None
        )

    def issue_disproven(self, messages: list, *, is_teardown=None) -> bool:
        """Host-native results are untrustworthy by nature: the injected
        fault (a network drop, a killed daemon) routinely severs the very
        channel that would report it, so an ``Error:`` result is as likely
        from a SUCCESSFUL injection as from a failed one (the forensic
        paradox). Counter-evidence is therefore never derivable — an
        issue-time attribution stands."""
        return False

    def injection_recency(
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> int:
        """Message index of the latest host-native carrier, or ``-1``.

        ``is_teardown`` is threaded (R23/G-7) — same exemption semantics
        as :meth:`detect`, so recency arbitration never rescues an exempted
        arm timer that the detection scan already skipped.
        """
        if not is_host:
            return -1
        from chaos_agent.agent.providers.message_scanning import scan_host_native_index

        return scan_host_native_index(
            messages, self.inject_tool_names, is_teardown=is_teardown,
        )

    def build_fault_handle(self, values: dict) -> Optional[dict]:
        """Claim a committed host-native injection: no experiment UID exists
        for this carrier, so the attributed method IS the handle fact."""
        values = values or {}
        if values.get("injection_method") != "host_native":
            return None
        return {"kind": "native", "method": "host_native"}

    def extract_experiment_id(self, messages: list, retired=None) -> str:
        """UID-less carrier: there is no experiment id to extract."""
        return ""

    def build_handle_from_messages(
        self, messages: list, retired=None, values: Optional[dict] = None
    ) -> Optional[dict]:
        """UID-less carrier: never claims message-history evidence (the
        durable method attribution is the only fact this backend owns)."""
        return None

    def created_experiment_ids(self, messages: list, state: dict) -> set[str]:
        """UID-less carrier: no experiment ids exist to prove. A host-native
        fault is undone by reverse commands, never by a destroy call, so the
        provenance gate has nothing to admit here."""
        return set()

    def destroyed_experiment_ids(self, messages: list) -> set[str]:
        """UID-less carrier: no destroy calls exist to attribute terminal
        state to (the issued-scan seam's neutral empty contribution)."""
        return set()

    def destroyed_proven_experiment_ids(self, messages: list) -> set[str]:
        """UID-less carrier: no destroy output exists to prove a death (the
        retire ledger's neutral empty contribution)."""
        return set()

    def classify_tool_target(
        self, tool_name: str, tool_args: Any, raw_command: str
    ) -> Optional[EffectiveTarget]:
        """Guard-side classification of this carrier's tools (phase-7 T5).

        Claims the ``host_inject`` injection tool (classifier above) and
        ``host_read`` — read-only BY ENFORCEMENT (its own tool-side guard
        rejects any mutating command), so it short-circuits to READONLY.
        ``None`` (not this carrier's tool) lets the registry scan
        continue."""
        # Lazy import — see the NOTE near the top of this file.
        from chaos_agent.agent.target_guard.types import (
            SCOPE_READONLY,
            EffectiveTarget,
        )

        if tool_name == "host_inject":
            return _classify_host_inject(
                coerce_tool_args_dict(tool_args),
                raw_command,
            )
        if tool_name == "host_read":
            return EffectiveTarget(
                scope=SCOPE_READONLY,
                namespace="",
                raw_command=raw_command,
            )
        return None

    def parse_injection_params(self, tool_name: str, tool_args: dict) -> Optional[dict]:
        """No issue-time key-parameter extraction for the raw-shell surface:
        ``host_inject`` carries the full command as one string, whose
        verification-relevant parts the recover context renders from the
        handle / execution artifacts instead."""
        return None

    def issue_time_method(
        self, tool_name: str, tool_args: dict, *, is_host: bool = False
    ) -> Optional[str]:
        """Issue-time attribution: a raw-shell inject tool enacts
        ``host_native`` — but ONLY on a resolved HOST channel (``is_host``);
        the same tool on a k8s channel is not attributed. Migrated from the
        execute-side classifier's hardcoded branch (phase-7 T4)."""
        if is_host and tool_name in self.inject_tool_names:
            return "host_native"
        return None

    def build_reconcile_fingerprint(
        self, tool_name: str, tool_args: Any
    ) -> Optional["RequestFingerprint"]:
        """Create-reconcile seam (D6): this carrier declares no create
        under the gate (``reconcile_create_tool_names`` is empty), so the
        fingerprint hook never claims — pinned ``None``."""
        return None

    async def reconcile_hold_feedback(
        self,
        tool_name: str,
        fp: "RequestFingerprint",
        hold_count: int,
        block_limit: int,
        kubeconfig: str = "",
        task_id: str = "",
    ) -> Optional[tuple[str, bool]]:
        """Create-reconcile seam (D6): no create under the gate — pinned
        ``None`` (the registry scan continues past this carrier)."""
        return None

    def reconcile_batch_held_feedback(
        self, tool_name: str, other_tool_name: str
    ) -> Optional[str]:
        """Create-reconcile seam (D6): no create under the gate — pinned
        ``None``."""
        return None

    async def rollback_handle(self, handle: dict, **kwargs) -> str:
        """Host-native faults are undone by reverse commands in the recover
        graph, not by a synchronous failure-path rollback."""
        return ""

    def was_fault_create_attempted(
        self,
        messages: list,
        injection_method: str | None = None,
        *,
        is_teardown=None,
    ) -> bool:
        """Always False, pinned explicitly: this UID-less carrier has no
        experiment record whose create could be "attempted but never
        landed" — whether a native injection was attempted is carried by
        the issue-time attribution and the LLM flow, and a True here would
        mis-trigger the recover terminal "no UID" branch. The builtin
        providers satisfy the protocol structurally, so the protocol
        default is invisible to them; this explicit mirror is what the
        conformance suite pins."""
        return False

    def scan_step_actions(
        self, steps: list[str], messages: list, *, is_teardown=None,
    ) -> Optional[StepActionScan]:
        """Form B hook (phase-8 T3): THIS backend's step vocabulary — host
        injection binaries (word-boundary matched so a short binary like
        ``dd`` / ``cp`` does not false-match inside another word) for the
        required side, attempted binaries in ``host_inject`` commands for
        the executed side. ``is_teardown`` is accepted for protocol
        uniformity (P3) and ignored: a kubectl delete is never a host
        step verb."""
        return StepActionScan(
            required=_required_host_binaries(steps),
            executed=_executed_host_binaries(messages),
        )

    def was_injection_attempted(self, messages: list, *, is_teardown=None) -> bool:
        """Explicitly not claimed (pinned False): the message back-scan
        hook consumed via the native resolve path is kubectl-native
        territory; THIS carrier's attempt state is carried by the
        issue-time attribution. Declared explicitly so the builtin set
        satisfies the protocol surface (the conformance suite pins it)."""
        return False

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """A host-native fault applies a raw host command (iptables / stress-ng /
        dd …), not a ChaosBlade experiment — there is no ``blade_status`` to poll,
        so Layer 1 is not applicable (the host effect is checked in Layer 2)."""
        from chaos_agent.agent.result.verdict import Layer1Result

        return Layer1Result(
            status="skipped",
            details="host-native injection (no blade experiment), Layer 1 not applicable",
        )

    async def layer1_raw_destroy(self, uid: str, kubeconfig: str = "") -> str:
        """No bare destroy exists for a host-native fault (nothing to destroy
        programmatically); the finalize retry never routes here."""
        return ""

    def classify_destroy_output(self, output: str) -> DestroyOutcome:
        """UID-less carrier: no destroy output exists to classify — the
        empty string this carrier returns is FAILED under the authority
        anyway; pinned explicitly so the protocol stays satisfied."""
        return DestroyOutcome.FAILED

    def tool_result_error_text(
        self, tool_name: str, content: str
    ) -> Optional[str]:
        """Text-dialect carrier: the host tools render failures as text (the
        ``Error:`` prefix / the ToolTimeoutError branch), which the generic
        verdict already reads, so there is no result shape of this carrier's
        own to judge — pinned abstaining explicitly so the protocol stays
        satisfied."""
        return None

    async def layer1_destroy(
        self,
        uid: str,
        kubeconfig: str = "",
        *,
        messages: list | None = None,
        injection_method: str | None = None,
        # Protocol parity with the ledger rung: there is no deterministic
        # Layer-1 here to hydrate an identity FOR (this returns ``skipped``
        # and the LLM flow's undo is Layer 1), so the list stays unread.
        artifacts: list | None = None,
    ) -> "Layer1Result":
        """No deterministic Layer-1 recovery exists for a host-native fault:
        the raw host command has no experiment to destroy — the LLM flow's
        undo IS Layer 1. Explicit ``skipped`` for protocol completeness."""
        from chaos_agent.agent.result.verdict import Layer1Result

        return Layer1Result(
            status="skipped",
            details="host-native injection (no experiment to destroy), Layer 1 destroy not applicable",
        )

    def recovery_vehicle(self, state: dict) -> str:
        """No durable recovery-vehicle record on this carrier (the mutation ran
        on this host) — nothing to render."""
        return ""

    def blocks_deterministic_destroy(
        self, state: dict, messages: list | None = None
    ) -> bool:
        """UID-less carrier: no deterministic destroy exists to block."""
        return False

    def recovery_facts_render(
        self, state: dict, *, spec_params: dict | None = None
    ) -> str:
        """UID-less carrier: no carrier-owned injection facts to render."""
        return ""

    def merge_deterministic_recover_verdict(
        self, layer1, state: dict, part_override: dict | None = None
    ):
        """UID-less carrier: no deterministic part to merge — identity."""
        return layer1

    def layer1_recover_guidance(
        self,
        state: dict,
        experiment_uid: str,
        *,
        combo_native: bool = False,
        combo_part: dict | None = None,
    ) -> str:
        """UID-less carrier: the LLM undo flow IS the recovery — no
        experiment guidance to contribute."""
        return ""

    def layer2_facts_note(self, state: dict) -> str:
        """UID-less carrier: no carrier-owned facts to note."""
        return ""

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
            "1. There is NO `experiment_uid` / ChaosBlade status to consult — verify the fault "
            "effect by observing the host directly: the observables the injected mechanism "
            "is expected to change.\n"
            "2. Run verification commands ON THE HOST through the configured host transport. "
            "Do not assume any other execution backend is available.\n"
            "3. Recovery is performed by reversing the native command (e.g. reclaiming a "
            "fill file, resuming a process), so confirm the effect is present before "
            "concluding the fault is in effect.\n"
        )

    def recover_layer2_context(
        self,
        state: dict,
        layer1,
        *,
        is_deterministic: bool,
        experiment_uid: str,
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

    async def recover(
        self, state: dict, handle: Optional[dict], **kwargs
    ) -> RecoverResult:
        """No-LLM verdict for the host-native / no-UID case (mirrors
        :meth:`K8sNativeProvider.recover`).

        There is no ``experiment_uid`` experiment to destroy and no code-side reverse
        command (the reverse lives in the skill case, executed by the LLM recover
        loop). Without an LLM this cannot be verified — report
        ``skipped``/unrecovered with the historical warning.
        """
        from chaos_agent.agent.result.verdict import (
            FailureCategory,
            Layer1Result,
            layer1_to_dict,
        )

        # UID-less carrier: the native handle carries no experiment uid —
        # the handle is the single identity source since phase-6 (the
        # legacy ``kwargs['blade_uid']`` echo was retired), so the
        # experiment_uid renders empty here by design.
        experiment_uid = str((handle or {}).get("value") or "")
        layer1 = Layer1Result(
            status="skipped",
            details="Host-native fault (no experiment_uid), Layer 1 recovery not applicable",
        )
        return RecoverResult(
            recovered=False,
            level="unrecovered",
            layer1=layer1_to_dict(layer1),
            layer2={
                "status": "skipped",
                "details": "No LLM available for host recovery verification",
            },
            warnings=(
                "Host-native fault: Layer 1 not applicable, Layer 2 skipped (no LLM). "
                "Recovery could NOT be verified — the fault may still be active.",
            ),
            experiment_uid=experiment_uid,
            handle=handle,
            failure=(
                FailureCategory.RECOVERY_FAILED,
                f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}",
            ),
        )

    def prompt_fragments(self) -> ProviderPrompts:
        return ProviderPrompts()


# ---------------------------------------------------------------------------
# Step self-check vocabulary (phase-8 Form B) — moved from the generic
# execute node's ``_injection_detection`` (the read-through family): the
# binary vocabulary was always THIS class's; the matching loops now live
# here too.
# ---------------------------------------------------------------------------


def _required_host_binaries(steps: list[str]) -> dict[str, str]:
    """REQUIRED host injection binaries mentioned in the drill steps.

    Word-boundary match against ``HostShellProvider.injection_binaries`` so
    a short binary (``dd`` / ``ip`` / ``cp``) does not false-match inside
    another word (``add`` / ``script``). High tolerance = avoid false
    requirements.
    """
    bins = HostShellProvider.injection_binaries
    required: dict[str, str] = {}
    for step in steps:
        first = step.split('\n')[0].strip()
        lower = step.lower()
        for b in bins:
            if re.search(rf"\b{re.escape(b)}\b", lower):
                required.setdefault(b, first)
    return required


def _executed_host_binaries(messages: list) -> set[str]:
    """host_inject command binaries ATTEMPTED (high tolerance, same rule)."""
    lookup = build_tool_call_args_lookup(messages)
    bins = HostShellProvider.injection_binaries
    executed: set[str] = set()
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") not in HostShellProvider.inject_tool_names:
            continue
        if not reached_target(msg.content):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        cmd = str((lookup.get(tc_id) or {}).get("command", "") or "").lower()
        for b in bins:
            if re.search(rf"\b{re.escape(b)}\b", cmd):
                executed.add(b)
    return executed


__all__ = ["HostShellProvider"]
