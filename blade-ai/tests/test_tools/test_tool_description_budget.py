"""Budget guard for LLM-visible tool schemas (anti-backflow).

Context: the kubectl docstring was once slimmed and its long-tail
examples moved to ``knowledge/kubectl-recipes.md`` ("examples that the
kubectl tool docstring no longer carries inline"). Without a guard, the
docstring grew back from 5179 → 5546 → 6318 chars. This test pins the
budget so any future growth must be a conscious decision, not drift.

Measurement caliber (calibrated 2026-07, reproduces the 12696-tok
baseline of the slimming plan):
  ``json.dumps(convert_to_openai_tool(tool), ensure_ascii=False)`` — the
  FULL schema the LLM sees (description + args). Counted with
  o200k_base, which tracks Qwen2.5 within ±1.4% on these schemas
  (K=qwen/o200k measured 0.979-1.037 across all 22 tools), so the MCP
  warn threshold (500 tok per tool) applies directly. ``transformers``
  + the Qwen2.5 tokenizer stays the arbiter when in doubt.

Rules when a cap trips:
  1. New content is a CLASS-A constraint distilled from a real incident
     (MUST/NOT/refused/auto-stripped) → raise that ONE tool's cap with a
     comment citing the incident, keep the total cap untouched.
  2. New content is a long-tail EXAMPLE / recipe → it belongs in
     ``knowledge/kubectl-recipes.md`` (or the matching knowledge doc),
     with a one-line pointer in the docstring. Do NOT raise the cap.
  3. Anything else → compress or drop; the schema budget is load-bearing
     for prompt-cache and context-window costs.
"""

import json

import pytest
import tiktoken
from langchain_core.utils.function_calling import convert_to_openai_tool

from chaos_agent.agent.nodes.planning.intent_clarification import (
    query_active_experiments,
    recover_task,
    submit_batch_intent,
    submit_fault_intent,
)
from chaos_agent.agent.nodes.verify._verifier_submit import (
    submit_recover_verification,
    submit_verification,
)
from chaos_agent.agent.replan import request_replan
from chaos_agent.agent.providers.chaosblade.cli import (
    blade_create,
    blade_destroy,
    blade_help,
    blade_query_k8s,
    blade_status,
)
from chaos_agent.agent.providers.chaosblade.cli_python import (
    blade_python_create,
    blade_python_prepare,
    blade_python_revoke,
)
from chaos_agent.agent.providers.faultdrill.assembler import (
    faultdrill_assemble_carrier,
)
from chaos_agent.tools.host_cmd import host_inject, host_read
from chaos_agent.tools.host_http_probe_cli import host_http_probe
from chaos_agent.tools.http_probe_cli import http_probe
from chaos_agent.tools.knowledge_reader import read_knowledge_resource
from chaos_agent.tools.kubectl_cli import kubectl, kubectl_read
from chaos_agent.tools.progress import update_progress
from chaos_agent.tools.wait import time_wait

_ENCODING = tiktoken.get_encoding("o200k_base")


def _schema_tokens(tool) -> int:
    """Tokens of the FULL tool schema as serialized to the LLM."""
    schema = json.dumps(convert_to_openai_tool(tool), ensure_ascii=False)
    return len(_ENCODING.encode(schema))


