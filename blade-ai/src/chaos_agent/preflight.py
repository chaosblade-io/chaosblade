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

logger = logging.getLogger(__name__)


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
    """Check that kubectl is executable."""
    path = settings.kubectl_path
    if shutil.which(path):
        return CheckResult(name="kubectl", severity="blocking", passed=True)

    return CheckResult(
        name="kubectl",
        severity="blocking",
        passed=False,
        message="kubectl 不可用",
        fix="请安装 kubectl，或通过 blade-ai config set kubectl_path <path> 指定路径",
    )


def check_blade() -> CheckResult:
    """Check that ChaosBlade binary is executable (warning: kubectl exec fallback available)."""
    blade = settings._resolve_blade_path()
    if blade and (os.path.isfile(blade) or shutil.which(blade)):
        return CheckResult(name="blade", severity="warning", passed=True)

    return CheckResult(
        name="blade",
        severity="warning",
        passed=False,
        message="blade 不可用",
        fix="将通过 kubectl exec 降级执行。建议安装 ChaosBlade 以获得完整能力:\n"
            "  blade-ai config set blade_path <path>",
    )


# ── Check lists per command ─────────────────────────────────────────

INJECT_CHECKS: list[Callable[[], CheckResult]] = [
    check_llm_api_key, check_kubeconfig, check_kubectl, check_blade,
]
RECOVER_CHECKS: list[Callable[[], CheckResult]] = [
    check_llm_api_key, check_kubeconfig, check_kubectl, check_blade,
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
# Why the LLM probe doesn't use ``settings.timeout_llm`` (180s):
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
    — see ``tui/config_store.py``) produces a cache miss and rebuilds
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
    """Bound the kubectl self-check timeout so startup never blocks 30s.

    Production runs use settings.timeout_kubectl; preflight prefers the
    smaller of (15, configured) to keep the panel responsive.
    """
    return min(15, settings.timeout_kubectl or 15)


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
    cmd: list[str] = [settings.kubectl_path]
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
    cluster_name = cluster_part[0] if cluster_part else ""
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


async def check_kubectl_version() -> CheckResult:
    """Run the same kubectl the agent uses, ask for its version."""
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
            message=f"blade 不可用 (path={settings._resolve_blade_path() or settings.blade_path})",
            fix="安装 ChaosBlade 并 blade-ai config set blade_path <path>\n"
                "未安装时 agent 会通过 kubectl exec 降级执行",
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


