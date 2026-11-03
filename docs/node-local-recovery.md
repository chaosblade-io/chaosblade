# Node-local recovery (experimental, protocol v1)

This opt-in mode lets an administrator stop a registered execution when the
Kubernetes API server is unavailable. It reuses the existing privileged
`chaosblade-tool` DaemonSet. It does not require another DaemonSet, a central
service, an external database, or KubeWiz.

The first version supports **node and pod network delay/loss on Linux**, with a
single root netem qdisc on an initially `noqueue` interface. It is a bounded first
implementation, not a claim of coverage for every Chaos Mesh fault type.

## Installation and normal use

Use matching Tool and Operator builds containing protocol v1. Enable the
Operator chart's `recovery.enabled` and `daemonset.enable`, configure a unique
`recovery.clusterID`, TLS server name and dedicated TLS Secret. See the Operator
repository's `docs/node-local-recovery.md` for chart settings.

The Tool image must include `ip`, JSON-capable `tc`, `nsenter`, and the existing
`bin/chaos_os` executor. No new executor library version is required by this
change. A node's recovery address defaults to its InternalIP and the configured
port. Override it with the Node annotation `chaosblade.io/recovery-address`
(an HTTPS URL) when the administrator uses a different management route.

Opt an experiment in through its normal CLI or YAML:

```sh
blade create k8s pod-network delay --recovery \
  --namespace default --names example --interface eth0 --time 50 \
  --kubeconfig /path/to/cluster-a.yaml --waiting-time 60s
```

```yaml
apiVersion: chaosblade.io/v1alpha1
kind: ChaosBlade
metadata:
  name: example-delay
  annotations:
    chaosblade.io/recovery: "true"
spec:
  experiments:
    - scope: pod
      target: network
      action: delay
      matchers:
        - name: namespace
          value: [default]
        - name: names
          value: [example]
        - name: interface
          value: [eth0]
        - name: time
          value: ["50"]
```

Supported selection matchers are `names`, equality `labels`, `namespace` for
pods, `evict-count`, and `evict-percent`. Fault parameters are `interface`,
`time` and optional integer `offset` for delay, or `percent` for loss. Filtered
traffic, host-network pods, other fault types, `force`, and automatic timeouts
are rejected in this mode. Rejection happens before any injection.

Normal deletion (`blade destroy` or deleting the CR) uses the same local stop
implementation as emergency recovery. A stopped run is terminal. To run another
experiment, create a new CR; changing the spec stops the old execution and does
not automatically start another one. Removing the opt-in annotation does not
move a registered execution back to the legacy path.

## Recovery without the Kubernetes API

Prepare one profile per cluster when installing the feature. This is independent
of kubeconfig and contains reachable seed nodes plus local credential paths:

```json
{
  "clusterID": "cluster-a",
  "nodes": [
    {
      "name": "worker-1",
      "address": "https://192.0.2.10:9444",
      "serverName": "recovery.cluster-a.example"
    }
  ],
  "tls": {
    "ca": "/secure/recovery/ca.crt",
    "certificate": "/secure/recovery/admin.crt",
    "key": "/secure/recovery/admin.key"
  }
}
```

Run these commands from a machine that can reach the node management addresses:

```sh
blade recovery status --profile cluster-a.json --experiment example-delay
blade recovery stop --profile cluster-a.json --experiment example-delay
```

Each participating node holds the complete immutable plan. The client discovers
it from a reachable seed, verifies its identity, and visits **all participants**.
No API discovery, `kubectl exec`, port-forward, TokenReview, or live Kubernetes
RBAC call is used by this emergency path. A reused CR name may match retained
history; select the exact `--run` shown by the ambiguity error in that case.

Optionally export an execution receipt while the API is available:

```sh
blade recovery export example-delay --kubeconfig /path/to/cluster-a.yaml \
  --output example-delay.json
blade recovery stop --plan example-delay.json \
  --ca /secure/recovery/ca.crt --cert /secure/recovery/admin.crt \
  --key /secure/recovery/admin.key
```

Export is not required before the outage: the cluster profile supports offline
plan discovery. Switching profile files selects a different cluster explicitly.
A profile and an explicitly supplied plan must have the same cluster ID.

