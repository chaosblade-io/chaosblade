---
title: "Verification Heuristics & Method Selection"
topics:
  - verification strategy
  - fault effect delay
  - multi-iteration verification
  - verification method priority
  - verification method selection by fault type
  - evidence sufficiency
  - handling ambiguous results
  - minimal container handling
fault_types:
  - all
summary: "Full-detail catalogue for verifying fault injection effects: fault-effect delay window (5-30s), multi-iteration verification pattern, method priority order, fault-type → method mapping (CPU/Memory/Network/Pod/Disk/Node), evidence sufficiency rules, ambiguous-result handling, and minimal-container fallback."
---

# Verification Heuristics & Method Selection

> **When to read this**: The inject Phase 1 prompt only carries a 5-line
> *Verification Strategy (Principles)*. The verifier prompt has the full
> details inline. Read this doc when you are in inject/execute and need
> the long-form heuristics — for example, when the user's fault type is
> not covered by a skill case and you need to design a verification step
> from scratch, or when iteration 1 returned an ambiguous result and you
> need to decide between waiting, switching method, or escalating.

## Fault Effect Delay

Fault injection is NOT instantaneous. After `blade create` reports `Success`:

- The actual fault effect may take **5-30 seconds** to become observable.
- The ChaosBlade daemon pod must receive the instruction and start the
  stress process inside the target container.
- Kubernetes `metrics-server` has its own sampling interval (binary
  default 60s, but the official Helm chart overrides to **15s** — most
  production clusters use 15s; configurable via `--metric-resolution`;
  kubelet computes metrics every 15s), so `kubectl top` lags reality
  by up to one window.

If you check immediately and see no signal, that is **not** evidence of
absence — it is evidence the window has not elapsed. Wait and re-check.

## Multi-Iteration Verification Pattern

1. **Iteration 1**: Run initial checks (`kubectl top`, `kubectl describe`).
2. **Iteration 2**: If iteration 1 showed no effect, re-check the same
   key indicators after the delay window has passed.
3. **Iteration 3+**: Consolidate findings. Only conclude "not in effect"
   after **2+** consistent negative checks across the delay window.

A single negative check is never enough.

## Verification Method Priority

1. Skill-provided injection verification instructions (highest confidence).
2. Fault-specific patterns from domain knowledge (e.g. CPU stress →
   `kubectl top`).
3. General health checks (`kubectl describe`, events, conditions).

Walk this list in order — only fall through when the higher tier does
not apply.

## Verification Method Selection by Fault Type

Beyond the priority order, choose your verification method based on the
fault type:

| Fault type | Primary method | Secondary method |
| --- | --- | --- |
| CPU / Memory stress | `kubectl top` (quantitative metrics) | `kubectl describe` (conditions) |
| Network delay / loss | `kubectl exec` connectivity test (application impact) | `kubectl describe` (events) |
| Pod kill / crash | `kubectl get pods` (restart count) | `kubectl describe` (events / OOMKilled) |
| Disk fill | `kubectl exec df -h` (filesystem) | `kubectl describe node` (DiskPressure condition) |
| Node-level faults | `kubectl describe node` (conditions) | cross-namespace pod status check |

If the active skill provides specific verification instructions, they
**override** these general patterns.

## Minimal Container Handling

Some container images lack common utilities (`top`, `ps`, `netstat`,
`df`, `curl`, etc.):

- If `kubectl exec` returns `command not found`, do **NOT** retry similar
  commands — the entire utility family is likely missing.
- Switch to `kubectl describe` for Pod-level signals (restart count,
  conditions, events).
- Use `kubectl get -o json` for structured data when `exec` is unavailable.
- For a node-level perspective when no in-container tool fits, fall back
  to `kubectl debug node/<node> --image=busybox -- sleep 3600` and run
  the check from the debug pod (host paths under `/host/...`).

## Evidence Sufficiency

Sufficient evidence requires:

1. **At least 2 independent data points** confirming the same conclusion.
2. **Data from different verification layers** (e.g. metrics + events,
   not just two metrics calls).
3. **Timing accounted for** — if all evidence is from a single point in
   time, wait and re-check.

A single positive data point is a hint, **not** a conclusion.

## Handling Ambiguous Results

When tool output contradicts expectations:

1. **Consider timing** — metrics may not reflect the fault yet (wait
   at least one metrics-server window — typically 15s in most clusters
   — and re-check).
2. **Cross-validate with a different command** — if `kubectl top` shows
   no change, check `kubectl describe` for condition changes.
3. **Never infer from absence** — "no signal" is not "no fault" until
   timing is accounted for.

If after two cross-validations the picture is still ambiguous, conclude
as `unverified` rather than `failed` and escalate to the user.

## See Also

- **`failure-modes.md`** — When verification has confirmed something
  *did* go wrong (partial injection, blade create error, cascading
  impact, recovery failure), use the decision trees there to choose
  between retry / abort / escalate. This doc gives you the *method* to
  detect; failure-modes gives you the *response* once detected.
