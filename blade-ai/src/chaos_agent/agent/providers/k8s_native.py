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

from typing import TYPE_CHECKING, Optional

from chaos_agent.agent.providers.base import ProviderPrompts, RecoverResult
from chaos_agent.transports import PROFILE_K8S

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from chaos_agent.agent.result.verdict import Layer1Result


# kubectl exec/debug inner-command read/mutate judgement is delegated to the
# shared read-only classifier (``tools.readonly``) so detect() attribution, the
# guard-scope classifier, and ``host_read`` all share ONE vocabulary. exec/debug
# default to MUTATING (fail-safe): only a command whose every pipeline stage is
# a known read-only probe is excluded. Dual-use tools (iptables / ip / tc /
# systemctl / mount / dmesg) are judged at the ARGUMENT level there (``iptables
# -L`` read, ``iptables -A`` mutating), so a novel injection shape (shell CPU
# loop, ``/etc/hosts`` edit, ``dmsetup``, ``nc`` listener, ``dmesg -C``) is
# attributed by default rather than slipping through as "not an injection".


def _is_readonly_exec_probe(v_args: str) -> bool:
    """True if a ``kubectl exec``/``debug`` inner command is a read-only probe.

    Thin delegate to
    :func:`chaos_agent.tools.readonly.is_readonly_kubectl_exec` — the single
    source of truth shared with the guard-scope classifier and ``host_read``.
    """
    from chaos_agent.tools.readonly import is_readonly_kubectl_exec

    return is_readonly_kubectl_exec(v_args)


def _exec_inner_command_mutates(v_args: str) -> bool:
    """True if a ``kubectl exec``/``debug`` inner command mutates state (an
    injection), False for a read-only probe.

    Fail-safe attribution: exec/debug default to MUTATING; only the shared
    read-only vocabulary in :func:`_is_readonly_exec_probe` is excluded. This
    inverts the former fault-family blacklist, which silently missed shell CPU
    loops (``while true``), ``/etc/hosts`` edits, ``dmsetup`` IO-error maps and
    ``nc`` port listeners — all real skill-case injections.
    """
    return not _is_readonly_exec_probe(v_args)


