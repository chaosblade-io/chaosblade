---
title: "Recovery-Carrier Arming: bare skeleton vs armed timer (Phase-2 fallback)"
topics:
  - recovery carrier
  - arming
  - rollback timer
  - exec
  - kubectl-native fault
  - channel degradation
fault_types:
  - all
summary: "Quick-reference for ARMING a recovery carrier when a kubectl-native (object-write) fault must recover via a carrier timer. The `run` step builds only a bare `sleep N` skeleton (the timer host) — it restores NOTHING. The arming `exec` (an SA-token curl PATCH/DELETE fired after the window) is what actually reverses the fault and stamps the carrier `recovery_armed`; the armed-before-inject gate refuses the injection until that stamp exists. Distilled from the authoritative references/carrier/recovery-carrier.md §3-4; read that for the full length-budget / fail-open / self-deauth rules."
phases:
  - execute
  - recover
---

# Recovery-Carrier Arming (Phase-2 fallback quick-reference)

## When this applies

A **kubectl-native object-write** fault (`kubectl patch/scale/label/taint/...` —
the verb itself is the mutation) carries **no experiment UID and no
self-timeout**. Its ONLY bounded recovery is a **recovery-carrier timer**: a
pod that, after the fault window, fires an SA-token REST call to reverse the
mutation. This is the fallback when the preferred CR / manifest channel is
unavailable or degraded (e.g. the CRD is not installed, or a route gate
rejected the apply).

## The two-step cognition — DO NOT STOP AT STEP 1

1. **`kubectl run` builds the SKELETON (timer host only).** A bare
   `drill-rc-<hash>` pod on `--restart=Never --command -- sleep N` is just a
   host with a shell and an SA token. **It restores nothing.** A carrier
   registered on this bare skeleton is `active`, NOT armed.
2. **`kubectl exec` ARMS it.** An exec into the carrier whose payload is a
   backgrounded timer — `( sleep <window>; <restore curl> ) >/tmp/restore.log
   2>&1 & echo armed` — is what actually schedules the rollback AND stamps the
   carrier `recovery_armed`.

> The **armed-before-inject gate** refuses the injection while the carrier is
> only registered/`active`. It demands `recovery_armed`, i.e. the arming exec
> must have landed. An unarmed carrier means the fault is permanently
> unrecovered.

## Step 3 first — SA token pre-auth (BEFORE arming)

Never arm on an unverified SA. `kubectl auth can-i --as=...` is FORBIDDEN
(impersonation reflects the CALLER's view — it has produced false allows).
Use the carrier SA's **real token** for a read-only GET, judged by HTTP code:

```bash
kubectl exec drill-rc-<hash> -n <namespace> -- sh -c \
  'TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); \
   curl -s -o /dev/null -w "%{http_code}" \
   --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
   -H "Authorization: Bearer $TOKEN" \
   https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<name>'
```

- `200` → read path OK; then reconcile the **write verbs** the restore payload
  will use (`patch`/`delete`/...) via a SelfSubjectAccessReview each — GET 200
  only proves the read path. **All write verbs allowed → arm; any false →
  ABORT** (fix the Role, never arm into a timer that will 403 silently at fire
  time).
- `403` → missing permission, ABORT.
- other (timeout/000) → channel unreachable, see degradation path in the
  authoritative doc.

## Step 4 — the arming exec (timer template)

Full form (≤2 restore curls, total < 1024B inline budget):

```bash
kubectl exec drill-rc-<hash> -n <namespace> -- sh -c \
  '( sleep <window>; \
     curl -s -X PATCH \
       --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
       -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
       -H "Content-Type: application/merge-patch+json" \
       -d "{\"spec\":{\"replicas\":<baseline>}}" \
       https://kubernetes.default.svc/apis/apps/v1/namespaces/<namespace>/deployments/<name>/scale \
  ) >/tmp/restore.log 2>&1 & echo armed'
```

For ≥3 restore curls or a tight byte budget, use the **compact-variable for-loop
form** (assign `C`/`T`/`U` inside the payload, write curl's common args once) —
see recovery-carrier.md §4. Both forms are approved; do not invent a third.

## Arming discipline (the load-bearing rules)

- **Wrap in `sh -c`.** A top-level bare `( sleep … ) &` is not interpreted by
  the exec-form channel and gets refused by the command guard
  (`unknown_binary`).
- **Countdown starts at ARM time**, so arming must be **immediately before the
  fault lands** (≤60s gap). Carrier build + token pre-auth are setup and may be
  front-loaded; **defer the arming exec to just before the first mutation**.
- **Keep `>/tmp/restore.log 2>&1`, never `>/dev/null`.** Recovery failure must
  leave forensic evidence — after fire, `kubectl exec <carrier> -- cat
  /tmp/restore.log` shows each restore curl's outcome. A near-empty log does
  NOT mean the timer never ran (`-f` swallows the body); replay out-of-band
  without `-f` to get the error.
- **Idempotent restore.** The timer may fire late and overlap an out-of-band
  `blade-ai recover`; the restore curl must be side-effect-free on repeat.
- **Re-arm on any fix.** If the restore script/params change after arming,
  first kill the old timer (`pkill -f "sleep <N with one bracketed digit>"`)
  then re-arm in full — editing the script does NOT reset the countdown.

## Authoritative source

This is a distilled quick-reference. The single source of truth for the full
SOP — byte-budget forms, fail-open self-deauth tail step, forensic wrapping,
six-object cluster-scoped stacks, teardown — is
`references/carrier/recovery-carrier.md` §3 (SA pre-auth) and §4 (arming
template).