# Per-tool full-schema caps (tokens, o200k_base ≈ Qwen2.5). Target is the
# MCP warn threshold 500; the tools above it sit at their CLASS-A floor —
# their remaining content is incident-distilled constraints plus the only
# enumeration of valid values, none of which may be dropped (slimming-plan
# hard constraint: "A 类一条不删"). The count is deliberately NOT spelled
# out here (a hardcoded "six" went stale as rows were added); a cap above
# 500 must instead carry a per-row reason, and
# ``faultdrill_assemble_carrier`` is the one row whose overage is only
# PARTLY class-A — see its note before treating any over-500 cap as settled.
_CAPS: dict[str, tuple[object, int]] = {
    "kubectl": (kubectl, 935),                      # class-A floor (~593 qwen); +shell-quoting MUST (task-190c94e8); +one-shot debug 120s cap / systemd-run carrier (inject-59b289a6: 300s loop hosted in a one-shot debug pod was killed at the 120s cap, fragmenting the fault window + ~150s re-arm); +two-layer v_args semantics (inject-17617837: old flat "No shell features" line contradicted skill recipes' sh -c heredocs and cost 90s model hesitation; the selector-strip regex it advertised silently amputated `ls -l /proc/$(cat ...)` inside quoted payloads); +run admitted for the recovery-carrier shape only, flag whitelist (openspec recovery-carrier-standard: new refused-verb-class security contract; clean tree was already 772 over the old 766 cap, this raise covers both); +create admitted for the carrier RBAC stack, imperative-create routing (inject-cc2d5080: the subcommand list lacked `create` and the "Non-workload via apply" line steered the model into a manifest apply that the kind whitelist refuses — the carrier never stacked and the fault landed with no timer armed; measured 881, 890 keeps the same serialization-drift headroom rationale as kubectl_read); +run armed-before-inject cognition (run6 inject-3d5de7fa: the bare `sleep N` skeleton passed the old `active`-tolerant gate and the object-write landed permanently unrecovered — the `run` line now states the skeleton does NOT self-restore and the gate REFUSES until an `exec` arms the carrier `recovery_armed`, with a pointer to knowledge `recovery-carrier-arming.md` where the arming SOP/recipe lives (rule 2: recipe out, class-A constraint in); measured 926, 935 keeps the same serialization-drift headroom)
    "kubectl_read": (kubectl_read, 660),            # class-A floor (~552 qwen); +shell-quoting MUST (task-190c94e8); +exec single-command constraint (c157857 real-rejection-rework drift); +jsonpath whole-template quoting (inject-774ecd39: unquoted template word-split remotely, error invisible); +kubectl-layer selector rejection clarified (inject-17617837). Cap 616→630 is NOT content growth: the schema measures 616 on py3.14-system and 623 on .venv with identical source — the ±7 tok spread is json.dumps/convert_to_openai_tool serialization drift across library versions (within the o200k/Qwen ±1.4% caliber noted in the header). Zero-headroom at 616 made this pin flip red per-environment; 630 absorbs the drift while staying ~1000 tok below the 6318-char bloat era.
    # 630→660 (this pass): class-A transparency raise on the Side-effects line.
    # The old unqualified "(`debug`'s probe Pod auto-cleaned)" was over-applied
    # by the model to a `-- sleep 60` KEEP-ALIVE probe pod (inject-14fdcd15:
    # the trace shows it reasoning "由工具自动回收 / auto-cleaned per the tool
    # doc for one-shot probes" about a keep-alive pod, then spending a manual
    # `kubectl delete pod node-debugger-*` round on a residue that planning-exit
    # cleanup already owned). Only the one-shot `-- CMD` form is tool-deleted
    # (kubectl_cli.py L1778); a `-- sleep N` keep-alive pod is removed at
    # planning exit (cleanup_planning_debug_pods), NOT by the tool. The line now
    # names both forms and steers a single capability check to the residue-free
    # one-shot form. Rule-3 compression applied first (both drafts measured
    # +31/+32 over the 16-tok headroom — the content is irreducible: it must
    # name BOTH lifecycle forms + the steer). Measured 650; 660 keeps the same
    # ~+8-tok cross-environment serialization-drift headroom as the 623→630 pin.
    "blade_python_create": (blade_python_create, 605),  # class-A floor (~592)
    "blade_python_prepare": (blade_python_prepare, 500),
    "blade_python_revoke": (blade_python_revoke, 375),
    "submit_fault_intent": (submit_fault_intent, 775),  # class-A floor (~586) + dynamic INTENT_* enums; +case hint param (case_resource_path); +intent-accuracy provenance contract (probe trail / template-not-data); +one-line duration contract (duration_seconds channel); +names-vs-labels disambiguation (names = scope-kind instances, workload/owner targets → labels — confusion caused real wrong-target submissions)
    "submit_batch_intent": (submit_batch_intent, 395),  # +one-line duration contract (duration_seconds channel)
    "submit_verification": (submit_verification, 610),  # 495→610 (B2 submit-time consistency contract, 2026-09-21 review): the four downgrade rules _verification_from_submit_args enforces (verified+peo=false→partial; verified+layer2 failed→unverified; layer2 partial→overall partial; layer2 passed + failed items with absence evidence→layer2 partial, else warning) were previously invisible until the post-hoc mismatch warning — the model could only learn the contract AFTER an inconsistent submit was rewritten under it. Teaching them at the only call-site-adjacent surface is contract-mandated semantics, not long-tail bloat (mirror of submit_recover_verification's unverified-verdict-semantics raise). Wording compressed 127→100 tok before raising (rule 3 applied first); measured 595 + ~2% serialization-drift headroom.
    "submit_recover_verification": (submit_recover_verification, 455),  # +unverified verdict vocabulary (openspec unverified-verdict-semantics): overall 4-value domain + "unverified ≠ unrecovered" anti-conflation note + layer2 "unknown" — contract-mandated, not long-tail example bloat; +B76 round-14 root-cause fix: vocabulary lines now DERIVED from the verdict enums (full 6-word layer2 + 7-word item sets — class-A closed-set contract, cited incident: three contradictory hand-copied vocabularies found live)
    "blade_create": (blade_create, 610),            # class-A floor (~597)
    "blade_destroy": (blade_destroy, 365),
    "blade_status": (blade_status, 350),            # 325→350 (this pass): D1 contract-honesty raise — docstring now documents that empty uid runs `blade status --type create` (a bare `blade status` errors 45000 "less parameter: type|uid"), fixing the false "lists all experiments" promise observed live in inject-c01968c5 (host磁盘IO案); rule-3 compression applied first (351→338 measured). Same class-A contract-honesty family as the host_inject B2 raise.
    "blade_help": (blade_help, 305),
    "blade_query_k8s": (blade_query_k8s, 360),
    "request_replan": (request_replan, 560),        # class-A floor (~550)
    "host_read": (host_read, 510),
    # 495→670 (this pass): class-A honesty raise (rule 1), cited incident
    # CPU#2 code4002 false-negative — a host CPU fullload (``blade ... cpu
    # fullload --timeout 360``) physically injected (out-of-band SSH confirmed
    # the burn) yet the framework ruled ``fail``: the primary evidence (CPU%)
    # was never sampled INSIDE the fault window, so ``primary_evidence_observed
    # =false`` capped the verdict at ``partial`` (→ CLI fail). Root cause was a
    # CONTRACT LIE, not a guard/scope defect: host_inject is the SUPERSET of
    # host_read (a clean read-only diagnostic is admitted with ``skip_guard``,
    # see host_cmd.py L284-298), and it is the ONLY tool bound in the EXECUTE
    # phase — the one phase guaranteed to run while a self-recovering fault is
    # still live. But the old docstring/schema said fault-injection ONLY
    # ("non-fault binaries rejected"), so the model quoted that line verbatim
    # and never attempted an in-window ``top -bn1``. The k8s EXECUTE phase has
    # NO such gap because the isomorphic ``kubectl`` tool's contract honestly
    # declares it "covers every read" — same superset structure, honest
    # contract, model uses it to observe. The fix mirrors kubectl: the
    # docstring/schema now name BOTH command kinds and state the in-window
    # sampling duty. The +173 tok is irreducible class-A content (two-channel
    # enumeration + the ONLY-in-window-channel fact + the self-recovering
    # sample-HERE instruction); rule-3 compression applied first (697→657).
    # Measured 657; 670 keeps the same ~+13-tok cross-environment
    # serialization-drift headroom as the kubectl_read/assembler pins, and sits
    # well under the flagship kubectl cap (935).
    # 670→730 (this pass): carried by the probe-mode exit-semantics contract —
    # the natural completion of the two-channel raise above. host_inject is the
    # ONLY tool bound in EXECUTE/RECOVER_VERIFY and both injects AND observes; a
    # READ-ONLY absence/health probe SPEAKS IN EXIT CODES (first-principles #31:
    # "a non-zero exit is channel signal, not a semantic verdict" — the same
    # discriminator host_http_probe_cli / _kubectl_impl(probe_mode=True) /
    # baseline_capture already apply). The old docstring said "Error: on ...
    # non-zero exit" unconditionally, so the model framed a SUCCESSFUL ``cat
    # <deleted>`` ENOENT observation as a tool failure. The Output section now
    # states the read-only-non-zero-is-OBSERVATION contract (host_cmd.py L478
    # enforces it). The +60 tok is irreducible class-A content (the OBSERVATION
    # vs real-failure enumeration); rule-3 compression applied first (765→716).
    # Measured 716; 730 keeps the same ~+13-tok drift headroom as the line above.
    # 730→760 (openspec single-source-readonly-probe-presentation, Layer 1): a
    # class-A contract-HONESTY fix, same family as the two raises above. The old
    # channel-1 line enumerated "non-fault MUTATING binaries rm/curl/systemctl
    # rejected" — FALSE: readonly.py::_classify_argv admits by command SHAPE, not
    # by binary, so a read-verb HTTP GET via curl/wget (a dual-use binary on a
    # read verb) IS admitted. The prose mirrored a shape classifier as a static
    # per-binary blacklist; the two models differ structurally, so the list could
    # never faithfully track it (curl was only the first collision — wget/find/
    # awk/ss and any future classifier evolution re-drift). Cost (2026-10-27
    # python Case #2 inject-956b3105): the model read "curl rejected", reached
    # for a python3 -c urllib probe, was correctly REJECT_DRIFT'd by the fault-
    # family guard, then burned ~51.6s + 1 drift enumerating 6 alternatives
    # before falling back to the very curl the docstring had wrongly banned.
    # Channel-2 now states the binary-agnostic SHAPE rule and names the read-verb
    # network probe as admitted; channel-1 drops the false per-binary list. The
    # +30 tok is irreducible class-A content (the shape-vs-binary admission
    # contract + the one named probe the in-process python target depends on);
    # rule-3 compression applied first (system 746→733). Measured 746 (.venv, the
    # higher side); 760 keeps the same ~+14-tok drift headroom as the 716→730 row.
    "host_inject": (host_inject, 760),
    "read_knowledge_resource": (read_knowledge_resource, 440),
    "time_wait": (time_wait, 225),
    "query_active_experiments": (query_active_experiments, 220),
    "recover_task": (recover_task, 320),  # +task_id multi-format example (461398b); +intent-exclusion clause (2026-09-20 pass-1~4 cleanup review): the REMEMBER mirror carrying "For ANY other intent, do NOT call recover_task" was deleted with the recency mirror, and the node's recover_handler has NO program-side intent check (a recover_task ToolMessage confirms the intent outright — verified in intent_clarification.py), so the schema's When-to-use is the only remaining carrier at tool-selection time; measured 308, 320 keeps the ±2% serialization-drift headroom rationale (231→308, class-A refused-constraint per rule 1)
    "update_progress": (update_progress, 395),  # +reserved terminal-marker note (inject-ac45b369 #55 it12: model wrote phase=execution-complete via update_progress at finish; validator rejected the WHOLE call incl. log_append, one 15.1s retry round. Class-A refused-constraint taught at the only call-site-adjacent surface; measured 386 + drift headroom)
    # Backfill of a BLANK registration, not a class-A raise: the M1
    # assembler shipped without a _CAPS row, so this guard — whose whole
    # reason to exist is that unregistered growth is invisible growth —
    # never saw it. Measured 641 (.venv py3.11 / lc-core 1.3.0) and 630
    # (system py3.14 / lc-core 1.4.0); 650 pins the higher side + the same
    # ~1.4% cross-environment serialization-drift headroom the kubectl_read
    # cap documents. Over the 500 warn line, and the overage is NOT all
    # class-A: the three-state receipt semantics ARE (``partial`` = carrier
    # still ARMED, do NOT rebuild, converge via blade-ai recover — misreading
    # it leaves a live liability) and so is the NEVER-call-twice constraint,
    # but the Inputs enumeration and the recipe-flavoured duration/buffer
    # arithmetic are rule-2 externalisation candidates (knowledge/ pointer).
    # Slimming was considered and NOT taken (option D-2 declined in favour of
    # D-1 register-at-current-size); revisit if this cap is ever raised again.
    # 650→730 (#55 M3 retest inject-593c7826 live-fire): +carrier_image — the
    # planning-phase image decision channel. The LLM had §9-decided terway
    # (probe candidates ∩ evidence archive) while the standalone selector
    # picked an unpullable curlimages image and the tool contract had NO
    # parameter to carry the decision — every fallback heuristic stays a
    # guess because the one agent that saw the evidence cannot speak. The
    # three teaching elements (when to pass / must-be-inside-the-allowlist
    # fail-closed / omitted→tool-picks) are class-A safety semantics, not
    # examples; measured 717, 730 keeps the same drift headroom.
    # 730→940 (2026-09-21 NXDOMAIN inject-2340dac9 generic fix): the
    # remaining overage is three incident-distilled class-A contracts —
    # (1) null-value live-capture on restore replace ops (the plan relay
    # froze a 2-space Corefile indent over a 1-space live baseline; the
    # baseline guard caught it but the fail-closed retry cost 118s — the
    # null form removes the LLM from the byte-exact relay path entirely);
    # (2) readback_compare exact|whitespace_insensitive with fail-closed
    # validation and conservative default (the ACK controller normalised
    # the written value's indentation, exact-compare readback produced a
    # partial false-negative + a 60.4s manual-scouting round); (3) One
    # ARMING per task window replacing the NEVER-call-twice wording (the
    # model read pre-arming fail-closed retries as contract violations —
    # execute burned 431s across two assembler incidents). Rule-2/3
    # content (Corefile/Caddyfile examples, mechanism prose) was
    # compressed FIRST (991→919); measured 919, 940 keeps the same drift
    # headroom rationale as the 717→730 raise.
    # 940→1110 (2026-09-28 #51-R5 create-domain recovery gap): +extra_delete
    # — the OPTIONAL create-domain inverse channel. A json-patch restore can
    # only rewrite an existing object's fields; it cannot express "delete an
    # independently created prop Secret", so a fault whose inject plan built
    # a prop object outside the patch domain (the case frontmatter's
    # mechanism_writes) left that object with NO autonomous executor — the
    # armed timer's TTL and the recover replay both only patched the target
    # back and the prop outlived the fault (#51-R5 live-fire). The overage is
    # class-A safety semantics, not examples: the entry shape, the
    # fail-closed cross-namespace / cluster-scoped refusal (the namespaced
    # carrier Role cannot authorize outside its own namespace), and the
    # dual-executor idempotence (rides BOTH timer and replay, a 404 is a
    # clean no-op) are the contract the LLM must honour at the call site.
    # Rule-3 compression applied first (the drafted bullet measured 1123;
    # tightened to 1084 before raising). Measured 1084, 1110 kept the same
    # ~2.4% cross-environment serialization-drift headroom as the 919→940
    # raise above.
    # 1110→1280 (this pass): TWO class-A contract additions, both load-bearing,
    # neither a long-tail example. (1) e75958a7 grew the description to 1231
    # with the computed-cert-payload sentinel (``<faultdrill:expired-cert
    # cn=... part=crt|key>`` → in-carrier ``openssl ca -enddate`` signing,
    # fail-closed capability probe, byte-identity base64 backfill) but never
    # raised this cap — leaving the row red at HEAD. (2) the duration_seconds
    # bullet now states the dispatch-time D+G pin: the framework overwrites the
    # argument, so the value passed is non-authoritative — a transparency fact
    # that removes the recurring "WINDOW DURATION DEVIATION" self-audit the
    # model spun on every faultdrill run (#43-R4, #12, and inject-14fdcd15
    # msg[93]: ~9k reasoning chars agonising over an argument it does not
    # control). Measured 1265; 1280 keeps ~1.2% drift headroom.
    # 1280→1360 (this pass): class-A raise (rule 1) for the
    # ``<faultdrill:b64><plaintext>`` sentinel line — the Secret/ConfigMap
    # credential cousin of the cert sentinel. Cited incident inject-654ab665
    # (W-73R): the main path made the LLM hand-encode the tampered credential
    # to base64 (byte-by-byte derivation dominated the planning reasoning,
    # 2927 contiguous chars) and it wrote a WRONG constant into the plan gate
    # (d3JvbmctcGFzcy0xMjM= / wrong-pass-123 vs the intent's wrong-pass-999);
    # only the verifier's independent hand-decode caught it, so a less careful
    # verifier would have false-negatived a healthy drill. The tool now
    # base64-encodes the plaintext sentinel deterministically (pure Python),
    # so no base64 constant is LLM-hand-transcribed. Wording compressed
    # 7→4 lines before raising (rule 3 applied first); measured 1348, 1360
    # keeps ~1% serialization-drift headroom.
    "faultdrill_assemble_carrier": (faultdrill_assemble_carrier, 1360),
    # Backfill of a BLANK registration (same class as the M1 assembler above):
    # http_probe shipped without a _CAPS row, so this guard — whose whole reason
    # to exist is that unregistered growth is invisible growth — never saw it, and
    # the W-70-5 ``--resolve`` addition (+248 tok) landed unmonitored. Rule-3
    # compress-first applied BEFORE pinning (1067→958 measured .venv / 940
    # py3.14): dropped only redundant prose + the "valve does not bite"
    # reassurance; every class-A constraint preserved (semantics unchanged, so no
    # A/A re-validation needed — a static wording compression, not a guidance
    # change). The remaining over-500 overage is class-A, not long-tail:
    # (1) the adoption steer "Use this (NOT a hand-written kubectl_read exec
    # curl)" + the resolve PREFER clause — the W-70-5 root-cause fix's guidance
    # carrier, live-validated by 70-R7 (verifier adopted http_probe(resolve) 3/3,
    # the Entry_code false advisory zeroed); (2) the capture-mode enumeration =
    # the only list of valid values; (3) resolve port-MUST-equal-url-port
    # (exit_code=6 fail mode) + the host:port:IPv4 fail-closed shape; (4) the
    # exit_code table + "a faulted target is DATA, not a tool error" semantics.
    # 970 pins the .venv high side + ~12 tok cross-environment serialization-drift
    # headroom (same rationale as the kubectl_read 650→660 / assembler pins).
    "http_probe": (http_probe, 970),
    # host_http_probe — the HOST-delivered sibling of http_probe (registered at
    # birth, NOT backfilled: this guard's whole reason to exist is that
    # unregistered growth is invisible growth). Same class-A content as
    # http_probe, minus the origin_pod / namespace params (host delivery has no
    # client pod to name), so it measures smaller: (1) the adoption steer "Use
    # this (NOT a hand-written host_read / host_inject curl)" + the http_probe-
    # vs-this vantage split — the guidance carrier that routes a bare-host /
    # python-app L7 verdict onto the structured probe instead of an unparseable
    # hand-written curl; (2) the capture-mode enumeration = the only list of
    # valid values; (3) resolve port-MUST-equal-url-port (exit_code=6 fail mode)
    # + the host:port:IPv4 fail-closed shape; (4) the exit_code table + "a
    # faulted target is DATA, not a tool error" semantics. Measured 866 (.venv);
    # 880 keeps the same ~14-tok cross-environment serialization-drift headroom
    # as the http_probe 958→970 pin.
    "host_http_probe": (host_http_probe, 880),
}

