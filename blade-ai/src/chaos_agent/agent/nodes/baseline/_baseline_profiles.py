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

from chaos_agent.agent.nodes.execute._injection_detection import _TOOL_POD_NAMESPACE
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
# ``tools.readonly`` (``is_readonly_argv`` / ``is_readonly_inner_tokens``), which
# accepts more commands than are worth advertising (shell no-ops like ``true`` /
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

# Shell metacharacters rejected in ANY baseline command (injection defense).
# ``--`` (kubectl exec separator) and single flags are fine; these are the
# constructs that chain/redirect/substitute commands.
_SHELL_METACHARS = ("|", ">", "<", ";", "&", "`", "$(", "\n")


def validate_command(command: str, profile: str) -> bool:
    """Return True iff *command* is a permitted read-only baseline command
    for *profile*.

    - Rejects shell metacharacters (pipe / redirect / chain / substitution).
    - ``k8s``: must be ``kubectl <allowed-subcommand> ...``; for ``exec`` the
      command after ``--`` must be a read-only diagnostic.
    - ``host``: leading binary must be a read-only diagnostic.
    """
    if not command or not command.strip():
        return False
    for bad in _SHELL_METACHARS:
        if bad in command:
            return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False

    # Read/mutate judgement is delegated to the shared read-only classifier so
    # baseline capture, host_read, and the kubectl-exec probe classifier all
    # share ONE vocabulary (with argument-level guards for dual-use tools like
    # ip / systemctl / mount / dmesg).
    from chaos_agent.tools.readonly import is_readonly_argv, is_readonly_inner_tokens

    if profile == PROFILE_K8S:
        if tokens[0] != "kubectl":
            return False
        if len(tokens) < 2 or tokens[1] not in K8S_ALLOWED_SUBCOMMANDS:
            return False
        if tokens[1] == "exec":
            # Defense-in-depth: a bare ``kubectl exec pod <cmd>`` (no ``--``)
            # still runs <cmd>, so the old "only check when -- present" rule
            # let non-diagnostics slip through. Require the canonical ``--``
            # separator (which the prompt mandates) and validate the command
            # after it as a read-only probe. Uses the EXEC-context judge so a
            # node probe through a debug pod (``chroot /host df -h``) is judged
            # by the command it actually runs — same semantics the guard and
            # kubectl_read apply.
            if "--" not in tokens:
                return False
            after = tokens[tokens.index("--") + 1:]
            if not after or not is_readonly_inner_tokens(after):
                return False
        return True

    if profile == PROFILE_HOST:
        return is_readonly_argv(tokens)

    # Unknown profile → reject (fail closed).
    return False


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
    '\"mode\": \"simple\"}\n'
    "- ``command`` is a SINGLE read-only command (no pipes, redirects, "
    "``;``, ``&&`` or command substitution).\n"
    "- ``description`` is a short metric label (e.g. 'Node disk usage', "
    "'Pod CPU/Memory').\n"
    "- ``mode`` defaults to 'simple'; only use another value if the "
    "capability section below explicitly tells you to.\n"
    "- Select the smallest command set that covers every state the fault will "
    "modify. The runtime enforces the collection budget.\n\n"
)

_FRAGMENT_K8S = (
    "# Capability: Kubernetes (kubectl)\n"
    "You operate against a Kubernetes cluster via ``kubectl``. Every "
    "``command`` MUST start with ``kubectl`` and use one of: "
    "get, top, describe, exec.\n"
    "- Use the ACTUAL resource names / namespace / labels provided in the "
    "task context — embed them directly, do not invent placeholders.\n"
    "- For ``kubectl exec``, the command after ``--`` MUST be a read-only "
    f"diagnostic ({', '.join(sorted(DIAG_BINARY_WHITELIST))}).\n"
    "- To capture NODE host-level metrics (disk/io on a node's filesystem), "
    "you cannot exec a node directly. Emit "
    f"``kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- <diag>`` with "
    "``\"mode\": \"debug_two_step\"`` — ``{debug_pod}`` is the ONLY "
    "placeholder allowed and is resolved at execution time.\n\n"
    "Examples:\n"
    '[{"description": "Pod CPU/Memory", '
    '"command": "kubectl top pod my-pod -n prod", "mode": "simple"},\n'
    '{"description": "Node disk IO", '
    f'"command": "kubectl exec {{debug_pod}} -n {_TOOL_POD_NAMESPACE} '
    '-- iostat -xd 1 3", "mode": "debug_two_step"}]\n'
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
    "- ``mode`` is always 'simple' (there is no debug pod on a host).\n\n"
    "Examples:\n"
    '[{"description": "Host CPU/load", "command": "top -bn1", '
    '"mode": "simple"},\n'
    '{"description": "Host memory", "command": "free -m", "mode": "simple"},\n'
    '{"description": "Host disk usage", "command": "df -h", '
    '"mode": "simple"}]\n'
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
