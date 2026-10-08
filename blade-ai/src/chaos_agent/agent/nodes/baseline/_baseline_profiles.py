"""Baseline capability profiles — decouple the baseline collection prompt
from any specific execution mechanism / fault type / connection channel.

The System Prompt is assembled from ONE universal core plus a per-profile
capability fragment:

    build_baseline_system_prompt(channel) = _BASELINE_CORE + FRAGMENT[profile]

A "profile" describes what the collector *can do* against the current
transport channel (run kubectl vs run host shell diagnostics), NOT what
fault is being injected. Adding a new capability (e.g. JVM diagnostics)
means registering one fragment + whitelist here — the core prompt and the
baseline_capture orchestration never change.

profile mapping:
    kubeconfig, kubewiz_k8s  → "k8s"   (semantic command is kubectl)
    ssh, kubewiz_host        → "host"  (semantic command is a host shell diag)
"""

from __future__ import annotations

import shlex

from chaos_agent.tools.pod_discovery import TOOL_POD_NAMESPACE as _TOOL_POD_NAMESPACE
from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S, profile_of

# ---------------------------------------------------------------------------
# Command whitelists (per profile) — the safety layer for LLM-generated cmds
# ---------------------------------------------------------------------------

# kubectl subcommands allowed for baseline collection (read-only + exec).
# ``debug`` is intentionally excluded: node host-level metrics are captured
# via ``kubectl exec {debug_pod} ... `` + mode="debug_two_step" instead, so a
# bare ``kubectl debug`` (which would open an interactive session) is rejected.
K8S_ALLOWED_SUBCOMMANDS = frozenset({"get", "top", "describe", "exec"})

# Diagnostics ADVERTISED to the LLM in the capability fragments below (both the
# host leading binary and the command after a ``kubectl exec --``).
#
# This is a RECOMMENDATION set, not the enforcement set: enforcement lives in
# ``tools.readonly``'s public surfaces (``contains_shell_metachar`` /
# ``host_command_rejection_reason`` / ``kubectl_exec_rejection_reason``), which
# accept more commands than are worth advertising (shell no-ops like ``true`` /
# ``echo``, pipeline filters that are useless here because pipes are rejected,
# and network egress tools like ``curl`` / ``wget``).
#
# Two invariants, the first asserted by
# ``test_baseline_advertised_binaries_are_accepted``:
#   1. Everything listed here MUST be accepted by ``tools.readonly``. Advertising
#      a command the validator rejects makes the LLM burn baseline attempts on
#      guaranteed failures.
#   2. A diagnostic the validator newly accepts stays INVISIBLE until it is added
#      here — capability without discoverability is dead capability.
# Dual-use entries (ip / systemctl / mount / dmesg / sysctl / journalctl /
# crictl) are advertised by bare name; ``tools.readonly`` admits only their
# inspection forms and rejects the mutating ones.
DIAG_BINARY_WHITELIST = frozenset({
    "df", "ps", "ls", "cat", "top", "iostat", "free",
    "uptime", "hostname", "mount", "grep", "wc", "du",
    "head", "tail", "find", "stat", "ip", "ss", "netstat",
    "vmstat", "mpstat", "sar", "dmesg", "nproc", "systemctl",
    # process / kernel / device inspection
    "pidof", "pgrep", "lsof", "lsmod", "lsblk", "blkid", "uname",
    # kernel parameters and service logs (read-only forms only)
    "sysctl", "journalctl",
    # container-runtime state (inspection verbs only)
    "crictl",
    # capability probe. ``command -v`` is a POSIX shell builtin, so it is the
    # most portable "is this installed?" check on a host channel (both host
    # channels hand a command STRING to a remote shell). ``which`` is a separate
    # package that minimal images drop, so it is deliberately NOT advertised.
    "command",
})

def validate_command(command: str, profile: str) -> bool:
    """Return True iff *command* is a permitted read-only baseline command
    for *profile*.

    Thin wrapper over :func:`validate_command_with_reason` — the boolean
    view of the SAME single enforcement source (adding a second judgement
    implementation would let the two drift apart).
    """
    return validate_command_with_reason(command, profile) is None


