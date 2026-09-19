---
title: "Outcome-to-Means Methodology"
topics:
  - intent clarification
  - effect-to-means translation
  - means comparison
fault_types:
  - all
summary: "Heuristic question chain for expanding outcome-stated intents into candidate means: what must break, which fault families could break it, how candidates differ on four axes, what is already broken on the target."
phases:
  - plan
---

# Outcome → Means Translation

## The gap

Users state OUTCOMES ("make the service down", "make it slow", "fill the
disk"). Fault tooling everywhere — this catalogue, Gremlin, Chaos Mesh —
organizes by MEANS. No tool on the market offers an effect-to-means lookup
table, because the effect space is as open as human language: the
translation is exactly the hypothesis-driven reasoning an engineer does
by hand. Your job is that reasoning, and this document is the discipline
behind it, not the answer to it.

## How to think

When the user's words name an outcome rather than a fault form, walk this
chain of questions:

1. **What does this outcome actually require to be broken?**
   "Service down" means some dependency in the serving path is severed —
   but which ones would produce it, and which would not? The answer lives
   in what the target actually is (replicas, probes, restartPolicy), not
   in the word "down".

2. **Which fault families could break it?**
   The industry organizes means by what they attack — process state,
   resources (CPU / memory / disk / IO), network paths, scheduling,
   configuration, data. These families are thinking dimensions, not a
   closed list: combine them, split them, and go beyond them when your
   domain knowledge says so. A family you skip is a candidate the user
   never sees.

3. **How do the candidates actually differ?**
   Compare on four axes: time-to-effect (seconds vs. minutes),
   certainty through the experiment window (does it hold, or does a
   controller heal it?), observable signature (what the user will see
   differ), and recovery path (how the fault gets reverted). A
   catalogue case IS a means: its content defines that means'
   mechanism, symptoms and recovery. Compare these axes from the
   candidate cases' own content — read them (`read_skill_resource`)
   rather than answering from generic K8s lore, which knows nothing
   of what the drill system's cases guarantee. Rank with the user's
   stated preference; when they stated none, rank by how likely each
   is to occur in the real world — a drill rehearses what production
   actually hits — refined by certainty, and say so.

4. **What is already broken on this target?**
   Faults multiply, not add. Live experiments on overlapping targets
   change what each candidate produces: a deletion loop over an
   already-corrupted image never comes back; network loss hides inside
   failing readiness probes and reads as "Running but unreachable".
   `query_active_experiments` is the cheap way to see them before
   recommending.

Then present: top-ranked means with a one-line rationale, the other
realizing means with their observable differences, and let the user
redirect in one word. Map means to concrete cases against the Skill
Index — catalogue directory names are means-named, so the match is
yours to make; this document deliberately names no directory.

## Canonical examples

**"把 eb 的限流组件挂掉" (down).** Q1: serving requires the process
alive, a working image, a scheduled pod, reachable network, enough
replicas. Q2: process-family (kill once / crash-loop), scheduling-family
(delete pod / scale to 0), image-family (corrupt tag — note: needs a
restart to bite), network-family (100% loss — note: pod stays Running
while unreachable), resource-family (OOM). Q3: the candidate cases are
the means — read them before ranking this axis: kill-once heals in
seconds per restartPolicy, deletion in ~30s via the controller but
changes pod identity; what corrupt tag or scale-to-0 does on expiry
comes from their cases, never from cluster lore.
Q4: query_active_experiments first — an image-corruption already running
would stack with deletion into an outage neither ranking assumed.
Present five means with signatures; the user picks the flavor of "down"
they meant.

**"接口变慢" (slow).** Q1: latency rises when requests queue behind a
slow hop — which hop? Q2: network-family (fixed delay / bandwidth cap),
resource-family (CPU throttle / disk IO pressure), and beyond the
families the catalogue offers (an application-level sleep is a means
too, if a case provides it). Q3: fixed delay shifts p99 by a known
constant — the most predictable; IO pressure is workload-dependent —
the most realistic. Q4: a CPU-load experiment already running on the
target would stack with a new CPU throttle into something neither
ranking assumed. Present the surviving means with signatures; the
user picks which hop to blame.

---

The families are where thinking starts, not where it ends.