`complete: true` means that every target in this plan is verified recovered or
was cancelled before injection. Partial/unreachable/ambiguous results retain
per-node errors and a failing exit status for `stop`; retry the same operation.
This result describes local recovery. The CR can remain stale until the API
returns, after which the Operator observes the stop and updates it.

## Lifecycle and correctness

1. The Operator resolves exact Node UID, Pod UID and container ID, then saves an
   immutable run plan in CR status. The CLI API mirror and Operator API package
   share the same v1 wire definition and validation.
2. The complete plan is registered on every participating Tool. Each node stores
   boot ID, namespace inode, process and interface identity, and claims its local
   interfaces. Only after every registration succeeds does the Operator persist
   `prepared` and start injection.
3. Local CLI calls and the HTTPS service share `recovery.Manager`. A host-mounted
   directory contains a SQLite journal and a cross-process effect lock. The
   existing nine-column experiment table is not changed. SQLite transactions do
   not span external commands.
4. Before an effect, `applying` is durable. After the existing OS executor runs,
   the adapter verifies the netem action and saves a fingerprint of the actual
   qdisc and transmit queue. Namespace file descriptors pin each operation.
5. Stop intent is durable before waiting for the effect lock. A concurrent apply
   finishes under that lock, then is recovered. Late apply requests and register
   retries cannot clear the terminal stop. Cancellation also creates tombstones
   for targets that never completed registration.
6. Cleanup verifies the original container/namespace/interface and fingerprint
   before invoking the existing inverse. It restores the executor's known queue
   length adjustment. Claims are released only after verified cleanup.
7. After API recovery or Operator restart, reconciliation reads local state before
   any create retry. A stop from any participant prevents further injection and
   requests cleanup on the remaining participants. Unresolved managed executions
   are excluded from the legacy age-based finalizer removal task.

Kernel effects and database commits cannot form one atomic transaction. A crash
between effect creation and its durable fingerprint can leave `unknown`. The
service refuses to delete rules with uncertain ownership and exposes the error;
it does not report a successful recovery. External root/CNI changes cannot be
serialized by ChaosBlade's lock and are outside its ownership contract.

## Limits and operations

- The node, original target namespace, local runtime for pod identity checks, and
  a management route to the Tool must remain available. This is not recovery of
  a dead node or an unreachable management network.
- The original application container must still be identifiable. Container exit,
  replacement, node reboot, changed interface identity, or foreign qdiscs yield
  an explicit unresolved result. This version does not search replacement pods
  or sandbox containers and never broadens the recovery target.
- A plan is limited to 128 targets/nodes and 256 KiB at the Operator. A node keeps
  up to 1024 run tombstones and refuses new registrations when full. Automatic
  history pruning is intentionally absent: deleting tombstones can admit stale
  create requests. Capacity exhaustion requires maintenance before new runs.
- Local effects are serialized per Tool node. While a managed execution holds
  claims, legacy local network mutations through `blade` on that Tool are
  conservatively refused. Other fault categories and default-off installations
  retain their existing path. Finish old network experiments before enabling or
  disabling this mode; existing experiments are not retroactively registered.
- Running managed CRs are observed every ten seconds. Calls have deadlines, the
  service accepts at most eight concurrent requests, and the emergency client
  visits at most eight nodes concurrently. No scalability or performance gain is
  claimed by this initial version.
- Use a dedicated recovery CA. A trusted client certificate grants cluster-wide
  read/stop authority, independent of Kubernetes RBAC. The service has no remote
  create, registration, arbitrary command, or generic destroy endpoint. Stop
  attempts log the client certificate identity. Protect profiles' private keys,
  restrict network access, and rotate certificates before expiry. Restart Tool
  processes after certificate rotation; live certificate reload is not included.
- Preserve the **whole recovery directory**, including SQLite sidecars and lock
  files, across Tool upgrades/restarts. Do not change cluster ID or node name for
  an existing state directory. Do not downgrade Tool/Operator or remove the
  recovery state while an execution remains unresolved.

## Validation

```sh
go test -race ./...
go vet ./...
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build ./cli
```

The tests cover restart persistence, terminal stop, concurrent apply/stop across
independent managers, conflicting claims, uncertain ownership, retries, target
replacement, partial results, mTLS authentication and offline discovery.
The Operator repository adds barrier/reconciliation/finalizer tests and
`hack/test-node-recovery-outage.py` for a disposable Minikube API outage test.
