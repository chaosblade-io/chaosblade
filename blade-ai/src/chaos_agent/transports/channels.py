"""Four TransportChannel implementations.

- KubeconfigChannel: direct kubectl/blade execution via --kubeconfig
- KubewizK8sChannel: wiz task exec connecting K8s clusters
- KubewizHostChannel: wiz task exec connecting hosts (kubewiz-host-channel)
- SSHChannel: SSH remote execution
"""

from __future__ import annotations

import base64
import os
import re
import shlex

from chaos_agent.config.settings import settings
from chaos_agent.models.command_result import CommandResult

from .base import PROFILE_HOST, PROFILE_K8S, TransportTarget
from .protocol import parse_wiz_output


def _wiz_timeout_seconds(timeout: float | None) -> int:
    """Resolve ``wiz task exec --wait-timeout`` (how long the CLI blocks
    waiting for the task). This is INDEPENDENT from ``--timeout`` (the task's
    own server-side execution budget, see ``_wiz_task_timeout_seconds``).

    wiz's built-in ``--wait-timeout`` default is 10s, which prematurely aborts
    long-running commands routed through the gateway (fault injection, or a
    self-severing exec that blocks until the channel drops). We mirror the
    caller's per-command ``timeout`` — unless ``kubewiz_wait_timeout`` pins an
    explicit override. The MIRROR branch is the shipped default
    (``kubewiz_wait_timeout = 0``): a former default of 30 pinned the wait and
    deterministically mis-killed commands that really ran >30s inside a larger
    caller budget (timeout_kubectl=60 / timeout_kubectl_exec=180) — wiz
    answered ``task timed out after 30s`` while the task kept running
    server-side, and ``classify_error`` reads that text as SHORT_RETRY, so a
    side-effecting command risked double execution (measured live 2026-09-20;
    with the override at 0 the same 45s sleep succeeds).

    ``isinstance(int)`` guards against a fully-mocked ``settings`` object
    (whose attribute would be a truthy MagicMock) falling into the override
    branch.
    """
    override = settings.kubewiz_wait_timeout
    if isinstance(override, int) and override > 0:
        return override
    if timeout and timeout > 0:
        return max(1, round(timeout))
    return 10  # wiz's own default, last-resort fallback


def _wiz_task_timeout_seconds() -> int:
    """Resolve ``wiz task exec --timeout`` — the task's own server-side
    execution budget, configured independently from ``--wait-timeout`` via
    ``kubewiz_task_timeout`` (default 600s).

    ``isinstance(int)`` guards against a fully-mocked ``settings`` object.
    """
    budget = settings.kubewiz_task_timeout
    if isinstance(budget, int) and budget > 0:
        return budget
    return 600


def _where(channel: str, detail: str = "") -> str:
    """Render the execution LOCATION suffix appended to a displayed command.

    task-46317228: ``display_command`` stripped the transport wrapper so the TUI
    showed a clean ``uptime`` — and with it went ``--cluster-uuid``, the only
    visible sign that the command was headed for the KubeWiz platform executor
    rather than the target host. The tool NAME did expose that a host tool was
    running on a k8s session (that is how the operator noticed), but nothing
    anywhere showed WHICH MACHINE answered. That second fact is what the
    verifier reasoned from.

    Keeping the semantic command as the subject and appending the location
    preserves the readable display while making the destination auditable.
    """
    ident = f" -> {_ellipsis(detail)}" if detail else ""
    return f"  [{channel}{ident}]"


def _ellipsis(value: str, keep: int = 24) -> str:
    """Shorten a cluster uuid / host name so the suffix cannot flood the line.

    Elides the MIDDLE, not the tail. Head-only truncation defeated the purpose
    on the very names this exists for: the accident's nodes were
    ``cn-shanghai-cloudspe.25.209.68.1`` and
    ``cn-shanghai-cloudspe.172.100.3.116`` — cutting at 12 chars renders both as
    ``cn-shanghai-…``, so the operator still cannot tell WHICH machine answered.
    What distinguishes a node is its tail, so keep both ends.
    """
    value = value.strip()
    if len(value) <= keep:
        return value
    head = (keep + 1) // 2
    # ``max(1, …)``: a ``tail`` of 0 would make ``value[-0:]`` return the WHOLE
    # string — the classic negative-slice trap — silently defeating truncation.
    tail = max(1, keep - head)
    return f"{value[:head]}…{value[-tail:]}"


