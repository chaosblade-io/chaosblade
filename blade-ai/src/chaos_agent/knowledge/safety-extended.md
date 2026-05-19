---
title: "Safety Extended: Advisory, Blast Radius, Decision Framework"
topics:
  - blast radius
  - abort or continue
  - escalate
  - advisory rules
  - decision framework
fault_types:
  - all
summary: "Advisory good-practice rules, the Blast Radius Assessment Framework (scope / dependencies / cross-namespace / data risk), and the Abort / Continue / Escalate decision framework. Sourced on demand when the cache-tight inject prompt's hard-only safety section is insufficient."
---

# Safety Extended: Advisory, Blast Radius, Decision Framework

> **When to read this**: The cache-tight inject prompt only carries
> Hard Rules + Caution Rule Compliance. Read this doc when planning a
> multi-target or node-scope injection, when you are unsure whether to
> abort or continue after a failure, or when the user pushes for a
> larger blast radius than they originally specified.

## Advisory Rules (Good Practice)

- Start with the **smallest effective scope** (1 pod before all pods) — rationale in chaos-engineering-principles Q9.3.
- Verify side effects after each destructive action.
- If unsure about safety, mark as warning and request confirmation.
- Prefer test/dev namespaces over production — rationale in chaos-engineering-principles Q9.1/Q9.3.
- For network faults (`pod-network loss`/`delay`), prefer port-specific
  parameters (`--local-port`, `--remote-port`, `--destination-ip`) to
  minimize blast radius. Only use full-interface injection
  (`--percent 100` without port filter) when the intent is to test
  complete network partition.
- **Timeout values** should balance observability and safety:
  - Too short (< 30s) and the fault may not become observable before
    auto-recovery.
  - Too long (> 600s) increases residual damage risk.
  - Consider: `metrics-server` sampling interval (binary default 60s, official Helm chart overrides to 15s — most production clusters use 15s; configurable via `--metric-resolution`; kubelet computes metrics every 15s), time needed
    for Layer 2 verification, and blast radius — **larger scope =
    shorter timeout**.

## Blast Radius Assessment Framework

Before multi-target or node-scope injection, assess impact across these
four dimensions:

1. **Scope**: How many pods/nodes are affected?
   `Single pod < single deployment < entire node`.
2. **Dependencies**: What services depend on the target? Check with
   `kubectl get endpoints` and `kubectl get svc`.
3. **Cross-namespace**: Node-scope faults affect ALL namespaces on that
   node. Use `kubectl get pods --all-namespaces -o wide` to assess
   adjacent impact.
4. **Data risk**: Could the fault cause data corruption or loss?
   (e.g. disk fill on database pods, network partition during writes.)

If blast radius **exceeds the user's stated scope**, report as WARNING
before proceeding — even if the technical request is valid.

## Decision Framework: Abort / Continue / Escalate

| Decision | When |
| --- | --- |
| **ABORT** (stop immediately) | Safety violation detected, cascading impact observed, target resource does not exist, user explicitly requests stop. |
| **CONTINUE** (proceed with caution) | Transient error that may resolve on retry, fault effect delay not yet elapsed, partial success where successful experiments can still be recovered. |
| **ESCALATE** (ask user) | Cannot resolve with available tools, unexpected error pattern that doesn't match known failure modes, potential data loss risk, all injection methods exhausted without success — output `[REPLAN]` to route back to Phase 1. |

The default tie-breaker is **ABORT** when scope is ambiguous and **ESCALATE**
when intent is ambiguous. CONTINUE is only correct when both scope and
intent are clear and the failure mode is recognized.
