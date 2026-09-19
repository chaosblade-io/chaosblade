"""Chaosblade carrier's verify-side domain: creation/delivery judgements,
experiment-UID extraction, and the Layer-1 execution domain.

Physically owned by the provider layer (phase-4 T2/T4): these are the blade
carrier's own combination judgements over the carrier-agnostic scan
primitives (``providers/message_scanning.py`` since phase-14 G1) — "was a
blade experiment create attempted but never landed", "was the live experiment
delivered through kubectl exec", and the experiment-UID extraction from the
blade-family tool evidence (the extraction family and blade-evidence scans
moved here from the retired ``chaosblade/detection.py`` in phase-14 G1 — they
are this carrier's own output-format knowledge, not shared primitives).
Since phase-4
T4 the Layer-1 EXECUTION domain (blade_status / blade_query_k8s parsing, the
kubectl-exec and host-blade runners) also lives here — the two providers whose
``layer1_verify`` dispatches into it import it as a same-package dependency
(no providers→nodes sideways coupling). ``nodes/verify/_verifier_layer1.py``
keeps the STATE orchestration; the transitional aliases it used to re-export
were retired with phase-5.

Symbols:
  Functions: was_blade_create_attempted, was_kubectl_exec_delivery,
             extract_experiment_uid_from_messages
  Experiment-UID extraction & blade-evidence scans (moved from the retired
  chaosblade/detection.py, phase-14 G1):
    Functions: extract_experiment_uid, scan_destroyed_uids,
               scan_blade_evidence_index, scan_kubectl_blade_success
  Layer-1 execution domain (moved from nodes/verify/_verifier_layer1.py):
    Constants: _MAX_DISCOVERY_PROBES, _EXPIRED_STATES, _RUNNING_STATES,
               _TRANSIENT_STATES, _FAILURE_SIGNALS, _TOOL_POD_NAMESPACE
    Type:      _QueryK8sResult
    Parsers:   _extract_json_object, _parse_iso_ts_seconds, _is_early_destroy,
               _parse_blade_status_output, _parse_blade_query_k8s_output,
               _find_blade_query_in_messages, _map_query_k8s_to_layer1
    Runners:   _run_layer1_via_kubectl_exec, _run_host_blade_layer1
"""

import json
import logging
import re
from collections import namedtuple

from langchain_core.messages import ToolMessage

