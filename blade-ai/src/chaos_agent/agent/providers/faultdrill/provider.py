"""FaultDrill execution backend provider.

Backend semantics (post faultdrill-cluster-native-recovery M2): the
apiserver-write domain's injection and recovery ride the PROGRAMMATIC
recovery-carrier assembler (``assembler.py``, design ND2) — one tool
call deterministically assembles + verifies + arms + injects the
one-shot cluster-native carrier, and ``blade-ai recover`` replays the
restore recipe from the task ledger (design ND7). There is no
experiment UID: the recovery handle is the recipe-bearing ledger handle.

The legacy CR channel (``kubectl apply`` of a FaultDrill
CustomResource + session-side reconciler) is REMOVED (M2 tasks
2.2/2.3) — its CR-face methods (``ensure_crd`` /
``verify_landing_readback`` / ``arm_session_reconciler``), the CRD
install machinery, the process-level reconcile loop and the CR-reading
recovery convergence are gone. The ``faultdrill_cr`` attribution face
below still recognises CR applies from migration-window sessions (task
2.5 retires the routing cases) — their faults stay recoverable because
the recover replays the recipe from the APPLIED MANIFEST in the ledger
(task 2.3's ND7 rewrite) — and the manifest-kind guard entries stay
(ND3: guards are not migrated — they keep blocking an LLM trying to
apply a FaultDrill CR by hand).

This provider fully owns the per-backend behaviour for the faultdrill
carrier:

- attribution (``detect`` / ``injection_recency`` / ``issue_time_method``):
  the carrier face is the assembler tool call
  (``faultdrill_assemble_carrier``); the legacy face is attributed by
  the stdin MANIFEST DOCUMENT KIND, never by command-line form —
  ``kubectl apply -f -`` is indistinguishable from any other stdin
  manifest apply on the command line, and ``apply`` is deliberately NOT
  in any ``inject_kubectl_subcommands`` set (zero recency competition
  with k8s_native, whose write vocabulary starts at ``scale``/``patch``).
  The scan pairs each AIMessage tool call with its ToolMessage result
  and applies the shared ATTEMPT rule (pre-execution rejections are not
  attempts; timeouts and non-zero exits are).
- Layer-1 / recover (``has_deterministic_recover=True``): BOTH faces
  replay the restore recipe from the task ledger (design ND7, task 2.3):
  the recipe-bearing fault_handle (the assembler receipt or the applied
  manifest's spec) drives ONE guard-2 idempotent replay
  (``_replay_restore_recipe`` → ``restore._do_restore``), mutually
  idempotent with the carrier timer; the CR-reading four-state
  convergence and its four CR helpers are retired.
- tools: ONE of its own — the programmatic recovery-carrier assembler
  (``faultdrill_assemble_carrier``, ``assembler.py``, openspec
  faultdrill-cluster-native-recovery M1): the EXECUTE-phase tool that
  deterministically assembles + verifies + arms + injects the one-shot
  recovery carrier, `blade_create`-style (design ND2). Its guard
  classification is claimed by ``classify_tool_target`` (ordinary net
  on the approved targetRef), and its receipts register the carrier
  artifact through ``collect_artifacts_from_messages`` (task 1.5).
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Optional

from langchain_core.messages import AIMessage, ToolMessage

from .crd import (
    CRD_KIND,
    CRD_PLURAL,
)
from .declaration import CARRIER_ID, SUPPORTED_ACTIONS, SUPPORTED_TARGETS
from .assembler import ASSEMBLER_TOOL_NAME, parse_receipt
from chaos_agent.agent.providers.base import (
    DestroyOutcome,
    ProviderPrompts,
    RecoverResult,
    StepActionScan,
)
from chaos_agent.agent.providers.message_scanning import (
    PRE_EXEC_REJECTION_MARKERS,
    is_budget_expiry_unknown,
    reached_target,
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
    # Two faces, one backend: ``faultdrill_cr`` (the CR apply riding the
    # standard kubectl face) and ``faultdrill_carrier`` (the programmatic
    # assembler tool — faultdrill-cluster-native-recovery M1; R2 review
    # Bug#3: without its own attribution the fault-handle projection
    # never claims an assembler injection, stranding the recover/verify
    # ledger chain). Both resolve to THIS provider through the method
    # index. The handles they build share the provider's ``handle_kind``:
    # kind is the provider-level ownership key (``resolve_by_handle_kind``
    # and the deterministic-recover identity gate both match
    # ``provider.handle_kind``), while the ``method`` field names the
    # face — so a carrier-face handle routes its deterministic destroy
    # exactly like a CR handle, and the recipe fields
    # (``target_ref`` / ``restore_patches``) ride along inside the dict.
    injection_methods = ("faultdrill_cr", "faultdrill_carrier")
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
    # The assembler reports failures STRUCTURALLY: its honest receipt is
    # JSON by design and its exceptions are caught in-tool (assembler.py's
    # ``# noqa: BLE001 — honest receipt, never a crash``), so a failure
    # carries neither the ``Error:`` prefix nor ``status="error"`` and the
    # generic verdict cannot see it. This backend reads its own receipt.
    result_shape_tool_names = frozenset({ASSEMBLER_TOOL_NAME})

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
        """Phase tools contributed to the factory tool union.

        EXECUTE → the programmatic recovery-carrier assembler
        (``faultdrill_assemble_carrier``, openspec
        faultdrill-cluster-native-recovery M1 / design ND2 — the
        ``blade_create`` precedent: the LLM decides ONCE, the tool
        deterministically assembles + verifies + arms + injects). The
        CR apply keeps riding the standard ``kubectl`` face for the
        migration window (ND9: M2 removes it), so every existing
        tool-face guard still sees the CR apply verbatim.

        Every other phase → nothing (the carrier is an EXECUTE-time
        asset; planning/verification ride the standard kubectl tools)."""
        from chaos_agent.agent.providers.base import EXECUTE

        if phase == EXECUTE:
            from .assembler import faultdrill_assemble_carrier

            return [faultdrill_assemble_carrier]
        return []

    def detect(self, messages: list, *, is_host: bool, is_teardown=None) -> Optional[str]:
        """Classify as ``faultdrill_cr`` when a kubectl apply carrying a
        FaultDrill stdin manifest was ATTEMPTED on a cluster channel, or
        as ``faultdrill_carrier`` when the assembler tool was attempted
        (faultdrill-cluster-native-recovery M1) — the LATEST family
        evidence names the face (a migration-window session may carry
        both; pure sessions see exactly one).

        Attribution keys on the injection ATTEMPT (AIMessage tool_calls),
        the shared high-tolerance rule: a pre-execution rejection (guard
        reject / validation error) is not an attempt; a timeout or
        non-zero exit still is.

        ``is_teardown`` is accepted for interface uniformity and ignored:
        neither face is a registered-vehicle teardown — teardowns are
        deletes, and these scans only match the apply (stdin manifest
        declares a FaultDrill document) and the assembler tool call.
        """
        if is_host:
            return None
        apply_events = [
            e for e in _faultdrill_apply_events(messages) if e["attempted"]
        ]
        asm_events = [
            e for e in _assembler_call_events(messages) if e["attempted"]
        ]
        if not apply_events and not asm_events:
            return None
        if asm_events and (
            not apply_events
            or asm_events[-1]["index"] > apply_events[-1]["index"]
        ):
            return "faultdrill_carrier"
        return "faultdrill_cr"

    def issue_disproven(self, messages: list, *, is_teardown=None) -> bool:
        """Counter-evidence for an issue-time attribution: the MOST
        RECENT faultdrill apply never landed — either an apiserver-visible
        error (``Error:`` prefix) OR a pre-execution rejection — AND no
        earlier apply in the epoch LANDED (a landed CR keeps the
        attribution live; a later failed re-apply never revokes it).

        The pre-execution-rejection face is load-bearing: the cr-channel
        route gate (tool_screener) intercepts the apply BEFORE dispatch and
        renders ``[target_guard] REJECT_BANNED``, which never carries the
        kubectl-layer ``Error:`` prefix that ``_result_is_error`` matches.
        Without this face a route-gate rejection leaves the issue-time
        ``faultdrill_cr`` attribution committed for a CR that was never even
        sent — poisoning verifier routing (Layer-1 skips it as "CR
        bookkeeping") and the recover dispatch (the handle points at a
        non-existent CR). ``reached_target`` shares PRE_EXEC_REJECTION_MARKERS
        with the ``attempted`` rule, so both faces of "never landed" draw on
        one vocabulary instead of drifting apart.

        The ASSEMBLER face (faultdrill-cluster-native-recovery M1) gets the
        same revocation through ``parse_receipt``'s registerability
        verdict — the face's landed/not-landed split by construction:
        ``failed`` receipts cleaned their stack (the fail-closed contract)
        and unparseable results (a screener rejection renders as
        ``[target_guard]`` free text) never armed anything, while
        ``success``/``partial`` both leave an ARMED carrier (partial =
        armed-but-unconfirmed — the fault may be live). The LATEST face
        owns the verdict (the same latest-face rule :meth:`detect`
        arbitrates with): an assembler session with an earlier landed CR
        keeps the CR evidence out of the judgement, and vice versa."""
        apply_events = _faultdrill_apply_events(messages)
        asm_events = _assembler_call_events(messages)
        if asm_events and (
            not apply_events
            or asm_events[-1]["index"] > apply_events[-1]["index"]
        ):
            landed_earlier = any(
                parse_receipt(e["result"]) is not None
                for e in asm_events[:-1]
            )
            if landed_earlier:
                return False
            return parse_receipt(asm_events[-1]["result"]) is None
        events = apply_events
        if not events:
            return False
        latest = events[-1]
        latest_result = latest["result"]
        # R66 (fourth case of the B39/B40 family): a caller-budget expiry
        # (R57/R59 render it behind an "Error:" head as outcome-UNKNOWN)
        # is UNJUDGEABLE, never a "never landed" verdict — for a
        # millisecond apiserver apply it most likely means the CR IS on
        # the cluster, so revoking would orphan a live recipe entity.
        # Same third-state predicate the native carrier's revocation
        # scan routes through (message_scanning.is_budget_expiry_unknown,
        # the single source); the direction here is "keep the
        # attribution", matching the host carrier's unconditional False.
        latest_never_landed = (
            not is_budget_expiry_unknown(latest_result)
            and (
                _result_is_error(latest_result)
                or not reached_target(latest_result)
            )
        )
        if not latest_never_landed:
            return False
        # The shield: an EARLIER budget expiry also counts as having
        # LANDED (the CR was most probably created before the local wait
        # died) — without this widening a UNKNOWN-then-rejected retry
        # pair would revoke a live CR despite the landed shield.
        landed_earlier = any(
            e["attempted"]
            and (
                not _result_is_error(e["result"])
                or is_budget_expiry_unknown(e["result"])
            )
            for e in events[:-1]
        )
        return not landed_earlier

    def injection_recency(
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> int:
        """Message index of the latest attempted event on EITHER face (CR
        apply or assembler call), or ``-1`` — the same latest-face rule
        :meth:`detect` arbitrates with."""
        if is_host:
            return -1
        indices = [
            e["index"]
            for e in _faultdrill_apply_events(messages)
            if e["attempted"]
        ] + [
            e["index"]
            for e in _assembler_call_events(messages)
            if e["attempted"]
        ]
        return max(indices, default=-1)

    def build_fault_handle(self, values: dict) -> Optional[dict]:
        """Claim a committed faultdrill injection from attribution facts
        (either face).

        Hydration is TWO-STAGE by design (mirrors the derive_handle_from_legacy
        seam): this, the VALUES stage, returns the minimal kind-bearing handle
        — the legacy ``values`` never carry the CR reference (there is no
        legacy field for it), but the kind routes ``resolve_by_handle_kind``
        to THIS provider. The authoritative ns/name ``value`` is hydrated at
        the MESSAGES stage by :meth:`build_handle_from_messages` (the applied
        manifest itself — or, for the assembler face, the receipt's
        ``artifact.recovery_handle`` recipe); a values-stage handle with an
        empty ``value`` is therefore legal mid-hydration, never a terminal
        handle.
        """
        values = values or {}
        method = values.get("injection_method")
        if method == "faultdrill_carrier":
            return {"kind": "faultdrill_cr", "method": "faultdrill_carrier"}
        if method != "faultdrill_cr":
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
        """Build the authoritative handle for whichever face acted LAST
        (the same latest-face rule :meth:`detect` arbitrates with — a
        migration-window session may carry both faces; pure sessions see
        exactly one).

        CR face: the LATEST attempted faultdrill apply's document
        metadata (``namespace`` from the manifest, else the ``-n`` flag,
        else ``default`` — kubectl's own resolution order for
        namespace-omitted applies) PLUS the spec's restore-relevant
        recipe (``targetRef`` / ``restorePatches`` / ``invalidSecret``
        — design ND7, task 2.3: the handle is recipe-bearing so the
        ledger-model recover replays from the LEDGER, never a
        cluster-side CR read; a recipe-less manifest yields the bare
        ns/name reference and the honest no-recipe recover). Assembler
        face: the recipe rebuilt from the LATEST REGISTERABLE receipt
        (``success``/``partial`` — an armed carrier); the tool strips the
        top-level ``recovery_handle`` before returning, so the receipt's
        ``artifact.recovery_handle`` is the only durable recipe copy in
        the history (R2 review Bug#3)."""
        apply_events = [
            e for e in _faultdrill_apply_events(messages) if e["attempted"]
        ]
        asm_events = [
            e for e in _assembler_call_events(messages) if e["attempted"]
        ]
        if asm_events and (
            not apply_events
            or asm_events[-1]["index"] > apply_events[-1]["index"]
        ):
            return _assembler_handle_from_events(asm_events)
        if not apply_events:
            return None
        latest = apply_events[-1]
        meta = latest["meta"] or {}
        name = str(meta.get("name") or "")
        if not name:
            return None
        namespace = str(
            meta.get("namespace")
            or _namespace_from_v_args(latest["v_args"])
            or "default"
        )
        handle = {
            "kind": "faultdrill_cr",
            "value": f"{namespace}/{name}",
            "method": "faultdrill_cr",
        }
        handle.update(latest.get("recipe") or {})
        return handle

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
        """Claims the assembler tool's guard classification; ``kubectl``
        / ``kubectl_read`` stay with the k8s_native domain classifier
        (single owner per tool — the CR apply rides that classification
        unchanged).

        The assembler call IS a mutation of the approved target (the
        tool injects the fault patches on targetRef), so the classifier
        answers the ORDINARY net: scope = canonicalised targetRef kind,
        namespace + names from the call — the drift comparison then
        holds the call to the approved identity exactly as a kubectl
        patch would be. The carrier stack itself is provider-side
        machinery (created inside the tool, never a tool_call), and the
        vehicle exemption for post-assembly execs rides the receipt-
        registered ``recovery_carrier`` artifact, not this classifier."""
        from chaos_agent.agent.target_guard.types import (
            ConfidenceLevel,
            EffectiveTarget,
            SCOPE_UNKNOWN,
        )

        from .assembler import ASSEMBLER_TOOL_NAME, canonical_kind

        if tool_name != ASSEMBLER_TOOL_NAME:
            return None
        args = _coerce_args_dict(tool_args) or {}
        try:
            kind = canonical_kind(str(args.get("target_kind") or ""))
        except ValueError:
            return EffectiveTarget(
                scope=SCOPE_UNKNOWN,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.UNKNOWN,
                reject_detail=(
                    f"{ASSEMBLER_TOOL_NAME} called with an unmappable "
                    "target_kind — the assembler refuses unknown kinds and "
                    "so does the guard"
                ),
                reject_suggestion=(
                    "Use a namespaced kind the recovery-carrier standard "
                    "maps (Deployment, Service, ConfigMap, ...)."
                ),
            )
        namespace = str(args.get("target_namespace") or "") or "default"
        names = (str(args.get("target_name") or "").strip(),)
        if not names[0]:
            return EffectiveTarget(
                scope=SCOPE_UNKNOWN,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.UNKNOWN,
                reject_detail=f"{ASSEMBLER_TOOL_NAME} called without target_name",
                reject_suggestion="Pass the approved target's name explicitly.",
            )
        return EffectiveTarget(
            scope=kind,
            namespace=namespace,
            names=names,
            raw_command=raw_command,
        )

    def tool_result_error_text(
        self, tool_name: str, content: str
    ) -> Optional[str]:
        """Failure verdict on an assembler receipt (``status == "failed"``).

        Only ``failed`` is a failure. ``success`` and ``partial`` are both
        REGISTERABLE receipts (:func:`parse_receipt`'s verdict) — ``partial``
        means the carrier is armed and the injection is unconfirmed, which is
        a live liability to recover, not a call that never happened, so
        reporting it as a plain failure would mislead the replan context.
        Every success/partial receipt also carries an ``error`` key (empty on
        success), so the status field is the only sound discriminator.

        The evidence returned is the receipt's own ``error`` plus the first
        failed step's detail — the step detail is what carries the underlying
        kubectl stderr, which is the text ``errors.classify_error`` can
        actually match on. Unparseable bodies abstain: a compacted receipt
        (``memory.tool_compactor`` truncates by bytes) is JSON-shaped but not
        valid JSON, and unknown is not success.
        """
        if tool_name != ASSEMBLER_TOOL_NAME:
            return None
        from chaos_agent.agent.tool_verdicts import loads_dict

        receipt = loads_dict(content)
        if receipt is None or receipt.get("status") != "failed":
            return None
        reason = str(receipt.get("error") or "").strip()
        parts = [reason or f"{ASSEMBLER_TOOL_NAME} reported status=failed"]
        steps = receipt.get("steps")
        if isinstance(steps, list):
            failed_step = next(
                (
                    s for s in steps
                    if isinstance(s, dict) and not s.get("ok", True)
                ),
                None,
            )
            if failed_step is not None:
                detail = str(failed_step.get("detail") or "").strip()
                if detail:
                    parts.append(
                        f"step {failed_step.get('step') or '?'}: {detail}"
                    )
        cleanup = [
            str(c).strip()
            for c in (receipt.get("cleanup_failures") or [])
            if str(c).strip()
        ]
        if cleanup:
            # Residue left behind by the failed attempt — the recover sweep
            # needs it named even though it is not the failure's cause.
            parts.append("cleanup residue: " + "; ".join(cleanup))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Artifact ledger (openspec faultdrill-cr-channel task 2.6)
    # ------------------------------------------------------------------

    def collect_artifacts_from_messages(
        self, messages: list, *, task_id: str = "", operation_family: str = "",
    ) -> list[dict]:
        """Artifacts from this carrier's landed operations.

        Two families, one ledger:

        - CR applies (openspec faultdrill-cr-channel task 2.6): one
          artifact per LANDED faultdrill apply — the durable FULL set.
          The fault HANDLE is latest-wins (``_project_fault_handle``),
          so a rename-retry's early CR would otherwise be orphaned with
          no recovery reference (review P11): the artifact ledger
          records EVERY landed apply under the task, keyed by its own
          ns/name — the sweep below is then each object's sweeper of
          record.

          "Landed" = attempted ∧ result not an error (the shared strict
          prefix rule): a failed or guard-rejected apply created no
          cluster object, so it registers nothing. Namespace resolution
          matches :meth:`build_handle_from_messages` (manifest ns >
          ``-n`` flag > ``default`` — kubectl's own resolution order).
        - Assembler receipts (faultdrill-cluster-native-recovery M1,
          task 1.5 — the receipt-transport branch): a successful/
          partial ``faultdrill_assemble_carrier`` result carries the
          PRE-BUILT ``recovery_carrier`` artifact dict (schema-equivalent
          to the LLM path's screener registration). This hook only
          MOVES it — filling the id provenance fields only a ToolMessage
          can supply (``created_tool_call_id`` / ``host_exec_*``) —
          exactly the receipt-driven semantics of
          ``_attach_recovery_carrier_rbac``. ``failed`` receipts cleaned
          their stack and register nothing (a deleted pod must not wire
          the vehicle exemption chain).
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
        artifacts.extend(
            _assembler_receipt_artifacts(messages, task_id=task_id),
        )
        return artifacts

    async def sweep_artifact(
        self, artifact: Any, *, kubeconfig: str = "", task_id: str = "",
    ) -> Optional[bool]:
        """Claim-based single-artifact sweep.

        ``None`` — not this carrier's artifact (the registry scan moves
        on). ``True`` — settled, mark the artifact ``cleaned``. ``False``
        — keep it; the next sweep round re-examines.

        Ledger-era semantics (task 2.3): the CR channel is gone, so a
        ``faultdrill_cr`` artifact is a LEFTOVER OBJECT of a
        migration-window apply — and the recipe it used to carry lives
        in the task ledger now (the applied manifest in the message
        history), so deleting the object can never destroy a recovery
        the replay needs (the old keep-while-Injected gate protected a
        recipe that no longer lives in the CR, and with no reconciler
        left the phase would never transition anyway). The sweep is
        therefore simply the object's sweeper of record: ONE idempotent
        delete (``--ignore-not-found`` — an external removal converges
        the same way); a refused delete keeps the row for the next round
        (never mistake an unwritable object for a settled one).
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
            "delete",
            [f"{CRD_PLURAL}.{settings.faultdrill_crd_group}", name,
             "-n", namespace, "--ignore-not-found"],
            kubeconfig,
        )
        return result.exit_code == 0

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
        if tool_name == ASSEMBLER_TOOL_NAME:
            # The assembler face: one call IS the whole injection chain
            # (build → verify → arm → inject), so the attempt attributes
            # exactly like an apply — the FAILED face (a failed receipt
            # cleaned its stack) is ``issue_disproven``'s to revoke.
            return "faultdrill_carrier"
        if tool_name not in self._APPLY_TOOL_NAMES:
            return None
        args = _coerce_args_dict(tool_args)
        if not args or str(args.get("subcommand") or "") != "apply":
            return None
        stdin = str(args.get("stdin_data") or "")
        if not stdin:
            return None
        if _faultdrill_manifest_doc(stdin) is None:
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
        """Pinned False: the recovery identity is the ledger recipe
        (hydrated by the recover/destroy seams), never an
        experiment-record creation a True here would claim — the
        terminal "no UID" branch belongs to experiment-record carriers."""
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
    # Layer-1 / recover surface (M2 task 2.3 — see module docstring)
    # ------------------------------------------------------------------

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """Final semantics: Layer 1 is not applicable for the inject
        verification — the recipe landing is not the fault landing
        (效果即真理), so verification falls to the Layer-2 effect judgment
        (which is the authority anyway). The recovery convergence itself
        is owned by the recover/destroy ledger replay, not by the verify
        chain."""
        from chaos_agent.agent.result.verdict import Layer1Result

        return Layer1Result(
            status="skipped",
            details="faultdrill injection: the recipe landing is bookkeeping, not the fault landing — Layer-2 effect judgment is the authority",
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
        artifacts: list | None = None,
    ) -> "Layer1Result":
        """Deterministic Layer-1 recovery: the ledger-model
        restore-recipe replay (M2 task 2.3, design ND7).

        The generic flow's identity key is the experiment UID, which this
        UID-less carrier has none of — the dispatch passes an empty ``uid``
        and the recipe-bearing handle hydrates from EVIDENCE instead, through
        the same two-source rung :meth:`_recipe_face_handle` gives the
        ``recover`` entry: ``messages`` first (the applied manifest's spec or
        the assembler receipt — either face), then the
        ``execution_artifacts`` ledger. Both are needed because neither
        survives every entry — a cross-task recover inherits no messages at
        all, and within one task ``memory.tool_compactor`` truncates the
        receipt outside the recent window. ONE replay through the shared
        guard-2 core either way. Verdict mapping: a converged replay reports
        ``passed``; a failed replay reports ``failed`` (terminal — the
        caller's non-UID branch then routes to Layer 2, which verifies the
        ACTUAL cluster state: the fault may have self-recovered); no
        replayable recipe reports ``skipped`` (not terminal — Layer 2 judges
        from cluster evidence).
        """
        from chaos_agent.agent.result.verdict import Layer1Result

        if str(uid or ""):
            return Layer1Result(
                status="skipped",
                details=(
                    "faultdrill: a uid-bearing dispatch never belongs to "
                    "this UID-less carrier (defensive — the identity "
                    "gates route uid dispatches to experiment carriers)"
                ),
            )
        # Same two-source rung the ``recover`` entry uses, with no dispatch
        # handle to honour (this entry's identity key — the UID — is empty by
        # construction for a UID-less carrier). Routing through the shared
        # helper rather than calling ``build_handle_from_messages`` directly
        # is what keeps the ledger rung from being a recover-only path.
        hydrated = self._recipe_face_handle(
            None, list(messages or []), list(artifacts or []),
        )
        verdict = await _replay_restore_recipe(hydrated or {}, kubeconfig)
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
        """Deterministic ledger-model recover (M2 task 2.3, design ND7)
        — provider-owned; the recover graph names no carrier.

        The recipe-bearing handle hydrates two-stage like every identity
        seam: a dispatch handle that already carries the recipe
        (``restore_patches`` / ``invalid_secret``) is terminal — the
        persisted ledger recipe IS the recovery identity — else the
        recipe hydrates from ``messages``
        (:meth:`build_handle_from_messages`: the assembler receipt or
        the applied manifest, either face). With no replayable recipe
        the honest verdict is unrecovered (fail-visible, never a
        fabricated success).

        Verdict mapping: a converged replay reports ``recovered`` with
        the Layer-2 skip note; a failed replay reports ``unrecovered`` +
        the failure category; no replayable recipe reports
        ``unrecovered`` + RECOVERY_FAILED with a zero-action Layer-1.
        """
        from chaos_agent.agent.result.verdict import (
            FailureCategory,
            Layer1Result,
            layer1_to_dict,
        )

        kubeconfig = str(kwargs.get("kubeconfig") or "")
        messages = list(kwargs.get("messages") or [])
        # ``state`` is the ledger's only carrier into this entry: a
        # cross-task recover reaches here with an EMPTY ``messages`` (the
        # graph boundary drops inject history) while ``execution_artifacts``
        # rode across, so reading the recipe from messages alone made the
        # durable copy invisible — see ``recipe_ledger`` for both failure
        # modes this closes.
        full = self._recipe_face_handle(
            handle, messages, list((state or {}).get("execution_artifacts") or []),
        )
        if full is None:
            layer1 = Layer1Result(
                status="skipped",
                details=(
                    "faultdrill recover: no addressable recipe (the "
                    "dispatch handle carries none and the message "
                    "history yields no faultdrill face)"
                ),
            )
            return RecoverResult(
                recovered=False,
                level="unrecovered",
                layer1=layer1_to_dict(layer1),
                layer2={
                    "status": "skipped",
                    "details": "FaultDrill recipe unaddressable — no replay possible",
                },
                warnings=(
                    "faultdrill fault: no restore recipe could be "
                    "hydrated for recovery — the fault's recipe is "
                    "unaddressable from this task's evidence. Verify "
                    "the cluster state manually.",
                ),
                experiment_uid="",
                handle=handle,
                failure=(
                    FailureCategory.RECOVERY_FAILED,
                    f"Layer1=skipped, Layer2=skipped, details={layer1.details[:200]}",
                ),
            )
        return await self._recover_from_ledger(
            full, kubeconfig, dispatch_handle=handle,
        )

    def _recipe_face_handle(
        self, handle: Optional[dict], messages: list,
        artifacts: Optional[list] = None,
    ) -> Optional[dict]:
        """The recipe-bearing handle for THIS recover, or ``None``.

        The dispatch handle is authoritative on the face; a
        recipe-bearing dispatch handle is terminal (never re-hydrated —
        the persisted ledger recipe IS the recovery identity, whichever
        face wrote it). A narrow dispatch handle (the values stage, or a
        face-pinned handle whose history no longer yields that face's
        recipe) hydrates from EVIDENCE — message history first, the
        ``execution_artifacts`` ledger second; a hydration that lands on
        the OTHER face is face drift — the narrow handle returns as-is
        for the honest no-recipe failure, never a silent re-route. With
        no dispatch handle at all (the claim-4 method dispatch), the
        LATEST face in the history decides.

        The ledger is a FALLBACK rung, not a preference: the message
        history stays first because it is the only source that can express
        BOTH faces (the applied CR manifest as well as the carrier
        receipt), so promoting the ledger would silently narrow the CR
        face. What the ledger adds is durability — it survives
        ``memory.tool_compactor`` truncating the receipt to unparseable
        JSON inside one task, and it survives the cross-task graph
        boundary, which drops messages entirely
        (the recover-state builder in ``state_mgmt.recovery_state`` starts
        from ``recover_reset_state()`` and flattens inject history into the
        ``inject_context`` string) while carrying ``execution_artifacts``
        across on both the checkpoint and the TaskSnapshot leg. Both rungs
        share ONE projection (``recipe_ledger.project_recipe_handle``), so
        the recipe completeness rule cannot drift between them."""
        from .recipe_ledger import recipe_from_artifacts

        def _recipe_bearing(h: Optional[dict]) -> bool:
            return bool(
                (h or {}).get("restore_patches")
                or ((h or {}).get("invalid_secret") or {}).get("name")
            )

        def _face_matches(h: Optional[dict], method: str) -> bool:
            return _recipe_bearing(h) and (
                not method
                or str((h or {}).get("method") or "") == method
            )

        if handle:
            if _recipe_bearing(handle):
                return handle
            method = str(handle.get("method") or "")
            hydrated = self.build_handle_from_messages(messages)
            if _face_matches(hydrated, method):
                return hydrated
            # Ledger rung, under the SAME face rule: a dispatch pinned to the
            # CR face must not be re-routed onto a carrier recipe, so the
            # method travels into the ledger read and a mismatch yields
            # nothing rather than a plausible-looking other-face recipe.
            from_ledger = recipe_from_artifacts(artifacts, method=method)
            if from_ledger is not None:
                return from_ledger
            return handle
        hydrated = self.build_handle_from_messages(messages)
        if _recipe_bearing(hydrated):
            return hydrated
        return recipe_from_artifacts(artifacts)

    async def _recover_from_ledger(
        self, handle: dict, kubeconfig: str, *, dispatch_handle: Optional[dict] = None,
    ) -> RecoverResult:
        """The ledger-model deterministic recover for EITHER face
        (design ND7 — ``blade-ai recover`` 从任务台账重放配方，与载体
        双执行幂等，作提前收敛兜底): replay the restore recipe through
        the shared guard-2 core (:func:`_replay_restore_recipe`). The
        carrier stack itself is NOT torn down here: the armed gate /
        deadline sweep owns it, and restore_patches carry baseline
        values, so the carrier's later timer fire is an idempotent no-op
        replay rather than a lost recovery.

        Verdict mapping: replay converged → ``recovered`` with the
        Layer-2 skip note; a failed replay → ``unrecovered`` +
        RECOVERY_FAILED (fail-visible); no replayable recipe →
        ``unrecovered`` (the honest verdict — never a fabricated
        success)."""
        from chaos_agent.agent.result.verdict import (
            FailureCategory,
            Layer1Result,
            layer1_to_dict,
        )

        verdict = await _replay_restore_recipe(handle, kubeconfig)
        layer1 = Layer1Result(
            status=verdict["status"], details=verdict["details"],
        )
        layer2 = {
            "status": "skipped",
            "details": (
                "Restore-recipe replay is deterministic (no LLM): judge "
                "the fault's disappearance from cluster evidence if a "
                "Layer-2 verdict is needed"
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
                    "skipped — the restore recipe replayed "
                    "deterministically (guard-2 idempotent). Any armed "
                    "carrier stays until its deadline sweep; its timer "
                    "fire is an idempotent no-op replay of the same "
                    "baseline patches.",
                ),
                experiment_uid="",
                handle=dispatch_handle,
                failure=None,
            )
        if verdict["status"] == "failed":
            return RecoverResult(
                recovered=False,
                level="unrecovered",
                layer1=layer1_to_dict(layer1),
                layer2=layer2,
                warnings=(
                    f"faultdrill fault ({handle.get('value') or handle.get('method') or 'ledger'}): "
                    "the deterministic restore replay FAILED — the fault "
                    "may still be active; an armed carrier remains the "
                    "recovery of record until its own deadline.",
                ),
                experiment_uid="",
                handle=dispatch_handle,
                failure=(
                    FailureCategory.RECOVERY_FAILED,
                    f"Layer1={layer1.status}, Layer2=skipped, "
                    f"details={layer1.details[:200]}",
                ),
            )
        # skipped: no replayable recipe — the honest zero-action failure.
        return RecoverResult(
            recovered=False,
            level="unrecovered",
            layer1=layer1_to_dict(layer1),
            layer2=layer2,
            warnings=(
                "faultdrill fault: no restore recipe could be hydrated "
                "for recovery — the fault's recipe is unaddressable from "
                "this task's evidence. Verify the cluster state manually.",
            ),
            experiment_uid="",
            handle=dispatch_handle,
            failure=(
                FailureCategory.RECOVERY_FAILED,
                f"Layer1=skipped, Layer2=skipped, details={layer1.details[:200]}",
            ),
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


def _faultdrill_manifest_doc(stdin_data: str) -> Optional[dict]:
    """The first FaultDrill document in a stdin manifest, or ``None``
    when no document declares the kind.

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
            return doc
    return None


def _cr_recipe_from_spec(spec: Any) -> dict:
    """Restore-relevant recipe projection of a FaultDrill manifest's
    ``spec`` (design ND7, task 2.3): ``targetRef`` + ``restorePatches``
    + ``invalidSecret`` — the fields the ledger-model recover replays.
    Inject-side fields (``patches`` / ``durationSeconds``) are
    deliberately excluded: the recovery handle is the RECOVERY identity,
    not the fault description. A recipe-less spec (or a non-dict) yields
    ``{}`` — the handle stays the bare ns/name reference and the honest
    no-recipe recover is fail-visible."""
    if not isinstance(spec, dict):
        return {}
    recipe: dict = {}
    target = spec.get("targetRef")
    if isinstance(target, dict) and target.get("name"):
        recipe["target_ref"] = dict(target)
    restore = spec.get("restorePatches")
    if isinstance(restore, list):
        ops = [op for op in restore if isinstance(op, dict)]
        if ops:
            recipe["restore_patches"] = ops
    invalid_secret = spec.get("invalidSecret")
    if isinstance(invalid_secret, dict) and invalid_secret.get("name"):
        recipe["invalid_secret"] = dict(invalid_secret)
    return recipe


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
    """Single execution seam for the restore replay's target readback
    and patch, and the sweep's delete (patch point for tests; lazy tool
    import). The readback is a READ (``get``), admitted by the
    command-level guard face the programmatic transport rides."""
    from chaos_agent.tools.kubectl_cli import exec_kubectl_raw

    return await exec_kubectl_raw(
        subcommand, v_args, kubeconfig, timeout=timeout, stdin_data=stdin_data,
    )


# ---------------------------------------------------------------------------
# Ledger-model restore replay (M2 task 2.3 — design ND7)
# ---------------------------------------------------------------------------


async def _replay_restore_recipe(handle: dict, kubeconfig: str) -> dict:
    """The ledger-model replay core (design ND7): ONE guard-2 idempotent
    restore-recipe replay onto the TARGET, for EITHER face — the recipe
    rides the task's fault_handle (the assembler receipt's
    ``recovery_handle`` or the applied manifest's spec), never a
    cluster-side CR read (the four CR helpers of the old convergence are
    retired with this rewrite).

    Guard 2 (the pre-restore readback inside ``restore._do_restore``) IS
    the convergence criterion, per op kind: a ``remove`` op whose path
    is already absent is dropped (a remove-only recipe on an
    already-restored target is a true zero-op), while ``replace`` ops
    are never filtered — an already-restored target just re-applies the
    same baseline values (one patch call, effect-level no-op, and a
    post-recovery drift gets corrected for free). Either way the
    three-way convergence table's idempotence holds (carrier timer
    first / replay first / both at once — json-patch semantics
    converge the same terminal state). The carrier stack (if
    armed) is deliberately NOT touched: restore_patches carry baseline
    values, so the timer's later fire is an idempotent replay, while
    tearing the carrier down here could strand the fault if this
    replay's patch is the one that fails.

    Returns ``{"status" ("passed" | "failed" | "skipped"), "details"}``:
    a converged replay → ``passed``; a failed replay → ``failed``
    (fail-visible); no replayable recipe → ``skipped`` (nothing
    addressable, zero actions).
    """
    from .restore import _do_restore, _target_namespace

    target = dict(handle.get("target_ref") or {})
    restore = [
        op for op in (handle.get("restore_patches") or [])
        if isinstance(op, dict)
    ]
    invalid_secret = dict(handle.get("invalid_secret") or {})
    if not (
        target.get("kind") and target.get("name")
        and (restore or invalid_secret.get("name"))
    ):
        return {
            "status": "skipped",
            "details": (
                "no replayable restore recipe (the fault handle and the "
                "message history yield no target_ref/restore_patches) — "
                "nothing addressable for the deterministic replay"
            ),
        }
    restored = await _do_restore(
        {
            "target_ref": target,
            "restore_patches": restore,
            "invalid_secret": invalid_secret,
        },
        kubeconfig,
    )
    where = f"{_target_namespace(target)}/{target.get('name')}"
    if restored:
        return {
            "status": "passed",
            "details": (
                f"restore recipe replayed onto {where} "
                f"({len(restore)} ops, guard-2 idempotent) — mutually "
                "idempotent with an armed carrier's timer fire; the "
                "artifact sweep owns the carrier stack"
            ),
        }
    return {
        "status": "failed",
        "details": (
            f"restore recipe replay FAILED on {where} — the restore "
            "patches or the derived-secret deletion errored; the fault "
            "may still be active"
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
    (the FaultDrill document's metadata dict), ``recipe`` (the spec's
    restore-relevant projection — ND7, consumed by
    ``build_handle_from_messages``), ``v_args``, ``result``
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
            doc = _faultdrill_manifest_doc(stdin)
            if doc is None:
                continue
            meta = (
                doc.get("metadata")
                if isinstance(doc.get("metadata"), dict) else {}
            )
            result = results.get(tc_id, "")
            events.append(
                {
                    "index": index,
                    "meta": meta,
                    "recipe": _cr_recipe_from_spec(doc.get("spec")),
                    "v_args": str(args.get("v_args") or ""),
                    "result": result,
                    "attempted": not any(
                        marker in result for marker in PRE_EXEC_REJECTION_MARKERS
                    ),
                    "tool_call_id": tc_id,
                }
            )
    return events


def _assembler_call_events(messages: list) -> list[dict]:
    """Every ``faultdrill_assemble_carrier`` tool call paired with its
    ToolMessage result — the assembler face's event scan (the same
    pairing walk as :func:`_faultdrill_apply_events`).

    Record fields: ``index`` (message index of the AIMessage), ``result``
    (paired ToolMessage content, ``""`` when absent), ``attempted`` (the
    shared ATTEMPT rule: pre-execution rejections are not attempts), and
    ``tool_call_id``. The registerability verdict over ``result`` is
    :func:`parse_receipt`'s — never re-derived here."""
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
            else:
                tc_id = getattr(tc, "id", "") or ""
                name = str(getattr(tc, "name", "") or "")
            if name != ASSEMBLER_TOOL_NAME:
                continue
            result = results.get(tc_id, "")
            events.append(
                {
                    "index": index,
                    "result": result,
                    "attempted": not any(
                        marker in result for marker in PRE_EXEC_REJECTION_MARKERS
                    ),
                    "tool_call_id": tc_id,
                }
            )
    return events


