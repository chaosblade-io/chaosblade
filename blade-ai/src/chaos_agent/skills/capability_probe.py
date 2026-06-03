"""Probe ChaosBlade binary capabilities via -h (help-only, zero injection).

Runs `blade create k8s ... -h` to extract the real capability matrix:
  resources (scope-target) → actions → flags + examples.

This is fed to the LLM during `capabilities sync` so it can generate
accurate structured commands using only real actions/flags.

Safety: ALL subprocess calls MUST end with "-h". An assertion enforces this.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

SCOPES = ("pod", "node", "container")
CORE_TARGETS = frozenset({"cpu", "mem", "disk", "network", "process", "pod", "file"})

# Flags that are infrastructure/transport (not experiment-specific).
# These are excluded from the per-action flag list given to the LLM.
_INFRA_FLAGS = frozenset({
    "kubeconfig", "kubectl-proxy", "token", "waiting-time",
    "namespace", "labels", "names", "timeout", "uid",
    "async", "debug", "endpoint", "nohup", "help",
    "cgroup-root", "chaosblade-deploy-mode", "chaosblade-download-url",
    "chaosblade-override", "chaosblade-path",
    "evict-count", "evict-percent", "evict-group",
})

_AVAIL_RE = re.compile(r"^\s{2,}(\S+)\s{2,}(.+?)\s*$")
_FLAG_RE = re.compile(r"^\s+(?:-\w,\s+)?--([\w-]+)(?:\s+\S+)?\s{2,}(.+?)\s*$")
_VERSION_RE = re.compile(r"Version:\s+(\S+)")


def _run_help(blade_path: str, *args: str) -> str:
    """Run `blade create k8s <args> -h` and return stdout+stderr.

    Safety: asserts the last argument is "-h" (help-only, never injects).
    """
    cmd_args = list(args)
    if not cmd_args or cmd_args[-1] != "-h":
        cmd_args.append("-h")
    assert cmd_args[-1] == "-h", "probe MUST be help-only, never inject"

    full_cmd = [blade_path, "create", "k8s"] + cmd_args
    try:
        r = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (r.stdout or "") + (r.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("blade probe failed for %s: %s", " ".join(cmd_args), e)
        return ""


def _parse_available(text: str) -> list[tuple[str, str]]:
    """Parse 'Available Commands:' section → [(name, description)]."""
    out: list[tuple[str, str]] = []
    in_block = False
    for line in text.splitlines():
        if line.strip().startswith("Available Commands:"):
            in_block = True
            continue
        if in_block:
            if line and not line[0].isspace():
                break
            m = _AVAIL_RE.match(line)
            if m:
                out.append((m.group(1), m.group(2).strip()))
    return out


def _parse_flags(text: str) -> list[dict]:
    """Parse 'Flags:' section → [{name, desc}] (experiment-specific only)."""
    out: list[dict] = []
    in_block = False
    for line in text.splitlines():
        if line.strip().startswith("Flags:"):
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("Global Flags:"):
                break
            m = _FLAG_RE.match(line)
            if m and m.group(1) not in _INFRA_FLAGS:
                out.append({"name": m.group(1), "desc": m.group(2).strip()})
    return out


def _parse_examples(text: str) -> list[str]:
    """Parse 'Examples:' section → [blade create ... command strings]."""
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.strip().startswith("Examples:"):
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("Flags:"):
                break
            stripped = line.strip()
            if stripped.startswith("blade create"):
                out.append(stripped)
    return out


def _get_version(blade_path: str) -> str:
    """Get blade version string."""
    try:
        r = subprocess.run(
            [blade_path, "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        m = _VERSION_RE.search(r.stdout or "")
        return m.group(1) if m else "unknown"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"


def probe_capabilities(blade_path: str) -> dict[str, Any]:
    """Probe blade binary for real capabilities. Help-only, zero injection.

    Returns:
        {
            "blade_version": "1.8.0",
            "resources": {"pod-cpu": ["fullload"], "node-disk": ["fill","burn"], ...},
            "flags": {"pod-cpu fullload": ["cpu-percent","cpu-count",...], ...},
            "examples": {"pod-cpu fullload": ["blade create k8s pod-cpu load --names ..."], ...}
        }
    """
    version = _get_version(blade_path)
    resources: dict[str, list[str]] = {}
    flags: dict[str, list[str]] = {}
    examples: dict[str, list[str]] = {}

    # Level 1: blade create k8s -h → Available Commands (resources)
    l1_text = _run_help(blade_path, "-h")
    for res_name, _ in _parse_available(l1_text):
        if "-" not in res_name:
            continue
        scope, _, target = res_name.partition("-")
        if scope not in SCOPES or target not in CORE_TARGETS:
            continue

        # Level 2: blade create k8s <resource> -h → actions
        l2_text = _run_help(blade_path, res_name, "-h")
        actions = [name for name, _ in _parse_available(l2_text)]
        if actions:
            resources[res_name] = actions

        # Level 3: blade create k8s <resource> <action> -h → flags + examples
        for action in actions:
            l3_text = _run_help(blade_path, res_name, action, "-h")
            key = f"{res_name} {action}"
            action_flags = _parse_flags(l3_text)
            action_examples = _parse_examples(l3_text)
            if action_flags:
                flags[key] = action_flags
            if action_examples:
                examples[key] = action_examples

    return {
        "blade_version": version,
        "resources": resources,
        "flags": flags,
        "examples": examples,
    }
