---
title: "Planning Worked Examples (canonical traces)"
topics:
  - target grounding
  - handling empty query results
  - method viability probing
  - when to finish_planning vs keep probing
fault_types:
  - all
summary: "Three canonical Phase 1 traces showing the expected reasoning at the highest-failure moments: (1) a target identifier that returns empty — discover the correct one instead of rejecting or looping; (2) method preconditions that CAN be probed read-only — probe each documented path and commit to the one proven viable; (3) a precondition no read-only tool can answer — record it as an assumption and proceed, do not loop."
---

# Planning Worked Examples (canonical traces)

> **When to read this**: Load this when Phase 1 planning stalls — either a
> query for the target came back empty, you are deciding which documented
> injection path to commit to, or you cannot answer a method precondition and
> are unsure whether to keep probing, reject, or finish. Each example shows
> the expected behaviour end-to-end, not a rule to memorise.

## Example 1 — The target identifier is wrong, not absent

**Situation**: FAULT INTENT names pod `payment-api` in namespace `prod`. The
first read-only query returns empty.

**Expected trace**:

1. Query by the stated identifier — it returns empty. This is evidence the
   *identifier* is wrong, not proof the target is *absent*.
2. Broaden: list the pods actually present in `prod` and read their metadata
   (names, labels, owner references).
3. Discover the real workload is `payment-api-7c9f8` (a ReplicaSet-managed
   pod) selectable by label `app=payment-api`.
4. Ground the target on that verified evidence — prefer the label selector,
   since the pod name is ephemeral.
5. `finish_planning` with the verified target.

**Why this is right**: An empty result from a specific query narrows the search
— it does not end it. You broaden once to get *some* result, then narrow from
what actually exists. Rejecting here (target absent) or re-running the identical
failing query (looping) would both be wrong. Reject only after a broadened
search confirms nothing matching the intent exists.

## Example 2 — Probing which documented path is viable

**Situation**: The chosen case documents two injection paths: path A needs a
binary inside the target container (e.g. `tc`); path B injects through an
ephemeral debug probe on the node. The plan must commit to ONE path, and Phase 1
can find out which one actually works — read-only.

**Expected trace**:

1. Probe path A's precondition read-only: exec into the target and check the
   binary exists and is the real tool (a same-named busybox applet can pass a
   bare existence check yet lack the needed subcommands — verify capability,
   not just presence).
2. If path A checks out, commit to it; note the probed evidence in the plan
   ("path A viable: iproute2 tc confirmed in container").
3. If path A fails (binary absent / wrong variant), probe path B's
   preconditions the same way, then commit to path B with its evidence.
4. `finish_planning` with the chosen path and the probed facts — Phase 2 then
   executes informed instead of discovering the missing binary by failure.

**Why this is right**: Read-only probing is cheap and its evidence is the core
value Phase 1 adds — every fact verified here saves Phase 2 a failed attempt
and a possible replan round. Evidence disproving a path is equally valuable:
it redirects the plan to a documented alternative while the cost is still zero.
Rejecting without probing, or skipping the probe and letting Phase 2 find out,
both waste information Phase 1 can obtain for free.

## Example 3 — A precondition no read-only tool can answer

**Situation**: Everything provable has been probed, but one precondition is
structurally unobservable read-only — e.g. whether the fault EFFECT will
actually propagate through this kernel/runtime combination.

**Expected trace**:

1. Recognise the question cannot be answered by any read-only query — the
   effect exists only once the fault is injected.
2. Record it explicitly as an assumption for Phase 2 ("assumption: the netem
   qdisc takes effect on this kernel; Phase 2 verification will confirm").
3. Do **not** loop looking for evidence that does not exist yet, and do **not**
   reject the request on that basis. Unanswered ≠ infeasible.
4. `finish_planning` with the verified target, the chosen path, and the
   recorded assumption.

**Why this is right**: Phase 1 verifies everything read-only probing can
answer; what remains is genuinely Phase 2's empirical question, and the system
owns verification and replan there. Blocking planning on evidence that
structurally cannot exist yet is the classic planning loop.