from chaos_agent.agent.providers.message_scanning import (
    KUBECTL_COMMAND_SUBCOMMANDS,
    KUBECTL_WRITE_SUBCOMMANDS,
    build_tool_call_args_lookup,
    exec_command_segments,
    exec_inner_command_mutates,
    scan_kubectl_injection_after_blade,
)
# Re-exported legislation constants (round-21) — this module keeps its
# historical public surface (cli_python and the r19/r20 probes/tests import
# the shapes from HERE) while the single source lives in the
# carrier-agnostic arbitration layer the general layer is allowed to import.
from chaos_agent.agent.providers.uid_shapes import (  # noqa: F401 — re-export
    DASHED_UUID_SHAPE,
    HEX16_UID_SHAPE,
    HEX_HEAD_GUARD,
    HEX_TAIL_GUARD,
    UID_SHAPE_ALTERNATION,
    UID_SHAPE_GATE,
)
from chaos_agent.agent.providers.base import DestroyOutcome
from chaos_agent.agent.result.verdict import Layer1Result
from chaos_agent.observability.status_tracker import get_tracker
from chaos_agent.tools.pod_discovery import (
    TOOL_POD_NAMESPACE as _TOOL_POD_NAMESPACE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Multi-strategy experiment-UID extraction (phase-9 T3.1)
#
# Moved from ``utils/blade_uid.py`` so the carrier's output-format knowledge
# lives in the providers package — ``chaosblade.py`` / ``chaosblade_python.py``
# / ``_chaosblade_verify.py`` consume it as a family. Line-for-line
# equivalent to the original (renamed ``extract_blade_uid`` →
# ``extract_experiment_uid``). Phase-14 G1: physically moved from the retired
# ``chaosblade/detection.py`` into THIS module — the extraction family is the
# blade domain's own output-format knowledge and stays inside it.
#
# Real-world `blade create` output appears in many shapes — clean JSON, JSON
# buried in a kubectl-exec stderr preamble, pretty-printed multi-line JSON,
# mixed code 200 / 54000 responses, and occasionally raw `chaosblade-*`
# resource names. A single regex or `json.loads` is brittle against this.
# The semantically-aware strategy runs first so that a code-54000 response
# with `success=false` is correctly rejected (the CRD exists but the
# experiment failed — extracting its UID would mislead the verifier).
#
# Strategy order:
#   1. JSON-aware (``json.JSONDecoder.raw_decode``):
#      - Walks every `{` in `text`, parses JSON segments, applies the
#        blade_create response semantics:
#          * code=200 + success=true        → return result
#          * code=54000 + success!=False    → return result.uid
#          * code=54000 + success=False     → reject AND block fallbacks
#            (the injection failed — extracting the UID would be misleading).
#   2. Loose regex on `"result"` / `"uid"` UUID-shaped fields — catches
#      malformed JSON that the parser bailed on (e.g., truncated stdout,
#      unescaped quotes from kubectl-exec wrapping).
#   3. ChaosBlade resource pattern `chaosblade-[a-f0-9]+` — last resort
#      for cases where blade emitted a resource name instead of a UID
#      (e.g. `kubectl get chaosblades` echo).
# ---------------------------------------------------------------------------

# Single-source experiment-UID shape legislation (round-19; round-20 Q1
# extended it to the STRATEGY anchors below; round-21 moved the constants
# to the carrier-agnostic arbitration layer
# ``agent/providers/uid_shapes.py`` and re-exports them here): every
# UID-bearing capture in the codebase — not just this module — composes
# from that block. Round-19's list enumerated only the three failed-create
# dialect anchors (FAILED_CREATE_UID_RE / RAW_FAILED_CREATE_UID_RE /
# PY_FAILED_CREATE_UID_RE) plus the _UID_SHAPE_RE gate — the two anchors
# INSIDE the extraction strategies never entered it, so each kept its own
# shape dialect (dashed-only here, prefixed-and-open-bounded below): the
# "enumerate the repair surface" defect in its 7th recurrence. Round-21
# found the 8th recurrence OUTSIDE this package (memory/compactor.py's two
# survival-context anchors and the side_effect conflict-check fallback each
# hand-copied a pre-legislation dialect): the phase-11 import boundary bars
# the general layer from importing this carrier subpackage, so a PRIVATE
# single source could never govern those consumers — the constants are
# legislated in uid_shapes.py, re-exported above (public surface unchanged
# for this package's consumers), and a source-level scan test
# (tests/test_agent/test_providers/test_uid_shape_legislation.py) refuses
# any hex-class regex in src/chaos_agent that does not compose them.

# Backward-compatible module-private alias (pre-round-21 spelling kept for
# the in-module call sites and the round-19/20 probes).
_UID_SHAPE_ALTERNATION = UID_SHAPE_ALTERNATION

# Loose ``"result"/"uid"`` key fallback for output whose JSON the parser
# bailed on (truncated stdout, unescaped quotes from kubectl-exec wrapping).
# Composes the full single-source shape domain (round-20 Q3).
_UUID_RE = re.compile(r'"(?:result|uid)"\s*:\s*"(' + _UID_SHAPE_ALTERNATION + r')"')

# ChaosBlade resource-name fallback (used when blade emits a resource ref
# rather than a UID — e.g. `chaosblade-1234abcd...`). The resource-name
# suffix IS the experiment UID (``chaosblade-<uid>``), so the capture STRIPS
# the prefix and composes the single-source hex16 shape (round-20 Q2: the
# pre-round-20 pattern captured the PREFIXED string verbatim — an un-shaped
# value that rode every ingestor, short-circuiting even the round-19 N1
# dict-branch gate — with an 8-hex lower bound and no upper bound).
_CHAOSBLADE_RESOURCE_RE = re.compile(r"\bchaosblade-(" + HEX16_UID_SHAPE + r")\b")


def extract_experiment_uid(text: str) -> str | None:
    """Extract a ChaosBlade experiment UID from arbitrary tool output.

    Returns the UID string on success, or None if no usable UID was found
    (including the case where a structured receipt indicates the injection
    actually failed — callers must treat None as "no live experiment").

    Strategy chain (round-22 Q4b tightened the fallthrough): the JSON-aware
    walk runs first and reports whether it SAW a structured blade receipt
    (a dict carrying ``code``/``success``). A structured refusal blocks the
    shape-only fallbacks — they exist for JSON-BLIND output (truncated
    stdout, unescaped quotes), and a receipt the structured strategy has
    already judged un-licensable (code=500 failed-create, the 54000
    terminal failure, any non-200 non-54000 receipt) must not be
    re-admitted by a regex that cannot even see the ``code`` it is
    ignoring. The pre-round-22 chain only blocked the 54000 spelling
    (the r20-Q8 sentinel): a code=500 receipt's ``"result": "<uid>"``
    fell straight through to the fallback and laundered a failed
    experiment's UID into the live slot.

    This extractor is only ever handed output from a PROVEN-pure domain:
    every caller gates on ``BladeExecPayload.pure_create`` first (a
    composite payload's receipt licenses NOTHING — round-18 F reverted
    the round-17 graded lane: its "strict" JSON-aware success anchor was
    believed to be the one shape a companion does not produce organically,
    but round-16 F had already established that an ECHO companion can
    forge ANY shape verbatim, so ``echo '{"code":200,...}'`` laundered a
    forged UID right through the strict lane. The segment-composition
    gate is the only lever; the extractor itself stays single-mode). The
    JSON-aware anchors shape-validate their payloads (``_UID_SHAPE_RE``):
    a success receipt whose ``result`` is not a UID-shaped string is not
    an experiment UID.
    """
    if not isinstance(text, str) or not text:
        return None

    uid, saw_receipt = _uid_strategy_json_aware(text)
    if uid is not None:
        return uid
    if saw_receipt:
        # A structured blade receipt was seen and refused to license a
        # UID — do NOT fall back to looser strategies (the 54000 ruling
        # of round-20 Q8, generalized to every structured refusal).
        return None

    uid = _uid_strategy_regex(text)
    if uid is not None:
        return uid

    return _uid_strategy_chaosblade_resource(text)


def _uid_strategy_json_aware(text: str) -> tuple[str | None, bool]:
    """Walk every ``{`` in ``text``, parse JSON segments, apply blade semantics.

    Returns ``(uid, saw_receipt)``:
      - uid: a UID extracted from a recognized success or
        54000-initializing response, else ``None``.
      - saw_receipt: ``True`` when the walk saw at least one structured
        blade FAILURE verdict — a receipt dict whose semantics are a
        refusal (``success`` is false, or a ``code`` that is neither 200
        nor the initializing 54000) — without licensing a UID from it.
        The caller must refuse to extract a UID from this output by any
        looser strategy (round-22 Q4b: the r20-Q8 54000 sentinel's
        reject-and-block ruling, generalized from one status code to
        every structured refusal). A SUCCESS receipt whose ``result``
        merely has an unusable shape (blade_status's dict result) is NOT
        a refusal and does not block the fallbacks — the first draft of
        this flag blocked it and broke the status-face dict lane
        (test_short_resource_suffix_refused: 200+true with a dict result
        is the status tool's NORMAL success form; shape-inapplicability
        is not a structured verdict).
    """
    decoder = json.JSONDecoder()
    scan_from = 0
    saw_receipt = False

    while True:
        idx = text.find("{", scan_from)
        if idx < 0:
            break
        try:
            data, end_idx = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            scan_from = idx + 1
            continue

        if isinstance(data, dict):
            if data.get("success") is False or (
                "code" in data and data.get("code") != 200 and data.get("code") != 54000
            ):
                # A structured FAILURE verdict (round-22 Q4b): the
                # shape-only fallbacks lose their jurisdiction over this
                # text — they exist for JSON-BLIND output and must not
                # overrule a verdict they cannot even read. (54000 counts
                # as failure only when its own branch below rules it
                # terminal; a non-200 code that is not 54000 — e.g. 500 —
                # is a refusal here and now.)
                saw_receipt = True
            if data.get("success") is True and data.get("code") == 200:
                result = data.get("result")
                # Shape-validated (round-18 F-e): a success receipt whose
                # ``result`` is not a UID-shaped string is not an experiment
                # UID — the anchor must not launder arbitrary strings into
                # the single-slot / evidence ledgers.
                if isinstance(result, str) and _UID_SHAPE_RE.fullmatch(result):
                    return result, True

            if data.get("code") == 54000:
                result = data.get("result")
                if isinstance(result, dict):
                    uid = result.get("uid")
                    if isinstance(uid, str) and uid:
                        error_msg = (data.get("error") or "").lower()
                        # Distinguish "still initializing" from "truly failed":
                        # - "unexpected status ... Initialized, please wait"
                        #   → CRD accepted, operator still bootstrapping.
                        #   The experiment MAY succeed; extract uid so the
                        #   verifier can check status later.
                        # - "command not found" / "exec failed" / other
                        #   → injection process actually failed; ignore uid.
                        _is_initializing = (
                            "please wait" in error_msg
                            or "initialized" in error_msg
                        )
                        if data.get("success") is False and not _is_initializing:
                            logger.info(
                                "experiment_uid extraction: 54000 + success=false + "
                                "terminal error, treating as failed (uid=%s ignored)",
                                uid,
                            )
                            return None, True
                        elif _UID_SHAPE_RE.fullmatch(uid):
                            logger.info(
                                "experiment_uid extraction: 54000, extracted uid=%s "
                                "(initializing=%s)",
                                uid, _is_initializing,
                            )
                            return uid, True
                elif isinstance(result, str) and result:
                    # 54000 with a STRING result (round-20 Q8, surfaced by
                    # the strategy-2 domain-parity fix): the python face's
                    # failure spelling — registered but failed. The dict
                    # branch's reject-and-block ruling applies verbatim: a
                    # terminal failure's UID must not be promoted to the
                    # live slot. The pre-r20 extractor only refused it by
                    # ACCIDENT — the dashed-only fallback regex happened
                    # not to match a hex16 string; once the fallback took
                    # the single-source domain, the un-gated branch
                    # surfaced (strategy 2 would launder the UID straight
                    # into the single slot).
                    error_msg = (data.get("error") or "").lower()
                    _is_initializing = (
                        "please wait" in error_msg or "initialized" in error_msg
                    )
                    if data.get("success") is False and not _is_initializing:
                        logger.info(
                            "experiment_uid extraction: 54000 + success=false + "
                            "terminal error (string result), treating as failed"
                        )
                        return None, True

        scan_from = end_idx

    return None, saw_receipt


def _uid_strategy_regex(text: str) -> str | None:
    """Find the first UUID-shaped value of `result` or `uid` in `text`."""
    match = _UUID_RE.search(text)
    if match:
        return match.group(1)
    return None


def _uid_strategy_chaosblade_resource(text: str) -> str | None:
    """Find a `chaosblade-<uid>` resource name as a last-resort identifier.

    The capture strips the ``chaosblade-`` prefix (round-20 Q2): the suffix
    is the experiment UID, and a prefixed string is not a UID-shape value —
    it used to ride the extractor's return verbatim into the single slot,
    the birth registry and the destroy whitelist, where ``blade destroy
    chaosblade-xxx`` can never match a real experiment.
    """
    match = _CHAOSBLADE_RESOURCE_RE.search(text)
    if match:
        return match.group(1)
    return None


def scan_destroyed_uids(messages: list) -> set[str]:
    """UIDs the LLM has issued a destroy for — BOTH delivery faces.

    A UID sent to ``blade_destroy`` (tool face) or to an inline
    ``kubectl exec ... blade destroy/revoke`` (vehicle face, round-14
    F1: issued = terminal applies channel-neutrally — the inline destroy
    the registry itself instructs for in-cluster deliveries used to leave
    the UID re-claimable as the live fault) is no longer an active
    injection: whether the destroy succeeded or failed, it is residual
    and MUST NOT be picked up as the current fault's carrier. In-package
    shared tool (phase-13): consumed by both blade-family providers'
    ``destroyed_experiment_ids`` hooks (the registry's union seam for the
    generic layer), by ``extract_experiment_uid_from_messages``
    internally, and by provider detection — every blade-family consumer
    applies the same destroyed-exclusion rigor (task-76c59364
    regression: ``ChaosbladeProvider.detect`` re-claimed a failed,
    already-cleaned experiment when only the extractor excluded).
    """
    destroyed: set[str] = set()
    for msg in messages:
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            if name == "blade_destroy":
                uid = args.get("uid", "") if isinstance(args, dict) else ""
                if uid:
                    destroyed.add(uid)
            elif (
                name == "kubectl"
                and isinstance(args, dict)
                and args.get("subcommand") == "exec"
            ):
                destroyed |= inline_destroy_uids(str(args.get("v_args") or ""))
    return destroyed


# ---------------------------------------------------------------------------
# Blade-delivery SYNTAX judgement (round-15 root fix)
#
# The word-containment gates this replaces (``"blade" in v_args and
# "create" in v_args`` — eleven copy-pasted sites across the blade scans,
# the provider faces, and the shared attribution scans) judged VOCABULARY,
# not SYNTAX: a composite decoy payload (``sh -c 'kubectl get pods -o
# json; echo blade create done'``) passed every one of them while carrying
# no blade command, and the kubectl output it returned then laundered K8s
# resource UIDs into the birth registry (round-15 H2 — the destroy
# provenance gate went on to ALLOW an inline destroy of a UID this task
# never created). The judgement is now ONE function over the carriers-
# shared syntax parser: a blade delivery is a command SEGMENT whose
# command-position head is ``blade``. A word inside an ``echo`` argument
# or a quoted literal is vocabulary, never a command.
# ---------------------------------------------------------------------------
BladeExecPayload = namedtuple(
    "BladeExecPayload", ["segments", "has_create", "has_destroy", "pure_create"]
)


def classify_blade_exec_payload(command: object) -> BladeExecPayload:
    """Single-source syntax judgement of a blade-exec delivery command.

    Wraps the carriers-shared parser :func:`exec_command_segments` and
    keeps only the segments whose COMMAND POSITION (head token, bare or
    ``/path`` form) is ``blade``:

    - ``segments`` — the ``blade ...`` token lists, verb at index 1
      (redirections stripped, wrapper prefixes resolved by the parser);
    - ``has_create`` — any segment enacting ``blade create``;
    - ``has_destroy`` — any segment enacting ``blade destroy``/``revoke``;
    - ``pure_create`` — EVERY command segment is a ``blade create``
      segment (no echo/query/destroy companion): the payload's whole
      output domain is the blade CLI's own output. Round-16 A/E/F: the
      receipt-ingestion dialect cannot be separated by SHAPE (a query
      output's ``"uid": "<hex16>"`` key is form-identical to a failed
      create's, and an ``echo`` companion can forge ANY shape), so the
      birth ledger trusts a receipt ONLY when the segment composition
      proves no other command contributed to it.

    Accepts v_args (the CALLER owns the subcommand check) or a full
    command line (``kubectl [flags] exec|debug ...`` — non-command-mode
    kubectl lines yield no segments inside the parser). Fail-closed on
    any parse doubt: unlexable input yields no segments and no judgements.
    """
    all_segments = exec_command_segments(command)
    segments = [
        seg
        for seg in all_segments
        if seg and (seg[0] == "blade" or seg[0].endswith("/blade"))
    ]
    verbs = {seg[1] for seg in segments if len(seg) >= 2}
    return BladeExecPayload(
        segments=segments,
        has_create="create" in verbs,
        has_destroy=bool(verbs & {"destroy", "revoke"}),
        pure_create=(
            bool(segments)
            and len(segments) == len(all_segments)
            and verbs == {"create"}
        ),
    )


def _is_blade_create_delivery(command: object) -> bool:
    """Injection adapter for the shared attribution scans: THIS module owns
    the blade-exec create-delivery judgement (round-15 root fix — the
    shared scans take it as a parameter so they stay carrier-agnostic, the
    same dependency direction ``is_mutating_command`` already rides)."""
    return classify_blade_exec_payload(command).has_create


def collect_flag_values(args: list[str], flag: str) -> list[str]:
    """Every value assigned to ``flag`` (both spellings, all instances).

    Single source for the blade token family (round-14: moved here from
    provider.py so the death-ledger scans and the classifier's inline
    parsers share ONE extractor — provider imports it as a same-package
    dependency, the same direction every other provider→verify import
    already runs). blade CLI follows pflag LAST-WINS on repeated flags
    (probe D8), so callers needing "the value blade honours" take the
    LAST; callers needing first-vs-last agreement take the whole list.
    """
    vals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == flag and i + 1 < len(args):
            vals.append(args[i + 1])
            i += 2
            continue
        if a.startswith(flag + "="):
            vals.append(a.split("=", 1)[1])
        i += 1
    return vals


# Experiment-UID shape a blade destroy target may take (round-16 B,
# tightened round-17 S3): a bare hex16 (the blade experiment UID — LOWERCASE
# hex, matching every ingestion anchor in this module, with an upper bound
# that keeps sha256-shaped 40-hex garbage out) or a dashed UUID (the legacy
# spelling the r14 anchors carried). Anything else — a variable reference
# ``$EXP_UID``, a command substitution, a path — is not a UID; registering
# it would persist a junk token into the durable retired ledger (r15 H1's
# residue in new shapes). Composes the full single-source shape domain
# (defined with the strategy anchors above — round-20 Q1). Round-22: the
# compiled gate itself is single-sourced too — this name is now an alias of
# the arbitration layer's UID_SHAPE_GATE (a second hand-compiled fullmatch
# gate over the same domain is a second legislation, the r21 review's
# finding on this very line).
_UID_SHAPE_RE = UID_SHAPE_GATE


def destroy_uid_from_tokens(rest: list[str]) -> str:
    """UID of an inline ``blade destroy/revoke`` from its token tail.

    ``blade destroy <uid>`` (positional) and ``blade destroy --uid <uid>``
    (flag) both reach the CLI; pflag last-wins applies to the flag
    spelling, so the LAST value is the one blade would act on. Explicit
    flag beats positional when both appear. Value-absorbing flags skip
    their values in the positional walk (round-14 F3: ``blade destroy
    --kubeconfig /root/kc <uid>`` used to hand ``/root/kc`` to the
    provenance gate — a false UID the whitelist can never match, a
    fail-closed FALSE REFUSAL of the task's own cleanup). The surviving
    token MUST match the experiment-UID shape (round-16 B: a variable
    reference, a command substitution or a redirection token is not a
    UID — returning it would persist a junk token into the durable
    ledger). A non-shaped token yields "" — the callers treat that as a
    form issue, not a default-clean verdict.
    """
    from chaos_agent.tools.guard_parser import BLADE_VALUE_FLAGS

    vals = collect_flag_values(rest, "--uid")
    if vals:
        return vals[-1] if _UID_SHAPE_RE.fullmatch(vals[-1]) else ""
    skip_value = False
    for tok in rest:
        if tok.startswith("-"):
            # Glued spellings carry their value after ``=`` (nothing to
            # skip); separated value-taking flags absorb the NEXT token.
            skip_value = tok in BLADE_VALUE_FLAGS
            continue
        if skip_value:
            skip_value = False
            continue
        if tok:
            # First positional is the target blade would act on — a
            # non-shaped one means the command itself is malformed.
            return tok if _UID_SHAPE_RE.fullmatch(tok) else ""
    return ""


def inline_destroy_uids(v_args: str) -> set[str]:
    """Every UID an inline ``blade destroy`` command targets.

    Round-15 root fix: the dual-lane union (regex + semantic token walk,
    round-14 F1) is retired. The regex lane captured the token after the
    verb LITERALLY — a flag spelling registered ``--uid``/``--kubeconfig``
    as killed UIDs (r14 F1), and those junk tokens persisted into the
    durable retired ledger through the registration seam (r15 H1: the
    A2 seam appends the whole proven set). The single source is now the
    payload classifier: destroy segments are command-position facts, and
    each segment's target UID comes from the SAME
    :func:`destroy_uid_from_tokens` walk the provenance gate applies —
    flag spellings resolve to their VALUES, value-absorbing flags skip
    their values, and no literal flag token ever reaches a ledger.

    ``revoke`` is NOT a death carrier (round-25 K1, aligning the inline
    face with the round-24 K3 ruling the host face already carries):
    revoke tears down a PREPARE uid — a precondition — so retiring its
    target would pollute a ledger only experiment uids consume, and a
    successful revoke against an owned experiment uid would FALSE-RETIRE
    a live experiment (strictly worse than the orphan). The mutating-
    action and provenance-gate semantics of ``destroy/revoke`` live in
    the provider's target-scope judgement (same vocabulary as
    ``readonly.py``), not in this death-registration scan.
    """
    uids: set[str] = set()
    for seg in classify_blade_exec_payload(v_args).segments:
        if len(seg) >= 2 and seg[1] == "destroy":
            uid = destroy_uid_from_tokens(seg[2:])
            if uid:
                uids.add(uid)
    return uids


# Framework-synthesized receipt prefixes — a ToolMessage carrying one of
# these NEVER executed its tool call (B76 review K4): the screener answers
# rejected/deferred batches itself (``[target_guard]`` / ``[screener]``
# renderings), and execute_loop answers replan-turn calls (``Not executed:``).
# These are framework CONTRACT strings (the same family as the screener's
# ``EMPTY_SELECTOR_HINT`` structural proof anchor), so matching them is a
# structural gate, not vocabulary luck: a future rewording of the receipt
# body cannot forge a death certificate if the prefix discipline holds.
_FRAMEWORK_RECEIPT_PREFIXES = (
    "[screener] ",
    "[target_guard] ",
    "Not executed: ",
)


def classify_destroy_output(output) -> "DestroyOutcome":
    """Three-state verdict on a raw destroy output — the single
    destroy-decision source for every framework-side consumer.

    The registry sweep's retire/failure fork, the verify-replan retire
    filter and this module's own death-proof predicate all compose THIS
    function (the three pre-merge tables judged "not found", prefixes and
    non-JSON outputs three different ways; a non-JSON no-keyword output
    retired on the sweep's table and stayed live on the authority's).

    Ordering is load-bearing and preserves :func:`parse_blade_destroy_output`
    semantics exactly: a framework-receipt prefix can never be SUCCESS
    (a never-executed call proves nothing), SUCCESS is decided first
    (``success`` truthy / ``code == 200`` JSON — the authority's predicate —
    or the non-JSON success/destroyed wording fallback), NOT_FOUND only on
    the failure side — "not found" wording inside a SUCCESS receipt is
    evidence prose, not a convergence-valve signal. Everything else is
    FAILED: doubt is not death (a false retire hides a LIVE experiment
    from every future recovery, strictly worse than the orphan the sweep
    exists to prevent).
    """
    text = output.strip() if isinstance(output, str) and output.strip() else ""
    if not text:
        return DestroyOutcome.FAILED
    prefixed = text.startswith(("Error:", "failed", *_FRAMEWORK_RECEIPT_PREFIXES))
    if not prefixed:
        try:
            payload = json.loads(text)
        except ValueError:
            if "success" in text.lower() or "destroyed" in text.lower():
                return DestroyOutcome.SUCCESS
        else:
            if isinstance(payload, dict) and (
                bool(payload.get("success")) or payload.get("code") == 200
            ):
                return DestroyOutcome.SUCCESS
    low = text.lower()
    if "not found" in low or "notfound" in low:
        return DestroyOutcome.NOT_FOUND
    return DestroyOutcome.FAILED


# ---------------------------------------------------------------------------
# Execution-event alignment (round-26 root fix — the receipt-side half of
# the composite-command family)
#
# The 1:1 fossil: every receipt-side consumer assumed ONE tool call == ONE
# command == ONE receipt, so the paired output was judged as one blob. A
# composite's real receipt is the CONCATENATION of its commands' outputs —
# and the blob judgement cannot attribute a slice to its command: the JSON
# authority lane needs a single object (a concatenation fails json.loads),
# so EVERY composite verdict rode the wording fallback, where any one
# command's "success"/"destroyed" substring launders every sibling's
# failure (round-26 K1/K2/K4). The command side learned to see every
# segment (round-25 token-face split); this is the mirror half — each
# segment gets its OWN receipt slice and its OWN verdict.
#
# The alignment's safety direction follows the ledger's structural
# asymmetry: a false retire has NO later escape (retired only grows, the
# live set only shrinks), while an unproven death has the sweep's
# status-recheck convergence valve. Every alignment doubt therefore
# lands on un-proven, never on proof-by-guesswork.
#
# Round-27 amendment: this machinery is the DEATH side's own. Births never
# needed positional binding (the uid lives in the receipt line, not the
# segment argv) — the birth faces now license content-derived through
# :func:`receipt_birth_uids`, and the strict layer-2 gate that leaked
# honest births through transport shapes no longer touches them.
# ---------------------------------------------------------------------------
ExecutionEvent = namedtuple(
    "ExecutionEvent", ["segment", "receipt_slice", "provable"]
)


def align_execution(v_args: str, receipt) -> list[ExecutionEvent]:
    """One execution event per command segment, each with its own receipt
    slice — the decomposition primitive for FACES THAT NEED POSITIONAL
    BINDING (round-27 amendment): the death face, where the uid rides the
    destroy segment's ARGV and the proof rides the receipt line, so a
    misattributed pairing false-retires a LIVE experiment. Birth faces do
    NOT compose this (round-27): a birth licence is content-derived
    (:func:`receipt_birth_uids`) — the uid lives in the line itself — and
    the positional gate only leaked honest births through transport
    shapes (the kubectl ``Error:`` wrapper, ``&&``/``||`` short-circuit,
    stderr trailers).

    Three layers, decided by STRUCTURE, not by receipt wording:

    - **Layer 3 (unprovable)** — any segment whose command-position head is
      not ``blade`` rode the output path (an ``echo``/``tee`` companion, or
      the downstream stage of a pipe: the round-25 split draws ``|`` as a
      segment boundary, so ``blade destroy X | wc -l`` is a blade segment
      plus a ``wc`` companion). A companion's contribution is forgeable and
      a pipe TRANSLATES the output (``wc -l`` emits a digit) — no slice can
      be attributed, so NO event is provable. For pipes this is not a
      limitation but the physical fact: the proof genuinely no longer
      exists; the sweep's convergence valve is the honest escape.
    - **Layer 1 (single command)** — the whole receipt belongs to the one
      segment, whatever its shape (transport preamble, wrapped JSON,
      prose): the existing single-command verdict lanes keep their exact
      behaviour, byte for byte.
    - **Layer 2 (pure-blade composite)** — blade prints ONE JSON object per
      invocation, so the honest receipt is one JSON object per line and the
      line count MUST equal the segment count. A ``&&`` left failure makes
      the right side never run (one line, two segments — mismatch, fail
      closed); a failure stack trace adds lines (mismatch, fail closed);
      any non-JSON line is not a blade receipt (fail closed). Only an
      exact, fully-JSON, line-per-segment alignment is provable — and then
      each event carries its OWN line, so a failed sibling can no longer
      ride a successful sibling's wording.

    ``provable=False`` means "this segment's receipt attribution is
    structurally unsound" — consumers must register NOTHING from it; the
    uid stays live (doubt is not death) and the convergence valve decides
    later. ``receipt_slice`` is ``None`` exactly when ``provable`` is
    False.
    """
    all_segments = exec_command_segments(v_args)
    events = [ExecutionEvent(seg, None, False) for seg in all_segments]
    if not all_segments:
        return events

    def _is_blade_head(seg: list[str]) -> bool:
        return bool(seg) and (seg[0] == "blade" or seg[0].endswith("/blade"))

    # Layer 3: a non-blade companion shares the output path.
    if any(not _is_blade_head(seg) for seg in all_segments):
        return events

    text = receipt if isinstance(receipt, str) else ""

    # Layer 1: a single command owns the whole receipt.
    if len(all_segments) == 1:
        return [ExecutionEvent(all_segments[0], text, True)]

    # Layer 2: line-per-segment JSON alignment, exact or nothing.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) != len(all_segments):
        return events
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            return events
        if not isinstance(obj, dict):
            return events
    return [
        ExecutionEvent(seg, ln, True)
        for seg, ln in zip(all_segments, lines)
    ]


def _destroy_output_proves_death(output) -> bool:
    """Whether a destroy call's raw tool output PROVES the experiment died.

    Thin composition over :func:`classify_destroy_output` (the single
    destroy-decision source): SUCCESS — and only SUCCESS — proves death.
    A key-less error JSON (``{"code": 500, "error": ...}``) is a FAILED
    destroy under the authority and must prove nothing here either; a
    second table here would drift exactly the way the readonly
    double-judge drift did (B76 review K2). Doubt is NOT death — a false
    retire hides a LIVE experiment from every future recovery, strictly
    worse than the orphan the sweep exists to prevent.

    Framework-synthesized receipts (K4) are excluded STRUCTURALLY by the
    classifier's prefix gate before any verdict: a never-executed call
    cannot prove death no matter how its receipt is worded.
    """
    return classify_destroy_output(output) is DestroyOutcome.SUCCESS


def scan_destroyed_proven_uids(messages: list) -> set[str]:
    """UIDs whose destroy is PROVEN by a SUCCESSFUL paired tool output.

    Death-registration half of the liability ledger (B76 review I1). Unlike
    :func:`scan_destroyed_uids` (issued = terminal, the conservative
    attribution semantics — a destroy ATTEMPT must stop the UID being
    re-claimed), this scan feeds the durable ``retired`` ledger, so it
    registers a UID only when the paired ToolMessage output confirms the
    kill. Two delivery forms, both channel-agnostic:

    1. ``blade_destroy`` tool calls (host delivery);
    2. kubectl-exec tool calls carrying ``blade destroy <uid>`` in
       ``v_args`` — the in-cluster delivery's LLM vehicle, structurally
       invisible to ``scan_destroyed_uids`` (I1c). One exec can carry
       MULTIPLE destroy payloads (``blade destroy A && blade destroy B``):
       round-26 retired the J3 shared-verdict contract (every captured UID
       shared the call's ONE blob verdict — one "success" substring
       retired a failed sibling, and an echo companion's wording forged the
       certificate). The receipt is now aligned per segment
       (:func:`align_execution`): each destroy's proof is its OWN receipt
       slice — a companion or a pipe makes the whole call unprovable, and
       a pure-blade composite needs an exact line-per-segment JSON
       alignment where every destroy's own line proves its own kill.

    An unpaired or failed call contributes NOTHING (doubt stays live; the
    sweep's status-recheck convergence valve is the later escape).
    """
    # tool_call_id → paired ToolMessage content (single pass, any position:
    # the pair may straddle compaction leftovers).
    results: dict[str, object] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", "")
            if tc_id:
                results[tc_id] = msg.content

    proven: set[str] = set()
    for msg in messages:
        for tc in getattr(msg, "tool_calls", None) or []:
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            if not isinstance(args, dict):
                continue
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            tc_id = (
                tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            )
            uids: list[str] = []
            if name == "blade_destroy":
                _uid = str(args.get("uid") or "").strip()
                if _uid:
                    uids.append(_uid)
            elif name == "kubectl" and args.get("subcommand") == "exec":
                # Round-26 root fix: per-segment receipt alignment. The
                # pre-fix lane collected every destroy UID across segments
                # and judged the WHOLE concatenated output once — one
                # command's success wording retired every sibling (K2), and
                # an echo companion riding the composite forged the
                # certificate outright (K1). Each destroy's proof is now its
                # OWN receipt slice; companions/pipes/misaligned shapes
                # prove nothing (the convergence valve owns the doubt).
                v_args = str(args.get("v_args") or "")
                receipt = results.get(tc_id) if tc_id else None
                for ev in align_execution(v_args, receipt):
                    if not ev.provable:
                        continue
                    if len(ev.segment) >= 2 and ev.segment[1] == "destroy":
                        uid = destroy_uid_from_tokens(ev.segment[2:])
                        if uid and _destroy_output_proves_death(
                            ev.receipt_slice
                        ):
                            proven.add(uid)
            if not uids:
                continue
            if tc_id and _destroy_output_proves_death(results.get(tc_id)):
                proven.update(uids)
    return proven


# Terminal create failures still owe cleanup: the CRD exists even when the
# create errored out, so its UID joins the birth registry (mirrors the
# host-face scan's treatment in ``created_experiment_ids``). Moved to this
# module (round-14 G1): the inline create scan below needs the same
# vocabulary, and provider→verify is the package's established import
# direction.
#
# Round-15 root fix (R1 — dialect anchors do not cross output domains): the
# ``"uid":`` JSON-key branch this RE used to carry is K8s-output
# vocabulary, not blade dialect. The host face is structurally closed (its
# input is ``blade_create`` ToolMessages only), so the branch was dormant
# there; the inline face's input is kubectl-exec output, where that branch
# matched every ``metadata.uid`` in a ``get -o json`` receipt — the wash-in
# channel of round-15 H2.
#
# Round-16 domain split (the r15 cut was HALF done — one leg over-cut,
# one leg left open): the ``UID: <uid>`` wording is the HOST face's
# dialect ONLY (cli.py's ``Experiment CRD was created (UID: ...)``
# wrapper — this RE stays that face's anchor). The exec channel sees
# the blade CLI's RAW failure JSON instead: a top-level
# ``"uid": "<hex16>"`` key — exactly the shape cli.py mines for the
# host face, and the shape the r15 cut wrongly deleted here too
# (round-16 A: failed-create ingestion went dark on the inline face).
# The companion anchor below is hex16-ONLY (a dashed UUID is K8s-object
# vocabulary, never a blade experiment UID), and SHAPE alone still cannot
# keep a query output out (its ``"uid"`` keys are form-identical,
# round-16 E3) — the pure-create segment gate in
# :func:`inline_blade_create_receipt_uids` is what separates the domains.
# Round-19 N3: the shape itself is now composed from the single source
# (HEX16_UID_SHAPE) — the pre-round-19 tolerance (uppercase, dashes, an
# 8-char lower bound, no upper bound) predated the r16/r17 legislation and
# contradicted it; the host face's ``UID: <uid>`` wording is cli.py's own
# re-wrap of a UID it mined with RAW_FAILED_CREATE_UID_RE, so a shaped
# lowercase hex16 is the only form this anchor ever legitimately sees.
# The trailing lookahead is load-bearing: unlike the JSON-key anchors
# (whose closing quote anchors the shape's right edge), this prose wording
# has no terminator, so a bare bound would PARTIAL-match a 40-hex string
# and admit its first 32 chars as a truncated, well-shaped fake UID.
# Round-22 Q1: the edge spelling is single-sourced too — the lookahead
# composes HEX_TAIL_GUARD (uid_shapes.py), the case-insensitive refusal
# round-19 N3b legislated. A hand-typed edge can drift casing silently
# (round-21's FALLBACK_UID_RE drifted exactly this way) while every
# shape-domain check stays green.
FAILED_CREATE_UID_RE = re.compile(
    r"UID:\s*(" + HEX16_UID_SHAPE + r")" + HEX_TAIL_GUARD
)

# Raw blade-CLI failed-create dialect (round-16 domain split): the exec
# channel's own spelling of "the CRD was created although execution
# failed" — a top-level ``"uid": "<hex16>"`` key in the CLI's error
# JSON. cli.py mines the SAME shape for the host face (single source).
# Round-19 N2: the shape bounds are the single source too — the open
# ``{16,}`` upper end used to admit 40-hex sha256-shaped garbage into the
# birth ledger through a pure-create receipt; it now composes from
# HEX16_UID_SHAPE (lowercase, bounded 16-32) like every capturing anchor.
RAW_FAILED_CREATE_UID_RE = re.compile(
    r'"uid"\s*:\s*"(' + HEX16_UID_SHAPE + r')"'
)


# The kubectl tool's failure wrapper: ``Error: kubectl <sub> (exit N): ``
# glued onto the first stdout line when the batch exits non-zero (e.g.
# ``create A && create B`` with B failing — A's line is prefixed). The
# wrapper is METADATA about the batch, not output of any create segment;
# birth licensing unwraps it before scanning lines (the death face keeps
# its own wrapper-tolerant normalisation inside the classifier).
_KUBECTL_ERROR_PREFIX_RE = re.compile(
    r"^Error:\s*kubectl\s+\S+\s+\(exit\s+-?\d+\):\s*"
)

# Round-29 K4 — the JSON-blind fallback's plural collector. The
# anchoring discipline is KEY-VALUE CONTEXT, never a bare shape: a
# truncated composite receipt keeps its semantic residue in
# ``"result":"<uid>"`` / ``"uid":"<uid>"`` fragments, and ONLY those
# count (the same discipline as RAW_FAILED_CREATE_UID_RE's failure
# lane — a forged ``UID:`` wording or a bare hex16 in noise licenses
# nothing, r16-F's boundary kept verbatim). The JSON-aware walk's
# ``saw_receipt`` refusal-blocking stays the authority ahead of this
# collector.
_BLIND_BIRTH_UID_RE = re.compile(
    r'"(?:result|uid)"\s*:\s*"(' + UID_SHAPE_ALTERNATION + r')"'
)


def receipt_birth_uids(content: object) -> list[str]:
    """Every SUCCESS birth a PURE-CREATE receipt licenses, content order.

    Round-27 root fix — the birth family's single licensing primitive,
    consumed by the whitelist inline face, the plural ownership face and
    the singular live face's kubectl lane (one source, three faces: the
    pre-round-27 faces each carried their own copy of the 1:1 assumption
    — the whitelist's success lane walked the blob and licensed only the
    FIRST birth, the singular face filtered deaths at message
    granularity and went blind on the sibling).

    A birth licence is CONTENT-DERIVED: ``blade create`` prints the uid,
    the segment argv never carried it, so every JSON line of a
    pure-create receipt licenses its own uid and POSITION is irrelevant.
    The round-26 per-event gate (:func:`align_execution`) was the death
    face's discipline — there the uid rides the destroy segment's argv
    and the proof rides the line, so misattribution false-retires a LIVE
    experiment — inherited by the birth face, where it leaked honest
    births through transport shapes the death gate was never asked to
    survive: the kubectl ``Error:`` wrapper above, ``&&``/``||``
    short-circuit (fewer lines than segments), stderr trailers (more).
    The caller's ``pure_create`` gate remains the domain lever — within
    it every line of stdout is this task's own ``blade create`` output
    (the companion-forgery lever is and stays the segment composition,
    rounds 16-18: an ``echo`` stage breaks ``pure_create`` and licenses
    nothing).

    A failure line licenses no birth (a failed create owns no
    liability); its CRD uid still reaches the whitelist through
    :data:`RAW_FAILED_CREATE_UID_RE` — different lane, different
    obligation (cleanup of a failed create is still owed).

    JSON-blind receipts (no line parses as a JSON object — truncated
    stdout, transport garbage) keep the whole-content extractor with its
    fallback chain: single-command receipts have always licensed through
    it, and a receipt with no structured line has nothing to
    misattribute.
    """
    if not isinstance(content, str) or not content:
        return []
    text = _KUBECTL_ERROR_PREFIX_RE.sub("", content, count=1)
    births: list[str] = []
    saw_receipt_line = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        # Round-28 K1 — the family's single-source legislation (round-22
        # Q4b, generalized from the extractor chain): only a STRUCTURED
        # BLADE RECEIPT — a dict carrying blade's ``code``/``success``
        # semantics — holds the fallback-blocking jurisdiction. A
        # non-receipt dict line (kubectl's own table JSON, an unrelated
        # dict) never carried blade semantics; the first draft let ANY
        # dict line block the JSON-blind fallback, orphaning a uid the
        # truncated text still carried — stricter than the family this
        # primitive licenses for, and divergent from the extractor chain
        # the fallback delegates to.
        if "success" not in parsed and "code" not in parsed:
            continue
        saw_receipt_line = True
        uid = extract_experiment_uid(stripped)
        if uid:
            births.append(uid)
    if not saw_receipt_line:
        # JSON-blind receipt: the whole-content extractor with its
        # fallback chain stays the FIRST licence (layer-1 parity —
        # truncated single receipts have always licensed through it);
        # round-29 K4 adds the plural sweep — every OTHER uid-shaped
        # birth in the truncated text joins it (the single-value
        # extractor licensed only the first, leaking the second past
        # ownership, the sweep and the plural poll when the 64KB output
        # safety valve's middle cut degraded a composite receipt to
        # free text). Shape collection is safe ONLY inside the caller's
        # pure-create domain gate (the r16-r18 segment-composition
        # lever) — an auxiliary extractor there, never an authority.
        uid = extract_experiment_uid(text)
        collected = dict.fromkeys(
            m.group(1) for m in _BLIND_BIRTH_UID_RE.finditer(text)
        )
        if uid:
            births.append(uid)
        births.extend(u for u in collected if u != uid)
    return births


def inline_blade_create_receipt_uids(messages: list) -> set[str]:
    """UIDs proven by inline ``kubectl exec ... blade create`` receipts.

    Birth-ledger channel parity (round-14 G1): the provenance scan's
    message side saw ONLY host ``blade_create`` ToolMessages, so an
    inline-delivered experiment was invisible to the destroy whitelist
    — the main chain hid this behind the durable birth registry
    (execute_loop's attribution sync), but the hydration fallback
    (legacy checkpoints / DB-only recovery, ``live_liability_uids``'s
    own documented target scenario) has no such cover: the LLM's own
    destroy of its own inline experiment was REFUSED with the receipt
    sitting in the visible history.

    Paired-call gate (task-51193464) + PURE-CREATE segment gate
    (round-16 A/E/F): a kubectl ToolMessage counts ONLY when its owning
    AIMessage tool_call is the blade-exec delivery AND the payload's
    every command segment is a ``blade create`` segment
    (:attr:`BladeExecPayload.pure_create`). The ingestion dialect is
    output-domain-split: the ``UID: <uid>`` wording is the HOST face's
    (cli.py's wrapper); this face consumes the blade CLI's RAW failure
    JSON — a top-level ``"uid": "<hex16>"`` key
    (:data:`RAW_FAILED_CREATE_UID_RE`, the shape cli.py itself mines).
    Shape alone cannot keep a query output out (its ``"uid"`` keys are
    form-identical, round-16 E3) and an ``echo`` companion can forge ANY
    shape (round-16 F): the segment composition is the only lever — an
    echo/query/destroy companion proves some OTHER command contributed
    to the receipt, so the receipt licenses NOTHING (fail-closed).
    Failed-create CRD UIDs count too — cleanup is still owed for them.

    Round-27: the success lane is plural and content-derived
    (:func:`receipt_birth_uids`) — position-independent licensing that
    unwraps the kubectl ``Error:`` transport wrapper and takes every JSON
    line's birth; positional alignment remains the DEATH face's own
    discipline.
    """
    lookup = build_tool_call_args_lookup(messages)
    uids: set[str] = set()
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if (getattr(msg, "name", "") or "") != "kubectl":
            continue
        args = lookup.get(getattr(msg, "tool_call_id", "") or "")
        if not isinstance(args, dict) or args.get("subcommand") != "exec":
            continue
        v_args = str(args.get("v_args") or "")
        if not classify_blade_exec_payload(v_args).pure_create:
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        # Round-27: the success lane rides the shared plural primitive —
        # a pure-create composite proves EVERY birth its lines carry (the
        # singular walk licensed only the first, so the second birth was
        # destroy-gate-REJECTed with the receipt sitting in the visible
        # history, and leaked out of the hydration fallback's ownership
        # rebuild — legacy checkpoints / DB-only recovery). The failure
        # lane below stays finditer (already plural).
        uids.update(receipt_birth_uids(content))
        uids.update(
            m.group(1) for m in RAW_FAILED_CREATE_UID_RE.finditer(content)
        )
    return uids


def scan_blade_evidence_index(
    messages: list, *, destroyed: set[str] | frozenset[str] = frozenset(),
) -> tuple[int, str | None]:
    """Most-recent NON-destroyed ChaosBlade injection evidence.

    Reverse-scans for the latest ``blade_create`` / ``kubectl`` ToolMessage
    carrying a parseable blade UID that has NOT been ``blade_destroy``'d.
    Returns ``(message_index, method)`` where method is ``host_blade`` (via the
    blade tool) or ``kubectl_exec`` (blade run through kubectl exec), or
    ``(-1, None)`` when no live blade experiment is attested.

    The ``message_index`` is the recency key the registry uses to arbitrate
    against a later kubectl-/host-native injection (attribute to the LAST
    successful injection, not the earliest blade UID in history).

    A ``kubectl`` ToolMessage attests blade evidence ONLY when its owning
    tool_call is the INGESTION-qualified blade-exec delivery —
    ``subcommand='exec'`` whose payload is pure-create
    (:attr:`BladeExecPayload.pure_create`), cross-checked through the
    AIMessage tool-call lookup. Rounds 17-18: this is an INGESTION face
    (it certifies a UID as blade evidence), so the gate is the
    segment-composition proof, NOT the looser "some segment creates"
    attribution judgement. The round-17 "graded" middle lane (composite
    receipts allowed the JSON-aware anchors only) is REVERTED — round-18 F
    proved the strict anchor forgeable (an ``echo`` companion prints the
    success JSON verbatim), and round-16 F had already ruled the segment
    composition the only lever: a composite receipt licenses NOTHING,
    exactly like the birth registry. A composite payload (real ``blade
    create`` + ``kubectl get pods -o json``) passing the attribution gate
    used to launder the K8s ``metadata.uid`` in with ``method=
    'kubectl_exec'`` when the create failed, deadlocking the executor on an
    unfulfillable UID. The ATTRIBUTION faces
    (:func:`scan_kubectl_blade_success`, the pod-name extractors) keep
    ``has_create`` — attributing WHICH channel delivered a create is correct
    there; ingesting WHAT a receipt licenses is decided here. A kubectl
    ToolMessage whose call cannot be resolved in the lookup is skipped
    fail-closed — an unattributable kubectl output must not license a blade
    attribution.

    Round-28 K2 — the kubectl lane licenses PLURALLY through the birth
    family's primitive (:func:`receipt_birth_uids`): the singular walk
    licensed only the FIRST birth, so a composite double-create whose
    first experiment was destroyed attested NOTHING (the whole message
    skipped on the dead uid, the still-live sibling invisible) and the
    attribution face returned ``(-1, None)`` over a task that still owed
    a live experiment — the fifth private copy of the 1:1 assumption
    (r27 rewired four faces; this one had its own). The host
    ``blade_create`` lane stays singular — the structured tool call is
    one-create-per-call by construction, no composite shape exists.
    """
    lookup = build_tool_call_args_lookup(messages)
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", "") or ""
        if name not in ("blade_create", "kubectl"):
            continue
        if name == "kubectl":
            tc_id = getattr(msg, "tool_call_id", "")
            args = lookup.get(tc_id) if tc_id else None
            if not isinstance(args, dict):
                continue
            v_args = args.get("v_args", "") or ""
            payload = classify_blade_exec_payload(v_args)
            if args.get("subcommand") != "exec" or not payload.pure_create:
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            # Round-28 K2: per-uid destroyed filter — the first birth the
            # receipt licenses that is STILL live wins the recency slot.
            for uid in receipt_birth_uids(content):
                if uid not in destroyed:
                    return i, "kubectl_exec"
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        uid = extract_experiment_uid(content)
        if uid and uid not in destroyed:
            return i, "host_blade"
    return -1, None


def scan_kubectl_blade_success(messages: list) -> bool:
    """True if ``kubectl exec`` was used to successfully inject a ChaosBlade
    experiment (bypassing the ``blade_create`` tool).

    Finds a kubectl ToolMessage carrying ChaosBlade success JSON
    (``{"code":200,"success":true,"result":"<uid>"}``) and cross-references
    the AIMessage tool_call to verify it was ``subcommand='exec'`` with
    ``blade`` + ``create`` in ``v_args``. Falls back to content-only detection
    when the tool_call_id is missing (older sessions / synthetic ids).
    """
    lookup = build_tool_call_args_lookup(messages)

    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue
        content = msg.content
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue

        if not (isinstance(data, dict)
                and data.get("success") is True
                and data.get("code") == 200
                and isinstance(data.get("result"), str)
                and data["result"]):
            continue

        tc_id = getattr(msg, "tool_call_id", "")
        if tc_id and tc_id in lookup:
            args = lookup[tc_id]
            subcommand = args.get("subcommand", "")
            v_args = args.get("v_args", "")
            if (
                subcommand == "exec"
                and classify_blade_exec_payload(v_args).has_create
            ):
                return True
            continue

        logger.debug(
            "kubectl ToolMessage with ChaosBlade success JSON: "
            "tool_call_id=%s not in AIMessage lookup, using content-only detection",
            tc_id or "(none)",
        )
        return True

    return False


def was_kubectl_exec_delivery(
    state: dict, messages: list | None = None,
) -> bool:
    """Whether the LIVE experiment was created via ``kubectl exec``.

    Two evidence sources, unioned — the same pattern as the blade_destroy
    provenance fix (SC1):

    1. ``state["injection_method"] == "kubectl_exec"`` — the framework's
       DURABLE record. It is committed in the same iteration the
       ``experiment_uid`` appears (channel A/B), is ``durable=True`` in the
       state lifecycle and is inherited by the recover graph, so it
       survives anything that happens to the message list.
    2. The message scan (:func:`scan_kubectl_blade_success`) — kept as a
       fallback for state-less paths (sessions restored without the
       method recorded). ``messages`` overrides ``state["messages"]``
       for callers that receive the history separately (the provider
       ``recover()`` contract passes it through kwargs).

    The scan alone is NOT durable: recovery runs LATE in the task, and
    compaction removes the injection evidence pair (the kubectl-exec
    AIMessage + its ChaosBlade-success ToolMessage are among the oldest
    messages) BY DESIGN. Losing it mis-routes a CRD-created experiment
    into the deterministic HOST ``blade_destroy`` — which cannot reach it
    ("record not found") — and withholds the kubectl-exec recovery
    instructions from the LLM flow. The verify side already routes on the
    durable record (``ChaosbladeProvider.layer1_verify``); recovery must
    agree with it.
    """
    if state.get("injection_method") == "kubectl_exec":
        return True
    msgs = messages if messages is not None else (state.get("messages") or [])
    return scan_kubectl_blade_success(msgs)


def was_blade_create_attempted(
    messages: list, injection_method: str | None = None,
    *, is_teardown=None,
) -> bool:
    """Check if ChaosBlade injection was attempted but ultimately failed.

    Returns False (not "attempted-and-failed") if:
      - a committed ``injection_method`` durable record exists (see below)
      - kubectl exec successfully injected a blade experiment (bypassing blade_create)
      - kubectl-native injection was used as an alternative after blade_create failed
    Returns True only if blade_create was called AND no successful injection
    was detected via any method.

    ``is_teardown`` (P3, the O-1 door closed): the teardown≠mutation
    matcher — a registered-vehicle cleanup delete after the failed
    ``blade_create`` is NOT a kubectl-native fallback injection, so it
    must not flip this judgement to "not attempted-and-failed". Callers
    with task state thread ``execution_artifacts.make_teardown_matcher(
    state["execution_artifacts"])``; ``None`` (the default) is the RAW
    scan (test fixtures, state-less callers whose histories carry no
    registered vehicles).

    This distinguishes two scenarios when experiment_uid is empty:
      - True:  ChaosBlade injection was attempted but failed → Layer 1 returns "failed"
      - False: Non-ChaosBlade fault, OR kubectl-based injection succeeded → Layer 1 returns "skipped"

    Durable record first: ``injection_method`` is committed when the
    injection is ISSUED/succeeds (Direction B) and survives compaction — the
    same rationale as :func:`was_kubectl_exec_delivery`. ANY committed
    attribution is positive proof that some injection succeeded, so the
    "blade attempted but nothing injected" branch cannot apply, regardless
    of what the (possibly compacted) message history still shows. Without
    this, a replan/compaction that removes the kubectl-native fallback
    evidence while leaving a failed ``blade_create`` ToolMessage mis-routes
    a live, recoverable fault into the terminal "no UID" failure. The
    message scan below stays as the fallback for state-less restored
    sessions that have no durable record.
    """
    if injection_method:
        return False

    # If kubectl-based blade injection succeeded, injection was NOT "attempted and failed"
    if scan_kubectl_blade_success(messages):
        return False

    # If kubectl-native injection was used as alternative after blade_create
    # failed, treat as non-ChaosBlade fault (Layer 1 = "skipped"). The
    # vocabulary is kubectl tool-domain knowledge
    # (``providers.message_scanning``, phase-14 G2: the "did native take over
    # after blade failed" judgement is boundary knowledge and lives with the
    # blade domain, but the word lists it consumes belong to the tool).
    if scan_kubectl_injection_after_blade(
        messages,
        KUBECTL_WRITE_SUBCOMMANDS,
        command_subcommands=KUBECTL_COMMAND_SUBCOMMANDS,
        is_mutating_command=exec_inner_command_mutates,
        is_blade_create_delivery=_is_blade_create_delivery,
        is_teardown=is_teardown,
    ):
        return False

    for msg in messages:
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "blade_create":
            return True
    return False


# ---------------------------------------------------------------------------
# Experiment-UID extraction from message evidence
# ---------------------------------------------------------------------------

def _parse_blade_uid_from_content(content) -> str | None:
    """Extract a ChaosBlade UID from ToolMessage content.

    Thin wrapper around this module's :func:`extract_experiment_uid`
    — accepts the raw `content` field of a ToolMessage (string or other) and
    delegates multi-strategy parsing to the shared provider-family helper.
    """
    if not isinstance(content, str):
        return None
    return extract_experiment_uid(content)


def _parse_uid_from_status_content(content) -> str | None:
    """Extract experiment UID from blade_status or blade_query_k8s output.

    blade_status / blade_query_k8s return:
        {"code":200,"success":true,"result":{"uid":"<hex>","phase":"Running",...}}

    Unlike blade_create (where ``result`` is a string UID), these tools
    return ``result`` as a **dict** containing a ``uid`` field. The
    standard ``extract_experiment_uid`` does not handle this case because its
    strategy 1 only accepts string results, and its regex strategy expects
    UUID format (8-4-4-4-12) while ChaosBlade UIDs are short hex strings.
    """
    if not isinstance(content, str) or not content:
        return None

    # First try the standard extractor (handles blade_create format
    # where result is a string, and chaosblade-<hex> resource names)
    uid = extract_experiment_uid(content)
    if uid:
        return uid

    # Handle blade_status/blade_query_k8s format where result is a dict.
    # Round-19 N1: this is the THIRD return point of the code=200/
    # success=true extraction family (after the result string and the
    # 54000-initializing uid) — it takes the same ``_UID_SHAPE_RE`` gate
    # the round-18 F-e ruling put on the first two; a non-shaped
    # ``result.uid`` string is not an experiment UID and must not ride
    # the single slot.
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    # ChaosBlade success response with dict result
    if data.get("success") is True and data.get("code") == 200:
        result = data.get("result")
        if isinstance(result, dict):
            uid = result.get("uid")
            if isinstance(uid, str) and _UID_SHAPE_RE.fullmatch(uid):
                return uid

    return None


def extract_experiment_uid_from_messages(
    messages: list,
    retired: "list[str] | set[str] | None" = None,
) -> str | None:
    """Scan messages for an experiment uid from a blade-family tool's output.

    ChaosBlade `blade create` returns JSON like:
        {"code": 200, "success": true, "result": "<uid>"}

    Sources scanned, in priority order:
      1. an experiment-creating tool (``blade_create`` for OS / K8s faults,
         ``blade_python_create`` for in-process application faults),
      2. ``kubectl exec ... blade create`` — the bypass the LLM may use when the
         blade tool fails on the host, where the success JSON lands in a kubectl
         ToolMessage,
      3. ``blade_status`` / ``blade_query_k8s`` — relevant when the create call
         timed out but the experiment was in fact created, so the LLM discovered
         the uid via a status query (uid nested in a dict ``result`` field).

    Only PURE-CREATE kubectl exec calls are considered for the kubectl face
    (round-17 H2c; round-18 F made it the ONLY lane — the graded middle
    lane is reverted): this is an INGESTION face — the segment composition
    (:attr:`BladeExecPayload.pure_create`) must prove no other command
    (``kubectl get -o json``, an ``echo``) contributed to the receipt before
    its output may license a UID. The round-17 middle lane let a composite
    receipt license the JSON-aware success anchors on the theory that a
    companion does not produce that shape "organically" — round-18 F
    proved an ``echo`` companion forges it verbatim, and round-16 F had
    already ruled segment composition the only lever, so composite
    receipts now license NOTHING (legislative parity with the birth
    registry). A composite payload whose create segment FAILED used to
    pass the looser ``has_create`` gate and let the loose regex fallback
    pick up the K8s ``metadata.uid`` from the companion ``get -o json``
    output, deadlocking recovery on an unfulfillable UID.
    Other kubectl outputs (get -o json, describe, ...) are NOT scanned, to
    prevent false-positive extraction from K8s resource ``metadata.uid``
    fields.

    ``retired``: UIDs destroyed by FRAMEWORK-side cleanup (verify-replan
    residual destroy). They leave no ``blade_destroy`` ToolMessage, so the
    message scan alone would resurrect them; callers holding
    ``state.retired_experiment_uids`` must pass it here (task-29848471).
    """
    kubectl_uid = None  # fallback uid from kubectl exec
    status_uid = None   # fallback uid from blade_status / blade_query_k8s

    # UIDs already sent to blade_destroy are cleaned-up / residual — never
    # treat them as the current active injection (root-cause guard).
    # ``scan_destroyed_uids`` is the carrier-agnostic primitive (the
    # execute-node's ``_collect_destroyed_uids`` mirrors it).
    destroyed = scan_destroyed_uids(messages)
    if retired:
        destroyed |= set(retired)

    # Build the tool_call_id set for the kubectl face — the ingestion gate
    # is the segment composition (round-17 H2c opened the graded lane,
    # round-18 F closed it: the strict anchor is forgeable, so a composite
    # receipt licenses NOTHING — legislative parity with the birth
    # registry). Only PURE-CREATE calls may license a UID:
    #   ``pure_create_call_ids`` — every segment is a ``blade create``, so
    #   the receipt's output domain is blade's own, full anchor chain.
    pure_create_call_ids: set[str] = set()
    for msg in messages:
        if not hasattr(msg, "tool_calls"):
            continue
        for tc in (msg.tool_calls or []):
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            if name == "kubectl" and isinstance(args, dict):
                v_args = args.get("v_args", "")
                payload = classify_blade_exec_payload(v_args)
                if payload.pure_create:
                    pure_create_call_ids.add(tc_id)

    # Check if an experiment-creating tool was attempted (even if it failed /
    # timed out). blade_status UID extraction is only relevant then — otherwise
    # the status check might pick up unrelated experiments.
    _has_blade_create = any(
        isinstance(msg, ToolMessage)
        and getattr(msg, "name", "") in _EXPERIMENT_CREATE_TOOLS
        for msg in messages
    )

    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        msg_name = getattr(msg, "name", "") or ""
        content = msg.content

        # Priority 1: a ToolMessage from an experiment-creating tool. Both
        # ``blade_create`` (OS / K8s carrier) and ``blade_python_create``
        # (in-process application carrier) return the same ChaosBlade CLI JSON
        # with the experiment uid, and both recover via ``blade destroy <uid>``.
        # Missing the python tool here would leave ``experiment_uid`` unset on the
        # ReAct path, so verification and recovery would have no uid to act on.
        if msg_name in _EXPERIMENT_CREATE_TOOLS:
            uid = _parse_blade_uid_from_content(content)
            if uid and uid not in destroyed:
                return uid

        # Priority 2: kubectl exec blade ToolMessage ONLY
        if msg_name == "kubectl" and not kubectl_uid:
            tool_call_id = getattr(msg, "tool_call_id", "") or ""
            if tool_call_id in pure_create_call_ids:
                # Composite receipts license NOTHING (round-18 F revert of
                # the round-17 graded lane — the strict anchor is forgeable
                # by an echo companion; segment composition is the only
                # lever, exactly like the birth registry's ruling).
                # Round-27: the destroyed filter sits at BIRTH granularity.
                # It used to sit at MESSAGE granularity — a composite
                # create message yielded its FIRST uid, a destroyed first
                # birth skipped the whole message, and the still-live
                # second birth in the SAME message was never re-extracted
                # (every singular consumer — the replan seam, the
                # compactor's survival pin, session recovery — reported
                # "no live experiment" while the sibling ran). The FIRST
                # live birth in content order wins: both live keeps the
                # first (the round-26 single-slot contract anchor), a dead
                # first-born now falls through to its live sibling.
                for _uid in receipt_birth_uids(content):
                    if _uid not in destroyed:
                        kubectl_uid = _uid
                        break

        # Priority 3: blade_status / blade_query_k8s ToolMessage
        # Relevant when blade_create timed out but experiment was created.
        if msg_name in ("blade_status", "blade_query_k8s") and not status_uid:
            if _has_blade_create:
                _uid = _parse_uid_from_status_content(content)
                if _uid and _uid not in destroyed:
                    status_uid = _uid

    # Return by priority: blade_create > kubectl exec > blade_status
    return kubectl_uid or status_uid


#: Experiment-creating tool names (both carriers of the blade family: the
#: OS/K8s carrier's ``blade_create`` and the python carrier's
#: ``blade_python_create`` — same ChaosBlade CLI receipt, same
#: ``blade destroy <uid>`` recovery). Hoisted module-level (round-26):
#: the singular and plural extraction faces below share ONE vocabulary,
#: not two copies that can drift.
_EXPERIMENT_CREATE_TOOLS = ("blade_create", "blade_python_create")


def extract_experiment_uids_from_messages(
    messages: list,
    retired: "list[str] | set[str] | None" = None,
) -> set[str]:
    """EVERY experiment uid provably born in these messages (plural face).

    The single-slot face (:func:`extract_experiment_uid_from_messages`)
    answers "which ONE experiment is current" — a 1-call=1-birth fossil:
    a composite inline create (``blade create A && blade create B``) proves
    TWO births in one call and the single-slot scan surfaces only the
    first, so the second is born an ORPHAN — never in the ownership
    ledger (``owned_experiment_uids``), invisible to
    ``live_liability_uids``, unrecoverable by any sweep (round-26 birth
    face, the symmetric defect of the death face's J3 hole). The birth
    registry's question is "which experiments does this task OWN" — a
    liability question, and the answer is plural by nature.

    Faces scanned:

    1. every ``blade_create``/``blade_python_create`` ToolMessage — each
       tool call IS one create, so every receipt proves its own birth
       (multiple calls, multiple births — the singular face returns only
       the newest);
    2. every PURE-CREATE kubectl exec call, licensed per receipt LINE
       (:func:`receipt_birth_uids`, round-27): a birth licence is
       content-derived — the uid lives in the line, the segment argv
       never carried it — so every JSON line of the receipt licenses its
       own birth, position-independently (the same composition gate the
       birth ledger always had: a companion licenses nothing). A failure
       line licenses nothing (a failed create owns no liability); the
       round-26 per-event positional gate was the death face's discipline
       inherited by the birth face, and it leaked honest births through
       transport shapes (the ``Error:`` wrapper, short-circuit, trailers).

    The status face is deliberately ABSENT: ``blade_status`` with no
    arguments lists EVERY experiment on the cluster — plural extraction
    from it would claim OTHER tasks' experiments as owned. The singular
    face's gated status fallback (create attempted, uid discovered via
    status) still feeds the single-slot seam, which the birth registry
    also consumes — nothing is lost, nothing foreign is claimed.

    ``retired``/destroyed uids are excluded (a dead experiment is not a
    live liability) — same exclusion the singular face applies.
    """
    born: set[str] = set()
    destroyed = scan_destroyed_uids(messages)
    if retired:
        destroyed |= set(retired)

    # tool_call_id → paired ToolMessage content (single pass, any position).
    results: dict[str, object] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", "")
            if tc_id:
                results[tc_id] = msg.content

    # Face 2: pure-create kubectl exec calls, receipt-aligned per segment.
    for msg in messages:
        for tc in getattr(msg, "tool_calls", None) or []:
            args = (
                tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            )
            if not isinstance(args, dict):
                continue
            name = (
                tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            )
            if name != "kubectl" or args.get("subcommand") != "exec":
                continue
            v_args = str(args.get("v_args") or "")
            if not classify_blade_exec_payload(v_args).pure_create:
                continue
            tc_id = (
                tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            )
            receipt = results.get(tc_id) if tc_id else None
            # Round-27: content-derived licensing (:func:`receipt_birth_uids`).
            # A birth licence lives in the receipt LINE — ``blade create``
            # prints the uid, the segment argv never carried it — so
            # positional alignment (the death face's binding discipline,
            # :func:`align_execution`) was over-strict here: the kubectl
            # ``Error:`` wrapper (a ``create A && create B`` whose second
            # stage failed exits non-zero and the tool glues the prefix
            # onto A's line), ``&&``/``||`` short-circuit (fewer lines than
            # segments) and stderr trailers (more) all leaked honest
            # births out of the liability ledger. The pure-create gate
            # above remains the domain lever: within it every JSON line of
            # the receipt is this task's own create output.
            for uid in receipt_birth_uids(receipt):
                if uid not in destroyed:
                    born.add(uid)

    # Face 1: every experiment-creating tool message — one create each.
    for msg in messages:
        if (
            isinstance(msg, ToolMessage)
            and getattr(msg, "name", "") in _EXPERIMENT_CREATE_TOOLS
        ):
            uid = _parse_blade_uid_from_content(msg.content)
            if uid and uid not in destroyed:
                born.add(uid)

    return born


# ---------------------------------------------------------------------------
# Layer-1 execution domain (moved verbatim from
# ``nodes/verify/_verifier_layer1.py`` in phase-4 T4 — design D2: the
# execution bodies the providers' ``layer1_verify`` dispatch into physically
# belong to the provider layer; only the STATE orchestration stayed in nodes).
# ---------------------------------------------------------------------------

# Layer1Result is now a Pydantic model imported from chaos_agent.agent.result.verdict

# Upper bound on how many discovered tool pods Layer 1 probes for the
# experiment record. Exec-carrier records live in one pod's local DB and
# discovery order is arbitrary, so we sweep broadly; this cap only bounds
# worst-case probe time on very large clusters (definitive results return
# early). See task-2d612caa.
_MAX_DISCOVERY_PROBES = 8


# ---------------------------------------------------------------------------
# Refactor 2: 提取 blade_status JSON 解析为独立函数
# 原因: blade_status 返回值解析嵌套 5-6 层，在 verifier() 和
#        _verifier_with_llm() 中完全重复
# 做法: 独立函数 + 扁平化 if/elif，消除深层嵌套
# ---------------------------------------------------------------------------

_EXPIRED_STATES = frozenset({"Destroyed", "destroyed", "Revoked", "revoked", "Completed", "completed"})

_RUNNING_STATES = frozenset({"Running", "running", "Success", "success"})

# Phases the Operator reports while it is still setting the experiment up. Not a
# verdict: ``Initialized`` means the CRD exists and the controller has not
# finished reconciling it, so ``statuses`` is still empty and there is nothing
# for Layer 1 to read. The fault itself may already be in effect — task-fc64c982
# stopped containerd, saw the node go Ready→NotReady, and was still reported
# ``failed`` because the CRD had not left ``Initialized`` by the time Layer 1
# polled. A setup phase is therefore a warning, which keeps Layer 2 in play to
# judge the actual cluster state.
_TRANSIENT_STATES = frozenset({"Initialized", "initialized", "Creating", "creating"})

# Substrings that unambiguously signal a FAILED / absent experiment. Checked in
# the non-JSON fallback path BEFORE the permissive _RUNNING_STATES match, so a
# wrapped `{"success":false,...}` (whose JSON was unparseable due to a shell
# "command terminated" trailer) is never misread as Running.
_FAILURE_SIGNALS = (
    "record not found",
    "not found",
    '"success":false',
    '"success": false',
    "command terminated with exit code",
)


def _extract_json_object(raw: str) -> dict | None:
    """Extract the first top-level JSON object from possibly-wrapped output.

    Transport wrappers can prepend an ``exit_code: N`` line and append a
    ``command terminated with exit code N`` trailer around the real ChaosBlade
    JSON, which makes a naive ``json.loads(raw)`` fail and pushes callers onto
    fragile substring matching. This scans for the first ``{`` and uses
    ``raw_decode`` so surrounding noise is ignored.
    """
    if not raw:
        return None
    start = raw.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw[start:])
        except json.JSONDecodeError:
            start = raw.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = raw.find("{", start + 1)
    return None