def validate_command_with_reason(command: str, profile: str) -> str | None:
    """Enforcement twin of ``validate_command`` that also states WHY.

    Returns ``None`` when the command is permitted; otherwise a
    human-readable rejection reason. Case #63 (inject-2ee3bdc7): the LLM
    derived a correct domain probe — ``dd ... conv=fsync`` for a
    write-latency case — which this gate rightly refused (it writes), but
    the refusal was then dropped on the floor with only a log line. The
    baseline receipt said ``7/7`` with no trace that a command had ever
    been rejected, so the fact "the case's primary metric is not
    collectible in this read-only channel" was invisible to the verifier
    and to post-hoc analysis. Callers that surface the receipt now carry
    the reason forward (see ``_llm_derive``).

    Branch order and semantics are identical to the historical
    ``validate_command`` body:

    - Rejects shell structure (pipe / redirect / chain / substitution).
    - ``k8s``: must be ``kubectl <allowed-subcommand> ...``; for ``exec`` the
      command after ``--`` must be a read-only diagnostic.
    - ``host``: leading binary must be a read-only diagnostic.

    The structural screen and the read/mutate judgement both live in
    ``tools.readonly``'s public surfaces, which self-dispatch on the guard
    engine (design 4.6, face 5) — baseline capture, host_read, and the
    kubectl-exec probe classifier share ONE vocabulary (with argument-level
    guards for dual-use tools like ip / systemctl / mount / dmesg). Under
    the facts engine the screens are structural, so a quoted literal no
    longer trips the metachar scan (the P1 fix).
    """
    if not command or not command.strip():
        return "empty command"
    from chaos_agent.tools.readonly import (
        contains_shell_metachar,
        host_command_rejection_reason,
        kubectl_exec_rejection_reason,
        kubectl_exec_target_form_reason,
    )

    # The upfront screen is the ONLY structural check the non-exec kubectl
    # subcommands (get/top/describe) get — the inner judges below only see
    # the command after ``--``.
    if contains_shell_metachar(command):
        return "contains shell metacharacters (pipe/redirect/chain/substitution)"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "not parseable as a single command"
    if not tokens:
        return "empty command"

    if profile == PROFILE_K8S:
        if tokens[0] != "kubectl":
            return "must start with 'kubectl' on the k8s profile"
        if len(tokens) < 2 or tokens[1] not in K8S_ALLOWED_SUBCOMMANDS:
            _sub = tokens[1] if len(tokens) > 1 else "(none)"
            return (
                f"kubectl subcommand '{_sub}' is not in the read-only "
                f"baseline set {sorted(K8S_ALLOWED_SUBCOMMANDS)}"
            )
        if tokens[1] == "exec":
            # Defense-in-depth: a bare ``kubectl exec pod <cmd>`` (no ``--``)
            # still runs <cmd>, so the old "only check when -- present" rule
            # let non-diagnostics slip through. Require the canonical ``--``
            # separator (which the prompt mandates) and validate the command
            # after it as a read-only probe. Uses the EXEC-context judge so a
            # node probe through a debug pod (``chroot /host df -h``) is judged
            # by the command it actually runs — same semantics the guard and
            # kubectl_read apply. The judge takes the full command line; its
            # judgement starts at the ``--`` boundary (prefix inert).
            if "--" not in tokens:
                return "exec must use the canonical '--' separator"
            after = tokens[tokens.index("--") + 1:]
            if not after:
                return "no command after '--'"
            # Target-zone form gate (Case #61 / W-61-1): the inner judge
            # deliberately ignores the prefix, so ``kubectl exec -l app=...
            # -- id`` used to slip through and fail at runtime with
            # ``unknown shorthand flag: 'l'``. The form gate rejects selector
            # flags and other malformed target forms BEFORE the inner judge,
            # so the reason surfaces with a ``{target_pod}`` fix hint instead
            # of being laundered by retry into ``expected_absence``.
            reason = kubectl_exec_target_form_reason(command)
            if reason is not None:
                return reason
            reason = kubectl_exec_rejection_reason(command)
            if reason is not None:
                return reason
        return None

    if profile == PROFILE_HOST:
        reason = host_command_rejection_reason(command)
        return reason

    # Unknown profile → reject (fail closed).
    return f"unknown profile '{profile}'"


# ---------------------------------------------------------------------------
# System Prompt: universal core + per-profile capability fragments
# ---------------------------------------------------------------------------
#
# U-shaped attention (Liu et al., 2023): critical rules live at the start
# (mission) and end (output contract); supporting detail sits in the middle.

