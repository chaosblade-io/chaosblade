"""Pre-flight self-check framework — single source of truth for all checks.

Two flavours of check live here, sharing the ``CheckResult`` shape:

  **Sync presence checks** (``check_llm_api_key`` / ``check_kubeconfig`` /
  ``check_kubectl`` / ``check_blade``) — used by CLI commands via
  ``run_command``. They only verify that config fields and binaries
  exist on disk. Fast, no I/O, no network. CLI startup latency wins.

  **Async live checks** (``check_llm_api_key_live`` / ``check_kubeconfig_live`` /
  ``check_kubectl_version`` / ``check_blade_version`` / ``check_skills`` /
  ``check_k8s_connectivity`` / ``check_chaosblade_operator``) — used by the
  TUI boot panel via ``run_tui_checks``. They actually exercise the
  dependency (LLM round-trip, ``kubectl`` subprocess, etc.) so a
  misconfiguration surfaces before the first user message. Trade ~1s of
  parallel I/O for "configured ≠ working" gaps the sync checks miss.

``run_command`` provides three-phase CLI orchestration:
  Phase 1: preflight checks (local mode only)
  Phase 2: execute local_fn or server_fn with auto cleanup + error mapping
  Phase 3: output formatting (handled by caller)
"""

import asyncio
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import typer

from chaos_agent.cli.config_manager import get_backend, get_mode
from chaos_agent.config.settings import settings
from chaos_agent.skills.loader import get_skills_dir
from chaos_agent.transports import (
    PROFILE_HOST,
    TransportRegistry,
    TransportTarget,
    execute_via_transport,
    is_host_scope_channel,
    is_kubewiz_channel,
    resolve_channel_name,
)

logger = logging.getLogger(__name__)


def _is_kubewiz_channel() -> bool:
    """Check if a kubewiz connection mode is active.

    Thin wrapper over :func:`chaos_agent.transports.is_kubewiz_channel` —
    resolves the active channel (honouring the ``kube_connection_mode``
    override and field-based inference) and returns ``True`` for
    ``kubewiz_k8s`` / ``kubewiz_host``.  Retained as a module-local name
    because it is referenced throughout this module's checks.
    """
    return is_kubewiz_channel()


def _is_host_scope_channel() -> bool:
    """True for host-scope channels (ssh / kubewiz_host).

    Host-scope drills target a machine directly, not a K8s cluster, so the
    K8s-oriented live checks (cluster connectivity, ChaosBlade Operator) do
    not apply.  Each connection mode should only validate its own config
    surface — running a kubectl/wiz cluster probe in ssh/kubewiz_host mode
    would spuriously fail (or hang) at TUI boot.

    Thin wrapper over :func:`chaos_agent.transports.is_host_scope_channel`,
    retained as a module-local name because it is referenced throughout
    this module's checks.
    """
    return is_host_scope_channel()


@dataclass
class CheckResult:
    """Single pre-flight check result."""
    name: str                   # check identifier, e.g. "llm_api_key"
    severity: str               # "blocking" | "warning"
    passed: bool
    message: str = ""           # failure description (empty if passed)
    fix: str = ""               # fix guidance (empty if passed)


# ── Atomic check functions ──────────────────────────────────────────

def check_llm_api_key() -> CheckResult:
    """Check that llm_api_key is configured."""
    if settings.llm_api_key:
        return CheckResult(name="llm_api_key", severity="blocking", passed=True)

    return CheckResult(
        name="llm_api_key",
        severity="blocking",
        passed=False,
        message="llm_api_key 未配置",
        fix="blade-ai config set llm_api_key <your-key>\n"
            "或设置环境变量 BLADE_AI_LLM_API_KEY=<your-key>",
    )


def expand_kubeconfig_path(path: str) -> str:
    """Expand ~ in a kubeconfig path. Empty input passes through.

    kubectl is invoked via execvp (no shell), so ~ would otherwise reach
    kubectl literally. Used by both CLI and TUI preflight.
    """
    if not path:
        return ""
    return os.path.expanduser(path)


def check_kubeconfig() -> CheckResult:
    """Check that a readable kubeconfig exists."""
    # Host-scope channels (ssh / kubewiz_host) don't use kubeconfig; their
    # required fields are validated by check_transport_config instead.
    if is_host_scope_channel():
        return CheckResult(name="kubeconfig", severity="blocking", passed=True)
    if _is_kubewiz_channel():
        missing = []
        if not settings.kubewiz_cluster_uuid:
            missing.append("BLADE_AI_KUBEWIZ_CLUSTER_UUID")
        if not settings.kubewiz_profile:
            missing.append("BLADE_AI_KUBEWIZ_PROFILE")
        if missing:
            return CheckResult(
                name="kubeconfig",
                severity="blocking",
                passed=False,
                message=f"kubewiz mode: missing required config: {', '.join(missing)}",
                fix="Set via environment variables or blade-ai config set",
            )
        return CheckResult(name="kubeconfig", severity="blocking", passed=True)
    raw = settings.kubeconfig_path
    path = expand_kubeconfig_path(raw)
    if not path:
        default = os.path.expanduser("~/.kube/config")
        if os.path.isfile(default):
            return CheckResult(name="kubeconfig", severity="blocking", passed=True)
        return CheckResult(
            name="kubeconfig",
            severity="blocking",
            passed=False,
            message="kubeconfig 未配置（默认 ~/.kube/config 也不存在）",
            fix="blade-ai config set kubeconfig_path <path>\n"
                "或设置环境变量 KUBECONFIG=<path>",
        )

    if not os.path.isfile(path):
        return CheckResult(
            name="kubeconfig",
            severity="blocking",
            passed=False,
            message=f"kubeconfig 文件不存在: {path}",
            fix="blade-ai config set kubeconfig_path <path>\n"
                "或设置环境变量 KUBECONFIG=<path>",
        )

    return CheckResult(name="kubeconfig", severity="blocking", passed=True)