def _parse_iso_ts_seconds(ts_value, created_value) -> float | None:
    """Return (UpdateTime - CreateTime) in seconds, or None if unparseable."""
    from datetime import datetime

    def _parse(ts) -> datetime | None:
        if not isinstance(ts, str) or not ts.strip():
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None

    updated = _parse(ts_value)
    created = _parse(created_value)
    if updated is None or created is None:
        return None
    return (updated - created).total_seconds()


def _is_early_destroy(res: dict, exp_status: str) -> bool:
    """True when an expired record was destroyed BEFORE its --timeout elapsed.

    Distinguishes timeout expiry (record lives out its full window) from an
    external destroy — e.g. the executor cleaning up its own injection record
    after a one-shot fault (task inject-e47de3e8: destroyed +20.4s after
    creation with --timeout=600). The two need different verdicts: expiry
    means the fault window closed and live observation is impossible, while an
    early destroy says nothing about the fault's actual effects.
    """
    flag = str(res.get("Flag", "") or "")
    match = re.search(r"--timeout[=\s]+(\d+)", flag)
    if not match:
        return False
    try:
        timeout_seconds = int(match.group(1))
    except ValueError:
        return False
    if timeout_seconds <= 0:
        return False
    elapsed = _parse_iso_ts_seconds(
        res.get("UpdateTime") or res.get("update_time"),
        res.get("CreateTime") or res.get("create_time"),
    )
    if elapsed is None:
        return False
    logger.info(
        "Layer1 expired-record attribution: status=%s elapsed=%.1fs timeout=%ss",
        exp_status, elapsed, timeout_seconds,
    )
    return elapsed < timeout_seconds