def _assembler_handle_from_events(events: list[dict]) -> Optional[dict]:
    """The assembler face's authoritative handle, rebuilt from the LATEST
    REGISTERABLE receipt (R2 review Bug#3: the tool strips the top-level
    ``recovery_handle`` before returning, so the recipe rides the
    receipt's ``artifact.recovery_handle`` — the only durable copy in the
    message history).

    ``success``/``partial`` receipts both carry an ARMED carrier (partial
    = armed-but-unconfirmed — the fault may be live); a ``failed`` receipt
    cleaned its stack and hydrates nothing, but an EARLIER registerable
    receipt's carrier may still be armed (the retry-built-new-stack
    semantics), so the scan walks back to the most recent registerable
    one. ``None`` when no registerable receipt exists at all.

    This is the MESSAGE rung of a two-source hydration; the other rung
    reads the same recipe out of the ``execution_artifacts`` ledger
    (``recipe_ledger.recipe_from_artifacts``) and exists because the
    history this scans is not always present — a cross-task recover
    inherits none of it, and ``memory.tool_compactor`` truncates the
    receipt outside the recent window. The receipt's
    ``artifact.recovery_handle`` is therefore NOT the only durable copy,
    and the two rungs share one projection so they cannot disagree on
    what a complete recipe is."""
    from .recipe_ledger import project_recipe_handle

    for event in reversed(events):
        receipt = parse_receipt(event["result"])
        if receipt is None:
            continue
        # The SHARED projection: ``recipe_ledger.project_recipe_handle`` is
        # also what the ledger rung builds its handle with, so "is this
        # recipe complete enough to replay" has one answer on both sources
        # (a target needs kind+name+namespace plus at least one restore op).
        # Duplicating the rule here is how the two would drift.
        handle = project_recipe_handle(
            (receipt.get("artifact") or {}).get("recovery_handle"),
        )
        if handle is not None:
            return handle
    return None


