---
title: "kubectl Recipes & JSONPath Catalogue"
topics:
  - kubectl recipes
  - kubectl exec
  - kubectl debug
  - jsonpath examples
  - field selectors
  - label selectors
fault_types:
  - all
summary: "Long-tail kubectl subcommand recipes (get / describe / top / logs / exec / debug / patch / scale / cordon / taint / delete) with field-selector, label-selector, and JSONPath examples that the kubectl tool docstring no longer carries inline."
---

# kubectl Recipes & JSONPath Catalogue

> **When to read this**: When the `kubectl` tool's compact docstring
> doesn't show the exact subcommand pattern you need — for example,
> filtering events by reason, extracting a specific field via JSONPath,
> or chaining a debug pod to inspect host paths. The tool docstring
> retains only the hard constraints (no shell pipes, exec/-l ban,
> debug/sleep, /host/ prefix).

## get

| Goal | Recipe |
| --- | --- |
| All pods in a namespace | `pods -n <ns>` |
| JSON for downstream parsing | `pods -n <ns> -o json` |
| Filter by label | `pods -n <ns> -l app=nginx` |
| Filter by phase | `pods -n <ns> --field-selector=status.phase=Pending` |
| Pods on a specific node | `pods --all-namespaces -o wide --field-selector=spec.nodeName=<node>` |
| Latest events for a namespace | `events -n <ns> --sort-by=.lastTimestamp` |
| Events of a specific resource | `events -n <ns> --field-selector involvedObject.name=<pod>` |
| Container statuses | `pod <pod> -n <ns> -o jsonpath='{.status.containerStatuses[*].state}'` |
| Restart count | `pod <pod> -n <ns> -o jsonpath='{.status.containerStatuses[*].restartCount}'` |
| Image of a container | `pod <pod> -n <ns> -o jsonpath='{.spec.containers[?(@.name=="<ctr>")].image}'` |
| Endpoints (service deps) | `endpoints -n <ns>` |

For "no resources matched the label selector", drop `-l`, list by name,
then inspect `.metadata.labels` to discover the actual label key (often
`app.kubernetes.io/name=<value>`).

## describe

| Goal | Recipe |
| --- | --- |
| Pod-level signals (events, conditions, restarts) | `pod <pod> -n <ns>` |
| Node conditions (DiskPressure, MemoryPressure, Ready) | `node <node>` |
| Service-level routing | `svc <name> -n <ns>` |

`describe` is your fallback when in-container utilities are missing — it
always returns event/condition data without `exec`.

## top

| Goal | Recipe |
| --- | --- |
| Sort pods by CPU | `pod -n <ns> --sort-by=cpu` |
| Sort pods by memory | `pod -n <ns> --sort-by=memory` |
| Single node usage | `node <node>` |

Requires metrics-server. Sampling interval: binary default 60s, official Helm chart overrides to **15s** (most production clusters use 15s); configurable via `--metric-resolution`; kubelet computes every 15s. — combine with multiple iterations of verification.

## logs

| Goal | Recipe |
| --- | --- |
| Tail recent lines | `<pod> -n <ns> --tail=50` |
| Previous container instance (after crash) | `<pod> -n <ns> --previous --tail=50` |
| Specific container in multi-container pod | `<pod> -n <ns> -c <container>` |
| Follow live | not advised under non-interactive runner — use `--tail` repeatedly instead |

## exec

`kubectl exec` does NOT support `-l/--selector` — first run
`kubectl get` to resolve a concrete pod name, then exec on that name.
The tool auto-strips the flag with a warning if you forget.

| Goal | Recipe |
| --- | --- |
| Inspect filesystem usage | `<pod> -n <ns> -- df -h` |
| Run blade inside the tool pod | `<pod> -n chaosblade -- blade create k8s pod-cpu fullload --cpu-percent 80` (timeout auto-injected) |
| Probe TCP connectivity | `<pod> -n <ns> -- nc -vz <host> <port>` |
| Process list | `<pod> -n <ns> -- ps -ef` |

If utilities are missing, switch to `describe` / `get -o json`.

## debug

```
kubectl debug node/<node> --image=busybox -- sleep 3600
```

Mandatory `-- sleep 3600` (or another keep-alive); bare invocations
exit immediately. Never pass `-it` (the runner is non-interactive).

Inside the debug pod, host paths live under `/host/...` — e.g. inspect
host disk usage with `chroot /host df -h /var/lib/kubelet`.

When done, clean up:
`kubectl delete pod <debug-pod-name> -n <ns>`. The debug pod name
typically starts with the target node/pod name and ends with `-debug`.

## patch / scale / cordon / taint / delete

| Goal | Recipe |
| --- | --- |
| Add a label | `pod <pod> -n <ns> --type=json -p '[{"op":"add","path":"/metadata/labels/x","value":"y"}]'` |
| Force-delete pod | `pod <pod> -n <ns> --force --grace-period=0` |
| Scale to zero | `deployment <name> -n <ns> --replicas=0` |
| Scale back | `deployment <name> -n <ns> --replicas=<original>` |
| Cordon a node | `kubectl cordon <node>` |
| Uncordon | `kubectl uncordon <node>` |
| Add taint | `nodes <node> key=value:NoSchedule` |
| Remove taint | `nodes <node> key-` |

`apply`, `create`, `replace`, `edit`, `expose`, `run`, `autoscale`, and
`rollout` are blocked by ToolGuard — they create or mutate workloads
outside the chaos scope.

## JSONPath Quick Reference

| Goal | Expression |
| --- | --- |
| Pod IP | `{.status.podIP}` |
| Node name | `{.spec.nodeName}` |
| All container names | `{.spec.containers[*].name}` |
| Restart count for a named container | `{.status.containerStatuses[?(@.name=="<ctr>")].restartCount}` |
| Pod conditions | `{.status.conditions[*]}` |
| Replica count | `{.spec.replicas}` |
| Map output (labels) | `{.metadata.labels}` |