def _parse_blade_status_output(raw: str) -> tuple[str, str, bool]:
    """Parse blade_status JSON output into (status, details, expired).

    Returns:
        status: "passed" if experiment is Running/Success, "failed" otherwise.
        details: Human-readable details string.
        expired: True if experiment status is Destroyed/Revoked/Completed (timeout expired).
    """
    data = _extract_json_object(raw)
    if data is None:
        # Fallback: raw string search. Guard against false positives first —
        # an explicit failure signal (e.g. `"success":false` / "record not
        # found" / a shell "command terminated" trailer) must NOT be read as
        # "Running" just because the substring "success" appears inside it.
        lowered = raw.lower()
        if any(sig in lowered for sig in _FAILURE_SIGNALS):
            return "failed", raw[:200], False
        if any(s in raw for s in _RUNNING_STATES):
            return "passed", "blade_status: Running (raw match)", False
        return "failed", raw[:200], False

    if not (data.get("success") or data.get("code") == 200):
        return "failed", raw[:200], False

    res = data.get("result", {})
    # Non-dict result (e.g. just a UID string) means success
    if not isinstance(res, dict):
        return "passed", "blade_status: Success (experiment running)", False

    exp_status = res.get("Status", res.get("status", "")) or res.get("phase", "")
    if exp_status in _RUNNING_STATES:
        return "passed", f"blade_status: {exp_status} (experiment running)", False
    if exp_status in _EXPIRED_STATES:
        if _is_early_destroy(res, exp_status):
            return (
                "warning",
                f"Experiment record is '{exp_status}' before its --timeout elapsed — "
                f"the record was destroyed externally (e.g. post-injection cleanup), "
                f"not by timeout expiry. Record liveness cannot judge the fault's "
                f"actual effects; Layer 2 will verify cluster-level evidence.",
                True,
            )
        return (
            "failed",
            f"Experiment status: {exp_status} — the fault window has expired "
            f"(--timeout elapsed), so live fault effects can no longer be observed. "
            f"Layer 2 may still find residual evidence.",
            True,
        )
    # Transient state: the experiment is mid-transition, either because the
    # Operator is still reconciling a freshly created CRD (``Initialized``) or
    # because blade reports "please wait" during setup/teardown. Neither is a
    # verdict — the fault may already be in effect, so Layer 2 decides on the
    # actual cluster state rather than on the controller's bookkeeping.
    error_msg = res.get("Error", "")
    if exp_status in _TRANSIENT_STATES:
        return (
            "warning",
            f"Experiment status: {exp_status} — the Operator is still setting the "
            f"experiment up, so no per-resource status is available yet. "
            f"Layer 2 will verify actual cluster state.",
            False,
        )
    if "please wait" in error_msg.lower():
        return (
            "warning",
            f"Experiment in transient state ({error_msg}). "
            f"Layer 2 will verify actual cluster state.",
            False,
        )
    return "failed", f"Experiment status: {exp_status}", False