_BASELINE_CORE = (
    # ── Primacy: mission (WHY + WHAT) ──
    "You are a chaos engineering baseline collection strategist. "
    "Derive the pre-injection baseline for causation attribution.\n\n"

    "# Core Principle\n"
    "Your baseline is the control for causation attribution. The verifier "
    "compares post-injection state against YOUR baseline to decide whether a "
    "change is fault-caused or pre-existing. If you miss a metric, the "
    "verifier CANNOT prove causation for it.\n\n"

    "Reason about what states the fault WILL modify — quantitative metrics "
    "(CPU, memory, disk, network) and qualitative state (replica count, pod "
    "phase, endpoint list, node condition). Collect baseline for each "
    "affected state, on the EXACT resource the fault targets. The verifier "
    "can only compare the SAME metric on the SAME resource.\n\n"

    # ── Middle: universal output contract ──
    "# Output Contract\n"
    "- Output ONLY a JSON list, no other text.\n"
    "- Each element: "
    '{\"description\": \"...\", \"command\": \"...\", '
    '\"mode\": \"simple\", \"class\": \"...\"}\n'
    "- ``command`` is a SINGLE read-only command (no pipes, redirects, "
    "``;``, ``&&`` or command substitution).\n"
    "- ``description`` is a short metric label (e.g. 'Node disk usage', "
    "'Pod CPU/Memory').\n"
    "- ``mode`` defaults to 'simple'; only use another value if the "
    "capability section below explicitly tells you to.\n"
    # ``class`` is the observation-dimension tag the program cross-checks
    # against the command's syntactic form (baseline-observation-contract).
    # Declaring it makes intent inspectable and lets a mismatch be rejected
    # BEFORE execution — Case #61 shipped ``kubectl exec -l app=... -- id``
    # (a selector form exec cannot honor) as ``container_internal`` and it
    # was laundered through retry into ``expected_absence``. Closed enum;
    # pick the ONE that matches what the command actually observes.
    "- ``class`` is a closed enum declaring WHAT the command observes. "
    "Pick exactly one per command; a mismatch between ``class`` and the "
    "command's form is rejected before execution:\n"
    "  * ``container_internal`` — state INSIDE a specific container's "
    "filesystem/process namespace (``kubectl exec <pod> -- <probe>``; the "
    "target MUST be a single pod name or the ``{target_pod}`` placeholder, "
    "never a selector).\n"
    "  * ``api_object`` — state of a Kubernetes API object as returned by "
    "the API server (``kubectl get`` / ``describe`` / ``top`` against "
    "pods, deployments, services, endpoints, PVs, etc.).\n"
    "  * ``node_level`` — state of the NODE itself (kernel counters, host "
    "filesystem, node conditions). Reached via ``kubectl describe/top "
    "node``, ``kubectl get pods --field-selector spec.nodeName=<node>``, "
    "or a ``{debug_pod}`` escape into the node's namespaces.\n"
    "  * ``host_level`` — state observed on a bare-host channel (no "
    "kubectl); use only when the capability section below is 'Host shell "
    "diagnostics'.\n"
    "- Select the smallest command set that covers every state the fault will "
    "modify. The runtime enforces the collection budget.\n\n"
)

