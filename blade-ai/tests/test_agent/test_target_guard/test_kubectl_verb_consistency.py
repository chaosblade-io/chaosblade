"""Cross-source consistency invariant for kubectl verb knowledge (V5.1).

kubectl verb knowledge is spread across several sets in different modules on
purpose, each a *distinct* policy. They form two families — the WRITE/execution
side (three sets, invariants 1–2) and the READ-ONLY side (two sets, invariants
3–4). The write-side trio:

  - ``_k8s_classifier.DESTRUCTIVE_KUBECTL_SUBS`` — the target-guard safety
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

The READ-ONLY surface has the same shape and the same failure mode. Two more
sets describe it:

  - ``tools.kubectl_cli.READONLY_SUBCOMMANDS`` — the Phase 1 tool schema (the
    read verbs we OFFER the LLM while planning; ``exec``/``debug`` ride along
    as read-only probe carriers, gated to non-mutating inner commands).
  - ``_k8s_classifier.READONLY_KUBECTL_SUBS`` — the target-guard read-only
    classification. Membership here is exactly what makes
    ``infer_effective_target`` return ``SCOPE_READONLY``; a verb absent from it
    falls through to the ``unknown kubectl subcommand`` default-deny →
    ``SCOPE_UNKNOWN``.

Again the members differ on purpose (the classifier also recognises read verbs
we never expose in Phase 1, e.g. ``events``/``wait``/``diff``), so we do NOT
merge them. But two more invariants must hold, or the tool PROMISES the LLM a
verb the enforcement layer then refuses:

  3. the execution gate must ADMIT every verb the Phase 1 schema offers
     (``READONLY_SUBCOMMANDS ⊆ KUBECTL_ALLOWED_SUBCOMMANDS``);
  4. the classifier must judge every PLAIN-READ verb the schema offers as
     read-only (``READONLY_SUBCOMMANDS − {exec, debug} ⊆
     READONLY_KUBECTL_SUBS``). ``exec``/``debug`` are exempt: they route to the
     probe-shape branch, not the plain-read set.

Invariant 4 is the one ``kubectl cluster-info`` violated (Case #88 iteration
5): offered by the schema and admitted by the gate, but missing from the
classifier's read-only set → ``SCOPE_UNKNOWN`` → phase1_screener defensively
rejected it AND sibling-skipped the legitimate reads batched with it, burning a
whole planning iteration. The write-side family (1–2) was pinned in V5.1; the
read-side family (3–4) is pinned here so neither surface can drift again.

These tests lock all four relations without touching production logic.
"""

from chaos_agent.agent.providers.k8s_native.classifier import (
    DESTRUCTIVE_KUBECTL_SUBS,
    READONLY_KUBECTL_SUBS,
)
from chaos_agent.agent.providers.k8s_native.provider import K8sNativeProvider
from chaos_agent.tools.guard import ToolGuard
from chaos_agent.tools.kubectl_cli import READONLY_SUBCOMMANDS


def test_inject_subcommands_are_all_destructive():
    """inject_kubectl_subcommands ⊆ DESTRUCTIVE_KUBECTL_SUBS."""
    inject = K8sNativeProvider.inject_kubectl_subcommands
    missing = inject - DESTRUCTIVE_KUBECTL_SUBS
    assert not missing, (
        "kubectl injection verbs not classified destructive by the guard: "
        f"{sorted(missing)}. Add them to _k8s_classifier.DESTRUCTIVE_KUBECTL_SUBS "
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


def test_readonly_schema_subcommands_are_executable():
    """READONLY_SUBCOMMANDS ⊆ KUBECTL_ALLOWED_SUBCOMMANDS (invariant 3).

    A read verb the Phase 1 schema offers the LLM but the execution gate
    refuses is a broken promise: the model plans around a tool call that can
    never run.
    """
    offered = set(READONLY_SUBCOMMANDS)
    blocked = offered - ToolGuard.KUBECTL_ALLOWED_SUBCOMMANDS
    assert not blocked, (
        "kubectl_read offers read subcommands the execution gate refuses: "
        f"{sorted(blocked)}. Admit them in ToolGuard.KUBECTL_ALLOWED_SUBCOMMANDS "
        "or drop them from kubectl_cli.READONLY_SUBCOMMANDS — the Phase 1 schema "
        "must not advertise a verb the tool layer will not run."
    )


def test_readonly_schema_subcommands_are_classifier_readonly():
    """READONLY_SUBCOMMANDS − {exec, debug} ⊆ READONLY_KUBECTL_SUBS (invariant 4).

    Membership in ``READONLY_KUBECTL_SUBS`` is exactly what makes the
    target-guard classifier return ``SCOPE_READONLY``; a plain-read verb the
    schema offers but the classifier does not recognise falls through to the
    ``unknown kubectl subcommand`` default-deny → ``SCOPE_UNKNOWN`` →
    phase1_screener rejects it and sibling-skips the whole batch. This is the
    ``kubectl cluster-info`` drift (Case #88 iteration 5). ``exec``/``debug``
    are exempt: they route to the probe-shape branch, not the plain-read set.
    """
    probe_branch = {"exec", "debug"}
    offered = set(READONLY_SUBCOMMANDS) - probe_branch
    unclassified = offered - READONLY_KUBECTL_SUBS
    assert not unclassified, (
        "kubectl_read offers plain-read subcommands the classifier does NOT treat "
        "as read-only (schema/classifier drift → false SCOPE_UNKNOWN reject + "
        f"phase1 sibling-skip collateral): {sorted(unclassified)}. Add each to "
        "providers/k8s_native/classifier.READONLY_KUBECTL_SUBS."
    )