def _assembler_receipt_artifacts(messages: list, *, task_id: str = "") -> list[dict]:
    """Carrier artifacts carried inside assembler receipts (task 1.5).

    The assembler tool is ONE call that builds + verifies + arms + injects
    (design ND2), so its ToolMessage result is not a kubectl transcript to
    mine but a RECEIPT: a JSON document whose ``artifact`` member is the
    pre-built ``recovery_carrier`` artifact (armed stamp included —
    ``build_carrier_artifact``). This helper is the receipt-transport
    branch: pair each assembler call with its result — the same pairing
    walk as :func:`_faultdrill_apply_events` — and hand the registerable
    receipts' artifacts to the ledger, filling ONLY the id provenance a
    ToolMessage can supply.

    ``parse_receipt`` is the single registerability verdict, and it is
    fail-closed on every face: ``failed`` receipts cleaned their stack and
    register NOTHING (a deleted pod must not carry the vehicle-exemption
    chain — the same reason the apply scan skips unlanded applies), and
    unparseable results register nothing either (a screener rejection
    renders as ``[target_guard]`` free text, which never parses as a
    receipt). Repeated rescans of the same receipt are idempotent: the
    artifact key is stable, so ``collect_execution_artifacts`` merges
    rather than duplicates, and the merge never rewinds lifecycle state.
    """
    from .assembler import ASSEMBLER_TOOL_NAME, parse_receipt

    results: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        tc_id = getattr(msg, "tool_call_id", "") or ""
        if tc_id:
            results[tc_id] = str(getattr(msg, "content", "") or "")

    artifacts: list[dict] = []
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in (getattr(msg, "tool_calls", None) or []):
            if isinstance(tc, dict):
                tc_id = tc.get("id", "") or ""
                name = str(tc.get("name", "") or "")
            else:
                tc_id = getattr(tc, "id", "") or ""
                name = str(getattr(tc, "name", "") or "")
            if name != ASSEMBLER_TOOL_NAME:
                continue
            receipt = parse_receipt(results.get(tc_id, ""))
            if receipt is None:
                continue
            artifact = receipt.get("artifact")
            if not isinstance(artifact, dict):
                continue
            artifact = deepcopy(artifact)
            # Id provenance only the ToolMessage side can supply — the
            # armed facts (status/deadline/timeout, rbac_family, cleanup
            # chain) travel IN the receipt already: this branch MOVES
            # them, it never rewrites them. The assembler call IS the
            # arming event on this path, so it seeds the replay-proof
            # set the same way an arming exec's tool_call_id would
            # (``_mark_bounded_host_recovery`` field semantics).
            artifact["created_tool_call_id"] = tc_id
            artifact["host_exec_tool_call_id"] = tc_id
            artifact["host_exec_seen_ids"] = [tc_id]
            if task_id:
                artifact["task_id"] = task_id
            artifacts.append(artifact)
    return artifacts
