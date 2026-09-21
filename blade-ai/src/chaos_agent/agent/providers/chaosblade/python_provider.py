"""ChaosBlade Python-application execution backend provider.

Backend semantics: faults are injected INSIDE a running Python process with
``blade create python <target> <action>`` and undone with ``blade destroy
<uid>``. The ChaosBlade Python executor (``chaosblade-exec-python``) runs as an
in-process agent that intercepts library calls via MonkeyPatch, so the fault is
a *method-level* application fault (Redis GET latency, MySQL query exception,
HTTP client return-value tampering) rather than an OS / container resource
fault.

Why a separate carrier from :class:`ChaosbladeProvider`
------------------------------------------------------
Same binary (``blade``), different *fault domain*: the target vocabulary is
middleware clients (``redis`` / ``mysql`` / ``http`` / ...) and the action
vocabulary is method-level verbs (``delay`` / ``throwCustomException`` /
``returnValue``) — disjoint from the OS-subsystem vocabulary the ChaosBlade OS
executor owns. Declaring them on a separate carrier keeps
``INTENT_TARGETS`` / ``INTENT_ACTIONS`` free of illegal combinations (a
``pod-redis fullload`` would otherwise become expressible).

The backend also uses its OWN injection tool (``blade_python_create``) rather
than ``blade_create``. That is what keeps the two ChaosBlade carriers from
fighting over attribution: ``verify.scan_blade_evidence_index`` only
recognises ``blade_create`` / ``kubectl`` ToolMessages, so a Python-agent
injection is invisible to :meth:`ChaosbladeProvider.detect` and cannot be
mis-attributed to ``host_blade`` (which would route recovery to the wrong
backend).

Prerequisite (NOT performed by this backend)
--------------------------------------------
An in-process agent must already be LISTENING inside the target application.
Getting there is two steps, both verified against chaosblade 1.9.0-alpha:
``blade prepare python --port P --target-script S`` writes a ``sitecustomize.py``
hook into the directory of ``S`` (it bundles its own agent library, so no extra
``pip install``), and the application must then be (re)started with that
directory on ``PYTHONPATH``. Prepare succeeding does not mean an agent is
running. Because the restart cannot happen mid-drill, this is an environment
precondition rather than an injection step, and each experiment stays a single
``create`` → UID → ``destroy`` cycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from .declaration import (
    PYTHON_CARRIER_ID,
    PYTHON_SUPPORTED_ACTIONS,
    PYTHON_SUPPORTED_TARGETS,
)
from chaos_agent.agent.providers.base import (
    ProviderPrompts,
    RecoverResult,
    coerce_tool_args_dict,
)
from chaos_agent.transports import PROFILE_HOST

# NOTE: module level here imports ONLY providers-internal + stdlib modules —
# target_guard symbols are imported lazily inside the functions that use them
# (fault_registry builds its vocabulary from this class at import time, so a
# module-level import reaching target_guard/__init__ → freeze → fault_registry
# would be circular; see the NOTE in chaosblade.py).

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from chaos_agent.agent.providers.base import DestroyOutcome
    from chaos_agent.tools.request_identity import RequestFingerprint
    from chaos_agent.agent.result.verdict import Layer1Result

    from chaos_agent.agent.target_guard.types import EffectiveTarget


def _classify_blade_python_create(
    args: dict,
    raw_command: str,
) -> EffectiveTarget:
    """Classify a ``blade_python_create`` tool_call.

    ``blade_python_create`` injects an in-process method fault into a running
    Python application (``blade create python <target> <action>``). Identity is
    the application process reached through the host channel, not a k8s
    namespace/selector, so the scope is the python fault scope and namespace is
    empty — mirroring ``_classify_host_inject``.

    Classified by its OWN tool name rather than falling through to
    ``_classify_blade_create``: that classifier would resolve ``target=redis``
    via ``BLADE_TARGET_TO_SCOPE`` to ``scope=pod``, which is a DIFFERENT
    capability profile from the approved python scope and would make the guard
    reject every in-process injection as cross-profile drift.

    ``fault_target`` / ``fault_action`` are still populated so the guard's
    fault-TYPE lock keeps pinning "which client, which fault verb".
    """
    # Lazy import — target_guard symbols must not be imported at module level
    # from a provider module (see the NOTE near the top of this file).
    from chaos_agent.agent.target_guard.types import ConfidenceLevel, EffectiveTarget

    from chaos_agent.agent.spec.fault_registry import python_scopes

    fault_target = str(args.get("target") or args.get("blade_target") or "").lower()
    fault_action = str(args.get("action") or args.get("blade_action") or "")
    # The scope name is registry-derived (declared by the python fault family).
    # Prefer an explicit ``scope`` arg when present; otherwise fall back to the
    # family's scope. Never leave it empty: an empty scope is not a sentinel, so
    # the guard would resolve it to the default (k8s) profile and reject the call
    # as cross-profile drift.
    _scopes = sorted(python_scopes())
    scope = str(args.get("scope") or "") or (_scopes[0] if _scopes else "")
    # Name the MISSING argument. Without this the guard can only report
    # "classifier confidence=unknown", which tells the model that something was
    # unparseable but not what — and it has no way to guess that the fault type
    # is what the target lock needs.
    missing = [
        name
        for name, value in (("target", fault_target), ("action", fault_action))
        if not value
    ]
    return EffectiveTarget(
        scope=scope,
        namespace="",
        host_name="",
        fault_target=fault_target,
        fault_action=fault_action,
        confidence=(
            ConfidenceLevel.HIGH
            if (fault_target and fault_action)
            else ConfidenceLevel.UNKNOWN
        ),
        raw_command=raw_command,
        reject_detail=(
            f"the python-app call is missing {' and '.join(missing)}, so the "
            "fault TYPE cannot be pinned against the approved one"
            if missing
            else ""
        ),
    )


class ChaosbladePythonProvider:
    """ChaosBlade Python-agent backend (in-process method faults; blade_destroy
    recovery)."""

    carrier = PYTHON_CARRIER_ID
    injection_methods = ("python_agent",)
    has_experiment_uid = True
    # UID-bearing carrier — never the UID-less verdict default.
    uid_less_verdict_default = False
    # phase-14 G7: kind value renamed off the blade-family vocabulary —
    # same carrier-neutral "experiment_uid" identity as the OS carrier.
    handle_kind = "experiment_uid"
    # One ``blade create python`` command per experiment — no multi-step
    # injection, so no step self-check is needed before a text-only exit.
    is_multi_step = False
    # This backend owns the programmatic Layer-1 recovery for its Python
    # experiments (same blade destroy + status domain as the OS carrier).
    has_deterministic_recover = True
    # This backend IS detected by tool name (unlike ChaosbladeProvider, which is
    # detected by a UID scan over blade_create/kubectl). Declaring the tool here
    # is what isolates the two ChaosBlade carriers' attribution.
    inject_tool_names = frozenset({"blade_python_create"})
    inject_kubectl_subcommands = frozenset()
    # Intent vocabulary this carrier contributes to the FaultFamily aggregate:
    # middleware clients the in-process agent can intercept, and the
    # method-level fault verbs it can apply.
    supported_targets = PYTHON_SUPPORTED_TARGETS
    supported_actions = PYTHON_SUPPORTED_ACTIONS
    # Binaries this backend runs, contributed to the tool guard's Gate-① binary
    # whitelist. Injection and recovery both go through ``blade``.
    injection_binaries = frozenset({"blade"})
    # In-process Python faults — no injection-infrastructure pods, so the
    # Tier-1 tool-pod-namespace exemption set is empty (the protocol
    # default, made explicit).
    tool_pod_namespaces = frozenset()
    # Phase-7 T2 per-tool pass sets. The python-agent tools talk to the local
    # agent over http — none takes a kubeconfig, so the safety-net injector
    # must NOT target them (the old name-prefix match misfired here).
    kubeconfig_scoped_tool_names = frozenset()
    # All three python tools declare ``task_id`` — declaring them here fixes
    # the audit-binding gap the hardcoded _TASK_SCOPED_TOOLS left open.
    audit_scoped_tool_names = frozenset(
        {
            "blade_python_create",
            "blade_python_prepare",
            "blade_python_revoke",
        }
    )
    log_shipping_tool_names = frozenset()
    # Create-reconcile gate (D6): this carrier's creates are NOT yet under
    # the gate — the python protocol's uncertain-outcome contract is not
    # wired (cli_python has no UNCERTAIN_OUTCOME_MARKER path), so arming
    # would be dead state. The declaration stays empty until that carrier
    # grows the same uncertain-outcome discipline (the protocol defaults,
    # made explicit).
    reconcile_create_tool_names = frozenset()
    reconcile_read_tool_names = frozenset()
    # Result-shape verdict (agent/tool_verdicts.py): ``blade_python_create``
    # returns the blade CLI's raw stdout on exit 0 (cli_python.py's ``return
    # result.stdout``), so an in-JSON failure (``{"code":...,"success":
    # false,...}``) with a zero exit code is invisible to the generic
    # ``Error`` prefix verdict — the same dialect the OS carrier declares,
    # read by the same shared predicate. The prepare/revoke tools are not
    # declared: see ``verify.blade_create_json_error_text``.
    result_shape_tool_names = frozenset({"blade_python_create"})

    def matches_channel(self, profile: str) -> bool:
        # ``blade create python`` talks to the in-process agent over
        # ``http://127.0.0.1:<port>``, so it MUST run on the machine hosting the
        # target application — i.e. through a host channel.
        return profile == PROFILE_HOST

    def required_params(self, scope: str) -> list[str]:
        from chaos_agent.agent.spec.fault_registry import required_intent_params

        return required_intent_params(scope)

    def tools(self, phase: str) -> list["BaseTool"]:
        """Tools contributed to the factory tool union per phase.

        - PLAN → ``blade_help`` / ``blade_status`` only (read-only: help text /
          list experiments). ``blade_python_create`` is intentionally ABSENT —
          there is no dry-run, so binding it in planning would hand the planner
          a path past the confirmation gate.
        - EXECUTE → the injection surface plus ``blade_destroy`` for ReAct
          cleanup of a partial/failed create, and the prepare/revoke pair for
          the agent-port precondition.
        - VERIFY / RECOVER_VERIFY → nothing. Layer 1 is deterministic
          (``blade_status``); application-side observation uses ``host_read``,
          which the host-shell backend already contributes to those phases (the
          factory unions every provider's tools).
        """
        from chaos_agent.agent.providers.base import EXECUTE, PLAN
        from chaos_agent.agent.providers.chaosblade.cli import (
            blade_destroy,
            blade_help,
            blade_status,
        )
        from chaos_agent.agent.providers.chaosblade.cli_python import (
            blade_python_create,
            blade_python_prepare,
            blade_python_revoke,
        )

        if phase == PLAN:
            return [blade_help, blade_status]
        if phase == EXECUTE:
            return [
                blade_python_create,
                blade_python_prepare,
                blade_python_revoke,
                blade_destroy,
                blade_help,
                blade_status,
            ]
        return []

    def _scan_index(self, messages: list) -> int:
        """Message index of the most-recent live Python-agent injection, or ``-1``.

        Reverse-scans this backend's own injection tool for a parseable
        experiment UID that has NOT been ``blade_destroy``'d, mirroring
        :func:`~chaos_agent.agent.providers.chaosblade.verify.scan_blade_evidence_index`
        but keyed on ``inject_tool_names`` instead of the ChaosBlade OS
        carrier's tool names. Excluding destroyed UIDs stops a cleaned-up failed
        create from re-claiming the task.
        """
        from langchain_core.messages import ToolMessage

        from .verify import (
            extract_experiment_uid,
            scan_destroyed_uids,
        )

        destroyed = scan_destroyed_uids(messages)
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if not isinstance(msg, ToolMessage):
                continue
            if getattr(msg, "name", "") not in self.inject_tool_names:
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            uid = extract_experiment_uid(content)
            if uid and uid not in destroyed:
                return i
        return -1

    def detect(
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> Optional[str]:
        """Return ``python_agent`` when a live Python-agent experiment is attested."""
        if not is_host:
            return None
        return "python_agent" if self._scan_index(messages) >= 0 else None

    def injection_recency(
        self, messages: list, *, is_host: bool, is_teardown=None,
    ) -> int:
        """Message index of this backend's injection evidence, or ``-1``."""
        if not is_host:
            return -1
        return self._scan_index(messages)

    def build_fault_handle(self, values: dict) -> Optional[dict]:
        """Claim a committed Python-agent experiment when the attribution is
        THIS backend's: the experiment UID rides the legacy ``experiment_uid``
        field, so only a ``python_agent`` attribution may claim it here
        (otherwise the ChaosBlade provider owns the UID)."""
        values = values or {}
        if values.get("injection_method") != "python_agent":
            return None
        uid = values.get("experiment_uid") or ""
        if not uid:
            return None
        return {"kind": "experiment_uid", "value": uid, "method": "python_agent"}

    def extract_experiment_id(self, messages: list, retired=None) -> str:
        """Live (non-destroyed, non-retired) Python-agent experiment UID.

        Scans this backend's OWN injection tool (``inject_tool_names``) rather
        than the OS carrier's blade_create evidence, mirroring :meth:`detect`.
        """
        from langchain_core.messages import ToolMessage

        from .verify import (
            extract_experiment_uid,
            scan_destroyed_uids,
        )

        dead = set(scan_destroyed_uids(messages)) | set(retired or ())
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if not isinstance(msg, ToolMessage):
                continue
            if getattr(msg, "name", "") not in self.inject_tool_names:
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            uid = extract_experiment_uid(content)
            if uid and uid not in dead:
                return uid
        return ""

    def extract_experiment_ids(self, messages: list, retired=None) -> set[str]:
        """EVERY live Python-agent experiment UID born in ``messages`` — the
        plural birth face the registry's ownership seam consumes (round-26).

        Each inject tool call IS one birth, so the plural face is the same
        walk as the singular with ``return``-first replaced by collect-all:
        multiple injects in one task are multiple liabilities, and the
        ownership ledger must see every one of them."""
        from langchain_core.messages import ToolMessage

        from .verify import (
            extract_experiment_uid,
            scan_destroyed_uids,
        )

        dead = set(scan_destroyed_uids(messages)) | set(retired or ())
        born: set[str] = set()
        for msg in messages:
            if not isinstance(msg, ToolMessage):
                continue
            if getattr(msg, "name", "") not in self.inject_tool_names:
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            uid = extract_experiment_uid(content)
            if uid and uid not in dead:
                born.add(uid)
        return born

    def destroyed_experiment_ids(self, messages: list) -> set[str]:
        """UIDs this carrier family's ``blade_destroy`` tool calls have
        targeted — the destroy half of the experiment lifecycle scan,
        union-aggregated by the registry's ``destroyed_experiment_ids`` seam
        (channel-unfiltered: python experiments are destroyed by the same
        ``blade destroy <uid>`` tool as OS ones)."""
        from .verify import scan_destroyed_uids

        return scan_destroyed_uids(messages)

    def destroyed_proven_experiment_ids(self, messages: list) -> set[str]:
        """PROVEN deaths for the ledger's death registration (B76 review
        I1) — same shared scan as the OS carrier: python experiments are
        destroyed by the same ``blade destroy <uid>`` tool family, so the
        output-proven judgement (paired ToolMessage confirms the kill) is
        carrier-family-wide. Only output-proven deaths register — a false
        retire hides a LIVE experiment from every future recovery."""
        from .verify import scan_destroyed_proven_uids

        return scan_destroyed_proven_uids(messages)

    def classify_destroy_output(self, output: str) -> "DestroyOutcome":
        """Three-state verdict on a raw destroy output — same shared
        classifier as the OS carrier (python experiments die through the
        same ``blade destroy`` tool family, so the decision source is
        carrier-family-wide)."""
        from .verify import classify_destroy_output

        return classify_destroy_output(output)

    def tool_result_error_text(
        self, tool_name: str, content: str
    ) -> Optional[str]:
        """Failure verdict on the blade CLI's JSON dialect — the shared
        predicate the OS carrier uses (``verify.blade_create_json_error_text``),
        so the two blade carriers cannot drift on the same bytes."""
        if tool_name not in self.result_shape_tool_names:
            return None
        from .verify import blade_create_json_error_text

        return blade_create_json_error_text(content)

    def build_handle_from_messages(
        self, messages: list, retired=None, values: Optional[dict] = None
    ) -> Optional[dict]:
        """Defense-in-depth hydration: claim a live Python-agent experiment
        UID found in the message history (see the Protocol hook — only
        consulted when the durable facts are absent)."""
        uid = self.extract_experiment_id(messages, retired)
        if not uid:
            return None
        # Mirror ChaosbladeProvider: backfill the durable method attribution
        # when present (the registry passes the whole state as ``values``),
        # falling back to this carrier's own method.
        return {
            "kind": self.handle_kind,
            "value": uid,
            "method": (values or {}).get("injection_method") or "python_agent",
        }

    def created_experiment_ids(self, messages: list, state: dict) -> set[str]:
        """Provenance for the destroy gate: every UID this task's own
        injection tool ever returned (destroyed ones included — provenance is
        historical, not live), PLUS the durable state record when the
        attribution is this backend's (``python_agent`` owns the legacy
        ``experiment_uid`` field then; the ChaosBlade provider claims it
        otherwise)."""
        from langchain_core.messages import ToolMessage

        from .verify import _UID_SHAPE_RE, extract_experiment_uid

        state = state or {}
        uids: set[str] = set()
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            if (getattr(message, "name", "") or "") not in self.inject_tool_names:
                continue
            content = (
                message.content
                if isinstance(message.content, str)
                else str(message.content)
            )
            uid = extract_experiment_uid(content)
            if uid:
                uids.add(uid)
        # Durable read-side gate (round-20 Q4, mirrors the ChaosBlade
        # provider): the durable sources trust the writer chain —
        # belt-and-suspenders at the trust-chain END so a non-shaped value
        # can never ride the whitelist whatever wrote it.
        if state.get("injection_method") == "python_agent":
            durable_uid = str(state.get("experiment_uid") or "").strip()
            if durable_uid and _UID_SHAPE_RE.fullmatch(durable_uid):
                uids.add(durable_uid)
        # Birth registry (B76 review G): carrier-neutral append-only ownership
        # record — keeps proving provenance across compaction and contract
        # replacement after the last-write-wins slot has moved on (same gap
        # as the ChaosBlade provider's durable source).
        uids.update(
            str(uid).strip()
            for uid in (state.get("owned_experiment_uids") or [])
            if str(uid).strip() and _UID_SHAPE_RE.fullmatch(str(uid).strip())
        )
        return uids

    def classify_tool_target(
        self, tool_name: str, tool_args: Any, raw_command: str
    ) -> Optional[EffectiveTarget]:
        """Guard-side classification of this carrier's tools (phase-7 T5).

        Claims the ``blade_python_create`` injection tool (classifier above)
        and this carrier's PRECONDITION tools (``blade_python_prepare`` /
        ``blade_python_revoke`` — ``prepare`` only registers the in-process
        agent's port with the blade CLI and ``revoke`` deregisters it;
        neither touches a fault target, so they short-circuit to READONLY).
        ``None`` (not this carrier's tool) lets the registry scan
        continue."""
        # Lazy import — see the NOTE near the top of this file.
        from chaos_agent.agent.target_guard.types import (
            SCOPE_READONLY,
            EffectiveTarget,
        )

        if tool_name == "blade_python_create":
            return _classify_blade_python_create(
                coerce_tool_args_dict(tool_args),
                raw_command,
            )
        if tool_name in ("blade_python_prepare", "blade_python_revoke"):
            return EffectiveTarget(
                scope=SCOPE_READONLY,
                namespace="",
                raw_command=raw_command,
            )
        return None

    def parse_injection_params(self, tool_name: str, tool_args: dict) -> Optional[dict]:
        """No issue-time key-parameter extraction: the python tools carry
        structured JVM-level fault parameters (class/method/…) directly, which
        the verifier renders from the fault spec — no flags string to parse."""
        return None

    def issue_time_method(
        self, tool_name: str, tool_args: dict, *, is_host: bool = False
    ) -> Optional[str]:
        """Issue-time attribution: any of this carrier's inject tools enacts
        ``python_agent``. Like the blade methods it produces an experiment
        UID, so the execute node still defers the COMMIT to the UID path;
        classifying it here keeps issue-time attribution complete (and stops
        the method reading as "no injection issued"). Migrated from the
        execute-side classifier's hardcoded branch (phase-7 T4)."""
        if tool_name in self.inject_tool_names:
            return "python_agent"
        return None

    def build_reconcile_fingerprint(
        self, tool_name: str, tool_args: Any
    ) -> Optional["RequestFingerprint"]:
        """Create-reconcile seam (D6): this carrier declares no create
        under the gate yet (``reconcile_create_tool_names`` is empty until
        the python protocol grows an uncertain-outcome contract), so the
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

    def issue_disproven(self, messages: list, *, is_teardown=None) -> bool:
        """Experiment attribution is RESULT-born (committed only when the UID
        appears in a successful create result), so there is no issue-time
        guesswork to revoke."""
        return False

    async def rollback_handle(self, handle: dict, **kwargs) -> str:
        """Deterministic undo of a committed Python-agent experiment
        (failure-path auto-rollback): a plain ``blade_destroy`` on the host."""
        uid = (handle or {}).get("value") or ""
        if not uid:
            return ""
        from chaos_agent.agent.providers.chaosblade.cli import blade_destroy

        try:
            destroy_result = await blade_destroy.ainvoke(
                {"uid": uid, "kubeconfig": kwargs.get("kubeconfig", "")}
            )
            logger.info("Auto-rollback result: %s", destroy_result)
            return f" (auto-rolled back experiment_uid={uid})"
        except Exception as rb_err:  # noqa: BLE001 — best-effort rollback
            return f" (rollback FAILED: {rb_err})"

    def scan_step_actions(
        self, steps: list[str], messages: list, *, is_teardown=None,
    ):
        """Explicitly not claimed (pinned None, phase-8 D4): an
        experiment-UID carrier judges injection completion by the
        experiment evidence chain (the UID), not step-verb heuristics —
        the step self-check is native-carrier territory."""
        return None

    def was_injection_attempted(self, messages: list, *, is_teardown=None) -> bool:
        """Explicitly not claimed (pinned False): the native-fallback
        message back-scan is native-carrier territory; THIS backend's
        attempt state is carried by :meth:`was_fault_create_attempted`
        (the experiment judgement)."""
        return False

    def was_fault_create_attempted(
        self,
        messages: list,
        injection_method: str | None = None,
        *,
        is_teardown=None,
    ) -> bool:
        """Attempted-but-no-UID judgement — same blade-family combination
        semantics as :meth:`ChaosbladeProvider.was_fault_create_attempted`
        (durable-attribution / kubectl-success / kubectl-native fallback
        exemptions, then the ``blade_create`` tool-name scan: the judgement
        has always been defined on that vocabulary, and the python create
        tool feeds the UID-extraction priority list instead)."""
        from .verify import (
            was_blade_create_attempted,
        )

        return was_blade_create_attempted(
            messages, injection_method, is_teardown=is_teardown,
        )

    async def layer1_verify(self, state: dict, **kwargs) -> "Layer1Result":
        """Deterministic Layer-1 verification: poll ``blade_status`` for the UID.

        Reuses the host-blade Layer 1 path — ``blade_status`` already routes to
        the host channel and queries that host's local experiment DB, which is
        exactly where a ``blade create python`` experiment is recorded.
        """
        from .verify import (
            _run_host_blade_layer1,
        )
        from chaos_agent.agent.execution_artifacts import make_teardown_matcher

        return await _run_host_blade_layer1(
            # Identity comes from the caller-resolved dispatch identity
            # (``_verifier_layer1`` passes ``experiment_uid`` explicitly; the
            # legacy ``kwargs['blade_uid']`` alias fallback was retired in
            # phase-14 — same EOL ruling as the recover seam: no pre-handle
            # callers exist).
            kwargs.get("experiment_uid", "") or "",
            kwargs.get("kubeconfig", "") or "",
            task_id=kwargs.get("task_id", ""),
            messages=state.get("messages", []),
            injection_method=state.get("injection_method"),
            # Teardown≠mutation at the Layer-1 attempted judgement (O-1,
            # P3) — same threading as ChaosBladeProvider.layer1_verify.
            is_teardown=make_teardown_matcher(
                state.get("execution_artifacts") or []
            ),
        )

    async def layer1_raw_destroy(self, uid: str, kubeconfig: str = "") -> str:
        """Bare destroy for the finalize retry (same host Layer-1 domain as
        :meth:`layer1_destroy` — no status verification; the retry prompt
        only needs the destroy output)."""
        from .recover import raw_destroy

        return await raw_destroy(uid, kubeconfig)

    async def experiment_destroyed(self, uid: str, kubeconfig: str = "") -> bool:
        """Status-only death check — the sweep's convergence valve (B76
        review J2: the same valve was host-carrier-only, so a python
        experiment's repeat-destroy not-found failure never converged).

        A ``blade create python`` experiment is recorded in the SAME
        host-local experiment DB (the argument :meth:`layer1_verify`
        already relies on), so ``blade_status`` proves its death exactly
        like the host carrier's. Mirrors
        :meth:`ChaosbladeProvider.experiment_destroyed` — False on any
        doubt, fail-closed (a false retire hides a LIVE experiment)."""
        from chaos_agent.agent.providers.chaosblade.cli import blade_status
        from .recover import parse_blade_status_destroyed

        try:
            out = await blade_status.ainvoke({"uid": uid, "kubeconfig": kubeconfig})
        except Exception:  # noqa: BLE001
            return False
        raw = out if isinstance(out, str) else str(out)
        verdict, _ = parse_blade_status_destroyed(raw)
        return verdict == "passed"

    async def layer1_destroy(
        self,
        uid: str,
        kubeconfig: str = "",
        *,
        messages: list | None = None,
        injection_method: str | None = None,
        # Protocol parity with the ledger rung: this backend's Layer-1
        # identity IS the experiment UID, so there is no recipe to hydrate
        # from ``execution_artifacts`` and the list stays unread.
        artifacts: list | None = None,
    ) -> "Layer1Result":
        """Deterministic Layer-1 recovery: destroy the python-agent experiment
        via the canonical host Layer-1 path (it is recorded in the local DB of
        the host the command ran on — see ``_chaosblade_recover``)."""
        from .recover import run_layer1_destroy

        return await run_layer1_destroy(
            uid,
            kubeconfig,
            messages=messages,
            injection_method=injection_method,
        )

    def recovery_vehicle(self, state: dict) -> str:
        """No durable recovery-vehicle record on this carrier (the experiment
        runs inside the target process) — nothing to render."""
        return ""

    def blocks_deterministic_destroy(
        self, state: dict, messages: list | None = None
    ) -> bool:
        """Python experiments are delivered by the agent's own tool, never
        through an in-cluster exec channel — the deterministic destroy
        always runs."""
        return False

    def recovery_facts_render(
        self, state: dict, *, spec_params: dict | None = None
    ) -> str:
        """Experiment UID line for the Layer-1 recovery context (the Python
        experiment rides the same blade-protocol identity)."""
        uid = state.get("experiment_uid") or ""
        return f"Blade UID: {uid}\n" if uid else ""

    def merge_deterministic_recover_verdict(
        self, layer1, state: dict, part_override: dict | None = None
    ):
        """No deterministic part to merge on this delivery — identity."""
        return layer1

    def layer1_recover_guidance(
        self,
        state: dict,
        experiment_uid: str,
        *,
        combo_native: bool = False,
        combo_part: dict | None = None,
    ) -> str:
        """The deterministic destroy always runs — no LLM guidance needed."""
        return ""

    def layer2_facts_note(self, state: dict) -> str:
        """No carrier-parsed flags on this delivery — nothing to note."""
        return ""

    def verify_prompt_note(
        self, injection_method: str, *, injection_pod_name: str | None = None
    ) -> str:
        """Post-injection verifier note: verify at the APPLICATION layer."""
        if injection_method != "python_agent":
            return ""
        return (
            "\n### Injection Method Note\n"
            "The fault was injected INSIDE the target Python process by the "
            "ChaosBlade Python agent (runtime method interception). Nothing "
            "changed at the OS, container or Kubernetes layer, so system-level "
            "metrics and cluster object state are EXPECTED to look normal — "
            "their being normal is NOT evidence the injection failed.\n"
            "Verify at the application layer instead, matching the injected "
            "action:\n"
            "- delay → the intercepted call's latency rises by roughly the "
            "configured time (compare against the pre-injection baseline)\n"
            "- throwCustomException → the intercepted call raises the "
            "configured exception type (visible in application logs / error "
            "rate)\n"
            "- returnValue → the intercepted call returns the configured value "
            "instead of the real one\n"
            "Only calls matching the experiment's matchers (e.g. a specific "
            "Redis command or SQL type) are affected; unmatched calls stay "
            "normal by design.\n"
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
        """Recover Layer-2 framing: confirm the application behaviour is normal
        again after ``blade_destroy`` removed the in-process interception."""
        layer1_context = (
            f"## Layer 1 Result (already completed)\n"
            f"blade_destroy for UID {experiment_uid}: {layer1.status}\n"
            f"Details: {layer1.details}\n"
            f"Raw output: {layer1.raw_output[:500]}\n\n"
        )
        layer2_instruction = (
            "PHASE TRANSITION: Layer 1 (recovery execution) is COMPLETE. "
            "You are now in Layer 2 (VERIFICATION). "
            "DO NOT execute more recovery actions — only VERIFY that the "
            "in-process interception is gone: the previously affected "
            "application calls behave normally again (latency back to baseline, "
            "no injected exception, real return values). Observe from the "
            "application side; OS / cluster state was never modified. "
            "Output RECOVERY_VERIFICATION_RESULT format, NOT "
            "RECOVERY_EXECUTION_RESULT.\n"
        )
        return layer1_context, layer2_instruction

    async def recover(
        self, state: dict, handle: Optional[dict], **kwargs
    ) -> RecoverResult:
        """Deterministic recovery: ``blade destroy <uid>`` + ``blade_status``.

        A Python-agent experiment is recorded in the local DB of the host the
        command ran on, so the canonical host Layer-1 recovery reaches it
        directly — there is no CRD / kubectl-exec variant to dispatch on.
        """
        from .recover import run_layer1_destroy
        from chaos_agent.agent.result.verdict import FailureCategory, layer1_to_dict

        # Identity from the recovery handle (value = experiment UID) — the
        # handle is the single identity source since phase-6 (the legacy
        # ``kwargs['blade_uid']`` fallback for pre-handle callers was
        # retired; old checkpoints are no longer supported).
        experiment_uid = str((handle or {}).get("value") or "")
        kubeconfig = kwargs.get("kubeconfig", "") or ""
        messages = kwargs.get("messages", []) or []

        layer1 = await run_layer1_destroy(
            experiment_uid,
            kubeconfig,
            messages=messages,
            injection_method=state.get("injection_method"),
        )
        # COMBO injection parity with ChaosbladeProvider.recover: a
        # kubectl-native component alongside the experiment cannot be undone
        # without an LLM, so even a successful destroy leaves the fault
        # partially active — never report recovered.
        _combo = bool(state.get("combo_native_issued"))
        recovered = layer1.is_passed() and not _combo
        layer2 = {
            "status": "skipped",
            "details": "No LLM available for application-side verification",
        }
        _warnings: list[str] = []
        if layer1.is_passed() and not _combo:
            _warnings.append(
                "Layer 2 (application-side) recovery verification was skipped. "
                "Only blade_destroy + blade_status verification was performed."
            )
        if _combo:
            _warnings.append(
                f"Combo injection: besides the experiment (uid={experiment_uid}), a "
                f"kubectl-native component was injected. The deterministic (no-LLM) "
                f"recovery path can ONLY destroy the experiment — the native "
                f"component was NOT undone. Use LLM-based recovery (blade-ai recover "
                f"with LLM) to reverse the native mutations."
            )
        warnings = tuple(_warnings)
        return RecoverResult(
            recovered=recovered,
            level="recovered" if recovered else "unrecovered",
            layer1=layer1_to_dict(layer1),
            layer2=layer2,
            warnings=warnings,
            experiment_uid=experiment_uid,
            handle=handle,
            failure=None
            if recovered
            else (
                FailureCategory.RECOVERY_FAILED,
                f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}",
            ),
        )

    def prompt_fragments(self) -> ProviderPrompts:
        return ProviderPrompts()


__all__ = ["ChaosbladePythonProvider"]