# ---------------------------------------------------------------------------
# Refactor 3: 提取 blade_query_k8s 结果解析为独立函数
# 原因: 同上，深层嵌套 + 两处重复
# 做法: 独立函数，职责单一——只负责解析 query k8s 返回值
# ---------------------------------------------------------------------------

_QueryK8sResult = namedtuple(
    "_QueryK8sResult", ["status", "details", "resource_statuses", "affected_count", "expired"],
)


def _parse_blade_query_k8s_output(raw: str) -> _QueryK8sResult:
    """Parse blade_query_k8s JSON output for per-resource status.

    Returns:
        _QueryK8sResult with:
            status: "passed" if all resources succeeded, "failed" if any failed,
                    "unknown" if output cannot be parsed (non-critical).
            details: Human-readable summary of resource-level results.
            resource_statuses: list of per-resource dicts from statuses[].
            affected_count: number of resources in statuses[].
            expired: True if any resource has state in _EXPIRED_STATES (Destroyed/Revoked/Completed).
    """
    _empty = _QueryK8sResult("unknown", "", [], 0, False)

    if not raw or raw.startswith("Error"):
        logger.debug(f"blade_query_k8s: empty/error output, raw={raw[:200]!r}")
        # Extract meaningful info from error messages (e.g., "not found" = CRD not yet ready)
        if "not found" in raw:
            return _QueryK8sResult("unknown", "blade_query_k8s: CRD not yet ready (will be available shortly)", [], 0, False)
        return _empty

    data = _extract_json_object(raw)
    if data is None:
        logger.debug(f"blade_query_k8s: non-JSON output, raw={raw[:200]!r}")
        return _empty

    if not (data.get("success") or data.get("code") == 200):
        logger.debug(f"blade_query_k8s: unsuccessful response, data={json.dumps(data, ensure_ascii=False)[:200]}")
        # ChaosBlade returns JSON errors like {"code":63061,"success":false,"error":"...not found"}
        err_msg = data.get("error", "")
        if "not found" in err_msg.lower():
            return _QueryK8sResult("unknown", "blade_query_k8s: CRD not found (experiment may still be initializing)", [], 0, False)
        return _empty

    qresult = data.get("result", {})
    statuses = qresult.get("statuses", [])

    if statuses:
        # Check for expired states FIRST (before generic failed check)
        expired_states = [s for s in statuses if s.get("state", "") in _EXPIRED_STATES]
        if expired_states:
            names = [s.get("name", "?") for s in expired_states]
            return _QueryK8sResult(
                "failed",
                f"blade query k8s: experiment expired (state: Destroyed/Revoked): {names}",
                statuses, len(statuses), True,
            )
        failed = [s for s in statuses if not s.get("success", True)]
        if failed:
            names = [s.get("name", "?") for s in failed]
            return _QueryK8sResult("failed", f"blade query k8s: failed resources: {names}", statuses, len(statuses), False)
        return _QueryK8sResult("passed", f"blade query k8s: all {len(statuses)} resource(s) Success", statuses, len(statuses), False)

    if isinstance(qresult, dict) and qresult.get("success", True):
        return _QueryK8sResult("passed", "blade query k8s: confirmed", [], 0, False)

    logger.debug(f"blade_query_k8s: unhandled format, result={json.dumps(qresult, ensure_ascii=False)[:200]}")
    return _empty


