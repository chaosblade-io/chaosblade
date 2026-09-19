"""kubectl-native execution backend provider.

Backend semantics: the fault is created with a plain ``kubectl`` write verb
(``scale`` / ``patch`` / ``cordon`` / ``taint`` / ``set`` / ``delete`` /
``drain`` / ``label``) instead of ChaosBlade — the alternative the LLM reaches
for when ``blade_create`` is unavailable or the scenario is inherently a config
mutation (e.g. scale-to-zero, invalid storageClass). There is no experiment UID
and no ``blade destroy``: recovery is the *reverse* kubectl operation, so
Layer-1 has no tool-level status to poll (``skipped``).

This provider fully owns every per-backend behaviour for the kubectl-native
carrier: tool binding (``tools``), detection (``detect``), Layer-1 (``skipped``),
the post-injection verifier note (``verify_prompt_note``), the recover Layer-2
framing (``recover_layer2_context``) and the deterministic no-UID recovery
verdict (``recover``). ``is_multi_step`` is True because a config-mutation
injection may span several kubectl steps with no single completion marker.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional

from langchain_core.messages import ToolMessage

from .declaration import (
    CARRIER_ID,
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
    KUBECTL_COMMAND_SUBCOMMANDS,
    KUBECTL_WRITE_SUBCOMMANDS,
    build_tool_call_args_lookup,
    exec_inner_command_mutates,
    reached_target,
    scan_kubectl_injection_after_blade,
)
from chaos_agent.transports import PROFILE_K8S

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


class K8sNativeProvider:
    """kubectl-native backend (config-mutation faults; kubectl-reverse recovery)."""

    carrier = CARRIER_ID
    injection_methods = ("kubectl_native",)
    has_experiment_uid = False
    # The verdict-default carrier: when fault dispatch exhausts every identity
    # claim (no handle, no message evidence, no attributed method) the
    # evidence-less state routes here — Layer-1 verdict ``skipped`` (the fault
    # effect is checked in Layer 2). Declared as a property so the registry's
    # claim-4 fallback never names this class (phase-7 T1).
    uid_less_verdict_default = True
    handle_kind = "native"
    is_multi_step = True
    # UID-less carrier: no programmatic Layer-1 recovery exists — the
    # LLM-driven undo flow IS the Layer 1.
    has_deterministic_recover = False
    inject_tool_names = frozenset()
    # kubectl write subcommands that, run successfully after a blade_create
    # attempt, mark a kubectl-native injection. Single source of truth lives
    # in the kubectl tool domain (``providers.message_scanning`` since
    # phase-14 G2 — the blade domain's boundary judgements consume the SAME
    # lists, so the physical home is carrier-neutral); this class attribute
    # is the protocol-shape reference (phase-8 T3.5 removed the generic
    # layer's ``_KUBECTL_INJECT_SUBCOMMANDS`` read-through).
    # Invariant (test_kubectl_verb_consistency): this set must stay a subset of
    # ``classifier.DESTRUCTIVE_KUBECTL_SUBS`` so every injection verb is also
    # classified destructive by the target guard.
    #
    # TEARDOWN≠MUTATION CONTRACT (B76 family → P3 closed): every hook on
    # this class that consumes this vocabulary (detect / issue_disproven /
    # injection_recency / scan_step_actions / was_injection_attempted)
    # accepts the ``is_teardown`` matcher and skips registered-vehicle
    # teardown calls at CALL granularity inside the vocabulary scans —
    # the exemption now lives IN this layer (``issue_time_method`` is the
    # one exception: the issue-loop caller applies the call-level skip
    # before classification, R6-1). The agent-side seams thread
    # ``execution_artifacts.make_teardown_matcher`` fresh at each
    # invocation; those obligations are enforced by
    # tests/test_agent/test_teardown_vocab_sentinel.py and pinned by the
    # family teeth (TestIssueTimeTeardownAttribution).
    inject_kubectl_subcommands = KUBECTL_WRITE_SUBCOMMANDS
    # kubectl subcommands that ENTER a pod/host to run a command (command-mode
    # injection: the ChaosBlade-unavailable node fallback runs a fault binary
    # via ``kubectl exec ... chroot /host``). Distinct from the object-write
    # verbs above (those mutate cluster objects; these open a shell) and kept
    # separate so the object-write invariant is untouched. Both shapes count as
    # a kubectl-native injection attempt. Neutral home (phase-14 G2):
    # ``message_scanning.KUBECTL_COMMAND_SUBCOMMANDS``.
    inject_command_subcommands = KUBECTL_COMMAND_SUBCOMMANDS
    # Intent vocabulary this carrier contributes to the FaultFamily aggregate —
    # the kubectl-native resource/subsystem types and mutation verbs. Single
    # source of the per-carrier vocabulary (family no longer re-declares it).
    supported_targets = SUPPORTED_TARGETS
    supported_actions = SUPPORTED_ACTIONS
    # Binaries this backend runs, contributed to the tool guard's Gate-① binary
    # whitelist: ``kubectl`` (config-mutation injection + reverse recovery) and
    # ``wiz`` (cluster inspection). Per-subcommand narrowing stays in the guard
    # (KUBECTL_ALLOWED_SUBCOMMANDS) — this only admits the binary itself.
    injection_binaries = frozenset({"kubectl", "wiz"})
    # No dedicated injection-infrastructure pods: kubectl-native talks to the
    # cluster API directly, so the Tier-1 tool-pod-namespace exemption has
    # nothing to exempt (empty set — the protocol default, made explicit).
    tool_pod_namespaces = frozenset()
    # Phase-7 T2 per-tool pass sets: both kubectl flavours declare
    # ``kubeconfig``; neither takes ``task_id``; only the mutating ``kubectl``
    # surface ships its runs to L4 (reads carry no inject-phase signal).
    kubeconfig_scoped_tool_names = frozenset({"kubectl", "kubectl_read"})
    audit_scoped_tool_names = frozenset()
    log_shipping_tool_names = frozenset({"kubectl"})
    # Create-reconcile gate (D6): kubectl-native mutations are declared
    # intents rendered from the fault spec — a retry re-applies the same
    # approved mutation, and the carrier has no non-idempotent
    # experiment-create whose uncertain outcome could arm the gate (the
    # protocol defaults, made explicit).
    reconcile_create_tool_names = frozenset()
    reconcile_read_tool_names = frozenset()
    # Action vocabulary for the multi-step injection step self-check (high
    # tolerance): ``step_kubectl_verbs`` are the kubectl write verbs that may
    # appear in a skill case 演练步骤; ``chinese_verb_map`` maps Chinese step
    # phrases to their kubectl verb. The self-check compares these REQUIRED
    # tokens against verbs actually attempted, and only softly reminds when a
    # token looks un-performed. (Includes step-only verbs like ``uncordon`` /
    # ``annotate`` used purely for parsing; distinct from the injection carrier
    # set ``inject_kubectl_subcommands``.)
    step_kubectl_verbs = frozenset(
        {
            "cordon",
            "uncordon",
            "taint",
            "delete",
            "scale",
            "patch",
            "drain",
            "label",
            "annotate",
            # restore-only verb (never an injection carrier): the PVC / limits /
            # topology cases restore from a stripped baseline with `replace` —
            # apply's three-way merge keeps injected fields, so it cannot
            # restore exactly. Guard-side KUBECTL_ALLOWED_SUBCOMMANDS admits it
            # to keep the superset invariant (refusing it makes the taught
            # restore step unexecutable).
            "replace",
        }
    )
    # ``chinese_verb_map`` maps a Chinese drill-step phrase to its kubectl verb.
    #
    # SELECTION RULE — a phrase must be HIGHLY SPECIFIC to the ACTION, because
    # it is matched as a SUBSTRING of the whole step (unlike the English verbs,
    # which are matched on word boundaries) and ``_injection_intent_steps`` only
    # filters steps whose FIRST characters are an observation prefix
    # (记录/查看/观察/确认/...). A phrase that also appears when describing an
    # OUTCOME produces a false REQUIRED verb, and the self-check then nudges the
    # model to perform an action the case never asked for — a harmful direction,
    # unlike the harmless under-reporting this vocabulary otherwise biases to.
    #
    # Worked example of what NOT to add: bare "驱逐". In K8s that is the everyday
    # word for an Evicted pod, so memory/disk-pressure cases say "Pod 可能被驱逐"
    # as an expected EFFECT — mid-sentence, not behind an observation prefix.
    # Mapping it to ``drain`` would tell the model to drain the node. The
    # action-bearing forms ("驱逐节点" / "排空节点") carry the object and cannot
    # match that sentence. Same reason bare "标签" and "修改" are absent: label
    # selectors appear in almost every step, and "修改" covers every mutation.
    chinese_verb_map = {
        "删除": "delete",
        "缩容": "scale",
        "扩容": "scale",
        "标记为不可调度": "cordon",
        "取消不可调度": "uncordon",
        "添加污点": "taint",
        "移除污点": "taint",
        # drain — object-bearing forms only (see the rule above).
        "驱逐节点": "drain",
        "排空节点": "drain",
        # label / annotate — the verb must be attached; "移除标签" is preferred
        # over "删除标签" because the latter also matches "删除" → ``delete``,
        # which the agent would have no way to satisfy.
        "打标签": "label",
        "添加标签": "label",
        "移除标签": "label",
        "添加注解": "annotate",
    }
    # Semantic equivalence for the self-check, used in BOTH directions so a step
    # and its execution match however each was written:
    #   forward  — ``patch`` writing one of these fields credits the dedicated
    #              verb (``patch metadata.labels`` ≡ ``label``);
    #   reverse  — executing a dedicated verb credits ``patch``, since each of
    #              these verbs IS a field patch.
    # Without this, a step spelled ``kubectl label node ...`` looks un-performed
    # when achieved via ``patch -p '{"metadata":{"labels":...}}'`` (and vice
    # versa). Field-name substring match only: over-crediting merely shrinks the
    # "missing" set, matching this vocabulary's high-tolerance / under-report
    # bias. Values are tuples because one field may back several verbs.
    patch_equivalent_verbs = {
        "labels": ("label",),
        "annotations": ("annotate",),
        "taints": ("taint",),
        "replicas": ("scale",),
        # spec.unschedulable is the field BOTH cordon and uncordon write.
        "unschedulable": ("cordon", "uncordon"),
    }

    def matches_channel(self, profile: str) -> bool:
        # kubectl-native faults are cluster-only.
        return profile == PROFILE_K8S

    def required_params(self, scope: str) -> list[str]:
        from chaos_agent.agent.spec.fault_registry import required_intent_params

        return required_intent_params(scope)

    def tools(self, phase: str) -> list["BaseTool"]:
        """kubectl-native tools contributed to the factory tool union per phase.

        - PLAN / VERIFY → ``kubectl_read`` only — the single read-only tool for
          every read-only phase (get/describe/top/logs + read-only exec/debug).
          The full ``kubectl`` write surface is intentionally ABSENT here: it
          was the bypass vector where a planner ran ``kubectl exec ... blade
          create`` past the confirmation gate. ``kubectl_read``'s ``Literal``
          subcommand constraint + read-only exec gating make that impossible.
        - EXECUTE → full ``kubectl`` (the config-mutation injection carrier).
        - RECOVER_VERIFY → full ``kubectl`` only. It runs the reverse operation
          AND is the superset of ``kubectl_read`` (it accepts every read verb +
          exec), so no separate read tool is bound.
        """
        from chaos_agent.agent.providers.base import (
            EXECUTE,
            PLAN,
            RECOVER_VERIFY,
            VERIFY,
        )
        from chaos_agent.tools import kubectl, kubectl_read

        if phase == PLAN:
            return [kubectl_read]
        if phase == EXECUTE:
            return [kubectl]
        if phase == VERIFY:
            return [kubectl_read]
        if phase == RECOVER_VERIFY:
            return [kubectl]
        return []

    def detect(self, messages: list, *, is_host: bool, is_teardown=None) -> Optional[str]:
        """Classify as ``kubectl_native`` when, on a cluster channel with no
        experiment UID, a mutating kubectl call was ATTEMPTED.

        On k8s, non-ChaosBlade == kubectl-native (there is no blade experiment,
        so Layer 1 is not applicable). Attribution is by the injection ATTEMPT
        (AIMessage tool_calls), NOT the tool result: the ChaosBlade-unavailable
        node fallback runs e.g. a network DROP via ``kubectl exec ... chroot
        /host iptables``, whose own exec connection is severed by the very fault
        it injects — a result scan would miss it (the forensic paradox).

        Command-mode ``exec``/``debug`` are only attributed when their inner
        command actually mutates: the read/mutate judgement is delegated to
        :func:`exec_inner_command_mutates`, which defaults exec/debug to a
        MUTATING injection and excludes only a bounded read-only inspection
        vocabulary (cat/ls/df/ps/wget/nslookup/tc show/iptables -L ...). A
        read-only ``exec ... cat`` → not attributed, and is never mis-routed to
        the kubectl-native Layer 1 / recover backend; a novel injection shape
        (shell CPU loop, /etc/hosts edit, dmsetup, nc listener) is attributed
        by default. Object-write verbs (scale/patch/...) are mutations by
        definition and need no inspection.

        ``is_host`` guards the seam so a host channel never resolves here; the
        registry already scopes candidates by channel, this is defence in
        depth. Note: this NO LONGER bails on a non-empty ``experiment_uid`` — a
        failed blade attempt that fell back to kubectl-native still leaves a
        (possibly stale) UID, so ownership is decided by RECENCY at the
        registry (:meth:`injection_recency`), not by the mere presence of a
        blade UID. This provider simply reports whether a native mutation was
        attempted; the registry attributes the LAST successful injection."""
        if is_host:
            return None
        from chaos_agent.agent.providers.message_scanning import (
            scan_kubectl_mutation_index,
        )

        idx = scan_kubectl_mutation_index(
            messages,
            self.inject_kubectl_subcommands,
            command_subcommands=self.inject_command_subcommands,
            is_mutating_command=exec_inner_command_mutates,
            is_teardown=is_teardown,
        )
        return "kubectl_native" if idx >= 0 else None

    def issue_disproven(self, messages: list, *, is_teardown=None) -> bool:
        """Counter-evidence for an issue-time attribution: the MOST RECENT
        object-write attempt came back as ``Error:`` — the API server proves
        the write never landed, so the attribution never committed — and NO
        earlier object-write in the epoch LANDED: a successful write already
        confirms the attribution (a mutation is live on the cluster), and a
        later failure never revokes it.

        Command-mode (exec/debug) attempts are deliberately NOT judgeable
        here: their error results may be the injected fault severing its own
        exec channel (the forensic paradox), so absence of a positive
        object-write verdict is never counter-evidence."""
        from chaos_agent.agent.providers.message_scanning import (
            scan_native_issue_disproven,
        )
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        return scan_native_issue_disproven(
            messages,
            self.inject_kubectl_subcommands,
            command_subcommands=self.inject_command_subcommands,
            is_mutating_command=exec_inner_command_mutates,
            is_blade_create_delivery=(
                FaultProviderRegistry.is_blade_exec_create_delivery
            ),
            is_teardown=is_teardown,
        )

    def injection_recency(
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> int:
        """Message index of the latest kubectl-native mutation, or ``-1``."""
        if is_host:
            return -1
        from chaos_agent.agent.providers.message_scanning import (
            scan_kubectl_mutation_index,
        )

        return scan_kubectl_mutation_index(
            messages,
            self.inject_kubectl_subcommands,
            command_subcommands=self.inject_command_subcommands,
            is_mutating_command=exec_inner_command_mutates,
            is_teardown=is_teardown,
        )

    def build_fault_handle(self, values: dict) -> Optional[dict]:
        """Claim a committed kubectl-native injection: no experiment UID exists
        for this carrier, so the attributed method IS the handle fact."""
        values = values or {}
        if values.get("injection_method") != "kubectl_native":
            return None
        return {"kind": "native", "method": "kubectl_native"}

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
        """UID-less carrier: no experiment ids exist to prove. A kubectl-native
        fault is undone by reversing the mutation, never by a destroy call, so
        the provenance gate has nothing to admit here."""
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
    ) -> Optional["EffectiveTarget"]:
        """Guard-side classification of this carrier's tools (phase-7 T5).

        Claims the ``kubectl`` / ``kubectl_read`` tools. The kubectl
        classifier family lives in this package (``_k8s_classifier.py``,
        migrated from ``target_guard/classifier.py``); the embedded
        ``kubectl exec ... blade create`` delivery is routed from there to
        the ChaosBlade carrier's inline-blade parser through the registry's
        domain-routing seam (phase-14 G3 — no cross-carrier import).
        """
        if tool_name not in ("kubectl", "kubectl_read"):
            return None
        # Lazy import — see the NOTE near the top of this file.
        from .classifier import (
            _classify_kubectl,
            _coerce_args_list,
        )

        raw_args = (
            coerce_tool_args_dict(tool_args) if isinstance(tool_args, dict) else None
        )
        return _classify_kubectl(
            _coerce_args_list(tool_args),
            raw_command,
            raw_args=raw_args,
        )

    def parse_injection_params(self, tool_name: str, tool_args: dict) -> Optional[dict]:
        """No issue-time key-parameter extraction for the native surface: the
        mutations it issues (patch/scale/…) carry their parameters in the
        neutral fault spec, which the verifier already renders."""
        return None

    def issue_time_method(
        self, tool_name: str, tool_args: dict, *, is_host: bool = False
    ) -> Optional[str]:
        """Issue-time attribution for the kubectl-native carrier: an
        object-write verb IS the mutation (``kubectl_native``); a command-mode
        ``exec``/``debug`` only when the inner command mutates (the shared
        fail-safe classifier). An embedded ChaosBlade delivery
        (``kubectl exec ... blade create``) is claimed EARLIER by the
        ChaosBlade provider via registration order, so it never reaches this
        hook. Migrated from the execute-side classifier's hardcoded branch
        (phase-7 T4)."""
        if tool_name != "kubectl":
            return None
        subcommand = tool_args.get("subcommand", "")
        v_args = tool_args.get("v_args", "") or ""
        if subcommand in self.inject_kubectl_subcommands:
            return "kubectl_native"
        if (
            subcommand in self.inject_command_subcommands
            and isinstance(v_args, str)
            and exec_inner_command_mutates(v_args)
        ):
            return "kubectl_native"
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

    async def verify_landing_readback(
        self, messages: list, state: dict, *, kubeconfig: str = ""
    ) -> Optional[dict]:
        """Landing readback guard (faultdrill-cr-channel task 2.1, design
        D5): this carrier's landings carry no CR recipe-integrity contract
        to verify — pinned ``None`` (the registry scan continues; the
        faultdrill channel's D5 seam is the only owner of the post-apply
        readback)."""
        return None

    async def rollback_handle(self, handle: dict, **kwargs) -> str:
        """Kubectl-native faults are undone by reversing the mutation in the
        recover graph, not by a synchronous failure-path rollback."""
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
        conformance suite pins. ``is_teardown`` is accepted for protocol
        uniformity (P3) and ignored: the judgement never consults the
        message history."""
        return False

    def scan_step_actions(
        self, steps: list[str], messages: list, *, is_teardown=None,
    ) -> Optional[StepActionScan]:
        """Form B hook (phase-8 T3): THIS backend's step vocabulary — kubectl
        write verbs (English tokens + the Chinese verb map) for the required
        side, attempted kubectl inject subcommands (``patch`` ↔ dedicated
        verb credited both ways) for the executed side. The ``is_teardown``
        matcher (P3) is threaded into the executed-side scan so a
        registered-vehicle teardown receipt credits no step verb — at call
        granularity, mixed batches included."""
        return StepActionScan(
            required=_required_kubectl_verbs(steps),
            executed=_executed_kubectl_verbs(messages, is_teardown=is_teardown),
        )

    def was_injection_attempted(self, messages: list, *, is_teardown=None) -> bool:
        """Form B hook (phase-8 T3): back-scan for a kubectl-native
        alternative injection after a ``blade_create`` attempt —
        object-write verbs, or exec/debug whose inner command mutates.
        Delegates to the shared scan primitive with THIS class's
        vocabulary (formerly the generic layer's read-through wrapper
        ``_was_kubectl_injection_attempted``)."""
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        return scan_kubectl_injection_after_blade(
            messages,
            self.inject_kubectl_subcommands,
            command_subcommands=self.inject_command_subcommands,
            is_mutating_command=exec_inner_command_mutates,
            is_blade_create_delivery=(
                FaultProviderRegistry.is_blade_exec_create_delivery
            ),
            is_teardown=is_teardown,
        )

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """No ChaosBlade experiment exists for a kubectl-native fault, so there is
        no tool-level status to poll — Layer 1 is not applicable."""
        from chaos_agent.agent.result.verdict import Layer1Result

        return Layer1Result(
            status="skipped",
            details="kubectl-native injection (no blade experiment), Layer 1 not applicable",
        )

    async def layer1_raw_destroy(self, uid: str, kubeconfig: str = "") -> str:
        """No bare destroy exists for a kubectl-native fault (nothing to
        destroy programmatically); the finalize retry never routes here."""
        return ""

    def classify_destroy_output(self, output: str) -> DestroyOutcome:
        """UID-less carrier: no destroy output exists to classify — the
        empty string this carrier returns is FAILED under the authority
        anyway; pinned explicitly so the protocol stays satisfied."""
        return DestroyOutcome.FAILED

    async def layer1_destroy(
        self,
        uid: str,
        kubeconfig: str = "",
        *,
        messages: list | None = None,
        injection_method: str | None = None,
    ) -> "Layer1Result":
        """No deterministic Layer-1 recovery exists for a kubectl-native fault:
        the mutation has no experiment to destroy — the LLM flow's undo IS
        Layer 1. Explicit ``skipped`` for protocol completeness."""
        from chaos_agent.agent.result.verdict import Layer1Result

        return Layer1Result(
            status="skipped",
            details="kubectl-native injection (no experiment to destroy), Layer 1 destroy not applicable",
        )

    def recovery_vehicle(self, state: dict) -> str:
        """No durable recovery-vehicle record on this carrier (the mutation ran
        through the agent's own kubectl) — nothing to render."""
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
        """Post-injection verifier note for the kubectl-native carrier."""
        if injection_method != "kubectl_native":
            return ""
        return (
            "\n### Injection Method Note\n"
            "Injection was performed via kubectl-native operations (no ChaosBlade). "
            "Verify the configuration change directly via the bound cluster query tools.\n\n"
            "**NOTE**: Some minimal container images lack common shell utilities (top, ps, netstat, etc.). "
            'If a container exec check returns empty output or "command not found", do NOT retry — '
            "use a cluster API describe-style check instead.\n"
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
        """Recover Layer-2 framing for a non-ChaosBlade (kubectl-native) fault."""
        if layer1.status == "skipped":
            layer1_context = (
                "## Layer 1 Result\n"
                "Layer 1 skipped: non-ChaosBlade fault with no recovery actions in skill files. "
                "Proceed directly to Layer 2 recovery verification.\n\n"
            )
            layer2_instruction = (
                "This is a non-ChaosBlade fault recovery. "
                "Verify the fault effect has been removed using the bound cluster query tools.\n"
            )
            return layer1_context, layer2_instruction

        layer1_context = (
            f"## Layer 1 Result (Recovery Execution)\n"
            f"This is a non-ChaosBlade fault. Recovery actions executed: {layer1.status}\n"
            f"Details: {layer1.details}\n\n"
        )
        layer2_instruction = (
            "PHASE TRANSITION: Layer 1 (recovery execution) is COMPLETE. "
            "You are now in Layer 2 (VERIFICATION). "
            "DO NOT execute more recovery actions — only VERIFY the fault effect is removed. "
            "Use the bound cluster query tools only to CHECK status, not to modify resources. "
            "Output RECOVERY_VERIFICATION_RESULT format, NOT RECOVERY_EXECUTION_RESULT.\n"
        )
        return layer1_context, layer2_instruction

    async def recover(
        self, state: dict, handle: Optional[dict], **kwargs
    ) -> RecoverResult:
        """Deterministic no-LLM verdict for the non-ChaosBlade / no-UID case.

        Without a ``experiment_uid`` there is no experiment to destroy and no LLM to
        run the reverse kubectl operation, so recovery cannot be verified —
        report ``skipped``/unrecovered with the historical warning.
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
            details="Non-ChaosBlade fault (no experiment_uid), Layer 1 recovery not applicable",
        )
        return RecoverResult(
            recovered=False,
            level="unrecovered",
            layer1=layer1_to_dict(layer1),
            layer2={
                "status": "skipped",
                "details": "No LLM available for specific verification",
            },
            warnings=(
                "Non-ChaosBlade fault: Layer 1 not applicable, Layer 2 skipped (no LLM). "
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
# token vocabulary was always THIS class's; the matching loops now live
# here too.
# ---------------------------------------------------------------------------


# English verbs count as REQUIRED only in COMMAND POSITION: the verb is the
# subcommand of a kubectl invocation on the step line (``kubectl [--flags]
# <verb>``). A bare prose mention is NOT an action-bearing form — counting it
# manufactures a REQUIRED verb and the soft self-check then nudges the model
# toward an out-of-scope mutation the case never asked for (task
# inject-65a44501: a step's reasoning citation "仅放行节点自操作（uncordon
# 族，#30 实测立法）" flagged ``uncordon`` as possibly-not-performed; the model
# burned an execute iteration justifying the refusal). This is the
# English-side mirror of ``chinese_verb_map``'s SELECTION RULE, and it restores
# symmetry with the executed side (which reads the ``subcommand`` field of
# issued kubectl calls). Under-anchoring a genuine bare-prose action merely
# skips the SOFT reminder — the documented under-report bias, not a regression.
_KUBECTL_INVOCATION_TMPL = (
    r"kubectl(?:\s+(?:--?[^\s`]+|\"[^\"]*\"|'[^']*'))*\s+{verb}\b"
)


def _required_kubectl_verbs(steps: list[str]) -> dict[str, str]:
    """REQUIRED kubectl write verbs mentioned in the drill steps (token -> step).

    English verbs are recognised only at a kubectl invocation's subcommand
    position (``kubectl [--flags] <verb>``, incl. quote-wrapped payload forms
    like ``sh -c 'kubectl --kubeconfig=... uncordon <node>'``); Chinese
    phrases go through ``chinese_verb_map`` unchanged. Bare prose mentions of
    an English verb (reasoning citations, outcome descriptions) are not
    action-bearing and are ignored — see ``_KUBECTL_INVOCATION_TMPL``.
    """
    verbs = K8sNativeProvider.step_kubectl_verbs
    cmap = K8sNativeProvider.chinese_verb_map
    required: dict[str, str] = {}
    for step in steps:
        first = step.split('\n')[0].strip()
        lower = step.lower()
        for v in verbs:
            if re.search(
                _KUBECTL_INVOCATION_TMPL.format(verb=re.escape(v)), lower
            ):
                required.setdefault(v, first)
        for cn, en in cmap.items():
            if cn in step:
                required.setdefault(en, first)
    return required


def _patch_equivalent_verbs(v_args: str) -> set[str]:
    """Dedicated verbs a ``kubectl patch`` is semantically equivalent to.

    A drill step may name the dedicated verb (``kubectl label node ...``)
    while the agent achieves the identical mutation via
    ``kubectl patch node -p '{...}'``. Crediting both keeps the self-check
    from flagging an action that WAS performed. Loose field-name match by
    design: over-crediting only shrinks the missing set, matching the
    high-tolerance / under-report bias.
    """
    lower = v_args.lower()
    return {
        verb
        for field, verbs in K8sNativeProvider.patch_equivalent_verbs.items()
        if field in lower
        for verb in verbs
    }


def _patch_expressible_verbs() -> frozenset[str]:
    """Dedicated verbs that ARE a field patch — executing one credits ``patch``.

    The mirror of :func:`_patch_equivalent_verbs`: a step may be spelled
    ``kubectl patch ...`` while the agent reaches the same state with the
    dedicated verb. Derived from the SAME provider table so both directions
    stay in sync from one source of truth.
    """
    return frozenset(
        verb
        for verbs in K8sNativeProvider.patch_equivalent_verbs.values()
        for verb in verbs
    )


def _executed_kubectl_verbs(messages: list, *, is_teardown=None) -> set[str]:
    """kubectl inject verbs ATTEMPTED (high tolerance: reached-cluster counts,
    incl. timeout / non-zero exit; only pre-exec rejections are excluded).

    Credits ``patch`` ↔ dedicated verb in both directions, so a step
    documented as ``label`` / ``taint`` / ``scale`` is not reported missing
    when carried out via ``patch`` (and a step documented as ``patch`` is
    not reported missing when carried out with the dedicated verb).

    Teardown≠step-credit (O-3, P3 call-granular): a registered-vehicle
    teardown delete's receipt credits NO verb when the ``is_teardown``
    matcher is threaded — the documented ``delete`` step stays honestly
    "not yet performed" even inside a mixed batch.
    """
    lookup = build_tool_call_args_lookup(messages)
    executed: set[str] = set()
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue
        if not reached_target(msg.content):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        args = lookup.get(tc_id) or {}
        if is_teardown is not None and is_teardown("kubectl", args):
            continue
        sub = args.get("subcommand", "")
        if sub in K8sNativeProvider.inject_kubectl_subcommands:
            executed.add(sub)
            if sub == "patch":
                executed |= _patch_equivalent_verbs(
                    str(args.get("v_args", "") or "")
                )
            elif sub in _patch_expressible_verbs():
                executed.add("patch")
    return executed


__all__ = ["K8sNativeProvider"]