# The two flagship tools carry the full five-section structure; long-tail
# examples live in knowledge/kubectl-recipes.md, not inline.
_STRUCTURED_TOOLS = (kubectl, kubectl_read)
_SECTIONS = (
    "When to use:",
    "Inputs:",
    "Output:",
    "Side effects:",
    "Constraints",
)

# Total budget across all tracked schemas (current ~9897). If this trips,
# the growth is aggregate drift — find the culprit via the per-tool caps.
# 9795 = 9750 + 45: single class-A raise for the kubectl one-shot debug
# 120s cap constraint (inject-59b289a6), approved by budget owner.
# 9867 = 9795 + 72: class-A raise for kubectl two-layer v_args semantics
# + kubectl_read selector-rejection clarification (inject-17617837),
# approved by budget owner.
# 9897 = 9867 + 30: class-A raise for update_progress reserved
# terminal-marker note (inject-ac45b369: #55 it12 phase=execution-complete
# write rejected, one 15.1s retry round), approved by budget owner.
# 9942 = 9897 + 45: class-A raise for the kubectl `run` armed-before-inject
# cognition (run6 inject-3d5de7fa: a bare-`sleep N` carrier passed the old
# `active`-tolerant gate and the object-write landed permanently
# unrecovered), approved by budget owner. Restores total headroom above the
# ±7-tok cross-environment serialization drift noted in the kubectl_read cap.
# 9942→10010 (2026-09-20 pass-1~4 cleanup review): the recover_task
# intent-exclusion clause is a user-ruled class-A addition (the deleted
# REMEMBER mirror was the only negative-form carrier and recover_handler
# confirms intent on the tool message with no program-side check), same
# raise-on-ruling path as a99774cc above.
# 10010→10670 (2026-09-20 assembler registration backfill): this is a
# COVERAGE change, not description growth — the M1 assembler's 641 tok have
# been riding every EXECUTE-phase request since it shipped, untracked. The
# tracked total was 10001 against a 10010 line, i.e. 9 tok of headroom, so
# the row could not be added without raising it. 10642 measured + ~28 tok
# for the cross-environment serialization drift documented on the
# kubectl_read cap (this tool alone measures 641 on .venv py3.11 vs 630 on
# system py3.14 — 11 tok, wider than kubectl_read's ±7). Approved by the
# budget owner as D-1 (register at current size); D-2 (slim the recipe-
# flavoured Inputs/duration prose into knowledge/ first, then register under
# 500) was declined, so the aggregate line absorbs the full 641.
# 10670→10740: the carrier_image class-A raise on
# faultdrill_assemble_carrier (650→730, #55 M3 retest inject-593c7826 —
# see the row note) carries the total with it; 10740 keeps the same
# drift headroom over the measured 10718.
# 10740→10940 (2026-09-21 NXDOMAIN inject-2340dac9): carried by the
# faultdrill_assemble_carrier class-A raise (730→940 — three
# incident-distilled contracts, see the row note; rule-2/3 content was
# compressed first, 991→919 measured). Measured total 10920, 10940
# keeps the same drift headroom as the line above.
# 10940→11090 (2026-09-21 B2 review pass): carried by the
# submit_verification contract raise (495→610 — submit-time
# consistency contract, rule-3 compression applied first, see the row
# note). Measured total 11070, 11090 keeps the same drift headroom.
# 11090→11240 (2026-09-28 #51-R5 create-domain recovery gap): carried by
# the faultdrill_assemble_carrier class-A raise (940→1110 — the
# extra_delete create-domain inverse channel, rule-3 compression applied
# first, see the row note). Measured total 11218, 11240 keeps the same
# ~20-tok drift headroom as the line above.
# 11240→11430 (Fix2 pass): carried by the faultdrill_assemble_carrier class-A
# raise (1110→1280 — the computed-cert-payload sentinel growth from e75958a7
# that was never capped, plus the duration_seconds D+G-pin transparency fact;
# see the row note). Measured total 11407, 11430 keeps the same ~20-tok drift
# headroom as the line above.
# 11430→11465: carried by the kubectl_read class-A raise (630→660
# — the Side-effects debug-pod lifecycle clarification, see the row note).
# Measured total 11443, 11465 keeps the same ~20-tok drift headroom.
# 11465→11548: carried by the faultdrill_assemble_carrier class-A
# raise (1280→1360 — the <faultdrill:b64> plaintext sentinel line, cited
# incident inject-654ab665 / W-73R base64 hand-computation; see the row note).
# Measured total 11526, 11548 keeps the same ~22-tok drift headroom.
# 11548→12397 (this pass): carried by BACKFILLING http_probe into _CAPS — it
# shipped unregistered, so the W-70-5 ``--resolve`` addition (+248 tok) landed
# invisible to this guard. Rule-3 compress-first applied BEFORE pinning
# (1067→958 measured .venv / 940 py3.14, cap 970); the over-500 remainder is
# class-A adoption steer (NOT-hand-written + PREFER-resolve), live-validated by
# 70-R7, not long-tail. Measured total 12375, 12397 keeps the same ~22-tok
# drift headroom.
# 12397→12570 (this pass): carried by the host_inject class-A honesty raise
# (495→670 — the two-channel inject+observe contract that fixes the CPU#2
# code4002 false-negative, see the row note; rule-3 compression applied first,
# 697→657 measured). Measured total 12548, 12570 keeps the same ~22-tok drift
# headroom as the line above.
# 12570→13437 (this pass): carried by registering the NEW host_http_probe tool
# (866 measured, cap 880) — the host-delivered sibling of http_probe, added at
# birth so its growth is monitored from day one (not backfilled like http_probe
# was). Measured total 13415, 13437 keeps the same ~22-tok drift headroom.
# 13437→13482 (this pass): carried by the blade_status D1 contract-honesty raise
# (325→350 — empty uid now documents `--type create` listing instead of the bare
# `blade status` that errors 45000 "less parameter: type|uid"; same class-A family
# as the host_inject B2 raise). Measured total 13460, 13482 keeps the same ~22-tok
# drift headroom as the line above.
# 13482→13540 (this pass): carried by the host_inject probe-mode exit-semantics
# raise (670→730 — a read-only absence/health probe's non-zero exit is OBSERVATION
# data, not an "Error:", per first-principles #31; see the _CAPS row note). Same
# class-A two-channel-contract family. Measured total 13518, 13540 keeps the same
# ~22-tok drift headroom as the line above.
# 13540→13568 (this pass): carried by the host_inject contract-HONESTY raise
# (730→760 — the docstring's false "curl rejected" per-binary enumeration
# replaced with the binary-agnostic SHAPE rule; see the _CAPS row note). Same
# class-A two-channel-contract family. Measured total 13546, 13568 keeps the same
# ~22-tok drift headroom as the line above.
_TOTAL_CAP = 13568


