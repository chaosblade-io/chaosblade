"""FaultProvider registry.

New fault execution backends are added by:
1. Implementing the :class:`~chaos_agent.agent.providers.base.FaultProvider`
   protocol.
2. Calling ``FaultProviderRegistry.register(MyProvider())``.
3. Declaring the matching :class:`FaultFamily` (so its ``carrier_types`` list
   includes the provider's ``carrier``) in ``fault_registry.py``.

No existing chokepoint (factory tool union, injection detection, Layer 1,
recovery, intent completeness) needs to change — each resolves the active
provider through this registry instead of a hardcoded ``if injection_method``.

Two resolution modes
---------------------
- ``resolve_by_method`` — POST-injection. The concrete ``injection_method`` is
  known (detected from message history), so the exact backend is resolvable for
  Layer-1 verification and recovery.
- ``resolve_by_scope`` — PRE-injection. Only the intent scope is known; the
  registry bridges scope → ``FaultFamily.carrier_types`` → the candidate
  providers (ordered by precedence) so intent completeness / prompt fragments
  can consult the likely backends. ``resolve_primary_by_scope`` returns the
  single most-likely one.
- ``applicable`` — PRE-injection, channel-based. Which backends can operate
  against a "k8s"/"host" profile (for prompt fragments / required-params union).
- ``all_providers`` — build-time. The factory unions every provider's tools per
  phase (tools are bound once at graph compile, not per request).

Mirrors ``TransportRegistry`` (class-level registry, ``register`` overwrites).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from chaos_agent.agent.providers.base import DestroyOutcome, FaultProvider

if TYPE_CHECKING:
    from chaos_agent.tools.request_identity import RequestFingerprint
    from chaos_agent.agent.target_guard.types import EffectiveTarget

logger = logging.getLogger(__name__)


def _session_messages_to_langchain(messages: list[dict]) -> list:
    """Best-effort conversion of SessionStore message dicts back to messages.

    Moved from ``agent/result/task_snapshot.py`` in phase-13 (D2): the
    conversion is part of the session-recovery ORCHESTRATION this registry
    seam owns, not task-snapshot-private logic. Consumed by
    :meth:`FaultProviderRegistry.recover_experiment_uid_from_session` and
    (re-export-free) by task_snapshot's inject-context builder.
    """
    if not messages:
        return []

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    tool_call_names: dict[str, str] = {}
    out: list = []

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type") or ""
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        msg_id = msg.get("id") if isinstance(msg.get("id"), str) else None

        if msg_type == "ai":
            tool_calls = msg.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id")
                    tc_name = tc.get("name")
                    if isinstance(tc_id, str) and isinstance(tc_name, str):
                        tool_call_names[tc_id] = tc_name
            kwargs = {"content": content}
            if msg_id:
                kwargs["id"] = msg_id
            if isinstance(tool_calls, list) and tool_calls:
                kwargs["tool_calls"] = tool_calls
            try:
                out.append(AIMessage(**kwargs))
            except Exception:
                out.append(AIMessage(content=content))
            continue

        if msg_type == "tool":
            tool_call_id = msg.get("tool_call_id") or f"session_tool_{idx}"
            name = msg.get("name") or tool_call_names.get(tool_call_id, "")
            kwargs = {"content": content, "tool_call_id": tool_call_id}
            if isinstance(name, str) and name:
                kwargs["name"] = name
            if msg_id:
                kwargs["id"] = msg_id
            try:
                out.append(ToolMessage(**kwargs))
            except Exception:
                continue
            continue

        if msg_type == "tool_execution":
            detail = msg.get("detail") if isinstance(msg.get("detail"), dict) else {}
            source = detail.get("source") if isinstance(detail.get("source"), str) else ""
            stdout = detail.get("stdout_preview") if isinstance(detail.get("stdout_preview"), str) else ""
            text = stdout or content
            kwargs = {
                "content": text,
                "tool_call_id": msg.get("tool_call_id") or f"session_exec_{idx}",
            }
            if source:
                kwargs["name"] = source
            try:
                out.append(ToolMessage(**kwargs))
            except Exception:
                continue
            continue

        try:
            if msg_type == "human":
                out.append(HumanMessage(content=content, id=msg_id))
            elif msg_type == "system":
                out.append(SystemMessage(content=content, id=msg_id))
        except Exception:
            continue

    return out


class FaultProviderRegistry:
    """Class-level registry of fault execution backends, keyed by ``carrier``."""

    _providers: dict[str, FaultProvider] = {}
    # injection_method → provider, rebuilt on every register for O(1) resolve.
    _method_index: dict[str, FaultProvider] = {}

    @classmethod
    def register(cls, provider: FaultProvider) -> None:
        """Register (or replace) a provider by ``carrier``.

        Rebuilds the injection-method index. A method claimed by two providers
        is a programming error (the runtime dispatch would be ambiguous) — we
        log a warning and let the last registration win, matching
        ``TransportRegistry``'s overwrite-on-duplicate contract.
        """
        cls._providers[provider.carrier] = provider
        cls._reindex()

    @classmethod
    def _reindex(cls) -> None:
        index: dict[str, FaultProvider] = {}
        for provider in cls._providers.values():
            for method in provider.injection_methods:
                if method in index and index[method] is not provider:
                    logger.warning(
                        "injection_method %r claimed by both %r and %r; "
                        "last registration wins",
                        method,
                        index[method].carrier,
                        provider.carrier,
                    )
                index[method] = provider
        cls._method_index = index

    @classmethod
    def all_providers(cls) -> tuple[FaultProvider, ...]:
        """All registered providers (registration order)."""
        return tuple(cls._providers.values())

    @classmethod
    def get(cls, carrier: str) -> Optional[FaultProvider]:
        """Retrieve a provider by its ``carrier`` id, or ``None``."""
        return cls._providers.get(carrier)

    @classmethod
    def resolve_by_method(cls, injection_method: str | None) -> Optional[FaultProvider]:
        """POST-injection: resolve the backend for a detected ``injection_method``.

        Returns ``None`` for unknown / ``None`` methods so callers keep their
        existing fallback behaviour during the incremental migration.
        """
        if not injection_method:
            return None
        return cls._method_index.get(injection_method)

    @classmethod
    def resolve_by_scope(cls, scope: str | None) -> list[FaultProvider]:
        """PRE-injection: bridge an intent ``scope`` to its *candidate* backends
        via the ``FaultFamily`` registry (``carrier_types`` ↔ provider
        ``carrier``).

        A scope maps to CANDIDATES, not a single backend: pre-injection the LLM
        has not yet chosen a backend, and one scope may be served by several
        (e.g. a k8s scope by ``chaosblade`` OR ``k8s_native``). Returns the
        registered providers for ``family.carrier_types`` in precedence order,
        skipping carriers with no registered provider. Empty list when no family
        owns the scope or none of its carriers are registered. Use
        :meth:`resolve_primary_by_scope` for the single most-likely backend.
        """
        if not scope:
            return []
        # Lazy import: keep the registry importable without pulling the spec
        # package at module load (matches the codebase's lazy-import style).
        from chaos_agent.agent.spec.fault_registry import family_for_scope

        family = family_for_scope(scope)
        if family is None:
            return []
        resolved: list[FaultProvider] = []
        for carrier in family.carrier_types:
            provider = cls._providers.get(carrier)
            if provider is not None:
                resolved.append(provider)
        return resolved

    @classmethod
    def resolve_primary_by_scope(cls, scope: str | None) -> Optional[FaultProvider]:
        """PRE-injection: the single most-likely backend for ``scope`` (the first
        registered candidate from :meth:`resolve_by_scope`), or ``None``."""
        candidates = cls.resolve_by_scope(scope)
        return candidates[0] if candidates else None

    @classmethod
    def applicable(cls, profile: str) -> list[FaultProvider]:
        """PRE-injection: providers that can operate against a channel ``profile``
        ("k8s"|"host"), in registration order."""
        return [p for p in cls._providers.values() if p.matches_channel(profile)]

    @classmethod
    def union_required_params(
        cls, scope: str | None, *, profile: str | None = None
    ) -> list[str]:
        """PRE-injection: order-preserving union of every registered provider's
        ``required_params(scope)``.

        Intent completeness must consult only candidate backends compatible
        with the current environment profile. Pre-injection the concrete
        backend is unknown, so parameters are the union across candidates for
        the current scope, not every provider registered in the process. This
        prevents a future cloud provider's ``region`` requirement from leaking
        into a Kubernetes or host dialogue.
        Self-bootstraps the built-ins on an empty registry (mirrors
        :meth:`detect_method`)."""
        if not cls._providers:
            cls.register_builtins()
        # Preserve the historical public API when the caller has not resolved
        # an environment yet. New prompt/context callers pass ``profile`` and
        # receive the narrower, environment-compatible union.
        candidates = list(cls._providers.values())
        if profile is not None:
            candidates = cls.resolve_by_scope(scope)
            candidates = [p for p in candidates if p.matches_channel(profile)]
            if not candidates:
                candidates = cls.applicable(profile)
            # A resolved-but-unsupported profile must not inherit every
            # provider's parameters. The caller is expected to fail closed.
            if not candidates:
                return []
        if not candidates:
            candidates = list(cls._providers.values())

        out: list[str] = []
        for provider in candidates:
            for param in provider.required_params(scope or ""):
                if param not in out:
                    out.append(param)
        return out

    @classmethod
    def union_tool_names(cls, attr: str) -> frozenset[str]:
        """Union of a tool-name set attribute across every registered provider.

        Consumption seam for the execute loop's per-tool passes — kubeconfig
        safety-net injection (``kubeconfig_scoped_tool_names``), task_id
        audit binding (``audit_scoped_tool_names``) and L4 SLS log shipping
        (``log_shipping_tool_names``): each pass iterates the union of the
        corresponding provider attribute, so a newly registered provider's
        tools join every pass without touching the generic layer. Providers
        that omit an attribute contribute nothing (getattr default).
        Self-bootstraps the built-ins on an empty registry (mirrors
        :meth:`detect_method`).
        """
        if not cls._providers:
            cls.register_builtins()
        union: set[str] = set()
        for provider in cls._providers.values():
            union.update(getattr(provider, attr, frozenset()) or frozenset())
        return frozenset(union)

    @classmethod
    def classify_tool_target(
        cls, tool_name: str, tool_args: Any, raw_command: str
    ) -> "EffectiveTarget | None":
        """Guard-side target classification for a tool call, enacted by the
        first provider that recognises the call, else ``None``.

        TargetGuard seam (phase-7 T5): the generic classifier
        (``classifier.infer_effective_target``) dispatches here so it holds
        no carrier tool-name branch or carrier vocabulary table — each
        provider classifies its OWN tools (injection, read-only, and any
        embedded delivery riding another carrier's tool). ``tool_args``
        keeps its raw shape; ``raw_command`` is the pre-computed audit
        rendering. Channel-independent (the guard rules on the tool_call
        itself). Providers that omit the hook contribute nothing (getattr
        default); registration order decides overlapping claims.
        """
        if not cls._providers:
            cls.register_builtins()
        for provider in cls._providers.values():
            hook = getattr(provider, "classify_tool_target", None)
            if hook is None:
                continue
            target = hook(tool_name, tool_args, raw_command)
            if target is not None:
                return target
        return None

    @classmethod
    def tool_result_error_text(
        cls, tool_name: str, content: str
    ) -> "str | None":
        """The failure verdict on a tool result, enacted by the provider that
        OWNS the tool's result shape, else ``None``.

        Result-shape seam for ``agent/tool_verdicts.py``: the generic verdict
        there covers ``status == "error"`` and the textual ``Error`` /
        ``[target_guard]`` renderings, which every backend shares. A tool
        that reports failures STRUCTURALLY (a JSON receipt) is invisible to
        both, so its carrier declares the tool in ``result_shape_tool_names``
        and answers here — the generic scan never names a carrier tool or
        parses a carrier receipt.

        ``None`` is an ABSTENTION and callers must not read it as success:
        it covers "not a failure", "no provider owns this tool" and "the
        owner does not recognise this shape" (a compacted receipt is exactly
        the last case). Providers that omit the hook contribute nothing
        (getattr default); registration order decides overlapping claims.
        """
        if not tool_name:
            return None
        if not cls._providers:
            cls.register_builtins()
        for provider in cls._providers.values():
            if tool_name not in (
                getattr(provider, "result_shape_tool_names", frozenset())
                or frozenset()
            ):
                continue
            hook = getattr(provider, "tool_result_error_text", None)
            if hook is None:
                continue
            text = hook(tool_name, content)
            if text is not None:
                return text
        return None

    @classmethod
    def classify_inline_blade_command(
        cls,
        inner: list[str],
        raw_command: str,
        *,
        fallback_ns: str,
        fallback_pod: str,
    ) -> "EffectiveTarget":
        """Domain-routing seam for an embedded ``blade ...`` CLI
        classification (phase-14 G3, design D3).

        The kubectl-native classifier parsing ``kubectl exec POD -- blade
        create ...`` reaches the blade carrier's inline CLI parser through
        THIS seam instead of a cross-carrier import: the registry is the
        providers package's legitimate vertical routing point, so no
        carrier sub-package reaches sideways into another. The
        ``inner[0] == "blade"`` gate already ran at the caller — this ROUTES
        (a single carrier owns the blade CLI parsing vocabulary), it does
        not arbitrate. Lazy import keeps the registration-time import order
        untouched (see the NOTE in chaosblade.py).
        """
        from chaos_agent.agent.providers.chaosblade.provider import (
            classify_inline_blade,
        )

        return classify_inline_blade(
            inner, raw_command, fallback_ns=fallback_ns, fallback_pod=fallback_pod
        )

    @classmethod
    def is_blade_exec_create_delivery(cls, command: object) -> bool:
        """Domain-routing seam for the blade-exec create-delivery syntax
        judgement (round-15 root fix).

        The kubectl-native attribution scans need to EXCLUDE blade-exec
        creates from their native-attribution paths; the judgement itself
        is blade-carrier vocabulary (``chaosblade/verify.classify_blade_exec_payload``
        — command-position syntax, the replacement for the eleven
        copy-pasted word-containment gates a composite decoy payload used
        to pass). Reaching it through the registry keeps the no-cross-
        -carrier-import rule (phase-14 G3 pattern) while
        ``providers.message_scanning`` stays carrier-agnostic: its scan
        functions take the judgement as an ``is_blade_create_delivery``
        parameter, injected by their callers.
        """
        from chaos_agent.agent.providers.chaosblade.verify import (
            classify_blade_exec_payload,
        )

        return classify_blade_exec_payload(command).has_create

    @classmethod
    def is_blade_exec_destroy_delivery(cls, command: object) -> bool:
        """Domain-routing seam for the blade-exec DEMOLITION-delivery
        syntax judgement (R25/G-8).

        The machinery≠mutation predicate needs to exempt an exec carrying
        a PURE blade destroy/revoke (the kubelet-stall case's preferred
        recovery through the tool pod; the re-arm protocol's teardown
        half); the judgement is blade-carrier vocabulary
        (``chaosblade/verify.classify_blade_exec_payload`` —
        command-position syntax, segment-level, wrapper-tolerant). The
        seam answers ``has_destroy and not has_create``: a create segment
        in the same payload keeps it attributed (a REAL experiment
        delivery, whose issue-time claim belongs to the provider's own
        hook anyway). The CALLER owns the fault-binary withhold (the
        k8s classifier's ``fault_binary_mutation`` flag — G-9 makes it
        segment-level), the same single flag the CHANNEL face withholds
        on. Reaching the judgement through the registry keeps the
        no-cross-carrier-import rule (phase-14 G3 pattern) while
        ``execution_artifacts`` stays carrier-agnostic.
        """
        from chaos_agent.agent.providers.chaosblade.verify import (
            classify_blade_exec_payload,
        )

        payload = classify_blade_exec_payload(command)
        return payload.has_destroy and not payload.has_create

    @classmethod
    def recovery_carrier_allowed_images(cls) -> "frozenset[str]":
        """Domain-routing seam for the recovery-carrier image allowlist
        (faultdrill-cluster-native-recovery M1).

        The faultdrill recovery-carrier assembler picks its carrier image
        from the SAME configured ∪ auto-discovered allowlist the k8s-native
        classifier legislates (``k8s_native.classifier.
        _recovery_carrier_allowed_images`` — the union behind condition 4
        of the carrier SHAPE predicate). A separately reimplemented union
        would drift from the classifier's verdict: the assembler could pick
        an image the in-net registration gate then rejects, or honour an
        allowlist the gate no longer consults. One union, one
        implementation, reached through the registry — the providers
        package's legitimate vertical routing point (phase-14 G3 pattern),
        so no carrier sub-package reaches sideways into another.
        """
        from chaos_agent.agent.providers.k8s_native.classifier import (
            _recovery_carrier_allowed_images,
        )

        return _recovery_carrier_allowed_images()

    @classmethod
    def is_recovery_carrier_run_shape(cls, run_v_args: list, name: str) -> bool:
        """Domain-routing seam for the recovery-carrier pod SHAPE
        predicate (faultdrill-cluster-native-recovery M1).

        The faultdrill recovery-carrier assembler self-checks its
        constructed ``kubectl run`` against the canonical five-condition
        shape predicate (``k8s_native.classifier._is_recovery_carrier_run``
        — construction guarantee asserted, not re-derived): the assembler's
        template must stay in lockstep with the classifier's verdict, or a
        malformed carrier would pass its own self-check yet fail the in-net
        registration gate (or the reverse — a legal carrier refused at
        construction). One predicate, one implementation, reached through
        the registry keeps the no-cross-carrier-import rule (phase-14 G3
        pattern) while the assembler keeps its single-source guarantee.
        """
        from chaos_agent.agent.providers.k8s_native.classifier import (
            _is_recovery_carrier_run,
        )

        return _is_recovery_carrier_run(list(run_v_args), name)

    @classmethod
    def parse_injection_params(cls, tool_name: str, tool_args: dict) -> "dict | None":
        """Structured injection parameters for a freshly issued tool call,
        parsed by the first provider that recognises the call, else ``None``.

        Issue-time extraction seam (phase-7 T3): the execute loop consults
        the registry before dispatch, so each backend recognises its own
        tool-call forms (the ChaosBlade provider owns both the direct
        ``blade_create`` call and the ``kubectl exec`` embedded delivery).
        A provider returning ``None`` means "not my carrier" — the scan
        continues; an empty dict means "my carrier, no key parameters".
        Providers that omit the hook contribute nothing (getattr default).
        """
        if not cls._providers:
            cls.register_builtins()
        for provider in cls._providers.values():
            parse = getattr(provider, "parse_injection_params", None)
            if parse is None:
                continue
            parsed = parse(tool_name, tool_args)
            if parsed is not None:
                return parsed
        return None

    @classmethod
    def issue_time_method(
        cls, tool_name: str, tool_args: dict, *, is_host: bool
    ) -> Optional[str]:
        """Issue-time attribution for a freshly-issued tool call, enacted by
        the first provider that recognises the call, else ``None``.

        Direction B seam (phase-7 T4): the execute loop records the
        ``injection_method`` the moment the tool call is ISSUED, so each
        backend recognises its own carrier forms (the ChaosBlade provider
        claims both ``blade_create`` and the ``kubectl exec ... blade
        create`` embedded delivery; k8s-native claims object-write verbs and
        mutating execs; host-shell claims its tools only on a host channel).
        A provider returning ``None`` means "not my carrier" — the scan
        continues. Registration order is load-bearing: ChaosBlade precedes
        k8s-native so an embedded blade delivery is never mis-attributed as
        a mutating exec. Providers that omit the hook contribute nothing
        (getattr default)."""
        if not cls._providers:
            cls.register_builtins()
        for provider in cls._providers.values():
            hook = getattr(provider, "issue_time_method", None)
            if hook is None:
                continue
            method = hook(tool_name, tool_args, is_host=is_host)
            if method is not None:
                return method
        return None

    @classmethod
    def collect_provider_artifacts(
        cls, messages: list, *, task_id: str = "", operation_family: str = "",
    ) -> list[dict]:
        """Carrier-owned artifact facts discovered from messages — AGGREGATED.

        Artifact-ledger seam (faultdrill-cr-channel task 2.6): a carrier
        that rides the standard kubectl face (e.g. the FaultDrill CR
        apply) discovers its own landed objects from the tool history,
        claim-based — the artifact layer stays free of carrier-specific
        imports and vocabulary. Unlike the first-claim-wins seams, this
        one AGGREGATES: several carriers may each own artifacts in the
        same history, and the ledger records the full set (the fault
        handle is latest-wins; a rename-retry's early object must not be
        orphaned — review P11). Providers that omit the hook contribute
        nothing (getattr default).
        """
        if not cls._providers:
            cls.register_builtins()
        artifacts: list[dict] = []
        for provider in cls._providers.values():
            hook = getattr(provider, "collect_artifacts_from_messages", None)
            if hook is None:
                continue
            artifacts.extend(
                hook(
                    list(messages or []),
                    task_id=task_id,
                    operation_family=operation_family,
                )
            )
        return artifacts

    @classmethod
    async def sweep_artifact(
        cls, artifact: Any, *, kubeconfig: str = "", task_id: str = "",
    ) -> "Optional[bool]":
        """Claim-based single-artifact sweep; ``None`` when unowned.

        Sweep seam (faultdrill-cr-channel task 2.6): the owning carrier
        decides its own artifact's lifecycle — keep-while-Injected for
        the CR channel (an Injected CR is still firing; deleting it
        mid-window would be an early recovery, and the reconciler
        deliberately leaves a Recovered CR's OBJECT in place, making this
        sweep its sweeper of record). ``True`` = settled (mark cleaned),
        ``False`` = keep (re-examined next round), ``None`` = no provider
        claimed it (the artifact stays untouched). First claim wins — an
        artifact has exactly one owning carrier.
        """
        if not cls._providers:
            cls.register_builtins()
        for provider in cls._providers.values():
            hook = getattr(provider, "sweep_artifact", None)
            if hook is None:
                continue
            outcome = await hook(
                artifact, kubeconfig=kubeconfig, task_id=task_id,
            )
            if outcome is not None:
                return outcome
        return None

    @classmethod
    def build_reconcile_fingerprint(
        cls, tool_name: str, tool_args: Any
    ) -> "Optional[RequestFingerprint]":
        """Create-reconcile request identity for a create tool call, built
        by the first provider that recognises the call, else ``None``.

        Create-reconcile seam (blade-create-reconcile-before-retry D6):
        the generic gate (``agent/nodes/execute/_reconcile_gate.py``) owns
        the three-state scan / interception / release / cap state machine
        and consults this seam for the judgment material — which argument
        keys form the four-dimension request identity (the same
        construction safety_check's conflict query consumes) and how the
        raw LLM argument shapes normalise is carrier knowledge. A provider
        returning ``None`` means "not my create tool" — the scan
        continues. Providers that omit the hook contribute nothing
        (getattr default)."""
        if not cls._providers:
            cls.register_builtins()
        for provider in cls._providers.values():
            hook = getattr(provider, "build_reconcile_fingerprint", None)
            if hook is None:
                continue
            fp = hook(tool_name, tool_args)
            if fp is not None:
                return fp
        return None

    @classmethod
    async def reconcile_hold_feedback(
        cls,
        tool_name: str,
        fp: "RequestFingerprint",
        hold_count: int,
        block_limit: int,
        kubeconfig: str = "",
        task_id: str = "",
    ) -> Optional[tuple[str, bool]]:
        """Interception-time cluster probe plus hold-feedback text for a
        held create retry, composed by the first provider that recognises
        the create tool, else ``None``.

        Returns ``(feedback_text, counts_as_reconciliation)``: the probe —
        which cluster query answers "is my uncertain create already in
        effect", and which scopes cannot be probed at all — and the
        feedback text (naming the carrier's reconciliation tools) are
        carrier judgment material, hence a hook; the generic gate only
        threads the outcome into its flag (``gate_reconciled``) and
        fabricated answers. A provider returning ``None`` means "not my
        create tool" — the scan continues. Providers that omit the hook
        contribute nothing (getattr default)."""
        if not cls._providers:
            cls.register_builtins()
        for provider in cls._providers.values():
            hook = getattr(provider, "reconcile_hold_feedback", None)
            if hook is None:
                continue
            outcome = await hook(
                tool_name, fp, hold_count, block_limit,
                kubeconfig=kubeconfig, task_id=task_id,
            )
            if outcome is not None:
                return outcome
        return None

    @classmethod
    def reconcile_batch_held_feedback(
        cls, tool_name: str, other_tool_name: str
    ) -> Optional[str]:
        """Fabricated notice for the OTHER calls of a batch held back
        together with a held create, composed by the provider whose create
        tool held the batch, else ``None``.

        The text names the carrier's reconciliation tools (carrier
        judgment material), so it is a hook rather than a generic template;
        it must carry the "was NOT executed" wording so the generic
        three-state scan treats it as never-executed. A provider returning
        ``None`` means "not my create tool" — the scan continues.
        Providers that omit the hook contribute nothing (getattr
        default)."""
        if not cls._providers:
            cls.register_builtins()
        for provider in cls._providers.values():
            hook = getattr(provider, "reconcile_batch_held_feedback", None)
            if hook is None:
                continue
            text = hook(tool_name, other_tool_name)
            if text is not None:
                return text
        return None

    # -- built-in backends + detection orchestration -----------------------

    @classmethod
    def register_builtins(cls) -> None:
        """Register the built-in execution backends in precedence order.

        Order is load-bearing for :meth:`detect_method` AND
        :meth:`issue_time_method`: ChaosBlade (which owns the UID-bearing
        ``host_blade`` / ``kubectl_exec`` methods) must be probed before the
        UID-less ``k8s_native`` and ``host_shell`` backends, matching the
        original ``_detect_injection_method`` branch order — otherwise an
        embedded ``kubectl exec ... blade create`` delivery would be
        mis-attributed as a mutating exec. Lazy imports break the
        ``registry ← concrete provider ← base`` import cycle and follow the
        codebase's deferred-import style. Idempotent: ``register`` overwrites,
        so calling twice is harmless.

        This is the single ordered bootstrap of the built-in set. The providers
        package invokes it at import time (see ``providers/__init__``) so callers
        never have to remember to bootstrap; it also remains the explicit
        re-registration entry after a ``clear()`` (test fixtures) and the lazy
        self-bootstrap used by :meth:`detect_method` on an empty registry.
        """
        from chaos_agent.agent.providers.chaosblade.provider import ChaosbladeProvider
        from chaos_agent.agent.providers.chaosblade.python_provider import (
            ChaosbladePythonProvider,
        )
        from chaos_agent.agent.providers.host_shell.provider import HostShellProvider
        from chaos_agent.agent.providers.k8s_native.provider import K8sNativeProvider

        # Ordered built-in set — precedence is significant (see docstring).
        # ``chaosblade_python`` is order-insensitive: it is detected by its own
        # injection TOOL name, which no other backend scans, so it never
        # competes for attribution and can sit last.
        for provider_cls in (
            ChaosbladeProvider,
            K8sNativeProvider,
            HostShellProvider,
            ChaosbladePythonProvider,
        ):
            cls.register(provider_cls())

        # FaultDrill carrier (openspec faultdrill-cluster-native-recovery)
        # — registered LAST and gated by ``faultdrill_enabled``. Post-CR-
        # channel (M2) the provider hosts the programmatic recovery-
        # carrier assembler (its EXECUTE tool), the migration-window CR
        # attribution face, and the ledger-model recover — the flag is
        # the provider's registration switch, not a channel dark-launch.
        # Order-insensitive like chaosblade_python: this backend claims
        # no verb/tool vocabulary (attribution keys on the assembler
        # tool name and the stdin manifest DOCUMENT KIND, which no other
        # backend scans), so it never competes for recency — it can only
        # ever win by positive evidence. When the flag is off the
        # provider does not exist structurally: there is no runtime
        # faultdrill branch anywhere in the graph.
        from chaos_agent.config.settings import settings

        if bool(getattr(settings, "faultdrill_enabled", False)):
            from chaos_agent.agent.providers.faultdrill.provider import (
                FaultDrillProvider,
            )

            cls.register(FaultDrillProvider())
        else:
            # Flag off — reconcile DOWN too, never leave a stale registration
            # behind a re-register: ``register`` overwrites but never removes,
            # so the disabled invariant (provider structurally absent) is
            # this pop, not just the skipped register above.
            from chaos_agent.agent.providers.faultdrill.declaration import (
                CARRIER_ID as _FAULTDRILL_CARRIER_ID,
            )

            if cls._providers.pop(_FAULTDRILL_CARRIER_ID, None) is not None:
                cls._reindex()

    @classmethod
    def detect_method(
        cls, messages: list, *, is_host: bool, is_teardown=None,
    ) -> Optional[str]:
        """Resolve the runtime ``injection_method`` by RECENCY, not raw
        precedence: among channel-scoped providers that recognise their carrier
        in ``messages``, the one whose injection evidence is MOST RECENT wins.

        This implements "attribute the LAST successful injection": after a
        replan switches from a failed ``blade_create`` to a kubectl-native
        fallback, the later native injection out-ranks the earlier (stale)
        blade UID instead of being hijacked by it (task-76c59364). Registration
        precedence is retained only as a TIE-BREAKER (equal recency → the
        earlier-registered provider, i.e. ChaosBlade, wins) so all existing
        single-carrier attributions are unchanged.

        Candidates are scoped by CHANNEL first: ``is_host`` maps to a channel
        profile and only providers whose ``matches_channel`` accepts it are
        probed. A host backend (``host_shell``) is therefore never a candidate
        for a k8s injection, and vice versa — the channel is a hard, known fact.

        Self-bootstraps the built-in backends on an empty registry, mirroring
        ``TransportRegistry``'s ``_ensure_default``; an explicitly-populated
        registry (e.g. test fixtures) is left untouched.
        """
        if not cls._providers:
            cls.register_builtins()
        from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

        profile = PROFILE_HOST if is_host else PROFILE_K8S
        best_method: Optional[str] = None
        best_key: tuple[int, int] | None = None
        for rank, provider in enumerate(cls._providers.values()):
            if not provider.matches_channel(profile):
                continue
            method = provider.detect(
                messages, is_host=is_host, is_teardown=is_teardown,
            )
            if not method:
                continue
            # Recency is the message index of this provider's injection
            # evidence. Providers predating the seam (e.g. test doubles) have no
            # ``injection_recency`` — fall back to 0 so ties resolve by the
            # registration-order rank below (legacy precedence behaviour).
            recency_fn = getattr(provider, "injection_recency", None)
            recency = (
                recency_fn(messages, is_host=is_host, is_teardown=is_teardown)
                if recency_fn is not None
                else 0
            )
            # Higher recency wins; equal recency → lower rank (earlier
            # registration precedence) wins via ``-rank``.
            key = (recency, -rank)
            if best_key is None or key > best_key:
                best_key = key
                best_method = method
        return best_method

    # -- fault-handle orchestration (carrier-neutral) -----------------------

    @classmethod
    def derive_handle_from_legacy(cls, values: dict) -> Optional[dict]:
        """Hydration seam: derive the fault handle from legacy attribution facts.

        The single place carrier fields are READ to build a handle. Used by
        ``materialize_fault_handle`` when a checkpoint / persisted snapshot
        predates ``fault_handle``: the attributed provider (by
        ``injection_method``) is asked first, then every provider may claim
        its own legacy fields in registration order. Returns the first
        non-empty handle, or ``None`` when no backend owns the facts.
        """
        if not cls._providers:
            cls.register_builtins()
        values = dict(values or {})
        provider = cls.resolve_by_method(values.get("injection_method"))
        candidates = [provider] + [
            p for p in cls._providers.values() if p is not provider
        ]
        for candidate in candidates:
            build = getattr(candidate, "build_fault_handle", None)
            if build is None:
                continue
            handle = build(values)
            if handle:
                return handle
        return None

    @classmethod
    def resolve_by_handle_kind(cls, handle: Optional[dict]) -> Optional[FaultProvider]:
        """POST-attribution: resolve the provider owning a fault handle.

        The handle's ``method`` attribution (when present) names the owning
        provider — prefer it over kind-only matching, so a third backend
        sharing a kind can never steal another backend's handle. Fall back to
        the first provider registering the ``kind`` (legacy handles carry no
        method). ``None`` when the handle is empty or no provider owns it."""
        if not cls._providers:
            cls.register_builtins()
        kind = (handle or {}).get("kind")
        if not kind:
            return None
        provider = cls.resolve_by_method((handle or {}).get("method"))
        if provider is None or getattr(provider, "handle_kind", "") != kind:
            provider = next(
                (
                    p
                    for p in cls._providers.values()
                    if getattr(p, "handle_kind", "") == kind
                ),
                None,
            )
        return provider

    @classmethod
    def is_experiment_handle(cls, handle: Optional[dict]) -> bool:
        """Whether ``handle`` belongs to a UID-bearing experiment carrier.

        Pinning membership is the owning provider's declaration, not a
        ``kind`` string comparison in the consumer: ``has_experiment_uid``
        carriers pin their UID into the recovery-handle contract, so a
        future experiment carrier (any ``handle_kind``) is covered by
        registration alone (phase-7 T6)."""
        provider = cls.resolve_by_handle_kind(handle)
        return provider is not None and bool(
            getattr(provider, "has_experiment_uid", False)
        )

    @classmethod
    def resolve_fault_dispatch(
        cls, values: dict
    ) -> tuple[FaultProvider, Optional[dict]]:
        """Fault identity dispatch: resolve ``(provider, identity_handle)``
        for a no-LLM verify / recover entry.

        Shared by the verify and recover chains (phase-4 T5): both must
        resolve the SAME provider for the same state. Identity ownership is
        NOT attribution ownership — a combo task (experiment + native
        mutation) attributes to the native backend, yet its experiment still
        needs the deterministic destroy / status poll. Ownership order
        (strongest first):

        1. **Live experiment claim** — the first-registered UID-bearing
           backend that claims the attribution facts. Registration order
           (not method attribution) preserves the legacy contract that a
           claimed experiment routes to the OS experiment carrier; the
           identity handle IS that experiment handle.
        2. **Message-history experiment claim** — when the durable facts
           are absent (heavily compacted legacy checkpoints), the providers'
           own evidence scan (:meth:`derive_handle_from_messages`) recovers
           a live experiment handle from the history. It routes exactly
           like claim 1 but never overrides a state-based claim.
        3. **Attribution handle** — ``materialize_fault_handle``'s
           carrier-agnostic ownership (native handles resolve to their
           backend through ``method`` / ``kind``).
        4. **Attributed method** — ``resolve_by_method`` on the durable
           ``injection_method``, defaulting to the UID-less native verdict
           backend (nothing to destroy deterministically, no method
           detected).
        """
        if not cls._providers:
            cls.register_builtins()
        values = dict(values or {})
        attribution = values.get("fault_handle")
        attribution = (
            attribution if isinstance(attribution, dict) and attribution else None
        )
        # 1. Live experiment claim (combo-safe: claims outrank attribution).
        for provider in cls._providers.values():
            if not provider.has_experiment_uid:
                continue
            if attribution is not None and getattr(
                provider, "handle_kind", ""
            ) == attribution.get("kind"):
                # An experiment-kind attribution handle already in state IS
                # this claim — prefer it over rebuilding from the legacy
                # fields (it may carry a more precise method attribution).
                if cls.resolve_by_handle_kind(attribution) is provider:
                    return provider, attribution
            handle = provider.build_fault_handle(values)
            if handle:
                return provider, handle
        # 2. Message-history experiment claim (defense seam): a live
        #    experiment handle recovered from the message history routes
        #    like claim 1 (the legacy contract — a live UID reaches the
        #    experiment carrier's destroy) but never overrides a
        #    state-based claim; the scan is the weakest evidence source.
        hint = cls.derive_handle_from_messages(values.get("messages") or [], values)
        if hint is not None:
            provider = cls.resolve_by_handle_kind(hint)
            if provider is not None and provider.has_experiment_uid:
                return provider, hint
        # 3. Attribution handle (hydrated from legacy facts when the
        #    checkpoint predates ``fault_handle``).
        from chaos_agent.agent.state import materialize_fault_handle

        attribution = attribution or materialize_fault_handle(values)
        if attribution is not None:
            provider = cls.resolve_by_handle_kind(attribution)
            if provider is not None:
                return provider, attribution
        # 4. Attributed method, then the UID-less verdict-default carrier
        #    (declared via ``uid_less_verdict_default`` — the registry never
        #    names a provider class; getattr defaults to False for backends
        #    that omit the property).
        provider = cls.resolve_by_method(values.get("injection_method"))
        if provider is not None:
            return provider, None
        for candidate in cls._providers.values():
            if getattr(candidate, "uid_less_verdict_default", False):
                return candidate, None
        return None, None

    @classmethod
    def derive_handle_from_messages(
        cls, messages: list, state: Optional[dict] = None
    ) -> Optional[dict]:
        """Message-history hydration seam: build a fault handle from live
        experiment evidence found in ``messages``, when neither the handle
        nor the legacy attribution facts are present (heavily compacted /
        legacy checkpoints).

        Each UID-bearing backend scans for its OWN experiment evidence and
        builds its own handle — the registry never names a carrier. Returns
        the first claim, or ``None`` when nothing is found.
        """
        if not cls._providers:
            cls.register_builtins()
        state = state or {}
        retired = state.get("retired_experiment_uids")
        for provider in cls._providers.values():
            build = getattr(provider, "build_handle_from_messages", None)
            if build is None:
                continue
            handle = build(messages, retired=retired, values=state)
            if handle:
                return handle
        return None

    @classmethod
    def extract_experiment_uid(
        cls, messages: list, retired=None, *, is_host: bool
    ) -> str:
        """Live experiment id present in ``messages``, claimed by the first
        channel-compatible UID-bearing provider, else ``""``.

        Replaces the execute loop's direct call to a carrier-specific
        extractor: the generic attribution sync asks the registry, and each
        provider scans for its own experiment id."""
        if not cls._providers:
            cls.register_builtins()
        from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

        profile = PROFILE_HOST if is_host else PROFILE_K8S
        for provider in cls._providers.values():
            if not provider.has_experiment_uid:
                continue
            if not provider.matches_channel(profile):
                continue
            extract = getattr(provider, "extract_experiment_id", None)
            if extract is None:
                continue
            uid = extract(messages, retired)
            if uid:
                return uid
        return ""

    @classmethod
    def extract_experiment_uids(
        cls, messages: list, retired=None, *, is_host: bool
    ) -> set[str]:
        """EVERY live experiment id born in ``messages`` — the UNION across
        channel-compatible UID-bearing providers (round-26 birth face).

        The single-slot seam above stays first-match ("which experiment is
        current"); this seam answers the ownership ledger's question —
        "which experiments does this task own" — where a composite inline
        create can prove MULTIPLE births in one call and every one of them
        is a liability the sweep must be able to recover. Union, not
        first-match: ownership is additive (a provider claiming a birth
        never invalidates another provider's claim).

        Providers without an explicit plural face
        (``extract_experiment_ids``) contribute their singular extraction
        wrapped in a set — an upgrade path, not a protocol break: every
        UID-bearing provider that has not pluralised yet still registers
        the birth its single-slot scan surfaces."""
        if not cls._providers:
            cls.register_builtins()
        from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

        profile = PROFILE_HOST if is_host else PROFILE_K8S
        born: set[str] = set()
        for provider in cls._providers.values():
            if not provider.has_experiment_uid:
                continue
            if not provider.matches_channel(profile):
                continue
            plural = getattr(provider, "extract_experiment_ids", None)
            if plural is not None:
                born |= {uid for uid in plural(messages, retired) if uid}
                continue
            extract = getattr(provider, "extract_experiment_id", None)
            if extract is None:
                continue
            uid = extract(messages, retired)
            if uid:
                born.add(uid)
        return born

    @classmethod
    def created_experiment_ids(cls, messages: list, state: dict) -> set[str]:
        """Provenance union: every experiment id ANY provider proves this task
        created (each backend scans its own create results and claims its own
        durable record).

        The single carrier-neutral seam generic nodes consult instead of
        naming a carrier extractor — the tool screener's destroy gate uses it
        to whitelist ``blade destroy`` UIDs (a task may only destroy
        experiments it created itself, failed-create CRDs included)."""
        if not cls._providers:
            cls.register_builtins()
        uids: set[str] = set()
        for provider in cls._providers.values():
            collect = getattr(provider, "created_experiment_ids", None)
            if collect is None:
                continue
            uids.update(collect(messages, state))
        return uids

    @classmethod
    def destroyed_experiment_ids(cls, messages: list) -> set[str]:
        """Terminal-state union: every experiment id ANY UID-bearing provider
        proves has been sent to a destroy (each backend scans its own destroy
        tool calls).

        Death-filter companion of :meth:`created_experiment_ids` (provenance
        union): destruction is a terminal-state FACT — a destroy issued by any
        carrier kills that experiment regardless of channel, so the
        aggregation is a UNION with NO channel filtering (the replan seam's
        compression-boundary fallback applies the same unconditional
        full-history scan). Providers that omit the hook contribute nothing
        (getattr default)."""
        if not cls._providers:
            cls.register_builtins()
        uids: set[str] = set()
        for provider in cls._providers.values():
            if not provider.has_experiment_uid:
                continue
            scan = getattr(provider, "destroyed_experiment_ids", None)
            if scan is None:
                continue
            uids.update(scan(messages))
        return uids

    @classmethod
    def destroyed_proven_experiment_ids(cls, messages: list) -> set[str]:
        """PROVEN-death union: destroys whose PAIRED tool output confirms
        success — the death-registration feed for the liability ledger (B76
        review I1).

        Stricter twin of :meth:`destroyed_experiment_ids`: that seam's
        "issued = terminal" is the right CONSERVISM for attribution (an
        attempted destroy must stop the UID being re-claimed as the live
        fault), but the retire LEDGER needs a higher bar — retirement
        excludes a UID from every live-liability read, so a false entry
        hides a LIVE experiment (strictly worse than the orphan the sweep
        exists to prevent). Hence: only an output-proven death registers,
        and the scan covers BOTH delivery forms (the ``blade_destroy`` tool
        and the kubectl-exec ``blade destroy`` vehicle the issued-scan
        cannot see — I1c). Providers that omit the hook contribute nothing.
        """
        if not cls._providers:
            cls.register_builtins()
        uids: set[str] = set()
        for provider in cls._providers.values():
            if not provider.has_experiment_uid:
                continue
            scan = getattr(provider, "destroyed_proven_experiment_ids", None)
            if scan is None:
                continue
            uids.update(scan(messages))
        return uids

    @classmethod
    async def sweep_live_liabilities(
        cls, values: dict, *, exclude_uid: str = "",
    ) -> tuple[list[str], list[str]]:
        """Destroy every live liability experiment except ``exclude_uid``
        (B76 review G — the liability-axis safety net).

        ``exclude_uid`` is a CALLER-OWNED policy, not a mechanism promise:
        exempt a UID only when its destroy is already proven elsewhere. The
        recover finale gates the identity UID's exemption on the Layer-1
        verdict (``_sweep_exempts_identity_uid``) — a failed main-flow
        destroy must not be exempted, or the net orphans the experiment it
        exists to catch (B76 review M2, the exempt orphan).

        The single ``experiment_uid`` slot is last-write-wins (correct for
        attribution), so a superseded experiment's recovery claim is erased
        the moment a newer create lands; this sweep is the carrier-neutral
        seam where the append-only birth registry (:func:`live_liability_uids`)
        turns back into real destroys. Ridden by every seam that owes a
        SETTLEMENT obligation (the settlement-action manifest,
        tests/test_agent/test_settlement_action_manifest.py — a new
        cleanup/rollback seam that dispatches its own singular destroy
        is a domain violation, not a simplification): the plan-change
        approval (contract-boundary serialization — the old contract's
        experiments must not run under the new one), the recover finale
        (the last point where the framework still holds the full
        ownership record), the failure-path auto-rollback (round-30),
        and the verify-replan residual cleanup (round-31 — the replan's
        fresh verification must not run under the residual fault).

        Deterministic destroy through the dispatched carrier's execution
        domain; a SUCCESSFUL destroy retires (the CALLER appends to
        ``retired_experiment_uids`` — a framework-side destroy leaves no
        ToolMessage in history), a FAILED one keeps the UID in the liability
        set so the next sweep / a re-run recover retries it.

        Kubeconfig (round-32, K1): the destroy resolves through the
        graph-wide three-level fallback
        (:func:`chaos_agent.agent.kubeconfig.resolve_kubeconfig` —
        state > spec > settings), the SAME contract the injection chain's
        create runs under — a destroy dispatched under a different config
        than its create targets a different cluster and the liability can
        never clear. A caller may therefore pass bare state (the CORRECT
        form — the empty CLI key falls through to spec/settings) or merge
        its own resolved value (idempotent: the resolver returns a
        non-empty state value unchanged).

        Returns ``(retired_new, failures)``; failures are ``uid: reason``
        lines for the caller's warning surface.
        """
        from chaos_agent.agent.state import live_liability_uids
        from chaos_agent.agent.kubeconfig import resolve_kubeconfig

        # Death-registration absorption (B76 review I1c): the sweep is the
        # framework's convergence point — absorb the message-side PROVEN
        # deaths into the LOCAL retired view FIRST, so the live set it
        # computes reflects kills the LLM flow already achieved (notably the
        # kubectl-exec vehicle whose destroy calls the issued-scan cannot
        # see). Local view only: the durable write side is execute_loop's
        # registration seam (and the callers' retire appends); this
        # absorption additionally covers histories that seam never saw
        # (sessions recovered before the seam existed, LLM destroys inside
        # THIS graph's own Layer-1 flow). Since B76 review J1 the liability
        # view itself applies the same PROVEN filter internally, making this
        # block belt-and-suspenders — kept because the absorbed view is also
        # what the dispatch/blocking reads below see.
        _proven = cls.destroyed_proven_experiment_ids(
            values.get("messages") or [],
        )
        if _proven:
            _retired_view = (
                set(values.get("retired_experiment_uids") or []) | _proven
            )
            values = {**values, "retired_experiment_uids": sorted(_retired_view)}

        residuals = [
            uid for uid in live_liability_uids(values) if uid != exclude_uid
        ]
        if not residuals:
            return [], []
        provider, _identity = cls.resolve_fault_dispatch(values)
        if provider is None or not provider.has_experiment_uid:
            return [], [f"{uid}: no experiment carrier dispatched" for uid in residuals]
        if provider.blocks_deterministic_destroy(values, values.get("messages") or []):
            # In-cluster delivery: the host-side destroy cannot reach a
            # CRD-created experiment ("record not found") and a soft failure
            # must not falsely retire a LIVE experiment — surface the UIDs
            # instead; the kubectl-exec destroy is the LLM flow's vehicle.
            return [], [
                f"{uid}: in-cluster delivery — destroy via "
                "`kubectl exec <tool-pod> -- blade destroy <uid>`"
                for uid in residuals
            ]
        # Config-domain alignment (round-32, K1): NOT a bare state read —
        # the state key is routinely empty on the CLI entry
        # (``kwargs.get("kubeconfig", "")``), and a bare read silently
        # re-homed every settlement destroy onto blade's own default
        # cluster; resolving through the shared fallback keeps the destroy
        # on the cluster its create ran under.
        kubeconfig = resolve_kubeconfig(values)
        retired_new: list[str] = []
        failures: list[str] = []
        for uid in residuals:
            try:
                out = await provider.layer1_raw_destroy(uid, kubeconfig)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{uid}: {e}")
                continue
            # Single-source three-state verdict: the carrier's classifier
            # is the ONE decision source (this seam's own prefix table was
            # the third table judging the same output — deleted; a non-JSON
            # no-keyword output used to retire here while the authority's
            # predicate kept it live). A carrier without the hook is
            # fail-closed FAILED — the UID surfaces, never silently retires.
            classify = getattr(provider, "classify_destroy_output", None)
            outcome = (
                classify(str(out or ""))
                if classify is not None
                else DestroyOutcome.FAILED
            )
            if outcome is DestroyOutcome.SUCCESS:
                retired_new.append(uid)
                continue
            if outcome is DestroyOutcome.NOT_FOUND:
                # Convergence valve (B76 review I2/I2b): a repeat-destroy of
                # an already-dead experiment surfaces record-not-found (the
                # local-DB record is gone) — without an escape the UID NEVER
                # retires and every re-run recover repeats the same
                # destroy+failure forever. The carrier gets one chance to
                # PROVE the death through its status check (the same layer
                # ``run_layer1_destroy``'s destroy-failed fallback already
                # trusts); any doubt keeps the failure — fail-closed, the
                # warning surface is the honest verdict.
                _status_check = getattr(provider, "experiment_destroyed", None)
                if _status_check is not None and await _status_check(
                    uid, kubeconfig,
                ):
                    retired_new.append(uid)
                    continue
            failures.append(
                f"{uid}: {str(out or '').strip()[:120] or '(empty destroy output)'}"
            )
            continue
        return retired_new, failures

    @classmethod
    def recover_experiment_uid_from_session(
        cls, session_messages: list, *, retired=None
    ) -> str:
        """Recover the experiment UID from persisted SESSION message dicts
        (task-file format), claimed by the first UID-bearing provider, else
        ``""``.

        Session-recovery seam (phase-13 D2): the task file persists messages
        as plain dicts — some carrying nested ``detail`` payloads the
        langchain conversion cannot fully represent — and the recovery runs
        with NO channel context (the task file does not record one). The
        seam orchestrates three layers, mirroring the single carrier-family
        function the task-snapshot reader used to call directly:

        1. best-effort langchain conversion (:func:`_session_messages_to_langchain`;
           a failed conversion falls through to the dict layer, never aborts);
        2. per-provider ``extract_experiment_id`` over the converted history —
           NO channel filtering (the ``derive_handle_from_messages``
           precedent: recovery cannot recover the channel, and the UID
           shapes are strict enough that cross-channel mis-extraction is
           negligible);
        3. dict-literal fallback via ``extract_experiment_id_from_session_dict``
           — each provider reads its own command vocabulary off the raw dict
           payloads (``detail.command`` etc.); that vocabulary judgement
           belongs to the carrier side, not the generic layer.

        ``retired`` defaults to ``None`` (session files carry no retired set);
        the parameter is kept for future callers holding one.
        """
        if not cls._providers:
            cls.register_builtins()
        if not isinstance(session_messages, list) or not session_messages:
            return ""
        try:
            langchain_messages = _session_messages_to_langchain(session_messages)
        except Exception:
            logger.debug(
                "Session message conversion failed; falling to dict layer",
                exc_info=True,
            )
            langchain_messages = []
        if langchain_messages:
            for provider in cls._providers.values():
                if not provider.has_experiment_uid:
                    continue
                extract = getattr(provider, "extract_experiment_id", None)
                if extract is None:
                    continue
                try:
                    uid = extract(langchain_messages, retired)
                except Exception:
                    logger.debug(
                        "Provider %r session-history extraction failed",
                        provider.carrier,
                        exc_info=True,
                    )
                    uid = ""
                if uid:
                    return uid
        for provider in cls._providers.values():
            if not provider.has_experiment_uid:
                continue
            fallback = getattr(
                provider, "extract_experiment_id_from_session_dict", None
            )
            if fallback is None:
                continue
            try:
                uid = fallback(session_messages)
            except Exception:
                logger.debug(
                    "Provider %r session-dict extraction failed",
                    provider.carrier,
                    exc_info=True,
                )
                uid = ""
            if uid:
                return uid
        return ""

    @classmethod
    async def rollback_handle(cls, handle: dict, **kwargs) -> str:
        """Dispatch a failure-path auto-rollback to the provider owning the
        handle's ``kind``; returns a human-readable status suffix
        (``""`` = nothing rolled back). Backends without a deterministic undo
        decline, so a UID-less fault is never fed to a carrier-specific
        destroy."""
        provider = cls.resolve_by_handle_kind(handle)
        if provider is None:
            return ""
        rollback = getattr(provider, "rollback_handle", None)
        if rollback is None:
            return ""
        return await rollback(handle, **kwargs)

    @classmethod
    def extract_kubectl_exec_pod_name(cls, messages: list) -> Optional[str]:
        """Dispatch the kubectl-exec delivery-pod extraction to the backend
        that owns that delivery (resolved by its ``kubectl_exec`` method —
        the ChaosBlade backend), so the generic execute loop records the
        delivery pod without importing a concrete provider module
        (phase-8 T4). Returns ``None`` when the owner is absent or does not
        implement the hook (resolve-then-getattr, the :meth:`rollback_handle`
        pattern)."""
        provider = cls.resolve_by_method("kubectl_exec")
        if provider is None:
            return None
        extract = getattr(provider, "extract_kubectl_exec_pod_name", None)
        if extract is None:
            return None
        return extract(messages)

    # -- test / lifecycle helpers ------------------------------------------

    @classmethod
    def clear(cls) -> None:
        """Drop all registrations. For tests that install fixtures."""
        cls._providers = {}
        cls._method_index = {}


__all__ = ["FaultProviderRegistry"]