# ---------------------------------------------------------------------------
# Refactor 4: 提取完整的 Layer 1 验证流程为独立函数
# 原因: Layer 1 逻辑（blade_status → blade_query_k8s）在两个入口函数中
#        完全重复 ~80 行，且包含 try/except 错误处理
# 做法: 独立 async 函数，返回 Layer1Result dataclass，彻底消除重复
# Note: _resolve_kubeconfig moved to _kubeconfig_inject.py for shared use
# ---------------------------------------------------------------------------


def _find_blade_query_in_messages(messages: list, experiment_uid: str) -> str:
    """Scan kubectl ToolMessages for blade query k8s output matching the given uid.

    When the host blade binary is unavailable, the LLM may have already run
    `blade query k8s create <uid>` via kubectl exec during the execution phase.
    This function finds that output so Layer 1 can use it as verification evidence.

    Returns the raw JSON string if found, empty string otherwise.
    """
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if experiment_uid in content and '"success"' in content:
            try:
                data = json.loads(content)
                if isinstance(data, dict) and data.get("success") is True:
                    result = data.get("result", {})
                    if isinstance(result, dict) and result.get("uid") == experiment_uid:
                        return content
            except (json.JSONDecodeError, TypeError):
                pass
    return ""


def _map_query_k8s_to_layer1(
    q_result: _QueryK8sResult, raw: str, pod_name: str, source: str,
) -> Layer1Result:
    """Map _QueryK8sResult to Layer1Result with expired detection.

    Used when kubectl exec path uses `blade query k8s create <uid>`
    instead of `blade status <uid>` (CRD UID not in pod's local DB).
    """
    if q_result.status == "passed":
        layer1_status = "passed"
    elif q_result.expired:
        # expired=True means experiment Destroyed/Revoked
        layer1_status = "failed"
    else:
        layer1_status = q_result.status  # "failed" or "unknown"
    return Layer1Result(
        status=layer1_status,
        details=f"blade query k8s via kubectl exec ({pod_name}, {source}): {q_result.details}",
        raw_output=raw,
        resource_statuses=q_result.resource_statuses,
        affected_count=q_result.affected_count,
        expired=q_result.expired,
    )