class TestToolDescriptionBudget:
    @pytest.mark.parametrize(
        "name", sorted(_CAPS), ids=lambda n: n
    )
    def test_schema_within_cap(self, name: str) -> None:
        tool, cap = _CAPS[name]
        count = _schema_tokens(tool)
        assert count <= cap, (
            f"{name} full schema grew to {count} tok (cap {cap}; MCP warn "
            "threshold 500). If this is a class-A incident constraint, "
            "raise THIS cap with a comment citing the incident. If it is a "
            "long-tail example, move it to knowledge/ (see "
            "kubectl-recipes.md) and leave a pointer. See "
            "tests/test_tools/test_tool_description_budget.py header."
        )

    @pytest.mark.parametrize(
        "tool", _STRUCTURED_TOOLS, ids=lambda t: t.name
    )
    def test_flagship_tools_keep_five_sections(self, tool) -> None:
        desc = tool.description
        missing = [s for s in _SECTIONS if s not in desc]
        assert not missing, (
            f"{tool.name} lost structural section(s) {missing}. The unified "
            "structure (When to use / Inputs / Output / Side effects / "
            "Constraints) is a hard constraint of the slimming plan — "
            "compress WITHIN sections, never drop the headers."
        )

    def test_total_budget(self) -> None:
        total = sum(_schema_tokens(t) for t, _ in _CAPS.values())
        assert total <= _TOTAL_CAP, (
            f"Total tool-schema budget is {total} tok (cap {_TOTAL_CAP}). "
            "See tests/test_tools/test_tool_description_budget.py header "
            "for the class-A / long-tail / drift triage rules."
        )
