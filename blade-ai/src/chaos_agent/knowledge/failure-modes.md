---
title: "Failure Modes & Recovery Guidance"
topics:
  - partial injection
  - blade create failure
  - verification failure
  - cascading impact
  - recovery failure
fault_types:
  - all
summary: "Decision trees for the five common failure modes during fault injection: partial injection (some targets succeed, others fail), blade_create CLI errors, verification cannot confirm fault effect, cascading impact beyond intended scope, and recovery / blade_destroy failures."
---

# Failure Modes & Recovery Guidance

> **When to read this**: When Phase 2 execution or verification hits an
> unexpected outcome and you need a structured response — should you
> retry, abort, escalate, or branch into recovery? This doc replaces
> the in-prompt failure-modes block to keep the system prompt slim.

## 1. Partial Injection Failure

If some targets succeed and others fail (e.g. 2/5 pods injected,
3 failed):

1. **Do NOT retry failed targets automatically** — the failure may
   indicate a systematic issue.
2. Capture `blade_uid` for ALL successful injections — they need recovery
   regardless of next step.
3. Decision path:
   - **Parameter-specific failure** (wrong flag): correct parameters
     and retry **once**.
   - **Target-specific failure** (pod evicted, node unreachable):
     report and ask the user.
   - **Systematic failure** (all targets fail): abort and report root
     cause analysis.
4. Ask the user whether to: (a) retry failed targets, (b) destroy
   successful ones and abort, (c) proceed with partial injection.

## 2. Blade Create Failure

If `blade_create` returns an error:

1. Read the error message carefully — most errors are parameter
   mismatches.
2. Do **NOT** retry with the same parameters.
3. Check if the target still exists (the pod may have been evicted).
4. If the error mentions `resource not found`, verify the target with
   `kubectl get` first.
5. If the error mentions `unknown flag` or version incompatibility,
   consult `chaosblade-cli.md` for alternative method options
   (kubectl exec into tool pod, kubectl-native operations).

## 3. Verification Failure

If Layer 2 verification cannot confirm the fault effect:

1. Consider the delay window (5-30 seconds for fault propagation —
   see `verification-heuristics.md`).
2. Switch verification method if one approach shows no signal (e.g. if
   `kubectl exec` fails, use `kubectl describe`).
3. After **3+** verification attempts with consistent negative result,
   conclude as `unverified` rather than `failed`.
4. **NEVER** retry injection as a workaround for verification failure.

## 4. Cascading Impact

If the fault appears to affect resources beyond the intended target:

1. Immediately destroy the experiment (`blade_destroy`).
2. Report the observed cascading impact to the user.
3. Suggest a narrower scope for retry (fewer targets, smaller percentage,
   port-specific filters for network faults).

## 5. Recovery Failure

If `blade_destroy` fails or the target doesn't recover:

1. Check if the ChaosBlade daemon pod is healthy:
   `kubectl get pods -n chaosblade`.
2. Try manual cleanup: `kubectl exec` into the target to remove stress
   processes (`pkill chaos`, `pkill stress-ng`, `rm -f /tmp/chaos_*`).
3. For node-disk fill that left files behind, use the same exec/debug
   approach to delete the fill files at the path you injected against.
4. Report to the user with specific diagnostic information — do **NOT**
   silently proceed.

A `blade destroy` that returns "Success" while the stress process
lingers is a known rare case — always re-verify with `blade_status` and
direct observation (e.g. CPU usage normalized).

## See Also

- **`verification-heuristics.md`** — Before declaring a verification
  failure (mode 3), use the heuristics there to confirm the fault-effect
  delay window has elapsed and you've cross-validated across at least
  two methods. Most "verification failure" calls are actually premature
  checks against the delay window.