# Same shape as ``_where`` produces, anchored at end of string.
_LOCATION_SUFFIX_RE = re.compile(r"\s{2}\[[^\[\]]*\]\s*$")


def strip_execution_location(shown: str) -> str:
    """Remove the ``_where`` location suffix from a displayed command.

    For consumers that MATCH ON command text rather than show it. The suffix
    carries a cluster uuid or host name, and evidence-coverage matching is
    substring-based over generic vocabulary ("load", "network", "status", …) —
    a cluster named ``prod-network`` would otherwise mark the network primary
    metric as covered on every single observation, silently suppressing the
    deterministic supplement probes. Lives beside ``_where`` so the two shapes
    cannot drift apart.
    """
    return _LOCATION_SUFFIX_RE.sub("", shown)


def _wiz_preserve_stderr(cmd: list[str]) -> str:
    """Build a wiz ``--command`` payload whose remote stderr survives the relay.

    Probed live against the platform: when the inner command exits non-zero
    with BOTH streams non-empty (e.g. kubectl renders a half-baked jsonpath
    on stdout while the real parse error sits on stderr), wiz relays stdout
    only — the stderr evidence is dropped before it ever reaches
    ``parse_wiz_output``, and the model is left self-repairing blind
    (incident inject-774ecd39). Stderr-only failures DO come through.

    Probed live as well: the platform word-splits ``--command`` and execs
    directly — shell metacharacters (``;`` ``|`` ``(...)`` ``$VAR``) are NOT
    interpreted, they break task submission. The only reliable vehicle for
    shell logic is an explicit ``sh -c '<script>'``.

    The script runs the payload in a SUBSHELL (so an inner ``exit`` cannot
    skip the cleanup), tees stderr to a pid-scoped temp file and appends it
    to stdout ONLY on non-zero exit (preceded by a newline so a stdout that
    ended mid-line doesn't fuse with the first stderr line), so success-path
    output stays byte-identical (no deprecation-warning contamination of
    parseable stdout).
    """
    cmd_str = " ".join(shlex.quote(p) for p in cmd)
    script = (
        f'E="/tmp/.wiz_err_$$"; '
        f'( {cmd_str} ) 2>"$E"; rc=$?; '
        '[ $rc -ne 0 ] && { printf "\\n"; cat "$E"; }; '
        'rm -f "$E"; exit $rc'
    )
    return f"sh -c {shlex.quote(script)}"


def _wiz_unwrap_for_display(cmd_str: str) -> str:
    """Recover the semantic command from a ``sh -c`` wrapped payload."""
    if not cmd_str.startswith("sh -c "):
        return cmd_str
    try:
        script = shlex.split(cmd_str)[2]
    except (IndexError, ValueError):
        return cmd_str
    prefix = 'E="/tmp/.wiz_err_$$"; '
    if not script.startswith(prefix):
        return cmd_str
    inner = script[len(prefix):]
    inner = re.sub(
        r' 2>"\$E"; rc=\$\?; \[ \$rc -ne 0 \] && \{ printf "\\n"; '
        r'cat "\$E"; \}; '
        r'rm -f "\$E"; exit \$rc$',
        "",
        inner,
    )
    if inner.startswith("( ") and inner.endswith(" )"):
        inner = inner[2:-2]
    return inner


def embed_stdin_in_command(cmd: list[str], stdin_data: str) -> list[str]:
    """Fold ``stdin_data`` into ``cmd`` as a base64 pipeline prefix.

    ``wiz task exec --command`` offers no stdin pipe, yet the platform
    executor DOES run the command through a shell (probed live: ``|`` and
    ``base64 -d`` work; heredocs with embedded newlines break task
    submission). The reliable single-line form is therefore::

        echo <b64> | base64 -d | <cmd ...>

    The blob is one base64 word (no shell metacharacters can survive
    encoding), so the manifest content cannot inject extra commands.
    Guard/audit still see the RAW semantic command with ``-f -``: the
    embedding happens only inside ``wrap_command`` AFTER the guard step,
    and the manifest itself was already screened upstream (screener /
    target_guard inspect ``stdin_data`` on the tool call).
    """
    blob = base64.b64encode(stdin_data.encode("utf-8")).decode("ascii")
    quoted_cmd = " ".join(shlex.quote(p) for p in cmd)
    return ["sh", "-c", f"echo {blob} | base64 -d | {quoted_cmd}"]