def check_kubectl() -> CheckResult:
    """Check that kubectl (or wiz in kubewiz mode) is executable."""
    from chaos_agent.utils.blade_paths import is_executable
    # SSH channel executes on the remote host; local kubectl is irrelevant.
    if resolve_channel_name() == "ssh":
        return CheckResult(name="kubectl", severity="blocking", passed=True)
    if _is_kubewiz_channel():
        if is_executable(settings._resolve_wiz_path()):
            return CheckResult(name="kubectl", severity="blocking", passed=True)
        return CheckResult(
            name="kubectl",
            severity="blocking",
            passed=False,
            message=f"wiz 不可用 (path={settings.wiz_path})",
            fix="安装 wiz CLI，或 blade-ai config set wiz_path <path>",
        )
    # is_executable (not bare shutil.which): kubectl_path may be configured
    # as a full path, and shutil.which mishandles a path-shaped cmd on
    # Windows before Python 3.12 (in scope via requires-python >=3.11).
    if is_executable(settings.kubectl_path):
        return CheckResult(name="kubectl", severity="blocking", passed=True)

    return CheckResult(
        name="kubectl",
        severity="blocking",
        passed=False,
        message="kubectl 不可用",
        fix="请安装 kubectl，或通过 blade-ai config set kubectl_path <path> 指定路径",
    )


def check_blade() -> CheckResult:
    """Check that ChaosBlade binary is executable (warning: kubectl exec fallback available).

    Pure detection — does NOT download. The CLI download step runs
    separately in ``run_command`` (with stderr progress), and the agent's
    ``blade_create`` ensures the binary on first injection. Keeping
    preflight side-effect-free is what lets the async TUI preflight stay
    within its 8s budget.
    """
    from chaos_agent.utils.blade_paths import is_executable
    if is_executable(settings._resolve_blade_path()):
        return CheckResult(name="blade", severity="warning", passed=True)

    return CheckResult(
        name="blade",
        severity="warning",
        passed=False,
        message="blade 未安装（首次注入时自动下载，或降级为 kubectl exec）",
        fix="首次注入会自动下载 ChaosBlade（约 51MB）；\n"
            "如需手动指定: blade-ai config set blade_path <path>",
    )


def check_transport_config() -> CheckResult:
    """Validate host-scope transport fields (ssh / kubewiz_host).

    K8s-scope config (kubeconfig file / kubewiz_k8s uuid+profile) is covered
    by check_kubeconfig.  This fills the gap for host-scope channels by
    delegating to the channel's own ``preflight`` — so a user who selects
    ssh/kubewiz_host but forgets a required field is told at boot time,
    not deep inside execution.
    """
    target = TransportTarget.from_state({})
    try:
        channel = TransportRegistry.resolve(target)
    except ValueError as exc:
        # Invalid channel override / under-specified scope — surface as a clean
        # boot-time failure instead of crashing with a traceback.
        return CheckResult(
            name="transport_config",
            severity="blocking",
            passed=False,
            message=f"传输通道配置无效: {exc}",
            fix="检查 kube_connection_mode 及对应通道所需字段（host_name / ssh_host 等）",
        )
    if channel.name not in ("ssh", "kubewiz_host"):
        # k8s-scope channels are validated by check_kubeconfig.
        return CheckResult(name="transport_config", severity="blocking", passed=True)
    errors = channel.preflight(target)
    if errors:
        return CheckResult(
            name="transport_config",
            severity="blocking",
            passed=False,
            message=f"{channel.name} 通道配置缺失: {'; '.join(errors)}",
            fix="通过 blade-ai config set 或环境变量补齐上述字段",
        )
    return CheckResult(name="transport_config", severity="blocking", passed=True)


# NOTE (Python-application faults): there is deliberately NO preflight check for
# the ChaosBlade Python agent. Two reasons, both structural:
#   1. The preflight signature carries no fault scope, so any check registered in
#      INJECT_CHECKS runs for EVERY drill and ``display`` would print its fix
#      guidance every time — telling k8s / host operators to attach an in-process
#      agent they do not need.
#   2. A local probe would check the WRONG machine: python-scope faults run over a
#      host channel (ssh / kubewiz_host), so the agent lives on the target
#      application's host, not on the machine running blade-ai.
# The precondition is surfaced where the scope IS known and the transport IS
# resolved: ``tools.blade_python.blade_python_create`` maps the CLI's
# "port not found" error to the prepare/attach guidance.


# ── Check lists per command ─────────────────────────────────────────

INJECT_CHECKS: list[Callable[[], CheckResult]] = [
    check_llm_api_key, check_kubeconfig, check_kubectl, check_transport_config, check_blade,
]
RECOVER_CHECKS: list[Callable[[], CheckResult]] = [
    check_llm_api_key, check_kubeconfig, check_kubectl, check_transport_config, check_blade,
]
LIST_CHECKS: list[Callable[[], CheckResult]] = [check_llm_api_key]
CONFIRM_CHECKS: list[Callable[[], CheckResult]] = [check_llm_api_key]
METRIC_CHECKS: list[Callable[[], CheckResult]] = []
CONFIG_CHECKS: list[Callable[[], CheckResult]] = []
VERSION_CHECKS: list[Callable[[], CheckResult]] = []


# ── Live (async) checks — TUI boot panel ─────────────────────────────
#
# These actually exercise dependencies (subprocess calls, HTTP round
# trips) instead of just asserting that config fields exist. They live
# next to the sync presence checks above so the panel and CLI matrices
# share one file and don't drift. The TUI boot screen consumes these
# via ``run_tui_checks``.

# Per-check timeouts. The async checks each get their own bound so a
# slow one doesn't block the whole gather; ``server/routes/preflight.py``
# wraps the gather with a global wait_for as the outer safety net.
#
# Why the LLM probe doesn't use ``settings.llm_read_timeout`` (180s):
#     That's the budget for a real chat completion — Qwen with
#     ``enable_thinking`` plus reasoning can easily take 5-10s end to
#     end. The preflight check doesn't need to run inference at all;
#     it only verifies that the configured API key authenticates and
#     the base URL is reachable. Hitting the much cheaper
#     ``GET /models`` endpoint gives the same auth/connectivity
#     guarantee in <500ms typical, so a 4s ceiling is generous for
#     even a cold TLS handshake on slow links.
_LLM_KEY_TIMEOUT_S = 4.0
_KUBECTL_VERSION_TIMEOUT_S = 5.0