class K8sNativeProvider:
    """kubectl-native backend (config-mutation faults; kubectl-reverse recovery)."""

    carrier = "k8s_native"
    injection_methods = ("kubectl_native",)
    has_experiment_uid = False
    is_multi_step = True
    inject_tool_names = frozenset()
    # kubectl write subcommands that, run successfully after a blade_create
    # attempt, mark a kubectl-native injection. Single source of truth: the
    # execute node's ``_KUBECTL_INJECT_SUBCOMMANDS`` reads this.
    # Invariant (test_kubectl_verb_consistency): this set must stay a subset of
    # ``classifier.DESTRUCTIVE_KUBECTL_SUBS`` so every injection verb is also
    # classified destructive by the target guard.
    inject_kubectl_subcommands = frozenset({
        "scale", "patch", "cordon", "taint", "set", "delete", "drain", "label",
    })
    # kubectl subcommands that ENTER a pod/host to run a command (command-mode
    # injection: the ChaosBlade-unavailable node fallback runs a fault binary
    # via ``kubectl exec ... chroot /host``). Distinct from the object-write
    # verbs above (those mutate cluster objects; these open a shell) and kept
    # separate so the object-write invariant is untouched. Both shapes count as
    # a kubectl-native injection attempt.
    inject_command_subcommands = frozenset({"exec", "debug"})
    # Intent vocabulary this carrier contributes to the FaultFamily aggregate —
    # the kubectl-native resource/subsystem types and mutation verbs. Single
    # source of the per-carrier vocabulary (family no longer re-declares it).
    supported_targets = (
        "pod", "finalizer", "replicas", "schedule", "pvc",
        "dns", "image", "probe", "volume", "cni", "endpoint",
    )
    supported_actions = (
        "patch", "cordon", "taint", "delete", "drain", "scale",
        "corrupt", "duplicate",
    )
    # Binaries this backend runs, contributed to the tool guard's Gate-① binary
    # whitelist: ``kubectl`` (config-mutation injection + reverse recovery) and
    # ``wiz`` (cluster inspection). Per-subcommand narrowing stays in the guard
    # (KUBECTL_ALLOWED_SUBCOMMANDS) — this only admits the binary itself.
    injection_binaries = frozenset({"kubectl", "wiz"})
    # Action vocabulary for the multi-step injection step self-check (high
    # tolerance): ``step_kubectl_verbs`` are the kubectl write verbs that may
    # appear in a skill case 演练步骤; ``chinese_verb_map`` maps Chinese step
    # phrases to their kubectl verb. The self-check compares these REQUIRED
    # tokens against verbs actually attempted, and only softly reminds when a
    # token looks un-performed. (Includes step-only verbs like ``uncordon`` /
    # ``annotate`` used purely for parsing; distinct from the injection carrier
    # set ``inject_kubectl_subcommands``.)
    step_kubectl_verbs = frozenset({
        "cordon", "uncordon", "taint", "delete", "scale", "patch",
        "drain", "label", "annotate",
    })
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

    def detect(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> Optional[str]:
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
        :func:`_exec_inner_command_mutates`, which defaults exec/debug to a
        MUTATING injection and excludes only a bounded read-only inspection
        vocabulary (cat/ls/df/ps/wget/nslookup/tc show/iptables -L ...). A
        read-only ``exec ... cat`` → not attributed, and is never mis-routed to
        the kubectl-native Layer 1 / recover backend; a novel injection shape
        (shell CPU loop, /etc/hosts edit, dmsetup, nc listener) is attributed
        by default. Object-write verbs (scale/patch/...) are mutations by
        definition and need no inspection.

        ``is_host`` guards the seam so a host channel never resolves here; the
        registry already scopes candidates by channel, this is defence in
        depth. Note: this NO LONGER bails on a non-empty ``blade_uid`` — a
        failed blade attempt that fell back to kubectl-native still leaves a
        (possibly stale) UID, so ownership is decided by RECENCY at the
        registry (:meth:`injection_recency`), not by the mere presence of a
        blade UID. This provider simply reports whether a native mutation was
        attempted; the registry attributes the LAST successful injection."""
        if is_host:
            return None
        from chaos_agent.agent.providers._detection import (
            scan_kubectl_mutation_index,
        )

        idx = scan_kubectl_mutation_index(
            messages,
            self.inject_kubectl_subcommands,
            command_subcommands=self.inject_command_subcommands,
            is_mutating_command=_exec_inner_command_mutates,
        )
        return "kubectl_native" if idx >= 0 else None

    def injection_recency(
        self, messages: list, blade_uid: str | None, *, is_host: bool
    ) -> int:
        """Message index of the latest kubectl-native mutation, or ``-1``."""
        if is_host:
            return -1
        from chaos_agent.agent.providers._detection import (
            scan_kubectl_mutation_index,
        )

        return scan_kubectl_mutation_index(
            messages,
            self.inject_kubectl_subcommands,
            command_subcommands=self.inject_command_subcommands,
            is_mutating_command=_exec_inner_command_mutates,
        )

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """No ChaosBlade experiment exists for a kubectl-native fault, so there is
        no tool-level status to poll — Layer 1 is not applicable."""
        from chaos_agent.agent.result.verdict import Layer1Result

        return Layer1Result(
            status="skipped",
            details="kubectl-native injection (no blade experiment), Layer 1 not applicable",
        )

    def verify_prompt_note(
        self, injection_method: str, *, injection_pod_name: str | None = None
    ) -> str:
        """Post-injection verifier note for the kubectl-native carrier."""
        if injection_method != "kubectl_native":
            return ""
        return (
            "\n### Injection Method Note\n"
            "Injection was performed via kubectl-native operations (no ChaosBlade). "
            "Verify the configuration change directly via kubectl.\n\n"
            "**NOTE**: Some minimal container images lack common shell utilities (top, ps, netstat, etc.). "
            "If kubectl(subcommand='exec', ...) returns empty output or \"command not found\", do NOT retry — "
            "use kubectl(subcommand='describe', ...) instead.\n"
        )

    def recover_layer2_context(
        self, state: dict, layer1, *, is_deterministic: bool, blade_uid: str,
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
                "Verify the fault effect has been removed using kubectl tools.\n"
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
            "Use kubectl only to CHECK status, not to modify resources. "
            "Output RECOVERY_VERIFICATION_RESULT format, NOT RECOVERY_EXECUTION_RESULT.\n"
        )
        return layer1_context, layer2_instruction

    async def recover(self, state: dict, handle: Optional[dict], **kwargs) -> RecoverResult:
        """Deterministic no-LLM verdict for the non-ChaosBlade / no-UID case.

        Without a ``blade_uid`` there is no experiment to destroy and no LLM to
        run the reverse kubectl operation, so recovery cannot be verified —
        report ``skipped``/unrecovered with the historical warning.
        """
        from chaos_agent.agent.nodes.recover._recover_layer1 import _recover_layer1_to_dict
        from chaos_agent.agent.result.verdict import FailureCategory, Layer1Result

        blade_uid = kwargs.get("blade_uid", "") or ""
        layer1 = Layer1Result(
            status="skipped",
            details="Non-ChaosBlade fault (no blade_uid), Layer 1 recovery not applicable",
        )
        return RecoverResult(
            recovered=False, level="unrecovered",
            layer1=_recover_layer1_to_dict(layer1),
            layer2={"status": "skipped", "details": "No LLM available for specific verification"},
            warnings=(
                "Non-ChaosBlade fault: Layer 1 not applicable, Layer 2 skipped (no LLM). "
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


__all__ = ["K8sNativeProvider"]