class KubeconfigChannel:
    """Direct kubectl/blade execution via --kubeconfig flag."""

    # Local subprocess — run_command pipes stdin_data straight to it.
    supports_stdin = True

    @property
    def name(self) -> str:
        return "kubeconfig"

    @property
    def profile(self) -> str:
        return PROFILE_K8S

    @property
    def priority(self) -> int:
        # Catch-all for k8s scope: claimed only when no more specific k8s
        # channel matches, mirroring the old "otherwise → kubeconfig" branch.
        return 0

    def claims(self, target: TransportTarget) -> bool:
        return target.scope == PROFILE_K8S

    def wrap_command(self, cmd: list[str], target: TransportTarget, timeout: float | None = None) -> list[str]:
        # kubeconfig is already injected into cmd by the caller;
        # no transport wrapper needed.
        return cmd

    def adapt_result(self, result: CommandResult, target: TransportTarget) -> CommandResult:
        return result  # passthrough

    def preflight(self, target: TransportTarget) -> list[str]:
        errors: list[str] = []
        kc = target.kubeconfig or settings.kubeconfig_path
        if kc:
            # KUBECONFIG may be an os.pathsep-joined list of files; kubectl
            # merges them. Accept as long as at least one component exists —
            # checking os.path.isfile on the raw joined string would wrongly
            # reject every multi-path config.
            paths = [os.path.expanduser(p) for p in kc.split(os.pathsep) if p]
            if not any(os.path.isfile(p) for p in paths):
                errors.append(f"kubeconfig file not found: {kc}")
        else:
            default = os.path.expanduser("~/.kube/config")
            if not os.path.isfile(default):
                errors.append("kubeconfig not configured")
        return errors

    def display_command(self, cmd: list[str], target: TransportTarget | None = None) -> str:
        detail = (target.kube_context if target else "") or ""
        return " ".join(cmd) + _where(self.name, detail)


class KubewizK8sChannel:
    """K8s cluster access via ``wiz task exec``."""

    # ``wiz task exec`` has no stdin pipe; the executor folds it into the
    # command instead (see ``embed_stdin_in_command``).
    supports_stdin = False

    @property
    def name(self) -> str:
        return "kubewiz_k8s"

    @property
    def profile(self) -> str:
        return PROFILE_K8S

    @property
    def priority(self) -> int:
        return 10

    def claims(self, target: TransportTarget) -> bool:
        return bool(target.scope == PROFILE_K8S and target.kubewiz_cluster_uuid)

    def wrap_command(self, cmd: list[str], target: TransportTarget, timeout: float | None = None, stdin_data: str = "") -> list[str]:
        if stdin_data:
            cmd = embed_stdin_in_command(cmd, stdin_data)
        wait_timeout = str(_wiz_timeout_seconds(timeout))
        task_timeout = str(_wiz_task_timeout_seconds())
        return [
            settings.wiz_path, "task", "exec",
            "--command", _wiz_preserve_stderr(cmd),
            "--cluster-uuid", target.kubewiz_cluster_uuid,
            "--profile", target.kubewiz_profile,
            "--timeout", task_timeout,
            "--wait-timeout", wait_timeout,
        ]

    def adapt_result(self, result: CommandResult, target: TransportTarget) -> CommandResult:
        return parse_wiz_output(result)

    def preflight(self, target: TransportTarget) -> list[str]:
        errors: list[str] = []
        if not target.kubewiz_cluster_uuid:
            errors.append("kubewiz_cluster_uuid not configured")
        if not target.kubewiz_profile:
            errors.append("kubewiz_profile not configured")
        return errors

    def display_command(self, cmd: list[str], target: TransportTarget | None = None) -> str:
        # Strip the wiz wrapper for readability, but never drop WHERE it ran:
        # ``--cluster-uuid`` addresses a CLUSTER, so the command lands on the
        # platform's executor, not on any machine the operator chose.
        detail = ""
        if target is not None:
            detail = target.kubewiz_cluster_uuid or ""
        elif "--cluster-uuid" in cmd:
            try:
                detail = cmd[cmd.index("--cluster-uuid") + 1]
            except IndexError:
                detail = ""
        inner = " ".join(cmd)
        if len(cmd) >= 5 and cmd[1:3] == ["task", "exec"]:
            try:
                inner = _wiz_unwrap_for_display(cmd[cmd.index("--command") + 1])
            except (ValueError, IndexError):
                pass
        return inner + _where(self.name, f"cluster {detail}" if detail else "")