async def _run_layer1_via_kubectl_exec(
    experiment_uid: str, kubeconfig: str, *, task_id: str = "",
    injection_pod_name: str | None = None,
) -> Layer1Result:
    """Layer 1 verification via kubectl exec into a tool pod.

    Used when injection_method is "kubectl_exec" (host blade binary may be
    incompatible, so host blade_status would fail).

    If the original injection pod name is known (injection_pod_name), it is
    tried first (Step 0) before discovering new pods (Step 1). This maximises
    success probability since the original pod is where the experiment was
    created and is most likely to have it visible.

    Error handling follows the principle "infrastructure failure ≠ experiment failure":
    - Type A (infrastructure failure): can't discover pods or can't exec
      into them -> "skipped" (non-terminal, Layer 2 proceeds)
    - Type B (experiment status failure): blade status returns Error/Destroyed
      -> "failed" (terminal, blocks Layer 2)

    Retries up to 2 different pods before giving up.
    """
    # No UID → nothing to poll. A kubectl-native injection (or a failed
    # blade_create) reaches here only via mis-detection; issuing `blade status
    # ''` / `blade query k8s create ''` returns ChaosBlade code 45000
    # ("less parameter: type|uid") which _parse_blade_status_output reads as a
    # genuine experiment FAILURE — wrongly failing a successful native fault
    # (task-76c59364). Treat an absent UID as "not applicable", not failed.
    if not experiment_uid:
        return Layer1Result(
            status="skipped",
            details="kubectl_exec Layer 1: no experiment_uid to query "
                    "(kubectl-native injection or failed blade create) — "
                    "Layer 1 not applicable, Layer 2 will verify cluster state.",
        )
    tracker = get_tracker(task_id) if task_id else None

    try:
        from chaos_agent.tools.kubectl import build_kubectl_cmd
        from chaos_agent.transports import (
            PROFILE_K8S,
            TransportTarget,
            execute_via_transport,
        )

        _target = TransportTarget.from_state({})

        # Step 0: Try the original injection pod first (if known)
        if injection_pod_name:
            # PRIMARY: blade query k8s (queries CRD, works with CRD UID)
            # blade status <crd_uid> returns "record not found" inside pod
            # because pod's local experiment DB uses a different UID.
            query_cmd = build_kubectl_cmd("exec", [
                injection_pod_name, "-n", _TOOL_POD_NAMESPACE,
                "--", "blade", "query", "k8s", "create", experiment_uid,
            ], kubeconfig=kubeconfig)
            try:
                query_run_result = await execute_via_transport(
                    query_cmd, _target, task_id=task_id, source="verifier-L1", expect_profile=PROFILE_K8S)
                raw = query_run_result.stdout

                # Check if blade query k8s is available (not in older ChaosBlade versions)
                if raw and "unknown command" not in raw and "command not found" not in raw:
                    if "error: unable to upgrade connection" in raw:
                        logger.info(
                            f"Original injection pod {injection_pod_name} unavailable, "
                            f"falling back to pod discovery"
                        )
                    else:
                        q_result = _parse_blade_query_k8s_output(raw)
                        # Only return if parseable; if unknown (kubectl exec error,
                        # non-JSON output), fall through to blade status fallback
                        if q_result.status != "unknown":
                            layer1_result = _map_query_k8s_to_layer1(q_result, raw, injection_pod_name, "original")
                            if tracker:
                                tracker.update(
                                    f"Layer 1 step 0: blade_query_k8s (kubectl exec {injection_pod_name}): {layer1_result.status}",
                                    {"step": "blade_query_k8s_kubectl", "status": layer1_result.status,
                                     "pod": injection_pod_name, "source": "original"},
                                )
                            return layer1_result
                        logger.info(
                            f"blade query k8s returned unparseable result from pod {injection_pod_name}, "
                            f"trying blade status fallback"
                        )
                elif raw and ("command not found" in raw or "No such file" in raw):
                    logger.info(
                        f"blade query k8s not available in pod {injection_pod_name}, "
                        f"trying blade status fallback"
                    )
                # If blade query k8s failed or unavailable, fall through to blade status
            except Exception as e:
                logger.info(
                    f"blade query k8s failed on original pod {injection_pod_name}: {e}, "
                    f"trying blade status fallback"
                )

            # FALLBACK: blade status (searches local DB, CRD UID may not be found)
            status_cmd = build_kubectl_cmd("exec", [
                injection_pod_name, "-n", _TOOL_POD_NAMESPACE,
                "--", "blade", "status", experiment_uid,
            ], kubeconfig=kubeconfig)
            try:
                status_result = await execute_via_transport(
                    status_cmd, _target, task_id=task_id, source="verifier-L1", expect_profile=PROFILE_K8S)
                raw = status_result.stdout

                # Check if the original pod is unavailable
                if raw and ("not found" in raw
                            or "error: unable to upgrade connection" in raw):
                    logger.info(
                        f"Original injection pod {injection_pod_name} unavailable, "
                        f"falling back to pod discovery"
                    )
                elif raw and ("command not found" in raw or "No such file" in raw):
                    logger.info(
                        f"blade binary not found in original pod {injection_pod_name}, "
                        f"falling back to pod discovery"
                    )
                elif raw:
                    # Got a parseable response from the original pod
                    status, details, expired = _parse_blade_status_output(raw)
                    if tracker:
                        tracker.update(
                            f"Layer 1 step 0: blade_status (kubectl exec {injection_pod_name}): {status}",
                            {"step": "blade_status_kubectl", "status": status,
                             "pod": injection_pod_name, "source": "original"},
                        )
                    return Layer1Result(
                        status=status,
                        details=f"blade_status via kubectl exec ({injection_pod_name}, original): {details}",
                        raw_output=raw,
                        expired=expired,
                    )
                else:
                    logger.info(
                        f"Empty response from original pod {injection_pod_name}, "
                        f"falling back to pod discovery"
                    )
            except Exception as e:
                logger.info(
                    f"Failed to query original pod {injection_pod_name}: {e}, "
                    f"falling back to pod discovery"
                )

        # Step 1: Discover running tool pods (cluster-wide)
        from chaos_agent.tools.pod_discovery import discover_tool_pods_cluster_wide
        try:
            pods_with_ns = await discover_tool_pods_cluster_wide(kubeconfig, task_id)
        except Exception as e:
            msg = f"kubectl exec: failed to discover tool pods: {e}"
            if tracker:
                tracker.update(f"Layer 1 (kubectl exec): {msg} -> skipped",
                               {"step": "discover_pods", "status": "skipped"})
            return Layer1Result(
                status="skipped",
                details=f"{msg} (infrastructure issue, not experiment failure)",
            )

        if not pods_with_ns:
            msg = "kubectl exec: no running tool pods found, cannot verify blade status"
            if tracker:
                tracker.update(f"Layer 1 (kubectl exec): {msg} -> skipped",
                               {"step": "discover_pods", "status": "skipped"})
            return Layer1Result(
                status="skipped",
                details=f"{msg} (infrastructure issue, not experiment failure)",
            )

        # Step 2: Try blade query k8s (primary) then blade status (fallback)
        # via kubectl exec on each discovered pod. Exec-carrier experiments
        # live in ONE pod's local DB and discovery order is arbitrary, so
        # every candidate must be probed before a "record not found" verdict
        # (task-2d612caa: a 2-pod cap read the wrong pod's empty DB as
        # experiment failure). The bound only limits worst-case probe time on
        # very large clusters; a definitive result always returns early.
        # NOTE: blade status v1.8.0 does NOT support --kubeconfig flag.
        # Inside the pod, blade can access the API server directly without kubeconfig.
        last_error = None
        for pod_name, pod_ns in pods_with_ns[:_MAX_DISCOVERY_PROBES]:
            # PRIMARY: blade query k8s (queries CRD, works with CRD UID)
            query_cmd = build_kubectl_cmd("exec", [
                pod_name, "-n", pod_ns,
                "--", "blade", "query", "k8s", "create", experiment_uid,
            ], kubeconfig=kubeconfig)
            try:
                query_result = await execute_via_transport(
                    query_cmd, _target, task_id=task_id, source="verifier-L1", expect_profile=PROFILE_K8S)
                raw = query_result.stdout

                # Check for Type A infrastructure errors
                if not raw or "error: unable to upgrade connection" in raw:
                    last_error = f"cannot exec into pod {pod_name}"
                    continue

                # If blade query k8s is available, use it
                if "unknown command" not in raw and "command not found" not in raw and "No such file" not in raw:
                    q_result = _parse_blade_query_k8s_output(raw)
                    # Only return if parseable; if unknown (kubectl exec error,
                    # non-JSON output), fall through to blade status fallback
                    if q_result.status != "unknown":
                        layer1_result = _map_query_k8s_to_layer1(q_result, raw, pod_name, "discovered")
                        if tracker:
                            tracker.update(
                                f"Layer 1 step 1/1: blade_query_k8s (kubectl exec {pod_name}): {layer1_result.status}",
                                {"step": "blade_query_k8s_kubectl", "status": layer1_result.status, "pod": pod_name},
                            )
                        return layer1_result
                    logger.info(f"blade query k8s returned unparseable result from pod {pod_name}, trying blade status")

                # FALLBACK: blade query k8s not available, try blade status
                logger.info(f"blade query k8s not available in pod {pod_name}, trying blade status")
            except Exception as e:
                logger.debug(f"blade query k8s failed in pod {pod_name}: {e}, trying blade status")

            # FALLBACK: blade status (searches local DB, CRD UID may not be found)
            status_cmd = build_kubectl_cmd("exec", [
                pod_name, "-n", pod_ns,
                "--", "blade", "status", experiment_uid,
            ], kubeconfig=kubeconfig)
            try:
                status_result = await execute_via_transport(
                    status_cmd, _target, task_id=task_id, source="verifier-L1", expect_profile=PROFILE_K8S)
                raw = status_result.stdout

                # Check for Type A infrastructure errors (can't execute command)
                if not raw or "command not found" in raw or "No such file" in raw:
                    last_error = f"blade binary not found in pod {pod_name}"
                    continue
                if "error: unable to upgrade connection" in raw:
                    last_error = f"cannot exec into pod {pod_name}"
                    continue

                # Per-pod local DB guard: an experiment created via exec on
                # one tool pod lives in THAT pod's local DB only — every
                # other tool pod reports "record not found". A discovered
                # pod's empty DB is therefore not a verdict; the next pod
                # may be the injection pod (task-2d612caa).
                if "record not found" in raw:
                    last_error = f"record not found in pod {pod_name} local DB"
                    continue

                # Type B: Parse blade status output (experiment status)
                status, details, expired = _parse_blade_status_output(raw)
                if tracker:
                    tracker.update(
                        f"Layer 1 step 1/1: blade_status (kubectl exec {pod_name}): {status}",
                        {"step": "blade_status_kubectl", "status": status, "pod": pod_name},
                    )
                return Layer1Result(
                    status=status,
                    details=f"blade_status via kubectl exec ({pod_name}): {details}",
                    raw_output=raw,
                    expired=expired,
                )
            except Exception as e:
                last_error = str(e)
                continue

        # All pods failed. Distinguish an experiment-level verdict from an
        # infrastructure failure: if every probed pod answered (exit code and
        # parseable JSON) but none holds the record, the experiment genuinely
        # cannot be found anywhere — that is a failure, not a skip.
        if last_error and "record not found" in last_error:
            msg = f"kubectl exec: experiment record not found in any tool pod's local DB ({last_error})"
            if tracker:
                tracker.update(
                    "Layer 1 (kubectl exec): record not found in all tool pods -> failed",
                    {"step": "blade_status_kubectl", "status": "failed"},
                )
            return Layer1Result(status="failed", details=msg)

        # Type A (infrastructure failure) -> skipped
        msg = f"kubectl exec: could not execute blade status in any tool pod ({last_error})"
        if tracker:
            tracker.update(
                "Layer 1 (kubectl exec): all tool pods failed -> skipped",
                {"step": "blade_status_kubectl", "status": "skipped"},
            )
        return Layer1Result(
            status="skipped",
            details=f"{msg} -- infrastructure issue, not experiment failure",
        )

    except Exception as e:
        logger.error(f"Layer 1 kubectl exec verification failed: {e}")
        return Layer1Result(
            status="skipped",
            details=f"kubectl exec verification error: {e} -- infrastructure issue, allowing Layer 2 to proceed",
        )