_FRAGMENT_K8S = (
    "# Capability: Kubernetes (kubectl)\n"
    "You operate against a Kubernetes cluster via ``kubectl``. Every "
    "``command`` MUST start with ``kubectl`` and use one of: "
    "get, top, describe, exec.\n"
    "- Use the ACTUAL resource names / namespace / labels provided in the "
    "task context — embed them directly, do not invent placeholders. The "
    "ONE exception is ``{target_pod}`` (see below).\n"
    "- For ``kubectl exec``, the command after ``--`` MUST be a read-only "
    f"diagnostic ({', '.join(sorted(DIAG_BINARY_WHITELIST))}).\n"
    # ``{target_pod}`` teaching: same shape as ``{debug_pod}`` — a named
    # placeholder resolved at execution time, never a literal name from the
    # prompt. Case #61 shipped ``kubectl exec -l app=<label> -- <cmd>``
    # (selector form; exec targets ONE pod, not a query) because the derive
    # LLM was told to embed values directly and had no pod name to embed on
    # a workload-scope drill. The placeholder gives it a way to say \"the\n"
    # pod this workload owns\" without inventing a selector or a name.
    "- To target a specific pod for ``kubectl exec`` (or any pod-scoped "
    "query) when the fault scope is a WORKLOAD (deployment/statefulset/"
    "daemonset/service), emit the ``{target_pod}`` placeholder — resolved "
    "at execution time to a literal pod name owned by that workload. NEVER "
    "emit a selector form (``kubectl exec -l app=... -- <cmd>``): exec "
    "targets ONE specific pod, and the selector form is rejected. NEVER "
    "emit the workload's own name as a pod name (``kubectl exec "
    "my-deploy -- <cmd>``) — that is a different object kind.\n"
    "- To capture NODE host-level metrics you cannot exec a node directly: "
    "emit ``kubectl exec {debug_pod} -n " + _TOOL_POD_NAMESPACE + " -- "
    "<probe>`` with ``\"mode\": \"debug_two_step\"`` — ``{debug_pod}`` and "
    "``{target_pod}`` are the ONLY placeholders allowed and both are "
    "resolved at execution time (never emit a literal debug pod name or a "
    "literal target pod name). Know your execution environment: the debug "
    "pod is a PRIVILEGED but MINIMAL container — a jump board into the "
    "node, NOT a diagnostic toolbox; its image may carry no diagnostic "
    "binaries at all. Read host-level state through these channels, in "
    "order of availability certainty:\n"
    "  1. ``kubectl get/top/describe`` when the metric is API-visible;\n"
    "  2. kernel pseudo-files — ``cat /proc/diskstats``, ``cat /proc/stat``, "
    "``cat /proc/meminfo`` (global counters: readable in-container, and the "
    "data IS the node's);\n"
    "  3. host tools via namespace entry — ``nsenter -t 1 -m -u -i -n -p "
    "-- iostat -xd 1 3`` (the node is a full OS, its tools are there);\n"
    "  4. a diagnostic binary BARE in the container only when the image is "
    "known to carry it — minimal debug images usually do not.\n\n"
    "Examples:\n"
    '[{"description": "Pod CPU/Memory", "class": "api_object", '
    '"command": "kubectl top pod my-pod -n prod", "mode": "simple"},\n'
    '{"description": "Container filesystem usage", "class": '
    '"container_internal", "command": "kubectl exec {target_pod} -n prod '
    '-- df -h", "mode": "simple"},\n'
    '{"description": "Node disk counters", "class": "node_level", '
    f'"command": "kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} '
    '-- cat /proc/diskstats", "mode": "debug_two_step"},\n'
    '{"description": "Node disk IO rate", "class": "node_level", '
    f'"command": "kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} '
    '-- nsenter -t 1 -m -u -i -n -p -- iostat -xd 1 3", '
    '"mode": "debug_two_step"}]\n'
)

_FRAGMENT_HOST = (
    "# Capability: Host shell diagnostics\n"
    "You operate directly on a single host (the command is transported to "
    "it for you). Emit plain read-only shell diagnostics — do NOT use "
    "kubectl. Allowed leading binaries: "
    f"{', '.join(sorted(DIAG_BINARY_WHITELIST))}.\n"
    "- No pipes, redirects, ``;``, ``&&`` or command substitution.\n"
    "- Sampling tools MUST carry an iteration COUNT (e.g. ``mpstat 1 1``, "
    "``vmstat 1 3``, ``iostat -xd 1 2``, ``top -bn1``); never emit an "
    "unbounded continuous sample (e.g. ``mpstat -P ALL 1``) — it runs "
    "forever and times out.\n"
    "- To check whether a tool is installed, prefer ``command -v <name>`` — a "
    "shell builtin, so it needs no extra package and is independent of the "
    "install path. ``ls`` also works but must name the REAL path: network "
    "binaries live in sbin, so list every candidate at once "
    "(``ls /usr/sbin/iptables /usr/bin/iptables /sbin/iptables``). Do NOT "
    "assume ``/usr/bin`` — a wrong path reads as \"not installed\".\n"
    "- ``mode`` is always 'simple' (there is no debug pod on a host).\n"
    "- ``class`` is always 'host_level' on this channel.\n\n"
    "Examples:\n"
    '[{"description": "Host CPU/load", "class": "host_level", '
    '"command": "top -bn1", "mode": "simple"},\n'
    '{"description": "Host memory", "class": "host_level", '
    '"command": "free -m", "mode": "simple"},\n'
    '{"description": "Host disk usage", "class": "host_level", '
    '"command": "df -h", "mode": "simple"}]\n'
)

_CAPABILITY_FRAGMENTS: dict[str, str] = {
    PROFILE_K8S: _FRAGMENT_K8S,
    PROFILE_HOST: _FRAGMENT_HOST,
}


def build_baseline_system_prompt(channel: str) -> str:
    """Assemble the baseline System Prompt = universal core + the capability
    fragment for *channel*'s profile.

    Unknown channels fail closed. They receive no executable capability
    fragment, which prevents a new environment from being guessed as K8s.
    """
    profile = profile_of(channel)
    fragment = _CAPABILITY_FRAGMENTS.get(profile)
    if fragment is None:
        fragment = (
            "# Capability: Unsupported environment\n"
            "No approved observation capability is registered for this environment. "
            "Output an empty JSON list and do not invent a command.\n"
        )
    return _BASELINE_CORE + fragment
