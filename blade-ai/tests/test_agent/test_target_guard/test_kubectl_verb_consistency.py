"""Cross-source consistency invariant for kubectl verb knowledge (V5.1).

Three kubectl subcommand sets live in different modules on purpose, each a
*distinct* policy:

  - ``classifier.DESTRUCTIVE_KUBECTL_SUBS`` — the target-guard safety
    classification (which verbs are destructive).
  - ``K8sNativeProvider.inject_kubectl_subcommands`` — the provider's
    injection-detection vocabulary (which verbs, run after blade_create, mark a
    kubectl-native injection).
  - ``tools.guard.KUBECTL_ALLOWED_SUBCOMMANDS`` — the execution gate (which
    verbs the tool layer will run).

Their members differ intentionally, so we do NOT merge them. But two safety
invariants must hold:

  1. any verb the provider treats as an injection carrier must also be
     classified destructive by the guard, or a newly added injection verb
     could slip past destructive classification;
  2. the execution gate must ADMIT every verb the provider declares — as an
     injection carrier or as a drill-step action. A verb declared in one place
     and refused by the gate is unexecutable-by-construction: the step can
     never be satisfied, and the multi-step self-check keeps asking the model
     to redo an action the guard rejects again. This is exactly what happened
     with ``label``/``drain``/``annotate``: declared by the provider, published
     to the LLM through ``INTENT_ACTIONS``, classified by the target guard —
     and rejected at execution with "kubectl subcommand not allowed".

These tests lock both relations without touching production logic.
"""

from chaos_agent.agent.providers.k8s_native import K8sNativeProvider
from chaos_agent.agent.target_guard.classifier import DESTRUCTIVE_KUBECTL_SUBS
from chaos_agent.tools.guard import ToolGuard


def test_inject_subcommands_are_all_destructive():
    """inject_kubectl_subcommands ⊆ DESTRUCTIVE_KUBECTL_SUBS."""
    inject = K8sNativeProvider.inject_kubectl_subcommands
    missing = inject - DESTRUCTIVE_KUBECTL_SUBS
    assert not missing, (
        "kubectl injection verbs not classified destructive by the guard: "
        f"{sorted(missing)}. Add them to classifier.DESTRUCTIVE_KUBECTL_SUBS "
        "so no injection carrier bypasses destructive classification."
    )


def test_inject_subcommands_are_executable():
    """inject_kubectl_subcommands ⊆ KUBECTL_ALLOWED_SUBCOMMANDS."""
    inject = K8sNativeProvider.inject_kubectl_subcommands
    blocked = inject - ToolGuard.KUBECTL_ALLOWED_SUBCOMMANDS
    assert not blocked, (
        "kubectl injection verbs the execution gate refuses: "
        f"{sorted(blocked)}. Either admit them in "
        "ToolGuard.KUBECTL_ALLOWED_SUBCOMMANDS (with a per-verb guard when the "
        "blast radius needs narrowing) or drop them from the provider — a verb "
        "declared as an injection carrier but never runnable makes the drill "
        "step impossible to satisfy."
    )


def test_step_verbs_are_executable():
    """step_kubectl_verbs ⊆ KUBECTL_ALLOWED_SUBCOMMANDS.

    The self-check reports a step's verb as "possibly not performed" until it
    observes that verb reaching the cluster. A verb the gate rejects can never
    be observed, so the reminder repeats every loop and pushes the model into
    retrying a call that is refused again.
    """
    steps = K8sNativeProvider.step_kubectl_verbs
    blocked = steps - ToolGuard.KUBECTL_ALLOWED_SUBCOMMANDS
    assert not blocked, (
        "drill-step kubectl verbs the execution gate refuses: "
        f"{sorted(blocked)}. The multi-step self-check would ask for an action "
        "the guard rejects, with no way to ever satisfy it."
    )


def test_step_verbs_superset_of_inject_verbs():
    """``step_kubectl_verbs`` (the multi-step self-check's REQUIRED-token
    vocabulary) includes every injection verb except the config-mutation
    ``set`` verb, which is an injection carrier but has no drill-step phrasing.
    Anchors the documented relationship so drift is caught."""
    inject = K8sNativeProvider.inject_kubectl_subcommands
    steps = K8sNativeProvider.step_kubectl_verbs
    assert inject - steps == {"set"}