@lru_cache(maxsize=4)
def _get_preflight_openai_client(
    model_name: str, base_url: str, api_key: str
) -> Any:
    """Cached ``openai.AsyncOpenAI`` for the lightweight preflight probe.

    The three arguments are **cache keys only** — they aren't forwarded
    to the constructor; settings is read live below. By keying on the
    live settings fingerprint, an in-process config change (TUI
    ``/config set`` writes the file then calls ``settings.reload()``
    — see ``config/config_store.py``) produces a cache miss and rebuilds
    the client with the new credentials. Without this, ``/doctor``
    after rotating an API key would still probe with the stale instance.

    We use the openai SDK directly instead of ``make_llm()`` /
    LangChain's ``ChatOpenAI``. The previous incarnation issued
    ``llm.ainvoke("ping")`` — which runs an actual chat completion
    end-to-end and on Qwen with ``enable_thinking`` regularly takes
    8-10s, far longer than any reasonable preflight budget. A
    ``client.models.list()`` GET asserts exactly what we care about
    (key auth + endpoint reachable) without paying for inference, so
    a 4s ceiling becomes plausible. langchain-openai depends on this
    same SDK, so the underlying transport / auth resolution is
    identical.

    maxsize=4 keeps memory bounded if the user flips configs a few
    times in a single session. Exceptions aren't cached.
    """
    del model_name, base_url, api_key  # cache keys only
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.api_base_url,
        timeout=_LLM_KEY_TIMEOUT_S,
        max_retries=0,
    )


def _self_check_timeout() -> int:
    """Bound the kubectl self-check timeout so startup never blocks too long.

    Must be strictly less than ``_PREFLIGHT_BUDGET_S`` (15s) so that
    individual kubectl checks timeout *before* the outer wait_for
    cancels the entire gather — otherwise a single slow check causes
    ALL results (including fast local checks) to be discarded.
    """
    return min(10, settings.timeout_kubectl or 15)


def _pretty_path(p: str | Path) -> str:
    """Collapse the user's home directory to ``~`` for display.

    The boot panel shows paths in the success line for kubeconfig /
    kubectl / blade / skills. Absolute paths under the home directory
    are usually long (>40 chars) and visually noisy; ``~/.kube/config``
    reads cleanly. Pure cosmetic — never use the return value for
    file I/O.
    """
    if not p:
        return ""
    s = str(p)
    home = os.path.expanduser("~")
    if home and s.startswith(home):
        return "~" + s[len(home):]
    return s


def _kubectl_base_cmd() -> tuple[list[str], Optional[str]]:
    """Build the kubectl prefix shared by every live kubectl check.

    Returns (cmd_prefix, kubeconfig_or_none). kubeconfig is returned for
    error-message display when the path turns out to be invalid.
    """
    cmd: list[str] = [settings._resolve_kubectl_path()]
    kubeconfig = expand_kubeconfig_path(settings.kubeconfig_path)
    if kubeconfig:
        cmd.extend(["--kubeconfig", kubeconfig])
    if settings.kube_context:
        cmd.extend(["--context", settings.kube_context])
    return cmd, kubeconfig or None