class KubewizHostChannel:
    """Host access via ``wiz task exec`` with kubewiz-host-channel."""

    # Same wiz limitation as KubewizK8sChannel — no stdin pipe.
    supports_stdin = False

    @property
    def name(self) -> str:
        return "kubewiz_host"

    @property
    def profile(self) -> str:
        return PROFILE_HOST

    @property
    def priority(self) -> int:
        # host_name wins over ssh_host, as in the old if/else ordering.
        return 20

    def claims(self, target: TransportTarget) -> bool:
        return bool(target.scope == PROFILE_HOST and target.host_name)

    def wrap_command(self, cmd: list[str], target: TransportTarget, timeout: float | None = None, stdin_data: str = "") -> list[str]:
        if stdin_data:
            cmd = embed_stdin_in_command(cmd, stdin_data)
        wait_timeout = str(_wiz_timeout_seconds(timeout))
        task_timeout = str(_wiz_task_timeout_seconds())
        return [
            settings.wiz_path, "task", "exec",
            "--command", _wiz_preserve_stderr(cmd),
            "--cluster-uuid", "kubewiz-host-channel",
            "--name", target.host_name,
            "--profile", target.kubewiz_profile,
            "--timeout", task_timeout,
            "--wait-timeout", wait_timeout,
        ]

    def adapt_result(self, result: CommandResult, target: TransportTarget) -> CommandResult:
        return parse_wiz_output(result)

    def preflight(self, target: TransportTarget) -> list[str]:
        errors: list[str] = []
        if not target.host_name:
            errors.append("host_name not configured")
        if not target.kubewiz_profile:
            errors.append("kubewiz_profile not configured")
        return errors

    def display_command(self, cmd: list[str], target: TransportTarget | None = None) -> str:
        detail = ""
        if target is not None:
            detail = target.host_name or ""
        elif "--name" in cmd:
            try:
                detail = cmd[cmd.index("--name") + 1]
            except IndexError:
                detail = ""
        inner = " ".join(cmd)
        if len(cmd) >= 5 and cmd[1:3] == ["task", "exec"]:
            try:
                inner = _wiz_unwrap_for_display(cmd[cmd.index("--command") + 1])
            except (ValueError, IndexError):
                pass
        return inner + _where(self.name, detail)


class SSHChannel:
    """Host access via SSH remote execution."""

    # ssh forwards the local subprocess stdin to the remote shell.
    supports_stdin = True

    @property
    def name(self) -> str:
        return "ssh"

    @property
    def profile(self) -> str:
        return PROFILE_HOST

    @property
    def priority(self) -> int:
        return 10

    def claims(self, target: TransportTarget) -> bool:
        return bool(target.scope == PROFILE_HOST and target.ssh_host)

    def wrap_command(self, cmd: list[str], target: TransportTarget, timeout: float | None = None) -> list[str]:
        cmd_str = " ".join(shlex.quote(p) for p in cmd)
        ssh_args = [
            "ssh",
            "-o", f"StrictHostKeyChecking={settings.ssh_strict_host_key_checking or 'accept-new'}",
            "-o", "BatchMode=yes",
        ]
        if target.ssh_key_path:
            ssh_args.extend(["-i", target.ssh_key_path])
        if target.ssh_port and target.ssh_port != 22:
            ssh_args.extend(["-p", str(target.ssh_port)])
        user_host = (
            f"{target.ssh_user}@{target.ssh_host}"
            if target.ssh_user
            else target.ssh_host
        )
        # "--" terminates option parsing: prevents an ssh_host beginning with
        # "-" from being misread as an ssh flag (e.g. -oProxyCommand=...).
        ssh_args.extend(["--", user_host, cmd_str])
        return ssh_args

    def adapt_result(self, result: CommandResult, target: TransportTarget) -> CommandResult:
        return result  # passthrough — ssh exit code is the real exit code

    def preflight(self, target: TransportTarget) -> list[str]:
        errors: list[str] = []
        if not target.ssh_host:
            errors.append("ssh_host not configured")
        if target.ssh_key_path and not os.path.isfile(target.ssh_key_path):
            errors.append(f"SSH key not found: {target.ssh_key_path}")
        return errors

    def display_command(self, cmd: list[str], target: TransportTarget | None = None) -> str:
        detail = ""
        if target is not None and target.ssh_host:
            detail = f"{target.ssh_user}@{target.ssh_host}" if target.ssh_user else target.ssh_host
        return " ".join(cmd) + _where(self.name, detail)