async def check_k8s_connectivity() -> CheckResult:
    """Probe K8s cluster reachability AND fetch its server version.

    We use ``kubectl version -o json`` rather than ``cluster-info`` —
    ``version`` requires API connectivity (same liveness signal as
    cluster-info) AND returns a structured ``serverVersion.gitVersion``
    we can surface in the success line. Failures get the same kubectl
    error text the old cluster-info path returned.
    """
    base_cmd, _ = _kubectl_base_cmd()
    timeout = _self_check_timeout()

    try:
        proc = await asyncio.create_subprocess_exec(
            *base_cmd, "version", "-o", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode == 0:
            server_version = _extract_kubectl_version(
                stdout.decode(errors="replace"), kind="server"
            )
            # API server URL is intentionally omitted from the success
            # message — the endpoint is sensitive (cluster IP / port)
            # and the self-check card lives in screenshots / share
            # logs. Server version alone is enough to confirm the
            # connection is healthy.
            msg = f"v{server_version}" if server_version else "connected"
            return CheckResult(name="k8s_connectivity", severity="blocking", passed=True, message=msg)

        error_msg = stderr.decode(errors="replace").strip()[:200]
        return CheckResult(
            name="k8s_connectivity",
            severity="blocking",
            passed=False,
            message=f"kubectl version failed: {error_msg}",
            fix="Check kubeconfig path and cluster access:\n  blade-ai config set kubeconfig_path <path>",
        )
    except asyncio.TimeoutError:
        await _safe_kill(proc)
        return CheckResult(
            name="k8s_connectivity",
            severity="blocking",
            passed=False,
            message=f"kubectl version timed out ({timeout}s)",
            fix="Check network connectivity to K8s API server",
        )
    except FileNotFoundError:
        return CheckResult(
            name="k8s_connectivity",
            severity="blocking",
            passed=False,
            message="kubectl not found",
            fix="Install kubectl or set path: blade-ai config set kubectl_path <path>",
        )
    except Exception as e:
        return CheckResult(
            name="k8s_connectivity",
            severity="blocking",
            passed=False,
            message=f"K8s connectivity check failed: {e}",
            fix="Check kubectl and kubeconfig configuration",
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
    """Split the combined replicas+images jsonpath output.

    Format produced by the kubectl jsonpath in ``check_chaosblade_operator``:

        <replicas>|<image>[,<image>]*\\n
        <replicas>|<image>[,<image>]*\\n
        ...

    Returns ``(replica_tokens, image_tokens)``. Each token list is
    flattened — order matches deployment-then-container.
    """
    replicas: list[str] = []
    images: list[str] = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        rep, sep, img_csv = line.partition("|")
        rep = rep.strip()
        if rep:
            replicas.append(rep)
        if sep and img_csv:
            for img in img_csv.split(","):
                img = img.strip()
                if img:
                    images.append(img)
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
    """Check if ChaosBlade Operator is deployed and capture its image tag.

    Uses a custom-columns query to grab BOTH the available replica
    counts and the container image strings in a single round-trip.
    Output format is two columns separated by a vertical bar:
    ``"<replicas> | <image>[,image]..."`` per row. Replicas drive
    pass/fail; image tag drives the success message.
    """
    base_cmd, _ = _kubectl_base_cmd()
    timeout = _self_check_timeout()

    try:
        proc = await asyncio.create_subprocess_exec(
            *base_cmd, "get", "deploy", "-n", "chaosblade",
            "-o", "jsonpath={range .items[*]}{.status.availableReplicas}|{range .spec.template.spec.containers[*]}{.image},{end}{'\\n'}{end}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode == 0:
            stdout_text = stdout.decode(errors="replace").strip()
            replica_tokens, image_tokens = _parse_operator_jsonpath(stdout_text)
            if _operator_replicas_ready(" ".join(replica_tokens)):
                version = _extract_operator_image_version(image_tokens)
                msg = f"v{version}" if version else "ready"
                return CheckResult(
                    name="chaosblade_operator",
                    severity="warning",
                    passed=True,
                    message=msg,
                )
            return CheckResult(
                name="chaosblade_operator",
                severity="warning",
                passed=False,
                message="ChaosBlade Operator not ready (no available replicas)",
                fix="Check Operator status: kubectl get pods -n chaosblade",
            )

        return CheckResult(
            name="chaosblade_operator",
            severity="warning",
            passed=False,
            message="ChaosBlade Operator not deployed",
            fix="Install ChaosBlade Operator:\n"
                "  helm repo add chaosblade https://chaosblade-io.github.io/charts\n"
                "  helm install chaosblade-operator chaosblade/chaosblade-operator -n chaosblade --create-namespace\n"
                "Or: /doctor for guided installation",
        )
    except asyncio.TimeoutError:
        await _safe_kill(proc)
        return CheckResult(
            name="chaosblade_operator",
            severity="warning",
            passed=False,
            message=f"Operator check timed out ({timeout}s)",
            fix="Check cluster connectivity",
        )
    except FileNotFoundError:
        return CheckResult(
            name="chaosblade_operator",
            severity="warning",
            passed=False,
            message="kubectl not found",
            fix="Install kubectl or set path: blade-ai config set kubectl_path <path>",
        )
    except Exception as e:
        return CheckResult(
            name="chaosblade_operator",
            severity="warning",
            passed=False,
            message=f"Operator check failed: {e}",
            fix="Install ChaosBlade Operator manually",
        )


async def run_tui_checks() -> list[CheckResult]:
    """Run all TUI live preflight checks in parallel and return ordered results.

    All seven checks are live. ``asyncio.gather`` runs them concurrently;
    per-check timeouts bound individual checks and the endpoint's outer
    ``wait_for(8s)`` (see ``server/routes/preflight.py``) bounds the whole
    panel. The order here is the display order the boot card uses.
    """
    raw = await asyncio.gather(
        check_llm_api_key_live(),
        check_kubeconfig_live(),
        check_kubectl_version(),
        check_blade_version(),
        asyncio.to_thread(check_skills),
        check_k8s_connectivity(),
        check_chaosblade_operator(),
        return_exceptions=True,
    )
    fallback_names = (
        "llm_api_key",
        "kubeconfig",
        "kubectl",
        "blade",
        "skills",
        "k8s_connectivity",
        "chaosblade_operator",
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