async def _kubectl_current_context(base_cmd: list[str]) -> str:
    """Return the active context name, or '' on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *base_cmd, "config", "current-context",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return stdout.decode(errors="replace").strip()
    except Exception:
        return ""
    return ""


async def _safe_kill(proc) -> None:
    """Reap a subprocess that timed out so we don't leak fds + zombie.

    All four live kubectl checks below share this cleanup pattern. We
    swallow broad Exception because cleanup failures are non-critical
    (process may already be dead, or in tests the ``proc`` may be a
    MagicMock whose ``wait`` isn't awaitable). The CheckResult the
    caller returns is what the user sees; this is best-effort hygiene.
    """
    try:
        proc.kill()
        await proc.wait()
    except Exception:
        pass


async def check_llm_api_key_live() -> CheckResult:
    """Validate the LLM API key + base_url with a lightweight liveness probe.

    Probe semantics: ``GET {base_url}/models`` via the OpenAI-compatible
    SDK. This asserts exactly what we want — the key authenticates and
    the endpoint is reachable — without paying for a chat completion
    round-trip. A real ``ainvoke("ping")`` on Qwen with
    ``enable_thinking`` regularly takes 8-10s and trips any reasonable
    preflight budget; ``models.list()`` typically completes in
    <500ms even on a cold connection.

    Result matrix:
      success           → passed (blocking severity, so failures gate the TUI)
      AuthenticationError (401/403) → key invalid, blocking fail
      NotFoundError (404)           → base_url shape wrong, blocking fail
      APIConnectionError            → network / DNS / TLS issue, blocking fail
      asyncio/APITimeoutError       → didn't finish in budget, warning fail
      anything else                 → warning fail with type name
    """
    if not settings.llm_api_key:
        return CheckResult(
            name="llm_api_key",
            severity="blocking",
            passed=False,
            message="llm_api_key 未配置",
            fix="blade-ai config set llm_api_key <your-key>\n"
                "或设置环境变量 BLADE_AI_LLM_API_KEY=<your-key>",
        )
    if not (settings.api_base_url or "").strip():
        return CheckResult(
            name="llm_api_key",
            severity="blocking",
            passed=False,
            message="api_base_url 未配置",
            fix="blade-ai config set api_base_url <url>",
        )

    try:
        client = _get_preflight_openai_client(
            settings.model_name,
            settings.api_base_url,
            settings.llm_api_key,
        )
    except Exception as e:
        return CheckResult(
            name="llm_api_key",
            severity="blocking",
            passed=False,
            message=f"openai client init 失败: {e}",
            fix="检查 api_base_url / llm_api_key 配置",
        )

    try:
        # asyncio.wait_for gives us an outer guard in case the SDK's
        # own timeout misbehaves; +1s buffer over the client timeout
        # so the SDK's own AbortController fires first and gives a
        # nicer error type than asyncio.TimeoutError.
        await asyncio.wait_for(
            client.models.list(),
            timeout=_LLM_KEY_TIMEOUT_S + 1.0,
        )
    except asyncio.TimeoutError:
        return CheckResult(
            name="llm_api_key",
            severity="warning",
            passed=False,
            message=f"LLM API 在 {_LLM_KEY_TIMEOUT_S:.0f}s 内未响应（key 可能仍然有效）",
            fix="检查网络连通性，或重试 /doctor",
        )
    except Exception as e:
        # Reuse map_error so the same exception → CheckResult mapping
        # the CLI uses at runtime applies here too. Override the name
        # back to "llm_api_key" so the boot card renders a stable row.
        mapped = map_error(e)
        if mapped is not None:
            return CheckResult(
                name="llm_api_key",
                severity=mapped.severity,
                passed=False,
                message=mapped.message,
                fix=mapped.fix,
            )
        return CheckResult(
            name="llm_api_key",
            severity="warning",
            passed=False,
            message=f"LLM probe raised {type(e).__name__}: {e}",
        )

    return CheckResult(
        name="llm_api_key",
        severity="blocking",
        passed=True,
        # Empty so the boot card falls back to the localised "passed"
        # label — per spec, the LLM key row stays terse since the
        # model name is already prominently displayed in the welcome
        # card's runtime block.
        message="",
    )


async def check_kubeconfig_live() -> CheckResult:
    """Let kubectl itself parse the kubeconfig — same parser the agent uses.

    ``kubectl config view --minify`` loads the file, resolves the
    active context, and prints the trimmed config or errors out. If
    kubectl accepts the file, every downstream agent call that depends
    on it will also accept it (no parser drift between preflight and
    runtime). Bonus: catches inter-field invariants (missing user, bad
    auth-info ref) that a pure YAML structure check would miss.
    """
    if resolve_channel_name() == "ssh":
        # Host-scope ssh channel needs no local kubeconfig — mirror the sync
        # check_kubeconfig exemption so ssh mode doesn't emit a spurious
        # blocking failure at TUI boot.
        return CheckResult(name="kubeconfig", severity="blocking", passed=True,
                           message="ssh mode (no kubeconfig needed)")
    if _is_kubewiz_channel():
        return CheckResult(name="kubeconfig", severity="blocking", passed=True,
                           message="kubewiz mode (no kubeconfig needed)")
    path_raw = settings.kubeconfig_path
    path = expand_kubeconfig_path(path_raw)
    if not path:
        default = os.path.expanduser("~/.kube/config")
        if not os.path.isfile(default):
            return CheckResult(
                name="kubeconfig",
                severity="blocking",
                passed=False,
                message="kubeconfig 未配置（默认 ~/.kube/config 也不存在）",
                fix="blade-ai config set kubeconfig_path <path>",
            )
        path = default
    if not os.path.isfile(path):
        return CheckResult(
            name="kubeconfig",
            severity="blocking",
            passed=False,
            message=f"kubeconfig 文件不存在: {path}",
            fix="blade-ai config set kubeconfig_path <path>",
        )

    base_cmd, _ = _kubectl_base_cmd()
    timeout = _self_check_timeout()
    try:
        proc = await asyncio.create_subprocess_exec(
            *base_cmd, "config", "view", "--minify",
            "-o", "jsonpath={range .clusters[*]}{.name}|{.cluster.server}{end}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        return CheckResult(
            name="kubeconfig",
            severity="blocking",
            passed=False,
            message="kubectl 不可用，无法验证 kubeconfig",
            fix="先确保 kubectl 已安装",
        )
    except asyncio.TimeoutError:
        await _safe_kill(proc)
        return CheckResult(
            name="kubeconfig",
            severity="blocking",
            passed=False,
            message=f"kubectl config view 在 {timeout}s 内未返回",
        )
    except Exception as e:
        return CheckResult(
            name="kubeconfig",
            severity="blocking",
            passed=False,
            message=f"kubeconfig 检查异常: {e}",
        )

    if proc.returncode != 0:
        err = (stderr or b"").decode(errors="replace").strip()[:200]
        return CheckResult(
            name="kubeconfig",
            severity="blocking",
            passed=False,
            message=f"kubectl 拒绝 kubeconfig: {err}",
            fix="检查 kubeconfig 内容、active context 和 user 引用",
        )

    summary = stdout.decode(errors="replace").strip()
    cluster_part = summary.split("|", 1)
    server = cluster_part[1] if len(cluster_part) > 1 else ""
    if not server:
        return CheckResult(
            name="kubeconfig",
            severity="blocking",
            passed=False,
            message=f"kubeconfig active context 未解析到 server URL: {summary or '(empty)'}",
        )

    return CheckResult(
        name="kubeconfig",
        severity="blocking",
        passed=True,
        # User asked to see the resolved kubeconfig path on success —
        # most useful when several config files live under ~/.kube/
        # and the user wants to confirm WHICH one this session picked.
        message=_pretty_path(path),
    )


async def _check_wiz_version() -> CheckResult:
    """Run ``wiz version`` and extract the client version."""
    import json as _json
    try:
        proc = await asyncio.create_subprocess_exec(
            settings._resolve_wiz_path(), "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except FileNotFoundError:
        return CheckResult(
            name="kubectl", severity="blocking", passed=False,
            message=f"wiz 不可用 (path={settings.wiz_path})",
            fix="安装 wiz CLI，或 blade-ai config set wiz_path <path>",
        )
    except asyncio.TimeoutError:
        await _safe_kill(proc)
        return CheckResult(
            name="kubectl", severity="blocking", passed=False,
            message="wiz version 超时",
        )
    except Exception as e:
        return CheckResult(
            name="kubectl", severity="blocking", passed=False,
            message=f"wiz version 调用异常: {e}",
        )

    if proc.returncode != 0:
        return CheckResult(
            name="kubectl", severity="blocking", passed=False,
            message="wiz version 退出码非零",
            fix="检查 wiz CLI 安装及登录状态",
        )

    try:
        obj = _json.loads(stdout.decode(errors="replace"))
        version = obj.get("client", "")
    except (ValueError, KeyError):
        version = ""

    msg = f"wiz v{version}" if version else "wiz ready"
    return CheckResult(name="kubectl", severity="blocking", passed=True, message=msg)


async def check_kubectl_version() -> CheckResult:
    """Run the same kubectl the agent uses, ask for its version."""
    if resolve_channel_name() == "ssh":
        # ssh channel executes on the remote host; local kubectl is
        # irrelevant (mirrors sync check_kubectl).
        return CheckResult(name="kubectl", severity="blocking", passed=True,
                           message="ssh mode (remote kubectl)")
    if _is_kubewiz_channel():
        return await _check_wiz_version()
    base_cmd, _ = _kubectl_base_cmd()
    try:
        proc = await asyncio.create_subprocess_exec(
            *base_cmd, "version", "--client", "-o", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_KUBECTL_VERSION_TIMEOUT_S
        )
    except FileNotFoundError:
        return CheckResult(
            name="kubectl",
            severity="blocking",
            passed=False,
            message=f"kubectl 不可用 (path={settings.kubectl_path})",
            fix="安装 kubectl，或 blade-ai config set kubectl_path <path>",
        )
    except asyncio.TimeoutError:
        await _safe_kill(proc)
        return CheckResult(
            name="kubectl",
            severity="blocking",
            passed=False,
            message=f"kubectl version 在 {_KUBECTL_VERSION_TIMEOUT_S:.0f}s 内未返回",
            fix="检查 kubectl 安装",
        )
    except Exception as e:
        return CheckResult(
            name="kubectl",
            severity="blocking",
            passed=False,
            message=f"kubectl 调用异常: {e}",
        )

    if proc.returncode != 0:
        err = (stderr or b"").decode(errors="replace").strip()[:200]
        return CheckResult(
            name="kubectl",
            severity="blocking",
            passed=False,
            message=f"kubectl version 退出码 {proc.returncode}: {err}",
            fix="安装或修复 kubectl",
        )

    # Resolve the absolute kubectl path so the success line shows
    # exactly which binary the agent will invoke at runtime. Falls
    # back to ``settings.kubectl_path`` (the literal config value) if
    # ``shutil.which`` can't resolve — should only happen on a race
    # where the binary was removed between the subprocess call above
    # succeeding and ``which`` running.
    resolved = shutil.which(settings.kubectl_path) or settings.kubectl_path
    return CheckResult(
        name="kubectl",
        severity="blocking",
        passed=True,
        message=_pretty_path(resolved),
    )


def _extract_kubectl_version(stdout_text: str, *, kind: str = "client") -> str:
    """Pull ``{kind}Version.gitVersion`` out of ``kubectl version -o json``.

    ``kind="client"`` returns the local kubectl binary's version (no
    network); ``kind="server"`` returns the cluster API server's
    version (requires API connectivity). Used by ``check_kubectl_version``
    and ``check_k8s_connectivity`` respectively.
    """
    import json

    try:
        obj = json.loads(stdout_text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""
    key = f"{kind}Version"
    block = obj.get(key) or {}
    git = block.get("gitVersion") or ""
    return git.lstrip("v") if isinstance(git, str) else ""


async def check_blade_version() -> CheckResult:
    """Reuse ``agent.env_info._get_blade_version`` for live blade probing.

    That helper already runs ``<blade_path> version`` with a timeout
    and is what the agent calls when building system-prompt env info,
    so the preflight result matches what the agent will see at runtime.
    Severity stays ``warning`` — a missing blade falls back to
    ``kubectl exec``, so it's not blocking.

    Pure detection — does NOT download. A 51MB download here would block
    the event loop and blow the 8s preflight budget (see
    server/routes/preflight.py). The binary is fetched off the hot path:
    the agent's ``blade_create`` calls ``ensure_chaosblade_async()`` on
    first injection.
    """
    from chaos_agent.agent.env_info import _get_blade_version

    try:
        raw = await _get_blade_version()
    except Exception as e:  # pragma: no cover — helper catches its own
        return CheckResult(
            name="blade",
            severity="warning",
            passed=False,
            message=f"blade 调用异常: {e}",
        )

    if not raw or raw == "not installed":
        return CheckResult(
            name="blade",
            severity="warning",
            passed=False,
            message="blade 未安装（首次注入时自动下载，或降级为 kubectl exec）",
            fix="首次注入会自动下载 ChaosBlade（约 51MB）；\n"
                "如需手动指定: blade-ai config set blade_path <path>",
        )

    # Show the resolved blade binary path so users can confirm which
    # blade (bundled vendor copy vs PATH-installed) the agent will
    # invoke. ``_resolve_blade_path`` is what env_info / direct_execute
    # actually call at runtime, so the path matches reality.
    resolved = settings._resolve_blade_path() or settings.blade_path
    return CheckResult(
        name="blade",
        severity="warning",
        passed=True,
        message=_pretty_path(resolved),
    )


def check_skills() -> CheckResult:
    """Check that skill files exist where the loader will look for them.

    Resolves the directory via skills.loader.get_skills_dir() so the result
    matches SkillRegistry.load_from_directory() — same path priority,
    same one-level iteration, no recursive os.walk.
    """
    actual_dir = get_skills_dir()

    if not actual_dir.is_dir():
        return CheckResult(
            name="skills",
            severity="warning",
            passed=False,
            message=f"Skills directory not found: {actual_dir}",
            fix="Skills will be loaded from package defaults",
        )

    count = sum(
        1 for d in actual_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    )
    disabled_count = len(settings.disabled_skills or [])

    if count == 0:
        return CheckResult(
            name="skills",
            severity="warning",
            passed=False,
            message=f"No skill files found in {actual_dir}",
            fix="Place SKILL.md folders under the skills directory, or run:\n"
                "  blade-ai skills install",
        )

    # Per spec: show the skills directory path on success — the count
    # is information the user can compute themselves once they know
    # where to look. Disabled count is appended only when non-zero so
    # the common case stays uncluttered.
    pretty = _pretty_path(actual_dir)
    if disabled_count > 0:
        message = f"{pretty} ({disabled_count} disabled)"
    else:
        message = pretty
    return CheckResult(name="skills", severity="warning", passed=True, message=message)


async def _check_host_connectivity() -> CheckResult:
    """Probe host reachability for host-scope channels (ssh / kubewiz_host).

    Runs a trivial no-op command (``echo``) through the resolved host
    channel — ``SSHChannel`` wraps it in ``ssh … -- echo``,
    ``KubewizHostChannel`` in ``wiz task exec --command "echo" --name
    <host>``.  Exit 0 proves the host is reachable and the auth/tunnel
    works, without touching any K8s API.  ``skip_guard=True`` because this
    is an internal probe (``echo`` isn't in the ToolGuard allowlist).

    A missing required field (e.g. ssh without ``ssh_host``) surfaces as a
    channel preflight failure inside ``execute_via_transport`` — exit code
    ``-1`` with the error in stderr — which we render as "not reachable".
    """
    target = TransportTarget.from_state({})
    chan = resolve_channel_name()
    timeout = _self_check_timeout()
    try:
        result = await execute_via_transport(
            ["echo", "blade-ai-preflight"],
            target,
            timeout=timeout,
            source="preflight-host-connectivity",
            skip_guard=True,
            # This check ASSERTS host reachability. Reached only from the
            # host-scope branch, so normally a tautology — but if the scope
            # says host while the resolved channel addresses a cluster, the
            # platform executor would answer ``echo`` successfully and
            # preflight would report the host as reachable. A false PASS
            # here is worse than no check.
            expect_profile=PROFILE_HOST,
        )
    except Exception as e:
        return CheckResult(
            name="host_connectivity", severity="blocking", passed=False,
            message=f"主机连通性探测异常: {str(e)[:120]}",
            fix="检查 host_name / ssh_host / ssh_user / ssh_key_path 配置及主机可达性",
        )
    if result.exit_code == 0:
        label = "ssh" if chan == "ssh" else "kubewiz-host"
        return CheckResult(
            name="host_connectivity", severity="blocking", passed=True,
            message=f"connected ({label})",
        )
    err = (result.stderr or result.stdout or "").strip()[:200]
    return CheckResult(
        name="host_connectivity", severity="blocking", passed=False,
        message=f"主机不可达: {err or ('exit ' + str(result.exit_code))}",
        fix="检查主机地址/端口/SSH 凭据，或 kubewiz host_name 与 profile 配置",
    )


async def check_k8s_connectivity() -> CheckResult:
    """Probe K8s cluster reachability AND fetch its server version."""
    if _is_host_scope_channel():
        # host-scope drill: no K8s cluster — probe the HOST instead
        # (mode-scoped check).  The row renders as ``host_connectivity``.
        return await _check_host_connectivity()
    from chaos_agent.tools.kubectl import exec_kubectl_raw

    timeout = _self_check_timeout()

    try:
        result = await exec_kubectl_raw("version", ["-o", "json"], timeout=timeout)
    except Exception as e:
        return CheckResult(
            name="k8s_connectivity", severity="blocking", passed=False,
            message=f"Unexpected error: {str(e)[:100]}", fix="Check cluster connectivity",
        )

    if result.exit_code == -1:
        # timeout or not found
        msg = result.stderr or "kubectl/wiz not found or timed out"
        fix = "Check kubeconfig path and cluster access:\n  blade-ai config set kubeconfig_path <path>"
        if "not found" in msg:
            fix = "Install kubectl or set path: blade-ai config set kubectl_path <path>"
        elif "timed out" in msg:
            fix = "Check network connectivity to K8s API server"
        return CheckResult(name="k8s_connectivity", severity="blocking", passed=False, message=msg, fix=fix)

    if result.exit_code == 0:
        server_version = _extract_kubectl_version(result.stdout, kind="server")
        msg = f"v{server_version}" if server_version else "connected"
        return CheckResult(name="k8s_connectivity", severity="blocking", passed=True, message=msg)

    error_msg = (result.stderr or result.stdout).strip()[:200]
    return CheckResult(
        name="k8s_connectivity", severity="blocking", passed=False,
        message=f"kubectl version failed: {error_msg}",
        fix="Check kubeconfig path and cluster access:\n  blade-ai config set kubeconfig_path <path>",
    )


def _operator_replicas_ready(stdout_text: str) -> bool:
    """Return True iff every deployment under chaosblade reports >=1 ready replica.

    Accepts space-separated integers like ``"1 1 1"``; empty input
    (no deployments) returns False.
    """
    tokens = stdout_text.split()
    if not tokens:
        return False
    for tok in tokens:
        try:
            if int(tok) <= 0:
                return False
        except ValueError:
            return False
    return True


def _parse_operator_jsonpath(stdout_text: str) -> tuple[list[str], list[str]]:
    """Split the combined name+replicas+images jsonpath output.

    Format produced by the kubectl jsonpath in ``check_chaosblade_operator``:

        <name>|<replicas>|<image>[,<image>]*\\n

    Only deployments whose name contains "chaosblade" are included.
    Returns ``(replica_tokens, image_tokens)``.
    """
    replicas: list[str] = []
    images: list[str] = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        name, rep, img_csv = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if "chaosblade" not in name:
            continue
        if rep:
            replicas.append(rep)
        if img_csv:
            for img in img_csv.split(","):
                img = img.strip()
                if img:
                    images.append(img)
    return replicas, images


def _parse_operator_json(stdout_text: str) -> tuple[list[str], list[str]]:
    """Parse ``kubectl get deploy -A -o json`` output for operator check.

    Searches all namespaces for deployments whose name contains
    "chaosblade" — the operator may be installed in any namespace.
    """
    import json as _json
    replicas: list[str] = []
    images: list[str] = []
    try:
        data = _json.loads(stdout_text)
        items = data.get("items", [])
        for item in items:
            name = item.get("metadata", {}).get("name", "")
            if "chaosblade" not in name:
                continue
            avail = item.get("status", {}).get("availableReplicas", 0)
            replicas.append(str(avail or 0))
            for container in item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
                img = container.get("image", "")
                if img:
                    images.append(img)
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return replicas, images


def _extract_operator_image_version(images: list[str]) -> str:
    """Pull a semver-looking tag out of a container image reference.

    Image refs look like ``ghcr.io/chaosblade-io/chaosblade-operator:1.7.4``
    or ``…:v1.7.4-amd64``. We take the substring after the LAST ``:``
    (port-in-host names like ``host:5000/img:tag`` still work because
    the port has no slash following the colon while the tag does
    follow the path) and return the version-looking prefix. Returns
    empty when nothing parseable is found.
    """
    if not images:
        return ""
    # Pick the operator's own image when multiple containers exist
    # (sidecars share the deployment). Match ``chaosblade-operator``
    # by name; fall back to the first image if no match.
    chosen = next(
        (img for img in images if "chaosblade-operator" in img),
        images[0],
    )
    # Drop the registry/repo prefix.
    tag_part = chosen.rsplit(":", 1)[-1] if ":" in chosen.split("/")[-1] else ""
    if not tag_part:
        return ""
    # Strip a leading ``v`` and grab the leading numeric.dotted prefix.
    cleaned = tag_part.lstrip("v")
    m = re.match(r"(\d+(?:\.\d+){0,3})", cleaned)
    return m.group(1) if m else ""


async def check_chaosblade_operator() -> CheckResult:
    """Check if ChaosBlade Operator is deployed and capture its image tag."""
    if _is_host_scope_channel():
        # host-scope drill: the ChaosBlade Operator is a K8s component and
        # is irrelevant here — skip (mode-scoped check).
        return CheckResult(
            name="chaosblade_operator", severity="warning", passed=True,
            message="host mode (Operator 检查已跳过)",
        )
    from chaos_agent.tools.kubectl import exec_kubectl_raw

    if _is_kubewiz_channel():
        v_args = ["deploy", "-A", "-o", "json"]
    else:
        _jsonpath = "jsonpath={range .items[*]}{.metadata.name}|{.status.availableReplicas}|{range .spec.template.spec.containers[*]}{.image},{end}{'\\n'}{end}"
        v_args = ["deploy", "-A", "-o", _jsonpath]

    timeout = _self_check_timeout()

    try:
        result = await exec_kubectl_raw("get", v_args, timeout=timeout)
    except Exception as e:
        return CheckResult(
            name="chaosblade_operator", severity="warning", passed=False,
            message=f"Unexpected error: {str(e)[:100]}", fix="Check cluster connectivity",
        )

    if result.exit_code == -1:
        msg = result.stderr or "timed out or kubectl not found"
        if "not found" in msg:
            return CheckResult(name="chaosblade_operator", severity="warning", passed=False,
                message="kubectl not found", fix="Install kubectl or set path: blade-ai config set kubectl_path <path>")
        return CheckResult(name="chaosblade_operator", severity="warning", passed=False,
            message=f"Operator check timed out ({timeout}s)", fix="Check cluster connectivity")

    if result.exit_code == 0:
        stdout_text = result.stdout.strip()
        if _is_kubewiz_channel():
            replica_tokens, image_tokens = _parse_operator_json(stdout_text)
        else:
            replica_tokens, image_tokens = _parse_operator_jsonpath(stdout_text)
        if _operator_replicas_ready(" ".join(replica_tokens)):
            version = _extract_operator_image_version(image_tokens)
            msg = f"v{version}" if version else "ready"
            return CheckResult(name="chaosblade_operator", severity="warning", passed=True, message=msg)
        return CheckResult(
            name="chaosblade_operator", severity="warning", passed=False,
            message="ChaosBlade Operator not ready (no available replicas)",
            fix="Check Operator status: kubectl get deploy -A | grep chaosblade",
        )

    return CheckResult(
        name="chaosblade_operator", severity="warning", passed=False,
        message="ChaosBlade Operator not deployed",
        fix="Install ChaosBlade Operator:\n"
            "  helm repo add chaosblade https://chaosblade-io.github.io/charts\n"
            "  helm install chaosblade-operator chaosblade/chaosblade-operator -n chaosblade --create-namespace\n"
            "Or: /doctor for guided installation",
    )


async def run_tui_checks() -> list[CheckResult]:
    """Run the TUI live preflight checks in parallel and return ordered results.

    The check set is **mode-scoped**: a K8s-scope channel (kubeconfig /
    kubewiz_k8s) validates the cluster surface (kubeconfig, kubectl,
    cluster connectivity, ChaosBlade Operator), while a host-scope channel
    (ssh / kubewiz_host) targets a bare machine — so it validates the host
    transport config and reachability instead, and drops the K8s-only rows
    that would otherwise render as meaningless green placeholders.

    ``asyncio.gather`` runs the selected checks concurrently; per-check
    timeouts bound individual checks and the endpoint's outer
    ``wait_for(15s)`` (see ``server/routes/preflight.py``) bounds the whole
    panel. Each spec's name is the fallback row name (used when a check
    raises) and the list order is the display order the boot card uses.
    """
    if _is_host_scope_channel():
        # Host-scope: no K8s cluster. Validate the host transport config
        # (ssh_host / host_name etc.) and probe host reachability. ``blade``
        # / ``skills`` stay because they are transport-agnostic capability
        # checks. ``check_transport_config`` is sync — run it off the loop.
        specs = [
            ("llm_api_key", check_llm_api_key_live()),
            ("transport_config", asyncio.to_thread(check_transport_config)),
            ("host_connectivity", _check_host_connectivity()),
            ("blade", check_blade_version()),
            ("skills", asyncio.to_thread(check_skills)),
        ]
    else:
        specs = [
            ("llm_api_key", check_llm_api_key_live()),
            ("kubeconfig", check_kubeconfig_live()),
            ("kubectl", check_kubectl_version()),
            ("blade", check_blade_version()),
            ("skills", asyncio.to_thread(check_skills)),
            ("k8s_connectivity", check_k8s_connectivity()),
            ("chaosblade_operator", check_chaosblade_operator()),
        ]
    fallback_names = tuple(name for name, _ in specs)
    raw = await asyncio.gather(
        *(coro for _, coro in specs),
        return_exceptions=True,
    )
    results: list[CheckResult] = []
    for name, r in zip(fallback_names, raw):
        if isinstance(r, CheckResult):
            results.append(r)
        elif isinstance(r, Exception):
            logger.warning(f"Preflight check {name!r} raised: {r}")
            results.append(CheckResult(
                name=name,
                severity="warning",
                passed=False,
                message=f"Check failed: {r}",
            ))
        else:  # pragma: no cover — gather can't return None
            results.append(CheckResult(
                name=name,
                severity="warning",
                passed=False,
                message="Check returned no result",
            ))
    return results


def needs_operator_install(results: list[CheckResult]) -> bool:
    """Check if any result indicates a missing ChaosBlade Operator."""
    return any(
        r.name == "chaosblade_operator" and not r.passed
        for r in results
    )


# ── Orchestration ───────────────────────────────────────────────────

def run(checks: list[Callable[[], CheckResult]]) -> list[CheckResult]:
    """Run a list of check functions and return all results."""
    results: list[CheckResult] = []
    for check in checks:
        try:
            results.append(check())
        except Exception as e:
            logger.debug("Pre-flight check raised exception: %s", e)
            results.append(CheckResult(
                name=check.__name__,
                severity="warning",
                passed=False,
                message=f"检查异常: {e}",
                fix="",
            ))
    return results


def display(results: list[CheckResult]) -> bool:
    """Format and print check results.

    Returns True if any blocking issue was found (caller should exit).
    """
    if not results:
        return False

    failures = [r for r in results if not r.passed]
    if not failures:
        return False

    for r in failures:
        prefix = "❌" if r.severity == "blocking" else "⚠️"
        print(f"{prefix} {r.message}", file=sys.stderr)
        if r.fix:
            for line in r.fix.split("\n"):
                print(f"   {line}", file=sys.stderr)

    blocking_count = sum(1 for r in failures if r.severity == "blocking")
    warning_count = sum(1 for r in failures if r.severity == "warning")

    parts = []
    if blocking_count:
        parts.append(f"{blocking_count} 个阻塞性")
    if warning_count:
        parts.append(f"{warning_count} 个警告")
    print(f"\n发现 {'、'.join(parts)}问题，请修复后重试。", file=sys.stderr)

    return blocking_count > 0


# ── Error mapping (runtime connectivity errors) ─────────────────────

def map_error(exc: Exception) -> Optional[CheckResult]:
    """Map a runtime exception to a user-friendly CheckResult.

    Handles LLM connectivity errors that occur during first API call.
    Returns None if the exception cannot be mapped (should be re-raised).
    """
    try:
        import openai
    except ImportError:
        openai = None  # type: ignore[assignment]

    exc_msg = str(exc)

    # Try to unwrap cause chain
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        inner = map_error(cause)
        if inner is not None:
            return inner

    # openai.AuthenticationError — 401, bad API key
    if openai and isinstance(exc, openai.AuthenticationError):
        return CheckResult(
            name="llm_api_key",
            severity="blocking",
            passed=False,
            message="LLM API key 无效 (401 Unauthorized)",
            fix="请检查 key 是否正确: blade-ai config set llm_api_key <correct-key>",
        )

    # openai.APIConnectionError — network / DNS failure
    if openai and isinstance(exc, openai.APIConnectionError):
        return CheckResult(
            name="api_base_url",
            severity="blocking",
            passed=False,
            message="无法连接 LLM API，请检查网络和 api_base_url 配置",
            fix="blade-ai config set api_base_url <url>",
        )

    # openai.NotFoundError — 404, bad endpoint
    if openai and isinstance(exc, openai.NotFoundError):
        return CheckResult(
            name="api_base_url",
            severity="blocking",
            passed=False,
            message="LLM API 端点不存在 (404)，请检查 api_base_url 配置",
            fix="blade-ai config set api_base_url <url>",
        )

    # Pattern match for common error messages (when openai is not importable)
    if "401" in exc_msg or "unauthorized" in exc_msg.lower() or "invalid api key" in exc_msg.lower():
        return CheckResult(
            name="llm_api_key",
            severity="blocking",
            passed=False,
            message=f"LLM API key 无效: {exc_msg}",
            fix="请检查 key 是否正确: blade-ai config set llm_api_key <correct-key>",
        )

    return None


# ── Command orchestration ──────────────────────────────────────────

def _ensure_blade_for_cli() -> None:
    """Download ChaosBlade for CLI inject/recover, with a stderr progress line.

    Best-effort and idempotent: returns instantly when blade is already
    resolvable; a download failure is non-fatal (the agent degrades to
    kubectl exec). Runs only in the sync CLI path, so the blocking
    download here is fine — there is no event loop or preflight budget.
    """
    from chaos_agent.utils.blade_paths import is_executable
    if is_executable(settings._resolve_blade_path()):
        return
    try:
        from chaos_agent.chaosblade_installer import (
            CHAOSBLADE_VERSION,
            ensure_chaosblade,
        )
    except Exception:
        return

    last_pct = [-1]

    def _progress(done: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        if pct != last_pct[0]:
            last_pct[0] = pct
            sys.stderr.write(
                f"\r  ⏳ 下载 ChaosBlade v{CHAOSBLADE_VERSION}: "
                f"{pct}% ({done / 1048576:.1f}/{total / 1048576:.1f} MB)"
            )
            sys.stderr.flush()

    sys.stderr.write("ChaosBlade 未安装，正在为首次使用下载...\n")
    sys.stderr.flush()
    try:
        ensure_chaosblade(on_progress=_progress)
        sys.stderr.write("\n  ✓ ChaosBlade 就绪\n")
    except Exception as e:
        sys.stderr.write(
            f"\n  ⚠ ChaosBlade 下载失败 ({e})；将尝试 kubectl exec 降级执行。\n"
        )
    sys.stderr.flush()


def run_command(
    checks: list[Callable[[], CheckResult]],
    local_fn: Callable[[Any], Awaitable[Any]],
    server_fn: Callable[[Any], Awaitable[Any]],
) -> Any:
    """Execute command with standard three-phase pattern.

    Phase 1: Pre-flight checks (local mode only).
    Phase 2: Execute local_fn or server_fn based on mode.
             - local mode: auto cleanup + map_error
             - server mode: direct execution (stateless HTTP)
    Returns: result dict for caller to format output (Phase 3).
    """
    mode = get_mode()

    # Phase 1
    if mode == "local":
        results = run(checks)
        if display(results):
            raise typer.Exit(code=1)
        # Pre-emptively fetch ChaosBlade for commands that need it
        # (inject/recover include check_blade). Done in the sync CLI path
        # — no event loop yet, no 8s budget — with a stderr progress line,
        # so the user sees the one-time 51MB download up front rather than
        # a silent pause mid-injection. blade_create still ensures it as a
        # safety net; both are idempotent.
        if check_blade in checks:
            _ensure_blade_for_cli()

    # Phase 2
    backend = get_backend()
    if mode == "local":
        async def _with_cleanup():
            try:
                return await local_fn(backend)
            finally:
                await backend.cleanup()
        try:
            return asyncio.run(_with_cleanup())
        except Exception as e:
            issue = map_error(e)
            if issue:
                display([issue])
                raise typer.Exit(code=1)
            raise
    else:
        return asyncio.run(server_fn(backend))