async def _run_host_blade_layer1(
    experiment_uid: str, kubeconfig: str, *, task_id: str = "",
    messages: list | None = None,
    injection_method: str | None = None,
    is_teardown=None,
) -> Layer1Result:
    """Execute host-blade Layer 1 verification: blade_status + blade_query_k8s.

    This is the ChaosBlade ``host_blade`` delivery body (local blade binary).
    The ``kubectl_exec`` delivery is a separate path selected by
    :class:`ChaosbladeProvider` via :func:`_run_layer1_via_kubectl_exec`, so this
    function carries no ``injection_method`` branching.

    Returns a Layer1Result with status, details, and raw output.
    Also emits per-step status events via StatusTracker so the user
    can see each check individually.
    """
    if not experiment_uid:
        # Same-package attempted judgement (was ``_was_blade_create_attempted``
        # through the nodes re-export pre-migration — same function object).
        if messages and was_blade_create_attempted(
            messages, injection_method, is_teardown=is_teardown,
        ):
            # blade_create was called but extract_blade_uid rejected the UID
            # (e.g., 54000+success=false). blade's error report may be wrong
            # (ChaosBlade may use fallback mechanisms like tc instead of
            # iptables). Mark as WARNING (non-terminal) — Layer 2 will
            # verify actual cluster state to determine the truth.
            return Layer1Result(
                status="warning",
                details="blade_create was called but reported error — "
                        "fault may still be in effect via fallback mechanisms. "
                        "Layer 2 will verify actual cluster state.",
            )
        return Layer1Result(
            status="skipped",
            details="Non-ChaosBlade fault (no blade_create used), Layer 1 not applicable",
        )

    tracker = get_tracker(task_id) if task_id else None

    try:
        from chaos_agent.agent.providers.chaosblade.cli import (
            blade_query_k8s,
            blade_status,
        )
        from chaos_agent.transports import PROFILE_HOST, profile_of, resolve_channel_name

        # Host scope has no cluster CRD: blade_query_k8s is k8s-only and would
        # just return a "not applicable" guidance string. blade_status (remote
        # local DB) is the authoritative Layer 1 check for host, so skip the
        # k8s-side query steps below when the resolved channel is a host channel.
        _is_host = profile_of(resolve_channel_name()) == PROFILE_HOST

        # Step 1: blade_status — experiment-level check
        status_output = await blade_status.ainvoke(
            {"uid": experiment_uid, "kubeconfig": kubeconfig}
        )
        raw = status_output if isinstance(status_output, str) else str(status_output)
        layer1_status, layer1_details, layer1_expired = _parse_blade_status_output(raw)

        # If blade_status failed because the experiment isn't in the local DB,
        # fall back to blade_query_k8s (cluster-side CRD query). Skipped for host
        # scope: there is no cluster CRD, so a host "record not found" is a
        # genuine failure that the k8s query cannot resolve.
        # Two cases: (1) explicit "record not found" message, (2) empty stdout
        # (kubewiz mode — experiment runs remotely, no local record exists).
        _fallback_used = False
        if not _is_host and layer1_status == "failed" and (not raw.strip() or "record not found" in raw.lower()):
            logger.info(f"blade_status local DB miss (raw={raw[:80]!r}), trying blade_query_k8s as fallback")
            try:
                query_output = await blade_query_k8s.ainvoke(
                    {"uid": experiment_uid, "kubeconfig": kubeconfig}
                )
                query_raw = query_output if isinstance(query_output, str) else str(query_output)
                q_result = _parse_blade_query_k8s_output(query_raw)
                if q_result.status != "unknown":
                    layer1_status = q_result.status
                    layer1_details = f"blade_query_k8s fallback: {q_result.details}"
                    layer1_expired = q_result.expired
                    # Preserve fallback data — will be used directly if Step 2 is skipped
                    q_resource_statuses = q_result.resource_statuses
                    q_affected_count = q_result.affected_count
                    _fallback_used = True
                    logger.info(f"blade_query_k8s fallback succeeded: status={q_result.status}")
            except Exception as qe:
                logger.debug(f"blade_query_k8s fallback also failed: {qe}")

        # Emit step 1 result
        step1_msg = f"Layer 1 step 1/2: blade_status: {layer1_status}"
        if layer1_details:
            step1_msg += f" - {layer1_details}"
        if tracker:
            tracker.update(step1_msg, {"step": "blade_status", "status": layer1_status})

        # Step 2: blade_query_k8s — per-resource check (supplementary)
        # Only if blade_status passed AND fallback was NOT used (fallback already
        # has the blade_query_k8s data; re-querying would waste an API call and
        # overwrite the fallback's resource_statuses/affected_count).
        query_status_str = "skipped"
        query_details_str = ""
        if _fallback_used:
            # Fallback already provided blade_query_k8s data — use it directly
            query_status_str = layer1_status
            query_details_str = layer1_details
        else:
            q_resource_statuses: list[dict] = []
            q_affected_count = 0
            if _is_host:
                # Host scope: no cluster CRD to query — blade_status above is
                # authoritative. Skip the supplementary k8s per-resource query.
                query_status_str = "n/a"
                query_details_str = "host scope: no cluster CRD (blade_status is authoritative)"
            elif layer1_status == "passed":
                try:
                    query_output = await blade_query_k8s.ainvoke(
                        {"uid": experiment_uid, "kubeconfig": kubeconfig}
                    )
                    query_raw = query_output if isinstance(query_output, str) else str(query_output)
                    q_result = _parse_blade_query_k8s_output(query_raw)
                    q_status = q_result.status
                    q_details = q_result.details
                    q_resource_statuses = q_result.resource_statuses
                    q_affected_count = q_result.affected_count
                    # If blade_query_k8s detected expired state, propagate expired flag
                    if q_result.expired:
                        layer1_expired = True
                    query_status_str = q_status
                    query_details_str = q_details

                    if q_status == "failed":
                        layer1_status = "failed"
                        layer1_details = f"blade_status: Running, but {q_details}"
                    elif q_status == "passed":
                        # CRD status settle guard: ChaosBlade CRD reports
                        # Success immediately upon creation, then asynchronously
                        # exec's the fault process into the target container.
                        # If exec fails (e.g. "dd: command not found" in minimal
                        # images), the CRD status flips to Error a few seconds
                        # later. Querying too early sees stale Success. Wait
                        # briefly and re-query to catch async failures.
                        import asyncio
                        if tracker:
                            tracker.update(
                                "CRD settle guard: waiting 5s to confirm injection process started",
                                {"step": "crd_settle_guard"},
                            )
                        await asyncio.sleep(5)
                        try:
                            recheck_output = await blade_query_k8s.ainvoke(
                                {"uid": experiment_uid, "kubeconfig": kubeconfig}
                            )
                            recheck_raw = recheck_output if isinstance(recheck_output, str) else str(recheck_output)
                            recheck = _parse_blade_query_k8s_output(recheck_raw)
                            if recheck.status == "failed":
                                q_status = "failed"
                                q_details = f"re-check after 5s: {recheck.details}"
                                q_resource_statuses = recheck.resource_statuses
                                q_affected_count = recheck.affected_count
                                query_status_str = q_status
                                query_details_str = q_details
                                layer1_status = "failed"
                                layer1_details = f"blade_status: Running, but {q_details}"
                                logger.info("CRD settle guard: status flipped to failed after 5s re-check")
                            elif recheck.expired:
                                layer1_expired = True
                            else:
                                layer1_details = f"blade_status: Running, {q_details} (confirmed after re-check)"
                        except Exception:
                            layer1_details = f"blade_status: Running, {q_details}"
                    else:
                        # q_status == "unknown": non-critical, keep blade_status result
                        if not layer1_details:
                            layer1_details = "blade_status: Running (blade_query_k8s unavailable)"
                except Exception as qe:
                    query_status_str = "error"
                    query_details_str = str(qe)
                    logger.debug(f"blade query k8s failed (non-critical): {qe}")

        # Emit step 2 result
        step2_msg = f"Layer 1 step 2/2: blade_query_k8s: {query_status_str}"
        if query_details_str:
            step2_msg += f" - {query_details_str}"
        if tracker:
            tracker.update(step2_msg, {"step": "blade_query_k8s", "status": query_status_str})

        # Degradation: when blade_status/blade_query_k8s report failure but we have
        # evidence of successful injection via kubectl exec (host blade binary broken),
        # try to find blade query k8s results in message history as fallback.
        if layer1_status == "failed" and experiment_uid and messages:
            # Fallback 1: find blade query k8s evidence from kubectl exec in message history
            fallback = _find_blade_query_in_messages(messages, experiment_uid)
            if fallback:
                layer1_status = "passed"
                layer1_details = (
                    "blade_status unavailable (host blade error), "
                    "but blade query k8s from kubectl exec confirmed injection success"
                )
            # Same-package scan primitive (was ``_was_kubectl_blade_injection_successful``
            # through the nodes thin wrapper pre-migration — the wrapper
            # delegates to this exact function, so behaviour is identical).
            elif scan_kubectl_blade_success(messages):
                # Fallback 2: kubectl exec injection output exists but no query evidence
                layer1_status = "skipped"
                layer1_details = (
                    f"blade_status/blade_query_k8s reported failure, "
                    f"but experiment_uid={experiment_uid} was extracted from kubectl exec injection output. "
                    f"Host blade binary may be incompatible."
                )

        # Fallback 3: Self-destructive fault detection.
        # Some faults (e.g. node-process stop containerd) destroy the very
        # communication channel Layer 1 uses to verify. The injection
        # succeeds, but blade_status/blade_query_k8s fail because the
        # target node is now unreachable. Detect this by checking if the
        # failure is a connectivity error AND the target node is NotReady
        # (which is observable via API server, not via the dead node).
        if layer1_status == "failed" and experiment_uid:
            _conn_keywords = (
                "connection refused", "connection timed out",
                "unreachable", "dial tcp", "i/o timeout",
            )
            _all_text = ((layer1_details or "") + (raw or "")).lower()
            if any(kw in _all_text for kw in _conn_keywords):
                try:
                    from chaos_agent.tools.kubectl import kubectl_read as _kro
                    _node_out = await _kro.ainvoke({
                        "subcommand": "get",
                        "v_args": "nodes",
                        "kubeconfig": kubeconfig,
                    })
                    _node_str = _node_out if isinstance(_node_out, str) else str(_node_out)
                    if "NotReady" in _node_str:
                        logger.info(
                            "Self-destructive fault detected: Layer 1 failed due to "
                            "connectivity loss, target node is NotReady — skipping "
                            "Layer 1 to let Layer 2 verify the actual fault effect"
                        )
                        layer1_status = "skipped"
                        layer1_details = (
                            "blade_status unreachable (node connectivity lost), "
                            "but target node is NotReady — consistent with a "
                            "self-destructive fault (e.g. containerd/kubelet stop). "
                            "Layer 2 will verify the actual fault effect."
                        )
                except Exception as _sde:
                    logger.debug(f"Self-destructive fault check failed: {_sde}")

        return Layer1Result(
            status=layer1_status,
            details=layer1_details,
            raw_output=raw,
            resource_statuses=q_resource_statuses,
            affected_count=q_affected_count,
            expired=layer1_expired,
        )

    except Exception as e:
        logger.error(f"Layer 1 verification failed: {e}")

        # Fallback 1: try to find blade query results from kubectl exec in message history.
        # When the host blade binary is unavailable, the LLM may have already
        # verified injection via kubectl exec blade query k8s.
        if experiment_uid and messages:
            fallback = _find_blade_query_in_messages(messages, experiment_uid)
            if fallback:
                return Layer1Result(
                    status="passed",
                    details="blade_status unavailable (host blade error), "
                            "but blade query k8s from kubectl exec confirmed injection success",
                    raw_output=fallback,
                )

        # Fallback 2: experiment_uid exists but tools failed — allow Layer 2 to proceed.
        # This happens when blade_create failed but kubectl exec injection succeeded,
        # and the host blade binary also cannot run blade_status.
        if experiment_uid:
            return Layer1Result(
                status="skipped",
                details=f"blade_status/blade_query_k8s unavailable ({e}), "
                        f"but experiment_uid={experiment_uid} was extracted from injection output",
                raw_output=str(e),
            )

        return Layer1Result(status="error", details=str(e), raw_output=str(e))
