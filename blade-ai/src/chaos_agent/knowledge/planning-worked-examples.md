---
title: "Planning Worked Examples (canonical traces)"
topics:
  - target grounding
  - handling empty query results
  - unverifiable method preconditions
  - when to finish_planning vs keep probing
fault_types:
  - all
summary: "Two canonical Phase 1 traces showing the expected reasoning at the highest-failure moments: (1) a target identifier that returns empty — discover the correct one instead of rejecting or looping; (2) a method precondition that cannot be checked read-only — proceed to finish_planning and let Phase 2 verify, do not loop."
---

# Planning Worked Examples (canonical traces)

> **When to read this**: Load this when Phase 1 planning stalls at target
> grounding — either a query for the target came back empty, or you cannot
> confirm a method's precondition with read-only tools and are unsure whether
> to keep probing, reject, or finish. These are the two moments where planning
> most often loops or gives up prematurely. Each example shows the expected
> behaviour end-to-end, not a rule to memorise.

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

## Example 2 — A precondition you cannot verify read-only

**Situation**: The chosen injection method needs a host binary (`tc`) or a
kernel capability that Phase 1's read-only tools cannot observe.

**Expected trace**:

1. Confirm what you *can* verify read-only: the target exists and a documented
   injection method matches the intent.
2. Recognise the precondition (host binary / kernel capability present) is a
   *runtime* fact that only Phase 2 can establish — no read-only query answers
   it here.
3. Do **not** loop trying to prove it from Phase 1, and do **not** reject the
   request as infeasible on that basis. Unverifiable ≠ infeasible.
4. `finish_planning` with the verified target and the documented method; note
   the precondition as an assumption for Phase 2 to confirm.

**Why this is right**: Target existence is the only grounding gate Phase 1
owns. Method runtime preconditions belong to Phase 2, which will discover the
tool's real interface and adapt or replan if the precondition fails. Blocking
Phase 1 on evidence it structurally cannot obtain is the classic planning loop.
