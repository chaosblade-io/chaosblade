"""FaultDrill CR execution backend provider.

Backend semantics: the fault is declared as ONE apiserver object — a
``FaultDrill`` CustomResource (``agent/providers/faultdrill/crd.py``).
``kubectl apply`` of the CR IS the injection; controller-style
reconciliation of ``spec.restorePatches`` IS the recovery; TTL is
``status.injectedAt`` (cluster state, not process memory — recovery
survives Agent death). There is no experiment UID: the recovery handle
is the CR reference (``kind=faultdrill_cr``, value ``ns/name``).

This provider fully owns the per-backend behaviour for the faultdrill
carrier:

- attribution (``detect`` / ``injection_recency`` / ``issue_time_method``):
  by the stdin MANIFEST DOCUMENT KIND, never by command-line form —
  ``kubectl apply -f -`` is indistinguishable from any other stdin
  manifest apply on the command line, and ``apply`` is deliberately NOT
  in any ``inject_kubectl_subcommands`` set (zero recency competition
  with k8s_native, whose write vocabulary starts at ``scale``/``patch``).
  The scan pairs each AIMessage tool call with its ToolMessage result
  and applies the shared ATTEMPT rule (pre-execution rejections are not
  attempts; timeouts and non-zero exits are).
- Layer-1 / recover (``has_deterministic_recover=True``): the
  four-state convergence over the CR's CURRENT cluster state IS the
  recovery, regardless of what the LLM says (``_recover_converge``, M2
  task 2.3 / design D4): Pending → delete the CR (zero inject actions);
  Injected → guard-2 idempotent ``restorePatches`` replay +
  derived-secret deletion landing ``phase=Recovered`` (the same walk
  the session reconciler's TTL verdict performs — replay and loop are
  mutually idempotent); Failed → best-effort restore + delete
  (terminal cleanup); Recovered → zero writes; NotFound (after the P12
  cross-namespace list cross-check) → zero actions + the honest
  ``unverified`` warning (the ATTEMPT-attribution residue). M2 task 2.1
  shipped the post-landing readback guard
  (:meth:`verify_landing_readback` — a landed CR whose recipe was pruned
  aborts HARD before any reconciliation; the v2 experiment's ``exit(5)``
  guard, productised) and task 2.2 shipped the session-side reconciler
  (``reconciler.py`` — armed via :meth:`arm_session_reconciler` on a
  verified landing; three idempotence guards, bounded-retry ``Failed``
  terminal, invalid-secret source-derivation).
- tools: NONE of its own. The carrier rides the existing ``kubectl``
  surface (bound for EXECUTE / RECOVER_VERIFY by K8sNativeProvider), so
  every tool-face guard (Gate-① binary whitelist, manifest-kind
  allowlist, write-set approval, target drift, audit) applies to the CR
  apply unchanged — adding no new tool and no new binary is a design
  property of this channel, not an omission.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from langchain_core.messages import AIMessage, ToolMessage

from .crd import (
    CRD_KIND,
    CRD_PLURAL,
    PHASE_FAILED,
    PHASE_INJECTED,
    PHASE_RECOVERED,
    PRESERVED_ARRAY_FIELDS,
)
from .declaration import CARRIER_ID, SUPPORTED_ACTIONS, SUPPORTED_TARGETS
from chaos_agent.agent.providers.base import (
    DestroyOutcome,
    ProviderPrompts,
    RecoverResult,
    StepActionScan,
)
from chaos_agent.agent.providers.message_scanning import (
    PRE_EXEC_REJECTION_MARKERS,
)
from chaos_agent.transports import PROFILE_K8S

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from chaos_agent.tools.request_identity import RequestFingerprint
    from chaos_agent.agent.result.verdict import Layer1Result
    from chaos_agent.agent.target_guard.types import EffectiveTarget


class FaultDrillProvider:
    """FaultDrill CR backend (declarative apiserver-write faults)."""

    carrier = CARRIER_ID
    injection_methods = ("faultdrill_cr",)
    has_experiment_uid = False
    # Not the UID-less verdict default (k8s_native is the single declared
    # fallback carrier); this backend is only reachable through positive
    # manifest-kind evidence, never through claim-exhaustion.
    uid_less_verdict_default = False
    # Recovery handle: the CR reference (value = ns/name). Distinct from
    # both "experiment_uid" (ChaosBlade) and "native" (bare method).
    handle_kind = "faultdrill_cr"
    is_multi_step = False
    # has_deterministic_recover is the property that separates this
    # carrier from k8s_native's False: reconciling restorePatches +
    # deleting the derived props IS the recovery — no LLM undo flow is
    # Layer 1 here.
    has_deterministic_recover = True
    # Attribution is manifest-kind based (see module docstring): no tool
    # name and no kubectl subcommand vocabulary is claimed. In
    # particular ``apply`` must never enter an inject_kubectl_subcommands
    # set — that would compete with k8s_native's recency arbitration for
    # every ordinary manifest apply.
    inject_tool_names = frozenset()
    inject_kubectl_subcommands = frozenset()
    # Routing into this channel is decided by skill-case metadata
    # (``recovery_channel: apiserver-write``) + planning + write-set
    # validation — never by scope bridging, so the intent vocabulary is
    # empty and INTENT_TARGETS / INTENT_ACTIONS stay byte-identical.
    supported_targets = SUPPORTED_TARGETS
    supported_actions = SUPPORTED_ACTIONS
    # Rides the existing kubectl surface: no binary, tool, kubeconfig
    # scope, audit scope or log-shipping scope of its own (kubectl is
    # already admitted by K8sNativeProvider — the unions stay unchanged).
    injection_binaries = frozenset()
    tool_pod_namespaces = frozenset()
    kubeconfig_scoped_tool_names = frozenset()
    audit_scoped_tool_names = frozenset()
    log_shipping_tool_names = frozenset()
    # A CR apply is retry-safe (re-applying the same manifest converges
    # the same object — no duplicate-experiment hazard), so the
    # create-reconcile gate has nothing to intercept here.
    reconcile_create_tool_names = frozenset()
    reconcile_read_tool_names = frozenset()

    # ------------------------------------------------------------------
    # Attribution vocabulary
    # ------------------------------------------------------------------

    #: The tool whose stdin manifest can declare this carrier's object.
    #: The kubectl tool is channel-agnostic (kubeconfig / kubewiz_k8s
    #: resolve downstream), so the name is the same on every k8s channel.
    _APPLY_TOOL_NAMES = frozenset({"kubectl"})

    def matches_channel(self, profile: str) -> bool:
        # The CR channel talks to the cluster API only.
        return profile == PROFILE_K8S

    def required_params(self, scope: str) -> list[str]:
        # Scope-generic answer (same as k8s_native): this carrier is not
        # scope-bridged, so it never contributes a narrower requirement.
        from chaos_agent.agent.spec.fault_registry import required_intent_params

        return required_intent_params(scope)

    def tools(self, phase: str) -> list["BaseTool"]:
        """No tools of its own — see module docstring.

        The CR apply flows through the standard ``kubectl`` tool that
        K8sNativeProvider already binds for EXECUTE / RECOVER_VERIFY, so
        the factory union is unchanged and every tool-face guard applies
        to the CR apply verbatim.
        """
        return []

    def detect(self, messages: list, *, is_host: bool, is_teardown=None) -> Optional[str]:
        """Classify as ``faultdrill_cr`` when a kubectl apply carrying a
        FaultDrill stdin manifest was ATTEMPTED on a cluster channel.

        Attribution keys on the injection ATTEMPT (AIMessage tool_calls),
        the shared high-tolerance rule: a pre-execution rejection (guard
        reject / validation error) is not an attempt; a timeout or
        non-zero exit still is.

        ``is_teardown`` is accepted for interface uniformity and ignored:
        a faultdrill APPLY is never a registered-vehicle teardown —
        teardowns are deletes, and this scan only matches applies whose
        stdin manifest declares a FaultDrill document.
        """
        if is_host:
            return None
        for event in _faultdrill_apply_events(messages):
            if event["attempted"]:
                return "faultdrill_cr"
        return None

    def issue_disproven(self, messages: list, *, is_teardown=None) -> bool:
        """Counter-evidence for an issue-time attribution: the MOST
        RECENT faultdrill apply came back as an apiserver-visible error
        (the CR never landed, so the attribution never committed) AND no
        earlier apply in the epoch LANDED (a landed CR keeps the
        attribution live; a later failed re-apply never revokes it)."""
        events = _faultdrill_apply_events(messages)
        if not events:
            return False
        latest = events[-1]
        if not _result_is_error(latest["result"]):
            return False
        landed_earlier = any(
            e["attempted"] and not _result_is_error(e["result"])
            for e in events[:-1]
        )
        return not landed_earlier

    def injection_recency(
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> int:
        """Message index of the latest attempted faultdrill apply, or ``-1``."""
        if is_host:
            return -1
        return max(
            (
                e["index"]
                for e in _faultdrill_apply_events(messages)
                if e["attempted"]
            ),
            default=-1,
        )

    def build_fault_handle(self, values: dict) -> Optional[dict]:
        """Claim a committed faultdrill injection from attribution facts.

        Hydration is TWO-STAGE by design (mirrors the derive_handle_from_legacy
        seam): this, the VALUES stage, returns the minimal kind-bearing handle
        — the legacy ``values`` never carry the CR reference (there is no
        legacy field for it), but the kind routes ``resolve_by_handle_kind``
        to THIS provider. The authoritative ns/name ``value`` is hydrated at
        the MESSAGES stage by :meth:`build_handle_from_messages` (the applied
        manifest itself); a values-stage handle with an empty ``value`` is
        therefore legal mid-hydration, never a terminal handle.
        """
        values = values or {}
        if values.get("injection_method") != "faultdrill_cr":
            return None
        return {"kind": "faultdrill_cr", "method": "faultdrill_cr"}

    def extract_experiment_id(self, messages: list, retired=None) -> str:
        """UID-less carrier: there is no experiment id to extract — the
        CR reference is the identity, extracted by
        :meth:`build_handle_from_messages`."""
        return ""

    def build_handle_from_messages(
        self, messages: list, retired=None, values: Optional[dict] = None
    ) -> Optional[dict]:
        """Build the authoritative handle from the applied manifest: the
        LATEST attempted faultdrill apply's document metadata
        (``namespace`` from the manifest, else the ``-n`` flag, else
        ``default`` — kubectl's own resolution order for namespace-
        omitted applies)."""
        events = [
            e for e in _faultdrill_apply_events(messages) if e["attempted"]
        ]
        if not events:
            return None
        meta = events[-1]["meta"] or {}
        name = str(meta.get("name") or "")
        if not name:
            return None
        namespace = str(
            meta.get("namespace") or _namespace_from_v_args(events[-1]["v_args"]) or "default"
        )
        return {
            "kind": "faultdrill_cr",
            "value": f"{namespace}/{name}",
            "method": "faultdrill_cr",
        }

    def created_experiment_ids(self, messages: list, state: dict) -> set[str]:
        """UID-less carrier: no experiment ids exist to prove (the CR
        object's existence is read from the cluster, not the history)."""
        return set()

    def destroyed_experiment_ids(self, messages: list) -> set[str]:
        """UID-less carrier: no destroy calls exist to attribute terminal
        state to (the issued-scan seam's neutral empty contribution)."""
        return set()

    def destroyed_proven_experiment_ids(self, messages: list) -> set[str]:
        """UID-less carrier: no destroy output exists to prove a death
        (the retire ledger's neutral empty contribution)."""
        return set()

    def classify_tool_target(
        self, tool_name: str, tool_args: Any, raw_command: str
    ) -> Optional["EffectiveTarget"]:
        """Claims no tool: ``kubectl`` / ``kubectl_read`` classification
        stays with the k8s_native domain classifier (single owner per
        tool) — the CR apply rides that classification unchanged."""
        return None

    # ------------------------------------------------------------------
    # Artifact ledger (openspec faultdrill-cr-channel task 2.6)
    # ------------------------------------------------------------------

    def collect_artifacts_from_messages(
        self, messages: list, *, task_id: str = "", operation_family: str = "",
    ) -> list[dict]:
        """One artifact per LANDED faultdrill apply — the durable FULL set.

        The fault HANDLE is latest-wins (``_project_fault_handle``), so a
        rename-retry's early CR would otherwise be orphaned with no
        recovery reference (review P11): the artifact ledger records
        EVERY landed apply under the task, keyed by its own ns/name — the
        sweep below is then each object's sweeper of record.

        "Landed" = attempted ∧ result not an error (the shared strict
        prefix rule): a failed or guard-rejected apply created no cluster
        object, so it registers nothing. Namespace resolution matches
        :meth:`build_handle_from_messages` (manifest ns > ``-n`` flag >
        ``default`` — kubectl's own resolution order).
        """
        from chaos_agent.config.settings import settings

        artifacts: list[dict] = []
        for event in _faultdrill_apply_events(messages):
            if not event["attempted"] or _result_is_error(event["result"]):
                continue
            meta = event["meta"] or {}
            name = str(meta.get("name") or "")
            if not name:
                continue
            namespace = str(
                meta.get("namespace")
                or _namespace_from_v_args(event["v_args"])
                or "default"
            )
            artifacts.append({
                "artifact_id": f"faultdrill_cr:{namespace}/{name}",
                "type": "faultdrill_cr",
                "kind": CRD_KIND,
                "status": "active",
                "task_id": task_id,
                "name": name,
                "namespace": namespace,
                "operation_family": operation_family or "faultdrill_cr",
                "created_tool_call_id": event.get("tool_call_id", ""),
                "cleanup": {
                    "tool": "kubectl",
                    "subcommand": "delete",
                    "v_args": (
                        f"{CRD_PLURAL}.{settings.faultdrill_crd_group} {name} "
                        f"-n {namespace} --ignore-not-found"
                    ),
                },
            })
        return artifacts

    async def sweep_artifact(
        self, artifact: Any, *, kubeconfig: str = "", task_id: str = "",
    ) -> Optional[bool]:
        """Claim-based single-artifact sweep, keep-while-Injected.

        ``None`` — not this carrier's artifact (the registry scan moves
        on). ``True`` — settled, mark the artifact ``cleaned``. ``False``
        — keep it; the next sweep round re-examines.

        The lifecycle mirrors the recovery-carrier vehicle family
        (keep-while-armed → sweep after the deadline), transposed to the
        CR's own state: the reconciler flips ``status.phase`` but
        deliberately leaves the OBJECT in place (a Recovered CR is the
        audit record), so this sweep is the object's sweeper of record —
        an Injected CR is still firing (deleting it mid-window would be
        an early recovery), a Recovered or externally-removed one is
        deleted idempotently, and every other state (Pending / Failed /
        unreadable) stays with the recover-convergence path, not this
        sweep.
        """
        if not isinstance(artifact, dict) or artifact.get("type") != "faultdrill_cr":
            return None
        name = str(artifact.get("name") or "")
        namespace = str(artifact.get("namespace") or "")
        if not name or not namespace:
            # Unnameable: nothing sweepable exists — settle the ledger row
            # instead of retrying a fact-free artifact forever.
            return True
        from chaos_agent.config.settings import settings

        result = await _kubectl(
            "get",
            [f"{CRD_PLURAL}.{settings.faultdrill_crd_group}", name, "-n", namespace, "-o", "json"],
            kubeconfig,
        )
        if result.exit_code != 0:
            if "NotFound" in str(result.stderr or ""):
                # Already gone (external removal / never landed visibly):
                # a zero-write settle — the same idempotence class as
                # ``_delete_cr``'s ``--ignore-not-found``.
                return True
            # Read failed for another reason (channel flake, RBAC): keep,
            # retry next round — never mistake an unreadable object for a
            # settled one.
            return False
        try:
            cr_json = json.loads(result.stdout)
        except (ValueError, TypeError):
            return False
        if not isinstance(cr_json, dict):
            return False
        phase = str((cr_json.get("status") or {}).get("phase") or "")
        if phase == PHASE_INJECTED:
            return False
        if phase == PHASE_RECOVERED:
            return await _delete_cr(f"{namespace}/{name}", kubeconfig)
        # Pending / Failed / unknown phases: the recover-convergence
        # path owns their transition (Pending → delete, Failed →
        # best-effort restore + delete); sweeping here could destroy a
        # recipe the replay needs. Keep, re-examine next round.
        return False

    def parse_injection_params(self, tool_name: str, tool_args: dict) -> Optional[dict]:
        """No issue-time key-parameter extraction: the CR manifest IS the
        full fault recipe (patches / restorePatches / invalidSecret /
        durationSeconds), and the readback guard compares it verbatim."""
        return None

    def issue_time_method(
        self, tool_name: str, tool_args: dict, *, is_host: bool = False
    ) -> Optional[str]:
        """Issue-time attribution: a ``kubectl`` apply whose stdin manifest
        declares a FaultDrill document IS this carrier's injection. The
        issue-time path sees the call before any result exists — attempt
        by construction."""
        if tool_name not in self._APPLY_TOOL_NAMES:
            return None
        args = _coerce_args_dict(tool_args)
        if not args or str(args.get("subcommand") or "") != "apply":
            return None
        stdin = str(args.get("stdin_data") or "")
        if not stdin:
            return None
        if _faultdrill_manifest_meta(stdin) is None:
            return None
        return "faultdrill_cr"

    def build_reconcile_fingerprint(
        self, tool_name: str, tool_args: Any
    ) -> Optional["RequestFingerprint"]:
        """Create-reconcile seam: this carrier declares no create under
        the gate (applies are retry-safe), so the fingerprint hook never
        claims — pinned ``None``."""
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
        """Create-reconcile seam: no create under the gate — pinned
        ``None`` (the registry scan continues past this carrier)."""
        return None

    def reconcile_batch_held_feedback(
        self, tool_name: str, other_tool_name: str
    ) -> Optional[str]:
        """Create-reconcile seam: no create under the gate — pinned
        ``None``."""
        return None

    async def rollback_handle(self, handle: dict, **kwargs) -> str:
        """FaultDrill faults are undone by the recover graph's reconcile
        convergence, not by a synchronous failure-path rollback."""
        return ""

    def was_fault_create_attempted(
        self,
        messages: list,
        injection_method: str | None = None,
        *,
        is_teardown=None,
    ) -> bool:
        """Pinned False: whether the CR actually landed is read from the
        CLUSTER (get the CR by handle), never inferred from message
        history — a True here would mis-trigger the recover terminal
        "no UID" branch that belongs to experiment-record carriers."""
        return False

    def scan_step_actions(
        self, steps: list[str], messages: list, *, is_teardown=None,
    ) -> Optional[StepActionScan]:
        """Form B hook: the CR channel's skill cases declare a CR template
        (not kubectl verb steps), so both sides of the step self-check
        are empty — the SOP path's verb vocabulary is untouched."""
        return StepActionScan(required=(), executed=())

    def was_injection_attempted(self, messages: list, *, is_teardown=None) -> bool:
        """Form B hook: a faultdrill apply in the history means THIS
        carrier's injection was attempted (the back-scan after a failed
        ``blade_create`` would find it here)."""
        return any(e["attempted"] for e in _faultdrill_apply_events(messages))

    # ------------------------------------------------------------------
    # Channel installability (faultdrill-cr-channel — design D2/D7)
    # ------------------------------------------------------------------

    async def ensure_crd(self, kubeconfig: str = "") -> dict:
        """Lazy-install / availability hook for the route gate.

        Optional registry hook (getattr-dispatched, NOT a Protocol
        member — same shape as :meth:`arm_session_reconciler`): the
        CR-channel route gate in ``tool_screener`` consults it right
        before admitting a FaultDrill CR apply, so the first legitimate
        CR write triggers the channel's own install decision family
        (probe → programmatic ``kubectl apply -f -`` → Established
        poll — :mod:`.crd_install`), never an LLM-triggered CRD apply
        (D2: the manifest-kind allowlist never admits the CRD itself).
        Returns the verdict as a provider-neutral dict (``usable`` /
        ``status`` / ``reason`` / ``detail``): ``usable=False`` is the
        DEGRADATION signal the gate turns into SOP re-plan guidance
        (D7: a routing branch, never an error).
        """
        from .crd_install import ensure_crd as _ensure_crd

        availability = await _ensure_crd(kubeconfig)
        return {
            "usable": availability.usable,
            "status": availability.status,
            "reason": availability.reason,
            "detail": availability.detail,
        }

    # ------------------------------------------------------------------
    # Post-landing readback guard (M2 task 2.1 — design D5)
    # ------------------------------------------------------------------

    async def verify_landing_readback(
        self, messages: list, state: dict, *, kubeconfig: str = ""
    ) -> Optional[dict]:
        """Readback guard for a freshly-landed faultdrill apply (M2 task
        2.1, design D5 — the v2 experiment's ``exit(5)`` guard, productised).

        Called by the execute loop's post-execution seam through the
        registry AFTER a faultdrill apply has LANDED (attempted and NOT
        an apiserver error — the failed face is ``issue_disproven``'s
        jurisdiction, this guard never re-litigates it). The CR is read
        back through the programmatic transport and ``spec.patches`` /
        ``spec.restorePatches`` must both survive non-empty: a landed CR
        whose recipe was pruned away (an incompatible CRD strips
        undeclared fields) is exactly the bare-injection hazard the v1
        incident legislated against — a reconciler running on an empty
        recipe mutates nothing while the task believes the fault is in
        effect.

        Idempotence: ``None`` for a CR already verified this task
        (``state["fault_readback_verified"]`` carries its handle value;
        the replan seam clears the bookkeeping with the handle, so a
        retry under a fresh epoch re-runs the check). A FAILED verdict
        writes no bookkeeping by design — the hard abort leaves the
        loop, and a remembered failure would mask a genuinely-fixed
        re-apply.

        Returns ``None`` when there is nothing to verify (no landed
        faultdrill apply, or the landing is already verified), else a
        verdict dict: ``ok`` / ``reason`` (``stripped`` — the recipe was
        pruned; ``read-error`` — the read itself failed, fail-closed:
        integrity unproven IS integrity failed; ``unnameable`` — the
        landed manifest carries no ``metadata.name``, the CR cannot
        even be addressed, and no recovery handle can reference it) /
        ``handle`` (``ns/name``) / ``detail`` (evidence for the abort
        message).
        """
        landed = [
            e for e in _faultdrill_apply_events(messages)
            if e["attempted"] and not _result_is_error(e["result"])
        ]
        if not landed:
            return None
        meta = landed[-1]["meta"] or {}
        name = str(meta.get("name") or "")
        if not name:
            # The guard's manifest anchoring does not require a name, so
            # a generateName-style faultdrill apply can land unaddressed
            # — no readback, no handle, no reconcile path. Fail-closed.
            return {
                "ok": False,
                "reason": "unnameable",
                "handle": "",
                "detail": (
                    "the landed FaultDrill manifest carries no metadata.name "
                    "— the CR cannot be addressed for a readback (and no "
                    "recovery handle can reference it)"
                ),
            }
        namespace = str(
            meta.get("namespace")
            or _namespace_from_v_args(landed[-1]["v_args"])
            or "default"
        )
        handle_value = f"{namespace}/{name}"
        if state.get("fault_readback_verified") == handle_value:
            return None
        ok, cr, stderr = await _read_cr_json(handle_value, kubeconfig)
        if not ok:
            return {
                "ok": False,
                "reason": "read-error",
                "handle": handle_value,
                "detail": (
                    "readback get failed: "
                    + (stderr or "empty stderr")[:160]
                ),
            }
        spec = cr.get("spec") if isinstance(cr.get("spec"), dict) else {}
        stripped = [f for f in PRESERVED_ARRAY_FIELDS if not spec.get(f)]
        if stripped:
            return {
                "ok": False,
                "reason": "stripped",
                "handle": handle_value,
                "detail": (
                    "recipe not found intact in the landed CR — empty/absent "
                    + " and ".join(stripped)
                    + " (an incompatible CRD pruned undeclared fields, or a "
                    "recipe-less manifest): reconciling it would be a BARE "
                    "injection on an empty recipe"
                ),
            }
        return {"ok": True, "reason": "", "handle": handle_value, "detail": ""}

    # ------------------------------------------------------------------
    # Session-side reconciler arming (M2 task 2.2 — design D4)
    # ------------------------------------------------------------------

    def arm_session_reconciler(
        self, handle_value: str, kubeconfig: str = ""
    ) -> bool:
        """Arm the background reconcile loop for a verified landing.

        Optional registry hook (getattr-dispatched, NOT a Protocol
        member — this capability exists only on the CR channel, unlike
        the readback guard whose integrity semantics every backend
        owns). The execute loop calls it right after a PASSING readback
        verdict: the landing's recipe survived, so reconciliation is
        safe to start (task 2.1's hard abort fires BEFORE this point on
        a stripped recipe). Idempotent per handle — see
        :func:`reconciler.arm_session_reconciler`.
        """
        from .reconciler import arm_session_reconciler

        return arm_session_reconciler(handle_value, kubeconfig)

    # ------------------------------------------------------------------
    # Layer-1 / recover surface (M2 task 2.3 — see module docstring)
    # ------------------------------------------------------------------

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """Final semantics: Layer 1 is not applicable for the inject
        verification — the CR landing is the RECIPE landing, not the
        fault landing (效果即真理), so verification falls to the Layer-2
        effect judgment (which is the authority anyway). The CR's own
        phase progression is owned by the session reconciler / recover
        replay, not by the verify chain."""
        from chaos_agent.agent.result.verdict import Layer1Result

        return Layer1Result(
            status="skipped",
            details="faultdrill_cr injection (CR channel): the CR landing is recipe bookkeeping, not the fault landing — Layer-2 effect judgment is the authority",
        )

    async def layer1_raw_destroy(self, uid: str, kubeconfig: str = "") -> str:
        """Final semantics: no bare destroy form — recovery is the
        four-state convergence over the CR reference, not a UID destroy;
        the finalize retry seam only fires for UID-bearing carriers
        (its gate is the live experiment UID, always empty here)."""
        return ""

    def classify_destroy_output(self, output: str) -> DestroyOutcome:
        """UID-less carrier: no destroy output exists to classify — the
        empty string is FAILED under the authority anyway; pinned
        explicitly so the protocol stays satisfied."""
        return DestroyOutcome.FAILED

    async def layer1_destroy(
        self,
        uid: str,
        kubeconfig: str = "",
        *,
        messages: list | None = None,
        injection_method: str | None = None,
    ) -> "Layer1Result":
        """Deterministic Layer-1 recovery: the four-state convergence over
        the CR reference (M2 task 2.3, design D4).

        The generic flow's identity key is the experiment UID, which this
        UID-less carrier has none of — the dispatch passes an empty
        ``uid`` and the CR reference (``ns/name``) is hydrated from
        ``messages`` (the applied manifest), the same two-stage
        hydration every identity seam runs. Verdict mapping: converged
        branches report ``passed``; a failed restore/cleanup reports
        ``failed`` (terminal — the caller's non-UID branch then routes to
        Layer 2, which verifies the ACTUAL cluster state: the fault may
        have self-recovered); the not-found residue reports ``skipped``
        (not terminal — Layer 2 judges from cluster evidence).
        """
        from chaos_agent.agent.result.verdict import Layer1Result

        handle_value = str(uid or "") or _handle_value_of(
            self.build_handle_from_messages(list(messages or []))
        )
        if not handle_value:
            return Layer1Result(
                status="skipped",
                details=(
                    "faultdrill_cr: no addressable CR handle for the "
                    "deterministic destroy (uid empty and the history "
                    "yields no CR reference)"
                ),
            )
        verdict = await _recover_converge(handle_value, kubeconfig)
        return Layer1Result(
            status=verdict["status"],
            details=verdict["details"],
        )

    def recovery_vehicle(self, state: dict) -> str:
        """M1 skeleton: no durable recovery-vehicle record yet — M2 adds
        the CR execution artifact (keep-while-Injected lifecycle)."""
        return ""

    def blocks_deterministic_destroy(
        self, state: dict, messages: list | None = None
    ) -> bool:
        """Final semantics: nothing blocks a deterministic reconcile —
        the CR reference is always reachable through the apiserver face
        the recovery replay rides (unlike an in-cluster exec delivery)."""
        return False

    def recovery_facts_render(
        self, state: dict, *, spec_params: dict | None = None
    ) -> str:
        """M1 skeleton: no carrier-owned injection facts rendered yet."""
        return ""

    def merge_deterministic_recover_verdict(
        self, layer1, state: dict, part_override: dict | None = None
    ):
        """Final semantics: this carrier is never the experiment half of a
        combo (no experiment UID, no native partner) — no composite
        verdict to merge, identity."""
        return layer1

    async def recover(
        self, state: dict, handle: Optional[dict], **kwargs
    ) -> RecoverResult:
        """Deterministic four-state recover convergence (M2 task 2.3,
        design D4) — provider-owned; the recover graph names no carrier.

        The CR reference is hydrated two-stage like every identity seam:
        the dispatch handle's ``value`` (``ns/name``) first, then the
        applied manifest in ``messages`` (:meth:`build_handle_from_messages`
        — the same hydration the attribution chain runs). With no
        addressable CR the honest verdict is unrecovered (fail-visible,
        never a fabricated success).

        Verdict mapping: a converged CR (pending-deleted /
        injected-restored / failed-cleaned / already-recovered) reports
        ``recovered`` with the Layer-2 skip note; a failed convergence
        reports ``unrecovered`` + the failure category; the not-found
        residue reports ``unverified`` + a loud warning with zero actions
        (the B85 law: ignorance is never success — and never a fabricated
        failure either; the row stays recoverable).
        """
        from chaos_agent.agent.result.verdict import (
            FailureCategory,
            Layer1Result,
            layer1_to_dict,
        )

        kubeconfig = str(kwargs.get("kubeconfig") or "")
        messages = list(kwargs.get("messages") or [])
        handle_value = _handle_value_of(handle) or _handle_value_of(
            self.build_handle_from_messages(messages)
        )
        if not handle_value:
            layer1 = Layer1Result(
                status="skipped",
                details=(
                    "faultdrill_cr recover: no addressable CR handle "
                    "(the dispatch handle carries no value and the "
                    "message history yields none)"
                ),
            )
            return RecoverResult(
                recovered=False,
                level="unrecovered",
                layer1=layer1_to_dict(layer1),
                layer2={
                    "status": "skipped",
                    "details": "FaultDrill CR unaddressable — no replay possible",
                },
                warnings=(
                    "faultdrill_cr fault: no CR reference could be hydrated "
                    "for recovery — the fault's recipe is unaddressable from "
                    "this task's evidence. Verify the cluster state manually.",
                ),
                experiment_uid="",
                handle=handle,
                failure=(
                    FailureCategory.RECOVERY_FAILED,
                    f"Layer1=skipped, Layer2=skipped, details={layer1.details[:200]}",
                ),
            )
        verdict = await _recover_converge(handle_value, kubeconfig)
        layer1 = Layer1Result(
            status=verdict["status"], details=verdict["details"],
        )
        layer2 = {
            "status": "skipped",
            "details": (
                "FaultDrill CR replay is deterministic (no LLM): judge the "
                "fault effect from cluster evidence if a Layer-2 verdict "
                "is needed"
            ),
        }
        if verdict["status"] == "passed":
            return RecoverResult(
                recovered=True,
                level="recovered",
                layer1=layer1_to_dict(layer1),
                layer2=layer2,
                warnings=(
                    "Layer 2 (fault-specific) recovery verification was "
                    "skipped — the CR replay converged deterministically "
                    "(restorePatches + derived-secret deletion).",
                ),
                experiment_uid="",
                handle=handle,
                failure=None,
            )
        if verdict["status"] == "failed":
            return RecoverResult(
                recovered=False,
                level="unrecovered",
                layer1=layer1_to_dict(layer1),
                layer2=layer2,
                warnings=(
                    f"faultdrill_cr fault ({verdict['handle']}): the "
                    "deterministic replay FAILED — the fault may still be "
                    "active; the CR's restorePatches field self-describes "
                    "the recovery recipe.",
                ),
                experiment_uid="",
                handle=handle,
                failure=(
                    FailureCategory.RECOVERY_FAILED,
                    f"Layer1={layer1.status}, Layer2=skipped, "
                    f"details={layer1.details[:200]}",
                ),
            )
        # not-found: zero actions, honest ignorance (B85 — never success,
        # never a fabricated failure; the row stays recoverable).
        return RecoverResult(
            recovered=False,
            level="unverified",
            layer1=layer1_to_dict(layer1),
            layer2=layer2,
            warnings=(
                f"faultdrill_cr fault: the attributed CR ({verdict['handle']}) "
                "cannot be read and no same-name CR exists in any namespace "
                "— zero recovery actions were taken. Either the apply never "
                "landed (the ATTEMPT high-tolerance attribution residue) or "
                "the CR was removed externally; recovery is UNCONFIRMED, not "
                "guaranteed.",
            ),
            experiment_uid="",
            handle=handle,
            failure=None,
        )

    def layer1_recover_guidance(
        self,
        state: dict,
        experiment_uid: str,
        *,
        combo_native: bool = False,
        combo_part: dict | None = None,
    ) -> str:
        """Deterministic-recover carrier: the four-state convergence IS
        the Layer-1 recovery — there is no LLM-driven experiment guidance
        to contribute (the deterministic dispatch owns this path; the
        guidance seam only fires on the LLM-driven route this carrier
        never takes)."""
        return ""

    def layer2_facts_note(self, state: dict) -> str:
        """Final semantics: no carrier-owned fact lines — the Layer-2
        effect judgment reads CLUSTER evidence (效果即真理); the CR's
        phase is recipe bookkeeping, not an effect verdict."""
        return ""

    def verify_prompt_note(
        self, injection_method: str, *, injection_pod_name: str | None = None
    ) -> str:
        """Post-injection verifier note for the faultdrill carrier.

        Final semantics (not a stub): the CR landing is the RECIPE
        landing, not the fault landing — 效果即真理 holds unchanged, and
        the Layer-2 effect judgment stays the verification authority.
        """
        if injection_method != "faultdrill_cr":
            return ""
        return (
            "\n### Injection Method Note\n"
            "Injection was a declarative FaultDrill CR apply. The CR records the "
            "recipe (patches / restorePatches / TTL); it is bookkeeping, NOT the "
            "verification criterion. Judge the fault's effect from cluster "
            "evidence exactly as for any other carrier — the CR having landed "
            "does not mean the fault has manifested.\n\n"
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
        """Final semantics: neutral empty framing — the deterministic
        replay already converged the cluster; the Layer-2 judgment reads
        cluster evidence, and the CR's own state adds no authority the
        effect verdict lacks."""
        return ("", "")

    def prompt_fragments(self) -> ProviderPrompts:
        return ProviderPrompts()


# ---------------------------------------------------------------------------
# Manifest-kind attribution scans (this carrier's private vocabulary —
# the registry philosophy: each backend recognises its own carrier forms).
# ---------------------------------------------------------------------------


def _coerce_args_dict(tool_args: Any) -> Optional[dict]:
    """Normalize a tool-call args payload to a dict.

    LangChain tool_calls normally carry a dict; some transports persist
    them as a JSON string. Anything else (or unparseable) is not ours.
    """
    if isinstance(tool_args, dict):
        return tool_args
    if isinstance(tool_args, str):
        try:
            parsed = json.loads(tool_args)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _faultdrill_manifest_meta(stdin_data: str) -> Optional[dict]:
    """Metadata dict of the first FaultDrill document in a stdin
    manifest, or ``None`` when no document declares the kind.

    Fail-closed: an unparseable manifest never attributes this carrier.

    First-document semantics is deliberate defense-in-depth: the guard
    legislates FaultDrill manifests SINGLE-document (classifier P7 — a
    second CR's fault would leak with no recovery handle), so under the
    gate there IS only one document. Should a multi-document apply
    somehow arrive through a non-tool path anyway, attributing the
    first document still beats losing the attribution entirely (a
    missing attribution skips recover for BOTH CRs; a doc-1 handle
    recovers at least the first).
    """
    import yaml

    try:
        docs = [d for d in yaml.safe_load_all(stdin_data) if isinstance(d, dict)]
    except yaml.YAMLError:
        return None
    for doc in docs:
        if doc.get("kind") == CRD_KIND:
            meta = doc.get("metadata")
            return meta if isinstance(meta, dict) else {}
    return None


def _namespace_from_v_args(v_args: str) -> str:
    """Namespace named by a ``-n`` / ``--namespace`` flag in the apply's
    ``v_args`` (kubectl's fallback when the manifest omits it)."""
    tokens = (v_args or "").split()
    for i, tok in enumerate(tokens):
        if tok in ("-n", "--namespace") and i + 1 < len(tokens):
            return tokens[i + 1]
        for prefix in ("-n=", "--namespace="):
            if tok.startswith(prefix):
                return tok[len(prefix):]
    return ""


async def _kubectl(
    subcommand: str, v_args: list[str], kubeconfig: str, *,
    stdin_data: str = "", timeout: float = 30.0,
):
    """Single execution seam for the readback guard (patch point for
    tests; lazy tool import — same discipline as ``crd_install._kubectl``).
    The readback is a READ (``get``), admitted by the command-level
    guard face the programmatic transport rides."""
    from chaos_agent.tools.kubectl import exec_kubectl_raw

    return await exec_kubectl_raw(
        subcommand, v_args, kubeconfig, timeout=timeout, stdin_data=stdin_data,
    )


async def _read_cr_json(handle_value: str, kubeconfig: str) -> tuple[bool, dict, str]:
    """``kubectl get faultdrills.<group> <name> -n <ns> -o json`` → ``(ok,
    cr_json, stderr)``.

    ``ok=False`` covers a non-zero exit AND an unparseable / non-object
    payload — fail-closed for the readback guard: integrity unproven IS
    integrity failed, so a garbage read aborts exactly like a stripped
    recipe (the reason label distinguishes them for observability).
    """
    from chaos_agent.config.settings import settings

    namespace, _, name = handle_value.partition("/")
    result = await _kubectl(
        "get",
        [f"{CRD_PLURAL}.{settings.faultdrill_crd_group}", name,
         "-n", namespace, "-o", "json"],
        kubeconfig,
    )
    if result.exit_code != 0:
        return False, {}, result.stderr or ""
    try:
        parsed = json.loads(result.stdout)
    except (ValueError, TypeError):
        return False, {}, "cr get returned unparseable JSON"
    if not isinstance(parsed, dict):
        return False, {}, "cr get returned non-object JSON"
    return True, parsed, ""


# ---------------------------------------------------------------------------
# Deterministic recover convergence (M2 task 2.3 — design D4)
# ---------------------------------------------------------------------------


def _handle_value_of(handle) -> str:
    """CR reference (``ns/name``) rendered from a fault handle."""
    return str((handle or {}).get("value") or "")


def _task_state_of(cr: dict) -> dict:
    """Reconciler ``task_state`` shape projected from a live CR's spec —
    the restore path reads the recipe from the CLUSTER, never from the
    injection history (the controller-death resilience property: the
    recipe survives in the cluster, process memory is irrelevant)."""
    spec = cr.get("spec") if isinstance(cr.get("spec"), dict) else {}
    return {
        "patches": list(spec.get("patches") or []),
        "restore_patches": list(spec.get("restorePatches") or []),
        "invalid_secret": dict(spec.get("invalidSecret") or {}),
        "target_ref": dict(spec.get("targetRef") or {}),
    }


async def _locate_cr_across_namespaces(
    handle_value: str, kubeconfig: str
) -> str:
    """P12 cross-check: the handle's namespace hydration falls back to
    ``default`` when the manifest omits it AND no ``-n`` flag was passed —
    but kubectl itself lands the apply in the CONTEXT namespace, so the
    CR may exist under a different namespace than the handle claims.
    Before judging "experiment lost", list the CR kind across ALL
    namespaces and match by name; a unique match re-addresses the
    recovery. A failed / empty / ambiguous listing returns ``""`` (the
    not-found verdict then stands — P13 accepts the reason-label drift:
    a disguised refusal and a true absence both end in the same
    zero-action degraded terminal)."""
    from chaos_agent.config.settings import settings

    _, _, name = handle_value.partition("/")
    if not name:
        return ""
    r = await _kubectl(
        "get",
        [f"{CRD_PLURAL}.{settings.faultdrill_crd_group}", "-A", "-o", "json"],
        kubeconfig,
    )
    if r.exit_code != 0:
        return ""
    try:
        listing = json.loads(r.stdout)
    except (ValueError, TypeError):
        return ""
    found = ""
    for item in listing.get("items") or []:
        meta = item.get("metadata") if isinstance(item, dict) else None
        if not isinstance(meta, dict) or str(meta.get("name") or "") != name:
            continue
        ns = str(meta.get("namespace") or "")
        if not ns:
            continue
        if found and found != f"{ns}/{name}":
            return ""  # same-name twins in two namespaces — do not guess
        found = f"{ns}/{name}"
    return found


async def _delete_cr(handle_value: str, kubeconfig: str) -> bool:
    """Delete the CR — ``--ignore-not-found`` keeps the delete idempotent
    (a replay after an external removal converges the same way)."""
    from chaos_agent.config.settings import settings

    namespace, _, name = handle_value.partition("/")
    r = await _kubectl(
        "delete",
        [f"{CRD_PLURAL}.{settings.faultdrill_crd_group}", name,
         "-n", namespace, "--ignore-not-found"],
        kubeconfig,
    )
    return r.exit_code == 0


async def _recover_converge(
    handle_value: str, kubeconfig: str, *, _redirected: bool = False
) -> dict:
    """Four-state deterministic convergence over the CR's CURRENT
    cluster state (M2 task 2.3, design D4 — the ``blade-ai recover``
    replay side of the "delayed, never lost" resilience claim).

    Branches (the ``state`` label → action):

    * ``pending``    — no phase landed yet: the recipe never executed, so
      deleting the CR IS the whole convergence (zero inject actions).
    * ``injected``   — reconcile-restore convergence: guard-2 idempotent
      ``restorePatches`` replay + derived-secret deletion, landing
      ``phase=Recovered`` — the SAME convergence walk the session
      reconciler's TTL verdict performs, so a concurrent loop and this
      replay are mutually idempotent (json-patch semantics + the status
      merge patch). A failed restore lands ``phase=Failed`` (fail-visible;
      the next replay takes the ``failed`` terminal-cleanup branch).
    * ``failed``     — D4 terminal cleanup: best-effort restore, then
      delete the CR (the bounded-retry recipe was bad, but its restore
      patches still get one honest attempt).
    * ``recovered``  — zero writes (an idempotent replay of an already
      converged CR — the cluster state itself is the verdict).
    * ``not-found``  — the CR cannot be read AND the P12 cross-namespace
      listing finds no same-name CR anywhere: zero actions + the honest
      "unconfirmable" verdict. This is the residue the ATTEMPT
      high-tolerance attribution rule guarantees exists (an apply that
      failed at the apiserver — CRD not installed — still attributes),
      and it is never a fabricated success.

    Returns ``{"state", "status" ("passed" | "failed" | "skipped"),
    "details", "handle"}``.
    """
    from .reconciler import _do_restore, _set_phase, _utc_now_iso

    ok, cr, stderr = await _read_cr_json(handle_value, kubeconfig)
    if not ok:
        if not _redirected:
            true_handle = await _locate_cr_across_namespaces(
                handle_value, kubeconfig,
            )
            if true_handle and true_handle != handle_value:
                # P12 redirection, exactly once: the handle's ns fallback
                # was wrong; the listing's own ns/name is authoritative.
                return await _recover_converge(
                    true_handle, kubeconfig, _redirected=True,
                )
        return {
            "state": "not-found",
            "status": "skipped",
            "handle": handle_value,
            "details": (
                f"CR {handle_value} unreadable ("
                f"{(stderr or 'no stderr')[:160]}) and no same-name CR "
                "exists in any namespace — zero recovery actions taken "
                "(attribution residue or external deletion; recovery is "
                "unconfirmable, never fabricated)"
            ),
        }
    status = cr.get("status") if isinstance(cr.get("status"), dict) else {}
    phase = str(status.get("phase") or "")
    if phase == PHASE_RECOVERED:
        return {
            "state": "recovered",
            "status": "passed",
            "handle": handle_value,
            "details": (
                "CR already Recovered — zero writes (idempotent replay; "
                "the cluster state itself is the verdict)"
            ),
        }
    if phase == PHASE_FAILED:
        # D4: best-effort restorePatches, then delete the CR.
        restored = await _do_restore(_task_state_of(cr), kubeconfig)
        deleted = await _delete_cr(handle_value, kubeconfig)
        if restored and deleted:
            return {
                "state": "failed",
                "status": "passed",
                "handle": handle_value,
                "details": (
                    "Failed CR: best-effort restore landed + CR deleted "
                    "(terminal cleanup — the bounded-retry recipe's "
                    "restore patches got their one honest attempt)"
                ),
            }
        return {
            "state": "failed",
            "status": "failed",
            "handle": handle_value,
            "details": (
                f"Failed CR terminal cleanup incomplete (restored={restored}, "
                f"cr_deleted={deleted}) — the best-effort restore or the CR "
                "deletion failed; the fault may still be active"
            ),
        }
    if phase == PHASE_INJECTED:
        restored = await _do_restore(_task_state_of(cr), kubeconfig)
        if restored:
            await _set_phase(
                handle_value, kubeconfig, PHASE_RECOVERED,
                {
                    "recoveredAt": _utc_now_iso(),
                    "restoreLog": (
                        "recover replay: restore_patches"
                        "+delete_invalid_secret ok"
                    ),
                },
            )
            return {
                "state": "injected",
                "status": "passed",
                "handle": handle_value,
                "details": (
                    "Injected CR converged: restorePatches replayed "
                    "(guard-2 idempotent) + derived secret deleted, "
                    "phase=Recovered landed"
                ),
            }
        await _set_phase(
            handle_value, kubeconfig, PHASE_FAILED,
            {
                "restoreLog": (
                    "recover replay: restore failed (patches or secret "
                    "deletion errored)"
                ),
            },
        )
        return {
            "state": "injected",
            "status": "failed",
            "handle": handle_value,
            "details": (
                "Injected CR restore FAILED — phase=Failed landed "
                "(fail-visible; the next replay takes the Failed "
                "terminal-cleanup branch)"
            ),
        }
    # Pending (phase empty): the recipe never executed — removal IS the
    # convergence, zero inject actions.
    deleted = await _delete_cr(handle_value, kubeconfig)
    if deleted:
        return {
            "state": "pending",
            "status": "passed",
            "handle": handle_value,
            "details": (
                "Pending CR deleted — the recipe never executed, so "
                "removal is the whole convergence (zero inject actions)"
            ),
        }
    return {
        "state": "pending",
        "status": "failed",
        "handle": handle_value,
        "details": (
            f"Pending CR deletion failed — the recipe never executed but "
            f"the CR ({handle_value}) could not be removed; retry recover"
        ),
    }


def _result_is_error(result_text: str) -> bool:
    """An error verdict on the paired ToolMessage result.

    STRICT PREFIX match on the tool layer's failure-marker contract —
    ``Error: kubectl <sub> ...`` (kubectl.py renders both the exception path
    and the exit-code path with exactly this prefix), the same convention the
    shared scans pin (``content.startswith("Error:")`` in
    message_scanning.py). A substring match would false-fire on embedded
    mentions (e.g. ``ToolGuardError:`` inside a trace) and couple this
    judgement to an unrelated string shape.

    Deliberately NOT distinguishing guard rejections from apiserver errors:
    on the LLM face both render identically (``Error: kubectl <sub>: <free
    text reason>`` — ``render_for_llm`` emits no stable marker), and BOTH
    verdicts are correct downstream anyway — a guard rejection means the
    write never landed, so ``issue_disproven`` revoking the issue-time
    attribution is exactly right, and a post-hoc ``detect`` over-attribution
    converges through the recover path's CR-read ground truth.
    """
    return (result_text or "").lstrip().startswith("Error:")


def _faultdrill_apply_events(messages: list) -> list[dict]:
    """Every ``kubectl apply`` tool call whose stdin manifest declares a
    FaultDrill document, paired with its ToolMessage result.

    Record fields: ``index`` (message index of the AIMessage), ``meta``
    (the FaultDrill document's metadata dict), ``v_args``, ``result``
    (paired ToolMessage content, ``""`` when absent), ``attempted``
    (shared ATTEMPT rule: pre-execution rejections are not attempts),
    ``tool_call_id`` (the apply call's id — the artifact ledger's
    ``created_tool_call_id`` provenance).
    """
    results: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = getattr(msg, "tool_call_id", "") or ""
        if tc_id:
            results[tc_id] = str(getattr(msg, "content", "") or "")

    events: list[dict] = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in (getattr(msg, "tool_calls", None) or []):
            if isinstance(tc, dict):
                tc_id = tc.get("id", "") or ""
                name = str(tc.get("name", "") or "")
                raw_args = tc.get("args", {})
            else:
                tc_id = getattr(tc, "id", "") or ""
                name = str(getattr(tc, "name", "") or "")
                raw_args = getattr(tc, "args", {})
            if name not in FaultDrillProvider._APPLY_TOOL_NAMES:
                continue
            args = _coerce_args_dict(raw_args)
            if not args or str(args.get("subcommand") or "") != "apply":
                continue
            stdin = str(args.get("stdin_data") or "")
            if not stdin:
                continue
            meta = _faultdrill_manifest_meta(stdin)
            if meta is None:
                continue
            result = results.get(tc_id, "")
            events.append(
                {
                    "index": index,
                    "meta": meta,
                    "v_args": str(args.get("v_args") or ""),
                    "result": result,
                    "attempted": not any(
                        marker in result for marker in PRE_EXEC_REJECTION_MARKERS
                    ),
                    "tool_call_id": tc_id,
                }
            )
    return events
