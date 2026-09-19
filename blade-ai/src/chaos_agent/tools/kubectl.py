"""kubectl CLI tool wrapper for LangGraph @tool function.

Unified kubectl tool that supports all subcommands via a single entry point.
Only ``kubeconfig`` is exposed as an explicit tool parameter; connection
identity (--context/--cluster) stays with the runtime channel — the LLM
never names a cluster (K7: under the kubewiz/single-cluster lock an
LLM-supplied cluster name is always invalid).

Two flavours bound at the graph layer:
  - ``kubectl`` (this module) — full surface (exec, delete, patch, ...);
    used in phase 2 / recover where mutation is expected. Being the superset,
    it also covers every read a read-only phase would do.
  - ``kubectl_read`` (this module) — read-only tool (get/describe/top/logs/... +
    read-only ``exec``/``debug``); the single observation tool for every
    read-only phase (intent / planning / verification). Constrains the
    subcommand at the signature level and gates ``exec``/``debug`` inner
    commands to read-only probes so no mutation slips through.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from langchain_core.tools import tool

from chaos_agent.config.settings import settings
from chaos_agent.errors import ToolGuardError, ToolTimeoutError
from chaos_agent.tools._tool_profiles import profile_for_tool
from chaos_agent.tools.guard import CommandResult
from chaos_agent.transports import (
    PROFILE_K8S,
    TransportTarget,
    display_via_transport,
    execute_via_transport,
)
from chaos_agent.utils.truncation import (
    TOOL_OUTPUT_SAFETY_VALVE_BYTES,
    apply_output_safety_valve,
)

logger = logging.getLogger(__name__)

_K8S_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# Output-format keys whose VALUE is a go template. Templates routinely
# contain spaces, single quotes (jsonpath string literals such as
# ``[?(@.type=='MemoryPressure')]``) and self-balanced actions
# (``{range}``/``{end}``), so they need template-aware tokenization
# instead of plain shell word splitting.
_TEMPLATE_OUTPUT_KEYS = ("jsonpath=", "go-template=")
# Locates a template KEY inside a word: at the word start (``jsonpath=...``)
# or after a flag's ``=`` (``-o=jsonpath=...`` / ``--output=go-template=...``).
_TEMPLATE_KEY_RE = re.compile(r"(?:^|=)(jsonpath|go-template)=")
# go-template block keywords: ``{range`` / ``{if`` / ``{with`` open a block
# closed by ``{end}``. Every action is brace-balanced on its own, so brace
# counting can NOT locate the end of a template — keyword pairing can.
_BLOCK_OPEN_RE = re.compile(r"\{(range|if|with)(?:\s|\)|$|\})")
_BLOCK_END_RE = re.compile(r"\{end\}")


def _template_word_depths(word: str) -> tuple[int, int]:
    """Net ``({, })`` and block ``(open, close)`` counts of one word."""
    return (
        word.count("{") - word.count("}"),
        len(_BLOCK_OPEN_RE.findall(word)) - len(_BLOCK_END_RE.findall(word)),
    )


def _split_args(args: str) -> list[str]:  # noqa: C901 — state machine
    r"""Split args string respecting shell quoting and go templates.

    Shell-like word splitting with two template-aware extensions, both
    verified live against a real cluster (task inject-c8cdd105):

    1. Quoted regions follow shell semantics: SINGLE-quote content is
       VERBATIM (nested quotes are not re-parsed — shlex consumes
       ``'[?(@.type=='X')]'`` as close/reopen pairs and strips the inner
       quotes kubectl's jsonpath needs, observed:
       ``unrecognized identifier MemoryPressure``); DOUBLE-quote regions
       honour backslash escapes (``\"`` ``\\`` ``\$`` ``\` `` yield the
       escaped char, ``\<newline>`` is a line continuation, any other
       ``\x`` stays verbatim — POSIX). Without the escape handling a
       legal ``-- sh -c "... \"...\" ..."`` exec payload fragmented
       into multiple argv tokens and the container's sh received
       word-split rubble (case #45 corrupt file 209B ≠ expected 206B;
       plan A fix, 2026-09-16).
    2. A ``jsonpath=`` / ``go-template=`` value is consumed as ONE token
       until its block keywords pair up (``{range}``…``{end}``) and braces
       balance — even when it contains spaces (observed: ``error parsing
       jsonpath {range, unclosed action`` from word splitting). An
       unmatched quote or an unterminated template consumes the rest of
       the input, so kubectl reports the REAL template error instead of a
       fragmentation artifact (fail-open; no silent misjoin — merge only
       starts right after a template key).
    """
    if not args:
        return []
    tokens: list[str] = []
    buf = ""
    i, n = 0, len(args)

    def flush() -> None:
        nonlocal buf
        if buf:
            tokens.append(buf)
            buf = ""

    def buf_ends_template_key() -> bool:
        return bool(re.search(r"(?:^|=)(jsonpath|go-template)=$", buf))

    def consume_template(
        start: int,
        keep_quotes: bool,
        skip_space: bool = False,
        prior: tuple[int, int] = (0, 0),
    ) -> int:
        """Consume the template value starting at ``start`` into buf.

        Returns the index just past the template. Characters are taken
        VERBATIM — inner quotes stay (kubectl's jsonpath needs its string
        literals: ``[?(@.type=='MemoryPressure')]``). Termination is
        decided at word boundaries OUTSIDE quotes, once braces balance
        and the block keywords (``{range}``/``{if}``/``{with}`` vs
        ``{end}``) pair up; on truncation, consumes to end of input so
        kubectl reports the REAL template error, not a fragmentation
        artifact. One matching pair of OUTER delimiter quotes is stripped
        (shell semantics — the form verified live against the cluster).
        ``skip_space`` continues a value already unbalanced inside buf
        across the following whitespace (``jsonpath={range .items}``),
        preserving the whitespace; ``prior`` is the (brace, block) depth
        the partial already in buf contributes.
        """
        nonlocal buf
        j = start
        out: list[str] = []
        if skip_space:
            # Preserve the whitespace: ``{range .items}`` needs the space.
            while j < n and args[j].isspace():
                out.append(args[j])
                j += 1
        bd, kd = prior
        quote: str | None = None
        first_quote = -1
        last_quote = -1
        consumed_word = False
        while j < n:
            c = args[j]
            if quote is not None:
                out.append(c)
                if c == quote:
                    last_quote = len(out) - 1
                    quote = None
                j += 1
                continue
            if c in ("'", '"'):
                quote = c
                out.append(c)
                if first_quote < 0:
                    first_quote = len(out) - 1
                j += 1
                continue
            if c.isspace():
                if consumed_word and bd == 0 and kd == 0:
                    break
                out.append(c)
                j += 1
                continue
            if _BLOCK_END_RE.match(args, j):
                kd -= 1
            elif _BLOCK_OPEN_RE.match(args, j):
                kd += 1
            if c == "{":
                bd += 1
            elif c == "}":
                bd -= 1
            out.append(c)
            consumed_word = True
            j += 1
        text = "".join(out)
        if (
            not keep_quotes
            and first_quote >= 0
            and first_quote < last_quote == len(text) - 1
            and text[first_quote] == text[last_quote]
            and text[:first_quote].strip() == ""
        ):
            # Drop the OUTER delimiter pair only; inner quotes survive.
            text = text[:first_quote] + text[first_quote + 1:last_quote]
        buf += text
        return j

    while i < n:
        ch = args[i]
        if ch.isspace():
            flush()
            i += 1
            continue
        if ch in ("'", '"'):
            if buf_ends_template_key():
                # Template value, quote opens here: keep inner quotes.
                i = consume_template(i, keep_quotes=True)
                continue
            # Plain quoted region: content until the matching quote; an
            # unmatched quote runs to end of input (never raise, never fall
            # back to whitespace-splitting quoted content). Single-quote
            # regions stay VERBATIM (POSIX: no escape concept inside '...').
            # Double-quote regions honour shell backslash escapes (#45
            # 方案 A): \" \\ \$ \` yield the escaped char, \<newline> is a
            # line continuation (both dropped); any other \x stays
            # verbatim — so a legal escaped exec payload reaches the
            # container's sh as ONE argv token instead of fragments.
            i += 1
            part: list[str] = []
            while i < n and args[i] != ch:
                if (
                    ch == '"'
                    and args[i] == "\\"
                    and i + 1 < n
                    and args[i + 1] in ('"', "\\", "$", "`", "\n")
                ):
                    if args[i + 1] != "\n":
                        part.append(args[i + 1])
                    i += 2  # escaped char kept; line continuation dropped
                    continue
                part.append(args[i])
                i += 1
            i += 1  # skip closing quote (or step past EOF when unmatched)
            buf += "".join(part)
            continue
        start = i
        while i < n and not args[i].isspace() and args[i] not in ("'", '"'):
            i += 1
        word = args[start:i]
        buf += word
        m = _TEMPLATE_KEY_RE.search(word)
        if m:
            # Key and (maybe partial) template in one word. Bare key
            # (``jsonpath=`` with the value following) or an unbalanced
            # value: consume the template continuation as one token. An
            # unbalanced partial inside the word continues across the
            # following space (``jsonpath={range .items}``).
            partial = word[m.end():]
            bd, kd = _template_word_depths(partial)
            unbalanced = bd > 0 or kd > 0
            if not partial or unbalanced:
                i = consume_template(
                    i,
                    keep_quotes=False,
                    skip_space=bool(partial) and unbalanced,
                    prior=(bd, kd),
                )
    flush()
    return tokens


def _namespace_from_args(args: list[str]) -> str:
    """Return an explicit kubectl namespace flag, if present.

    R46: the scan stops at pflag's TRUE separator — a ``--`` in a
    value-taking flag's value slot (``--profile-output --``) is that
    flag's VALUE, not a boundary, and an explicit ``-n`` after it is
    still a real flag. A line with no true separator keeps the legacy
    first-``--`` boundary (the separator-less shape refuses upstream).
    """
    from chaos_agent.tools._readonly_facts import exec_separator_index

    separator = exec_separator_index(args)
    if separator is None and "--" in args:
        separator = args.index("--")
    limit = separator if separator is not None else len(args)
    for index in range(limit):
        token = args[index]
        if token in ("-n", "--namespace") and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--namespace="):
            return token.split("=", 1)[1]
    return ""


def _debug_target_node_name(processed_args: list[str]) -> str:
    """The target node for a node-scoped ``kubectl debug`` call, else ``""``.

    Only node-scope calls create a discoverable ``node-debugger-*`` pod, so
    the parse-failure discovery fallback needs the node name to filter by
    ``spec.nodeName`` (project convention). Handles both ``node/<name>`` and
    the two-token ``node <name>`` forms. Flags that take a separate value
    (``-n default``, ``--image busybox``) must skip BOTH tokens — otherwise
    the flag's value is misread as the first positional and the discovery
    fallback is silently disabled for calls like ``-n default node/a``.

    R46: arity comes from the SHARED table
    (``_readonly_facts.kubectl_flag_takes_value``). The hand-written
    7-item set this replaces missed the globals (``--profile-output
    out.json``, ``--request-timeout 30s``, ``-v 6``, ``--as admin``…) and
    read the flag's VALUE as the first positional — the same silently-
    disabled discovery as the ``-n default node/a`` case above.
    """
    from chaos_agent.tools._readonly_facts import kubectl_flag_takes_value

    i = 0
    while i < len(processed_args):
        tok = processed_args[i]
        if tok == "--":
            break
        if kubectl_flag_takes_value(tok):
            i += 2  # skip flag AND its value
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok.startswith("node/"):
            return tok[len("node/"):]
        if tok == "node" and i + 1 < len(processed_args):
            return processed_args[i + 1]
        return ""  # first positional is a pod → pod-scoped debug
    return ""


def _debug_target_pod_name(processed_args: list[str]) -> str:
    """The target Pod name for a Pod-scoped ``kubectl debug`` (ephemeral
    container) call, or ``""`` for a node-scoped debug.

    ``kubectl debug <pod> --image=... --target=<c> -- <cmd>`` attaches an
    EPHEMERAL CONTAINER to an EXISTING pod. The first positional token is that
    pod. A node debug (``kubectl debug node/<node> ...``) is NOT pod-scoped and
    returns ``""``. Distinguishing the two is a SAFETY boundary: the target pod
    is the user's workload and must NEVER be deleted during cleanup, unlike a
    node-debugger pod which the tool creates and owns.

    ``--copy-to`` also returns ``""``: kubectl documents it as "Create a copy of
    the target Pod with this name", so it produces a NEW tool-owned pod (whose
    name kubectl does print) rather than an ephemeral container. Routing it here
    would look for ephemeral containers that never appear and leave the copy
    running with nothing tracking it.

    Value-consuming flags in SPACE form (``--image busybox``, ``-n ns``) must
    have their VALUE skipped — otherwise the value is mistaken for the pod name
    (e.g. ``--image busybox p0`` would return ``busybox``, or ``-n ns p0`` would
    return ``ns``). ``--flag=value`` form is a single ``-``-prefixed token and
    is already skipped as a flag.

    R46: arity comes from the SHARED table
    (``_readonly_facts.kubectl_flag_takes_value``). The hand-written
    12-item set this replaces missed the globals (``--profile-output
    out.json``, ``--request-timeout 30s``, ``-v 6``, ``--as admin``,
    ``--cache-dir``…) and read the flag's VALUE as the "target pod": a
    node-scoped call (``debug --request-timeout 30s node/n1 -- sleep
    3600``) took the ephemeral-container arm, returned an error naming a
    pod that does not exist, and the created node-debugger pod — reported
    through neither that arm nor ``[debug-pod-meta]`` — leaked
    unregistered.
    """
    from chaos_agent.tools._readonly_facts import kubectl_flag_takes_value

    # Copy mode creates a pod, not an ephemeral container (see docstring).
    for tok in processed_args:
        if tok == "--":
            break
        if tok == "--copy-to" or tok.startswith("--copy-to="):
            return ""
    i = 0
    while i < len(processed_args):
        tok = processed_args[i]
        if tok == "--":
            break
        if kubectl_flag_takes_value(tok):
            i += 2  # skip flag AND its value
            continue
        if tok.startswith("-"):
            i += 1  # ``--flag=value`` or a valueless flag
            continue
        # First positional. ``node/<name>`` (or ``node <name>``) is node-scope.
        if tok == "node" or tok.startswith("node/"):
            return ""
        return tok
    return ""


def _parse_ephemeral_container_name(pod_json: str) -> str:
    """Newest ephemeral container name from a target pod's JSON.

    Pod-scoped ``kubectl debug`` prints no container name on stdout — kubectl
    generates a random ``debugger-xxxxx`` and records it only in the pod
    object. The LAST entry of ``spec.ephemeralContainers`` is the one just
    created (kubectl appends). ``status.ephemeralContainerStatuses`` must NOT
    be used for this: the API returns that list in ALPHABETICAL order (cluster
    evidence: spec order ``crzj6 5wzdw ... z4gl7`` vs status order
    ``2hhgs 5wzdw ... z4gl7``), so ``names[-1]`` picks a stale container and
    the wait reports "did not start: Completed" against a container from a
    previous drill.
    """
    try:
        data = json.loads(pod_json)
    except (TypeError, json.JSONDecodeError):
        return ""
    spec_names = [
        ec.get("name", "")
        for ec in (data.get("spec") or {}).get("ephemeralContainers") or []
        if isinstance(ec, dict) and ec.get("name")
    ]
    if spec_names:
        return spec_names[-1]
    statuses = (data.get("status") or {}).get("ephemeralContainerStatuses") or []
    names = [s.get("name", "") for s in statuses if isinstance(s, dict) and s.get("name")]
    return names[-1] if names else ""


#: Cluster vs local clock skew tolerance for timestamp attribution. Stale
#: containers from earlier drills are minutes-to-hours old; 30s keeps them
#: excluded while absorbing apiserver/local clock drift.
_CLOCK_SKEW_TOLERANCE_S = 30.0

#: Substrings of the kubectl error text that identifies "exec into a pod
#: whose lifecycle has ended". Collected in one place because they are
#: Wording-dependent: if kubectl ever rephrases the message, the keep-alive
#: guidance silently degrades back to the bare error (fail-safe) — a single
#: edit site is the mitigation. ``node-debugger-`` is NOT part of this
#: tuple: it is the upstream naming contract of ``kubectl debug node/``
#: (also relied on by the debug-pod discovery fallback), not an error-text
#: substring.
_COMPLETED_POD_EXEC_ERR_SUBSTRINGS = (
    "cannot exec into a container in a",
    "completed pod",
    "terminated state",
)


def _started_at_epoch(state: dict) -> float:
    """Epoch seconds of the container's ``startedAt`` (0.0 if absent/unparseable)."""
    for key in ("running", "terminated"):
        ts = (state.get(key) or {}).get("startedAt")
        if ts:
            try:
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
    return 0.0


def _time_attributed(started_epoch: float, dispatch_ts: float) -> bool:
    """Timestamp attribution for when the pre-dispatch snapshot FAILED.

    A candidate whose ``startedAt`` falls at/after dispatch (within skew
    tolerance) is the container this call created — stale containers from
    earlier drills started minutes-to-hours ago, far outside the tolerance.
    """
    return bool(
        started_epoch
        and dispatch_ts
        and started_epoch >= dispatch_ts - _CLOCK_SKEW_TOLERANCE_S
    )


async def _ephemeral_spec_names(
    target_pod: str,
    namespace: str,
    kubeconfig: str,
) -> set[str] | None:
    """Ephemeral container names present on the pod BEFORE dispatch.

    Snapshot basis for attributing the container a Pod-scoped debug call
    creates: after dispatch the NEW container is the spec entry absent from
    this set. Guards against both the alphabetical-status trap and foreign
    actors creating ephemeral containers concurrently. ``None`` means the
    fetch FAILED (not "the pod has none") — the caller must then treat every
    candidate as unconfirmed rather than as freshly created.
    """
    cmd = build_kubectl_cmd(
        "get", ["pod", target_pod, "-n", namespace, "-o", "json"],
        kubeconfig,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
    except Exception:
        logger.debug("Pre-debug snapshot failed for %s/%s", namespace, target_pod, exc_info=True)
        return None
    if result.exit_code != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    return {
        ec.get("name", "")
        for ec in (data.get("spec") or {}).get("ephemeralContainers") or []
        if isinstance(ec, dict) and ec.get("name")
    }


def _extract_debug_profile(v_args: str) -> str:
    """The ``--profile`` value from a debug ``v_args`` (``""`` if absent).

    Carried into debug-pod-meta so carrier resolution and diagnostics see the
    profile the Agent requested.
    """
    try:
        tokens = shlex.split(v_args)
    except ValueError:
        return ""
    for i, tok in enumerate(tokens):
        if tok == "--profile" and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--profile="):
            return tok.split("=", 1)[1]
    return ""




async def _resolve_effective_namespace(
    kubeconfig: str,
) -> str:
    """Resolve the namespace selected by the active transport context.

    This works for both direct kubectl and kubewiz because it asks the same
    transport that will create the debug pod.  Falling back to ``default`` is
    Kubernetes-compatible, but only after the live context produced no value.
    """
    cmd = build_kubectl_cmd(
        "config",
        ["view", "--minify", "-o", "jsonpath={..namespace}"],
        kubeconfig,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
    except Exception:
        logger.debug("Failed to resolve active kubectl namespace", exc_info=True)
        return "default"
    namespace = result.stdout.strip().strip("'\"") if result.exit_code == 0 else ""
    if namespace and _K8S_NAMESPACE_RE.fullmatch(namespace):
        return namespace
    return "default"


async def _debug_pod_metadata(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
) -> tuple[dict, str]:
    """Read the authoritative identity and status of a created debug pod."""
    cmd = build_kubectl_cmd(
        "get", ["pod", pod_name, "-n", namespace, "-o", "json"],
        kubeconfig,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
    except Exception as exc:
        return {}, str(exc)
    if result.exit_code != 0:
        return {}, result.stderr or result.stdout
    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return {}, f"invalid pod JSON: {exc}"

    metadata = data.get("metadata") or {}
    spec = data.get("spec") or {}
    status = data.get("status") or {}
    containers = spec.get("containers") or []
    container_statuses = status.get("containerStatuses") or []
    waiting_reasons = []
    # One-shot ``kubectl debug ... -- CMD`` pods end in Succeeded/Failed; the
    # command's exit code / termination reason live in the first container's
    # ``terminated`` state. ``None`` = container has not terminated yet.
    exit_code = None
    terminated_reason = ""
    for container_status in container_statuses:
        state_obj = container_status.get("state") or {}
        waiting = state_obj.get("waiting") or {}
        if waiting.get("reason"):
            waiting_reasons.append(waiting["reason"])
        terminated = state_obj.get("terminated") or {}
        if exit_code is None and terminated:
            exit_code = terminated.get("exitCode")
            terminated_reason = terminated.get("reason") or ""
    return {
        "name": metadata.get("name") or pod_name,
        "namespace": metadata.get("namespace") or namespace,
        "uid": metadata.get("uid") or "",
        "node": spec.get("nodeName") or "",
        "privileged": any(
            (container.get("securityContext") or {}).get("privileged") is True
            for container in containers
            if isinstance(container, dict)
        ),
        "phase": status.get("phase") or "Unknown",
        # Pod-level failure cause (Evicted / nodeAffinity / unschedulable).
        # Distinct from the container-level fields below: a pod rejected
        # BEFORE its container ever starts (disk-pressure admission/taint
        # blocks, #29 verify) has empty containerStatuses — waiting_reasons
        # and the terminated state stay empty and the ONLY failure signal
        # lives in status.reason/status.message. Without them the model
        # must round-trip node conditions + events to attribute a failure
        # whose reason this data source already knew.
        "reason": status.get("reason") or "",
        "message": status.get("message") or "",
        "ready": bool(container_statuses) and all(
            container_status.get("ready") is True
            for container_status in container_statuses
        ),
        "waiting_reasons": waiting_reasons,
        "exit_code": exit_code,
        "terminated_reason": terminated_reason,
    }, ""


async def _wait_for_created_debug_pod(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
) -> tuple[bool, dict, str]:
    """Wait until a created debug pod is executable and return its identity."""
    wait_seconds = min(60, max(1, int(settings.timeout_kubectl_exec)))
    wait_cmd = build_kubectl_cmd(
        "wait",
        [
            "--for=condition=Ready",
            f"pod/{pod_name}",
            "-n", namespace,
            f"--timeout={wait_seconds}s",
        ],
        kubeconfig,
    )
    try:
        target = TransportTarget.from_state({})
        wait_result = await execute_via_transport(
            wait_cmd, target, timeout=wait_seconds + 10, expect_profile=PROFILE_K8S,
        )
        wait_error = "" if wait_result.exit_code == 0 else (
            wait_result.stderr or wait_result.stdout
        )
    except Exception as exc:
        wait_result = None
        wait_error = str(exc)

    metadata, metadata_error = await _debug_pod_metadata(
        pod_name, namespace, kubeconfig,
    )
    if metadata_error:
        return False, {}, metadata_error
    if not metadata.get("uid") or not metadata.get("node"):
        return False, metadata, "debug pod identity is incomplete"
    if (
        wait_result is not None
        and wait_result.exit_code == 0
        and metadata.get("phase") == "Running"
        and metadata.get("ready") is True
    ):
        return True, metadata, ""
    reasons = ", ".join(metadata.get("waiting_reasons") or [])
    detail = reasons or metadata.get("phase") or wait_error or "not ready"
    return False, metadata, f"debug pod did not become Ready: {detail}"


def _debug_has_oneshot_command(processed_args: list[str]) -> bool:
    """Whether a debug call is ONE-SHOT COMMAND mode (``... -- CMD ...``).

    Command mode runs CMD once and the pod terminates (Succeeded/Failed) — the
    ``condition=Ready`` gate used by interactive mode NEVER becomes true there,
    so waiting for it is a guaranteed false negative (task-29848471 k3 false
    alarm). Two forms stay INTERACTIVE despite having tokens after ``--``:

      - ``-- sleep <N>`` — the project's documented convention for node debug
        (``MUST append -- sleep 3600``): a keep-alive placeholder so the
        caller can ``kubectl exec`` afterwards. The pod stays Running/Ready.
        Equivalent keep-alive spellings stay interactive too: an absolute
        path sleep (``/bin/sleep 3600``) and a PURE sleep wrapped in a shell
        (``sh -c 'sleep 3600'``). A composite script (``sh -c 'sleep 5 &&
        df -h'``) is still one-shot — it terminates on its own.
      - ``-it`` / ``-i`` / ``-t`` flags — interactive attach by intent.

    A trailing bare ``--`` (no tokens) is interactive too.
    """
    # R45: the boundary is pflag's OWN — a ``--`` in a value-taking flag's
    # value slot (``-c --`` / ``--image --`` / ``--profile-output --``) is
    # that flag's VALUE, never the separator. The legacy first-standalone-
    # ``--`` slice started the command view inside a flag value and read
    # ``-- sleep 3600`` as "not keep-alive", routing a keep-alive carrier
    # into the one-shot arm — which deletes the pod after its terminal
    # poll (a live carrier killed). The shared value-aware locator; a line
    # with no TRUE separator keeps the legacy boundary (the R44
    # separator-less shape is refused upstream).
    from chaos_agent.tools._readonly_facts import exec_separator_index

    separator = exec_separator_index(processed_args)
    if separator is None and "--" in processed_args:
        separator = processed_args.index("--")
    if separator is None:
        return False  # no `--` at all — interactive
    interactive_flags = {"-it", "-i", "-t", "--stdin", "--tty"}
    for tok in processed_args[:separator]:
        if tok in interactive_flags:
            return False
    command = processed_args[separator + 1:]
    if not command:
        return False  # trailing bare `--` — interactive
    return not _is_keepalive_sleep(command)


def _is_keepalive_sleep(command: list[str]) -> bool:
    """Whether the debug CMD is a pure keep-alive sleep placeholder.

    Covers the documented bare form (``sleep 3600``), the absolute-path
    variant (``/bin/sleep``), and a shell wrapping NOTHING BUT a sleep
    (``sh -c 'sleep 3600'`` / ``bash -c "sleep 60"``). Anything composite
    (``sleep 30 && df -h``) returns False — it terminates on its own and
    belongs to one-shot mode. Misclassifying a keep-alive as one-shot
    kills the carrier pod the caller is about to exec into.
    """
    if not command:
        return False
    base = command[0].rsplit("/", 1)[-1]
    if base == "sleep":
        return True
    if base in ("sh", "bash") and "-c" in command:
        script = command[command.index("-c") + 1] if command.index("-c") + 1 < len(command) else ""
        return bool(re.fullmatch(r"\s*sleep\s+\d+\s*", script or ""))
    return False


async def _wait_for_debug_pod_terminal(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
) -> tuple[bool, dict, str]:
    """Poll a one-shot debug pod until its phase is terminal.

    Returns ``(terminal, metadata, error)``. Terminal means ``Succeeded`` or
    ``Failed`` (the CMD ran to completion — success/failure of the COMMAND is
    in ``metadata['exit_code']``, not here); a still-running pod at the budget
    limit returns ``(False, last_metadata, ...)``. Budget is capped at 120s —
    debug one-shots are probes, not long jobs.
    """
    wait_seconds = min(120, max(1, int(settings.timeout_kubectl_exec)))
    deadline = asyncio.get_running_loop().time() + wait_seconds
    last_error = ""
    metadata: dict = {}
    while True:
        metadata, meta_error = await _debug_pod_metadata(
            pod_name, namespace, kubeconfig,
        )
        if meta_error:
            return False, {}, meta_error
        phase = metadata.get("phase") or "Unknown"
        if phase in ("Succeeded", "Failed"):
            return True, metadata, ""
        last_error = f"still in phase {phase}"
        if asyncio.get_running_loop().time() >= deadline:
            return False, metadata, (
                f"one-shot debug pod did not terminate within {wait_seconds}s "
                f"({last_error})"
            )
        await asyncio.sleep(2)


async def _debug_pod_logs_tail(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
    tail: int = 20,
) -> str:
    """Best-effort last ``tail`` log lines of a terminated debug pod."""
    cmd = build_kubectl_cmd(
        "logs", [pod_name, "-n", namespace, f"--tail={tail}"],
        kubeconfig,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
    except Exception:
        logger.debug("Failed to fetch debug pod logs %s/%s", namespace, pod_name, exc_info=True)
        return ""
    if result.exit_code != 0:
        return ""
    return (result.stdout or "").strip()


def _select_created_ephemeral(
    pod_json: str, pre_existing: set[str] | None
) -> tuple[str, bool]:
    """The ephemeral container THIS debug call created.

    Returns ``(name, attributed)``. Spec order is creation order, so the
    newest entry not present in the pre-dispatch snapshot is the
    attribution; ``attributed=True`` means the name was diffed against a
    SUCCESSFULLY fetched snapshot. With ``pre_existing=None`` (snapshot
    fetch failed) every candidate is unconfirmed — the caller must NOT treat
    terminal states of such a container as final, because it may be a stale
    container from an earlier session and the one this call actually created
    may simply not be visible yet (API lag).
    """
    try:
        data = json.loads(pod_json)
    except (TypeError, json.JSONDecodeError):
        return "", False
    spec_names = [
        ec.get("name", "")
        for ec in (data.get("spec") or {}).get("ephemeralContainers") or []
        if isinstance(ec, dict) and ec.get("name")
    ]
    if pre_existing is not None:
        fresh = [n for n in spec_names if n not in pre_existing]
        if fresh:
            return fresh[-1], True
        # Valid snapshot, no new entry: the created container is not visible
        # yet — every candidate is stale.
        return "", False
    # Snapshot fetch failed: best guess only.
    if spec_names:
        return spec_names[-1], False
    return _parse_ephemeral_container_name(pod_json), False


async def _wait_for_ephemeral_container(
    target_pod: str,
    namespace: str,
    kubeconfig: str,
    pre_existing: set[str] | None = None,
    dispatch_ts: float = 0.0,
) -> tuple[str, str, dict, str]:
    """Resolve + await the ephemeral container a Pod-scoped debug just created.

    Returns ``(state, container_name, target_pod_metadata, detail)`` where
    ``state`` is ``"running"``, ``"terminated"`` or ``""`` (never observed
    executable). For ``terminated`` the detail is ``"exit <code> (<reason>)"``
    — a one-shot probe finishing fast is a SUCCESS signal the caller grades,
    not the old "did not start: Completed" false alarm.

    Unlike a node-debugger POD (which has its own Ready condition), an ephemeral
    container has no Ready gate — it is executable once its ``state`` is
    ``running``. We poll the TARGET pod for the container THIS call created
    (pre-dispatch snapshot diff; spec order is creation order — the status list
    is alphabetical and must not rank candidates). If the snapshot fetch
    failed, ``dispatch_ts`` enables timestamp attribution: a candidate whose
    ``startedAt`` falls at/after dispatch is ours. The pod identity returned
    is the TARGET pod's (uid/node/namespace) so carrier resolution can pin the
    host, but the carrier's executable handle is the container NAME, not a
    separate pod.
    """
    _pre = pre_existing
    wait_seconds = min(60, max(1, int(settings.timeout_kubectl_exec)))
    deadline = asyncio.get_running_loop().time() + wait_seconds
    last_error = ""
    container_name = ""
    while True:
        cmd = build_kubectl_cmd(
            "get", ["pod", target_pod, "-n", namespace, "-o", "json"],
            kubeconfig,
        )
        try:
            target = TransportTarget.from_state({})
            result = await execute_via_transport(
                cmd, target, timeout=settings.timeout_kubectl,
                expect_profile=PROFILE_K8S,
            )
        except Exception as exc:
            last_error = str(exc)
            result = None
        if result is not None and result.exit_code == 0:
            container_name, _attributed = _select_created_ephemeral(
                result.stdout, _pre,
            )
            if container_name:
                try:
                    data = json.loads(result.stdout)
                except (TypeError, json.JSONDecodeError):
                    data = {}
                meta = data.get("metadata") or {}
                spec = data.get("spec") or {}
                # privileged from the ephemeral container's own securityContext
                # (a --profile=netadmin container adds NET_ADMIN via capabilities,
                # NOT privileged=true; report the true spec value and let carrier
                # resolution decide what it accepts).
                _priv = False
                for ec in spec.get("ephemeralContainers") or []:
                    if isinstance(ec, dict) and ec.get("name") == container_name:
                        _sc = ec.get("securityContext") or {}
                        _priv = _sc.get("privileged") is True
                        break
                for st in (data.get("status") or {}).get(
                    "ephemeralContainerStatuses") or []:
                    if st.get("name") != container_name:
                        continue
                    state = st.get("state") or {}
                    tgt_meta = {
                        "name": meta.get("name") or target_pod,
                        "namespace": meta.get("namespace") or namespace,
                        "uid": meta.get("uid") or "",
                        "node": spec.get("nodeName") or "",
                        "privileged": _priv,
                        "phase": (data.get("status") or {}).get("phase") or "Unknown",
                    }
                    _started = _started_at_epoch(state)
                    terminated = state.get("terminated") or {}
                    if "running" in state:
                        if _attributed or _time_attributed(_started, dispatch_ts):
                            return "running", container_name, tgt_meta, ""
                        # Unconfirmed candidate running since BEFORE dispatch:
                        # stale — keep waiting for this call's container.
                        last_error = "created container not visible in spec yet"
                    elif terminated:
                        # Terminal state is final — no point polling further.
                        # Whether an immediate termination is success (one-shot
                        # probe) or a fault (chain short-circuit) is graded by
                        # the caller, which knows the command's intent.
                        _code = terminated.get("exitCode")
                        _reason = terminated.get("reason") or ""
                        if _attributed or _time_attributed(_started, dispatch_ts):
                            return (
                                "terminated",
                                container_name,
                                tgt_meta,
                                f"exit {_code} ({_reason})" if _reason else f"exit {_code}",
                            )
                        # Unconfirmed and started before dispatch: a stale
                        # container's exit — keep waiting for the container
                        # this call actually created.
                        last_error = "created container not visible in spec yet"
                    else:
                        waiting = (state.get("waiting") or {}).get("reason", "")
                        last_error = waiting or "ephemeral container not running"
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(2)
    return "", container_name, {}, (last_error or "ephemeral container did not start")


async def _delete_created_debug_pod(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
) -> bool:
    """Best-effort removal for a debug pod that never became executable."""
    cmd = build_kubectl_cmd(
        "delete",
        ["pod", pod_name, "-n", namespace, "--ignore-not-found"],
        kubeconfig,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
        return result.exit_code == 0
    except Exception:
        logger.warning(
            "Failed to clean unready debug pod %s/%s",
            namespace,
            pod_name,
            exc_info=True,
        )
        return False


def _build_kubectl_global_args(
    kubeconfig: str = "",
) -> list[str]:
    """Build kubectl global flags list.

    --kubeconfig: explicit parameter > settings (includes KUBECONFIG env via
    AliasChoices). --context: runtime-channel authority (settings) ONLY —
    never a caller parameter. --cluster is NOT emitted at all: under the
    kubewiz/single-cluster lock the connection identity belongs to the
    transport channel, and an LLM-supplied cluster name is always wrong
    (K7: it caused exit-1 "cluster does not exist" detours, #51-R3).
    """
    args: list[str] = []

    # --kubeconfig: tool param > settings fallback
    kc = kubeconfig or settings.kubeconfig_path
    if kc:
        kc = os.path.expanduser(kc)
        args.extend(["--kubeconfig", kc])

    # --context: settings only (runtime channel owns connection identity)
    if settings.kube_context:
        args.extend(["--context", settings.kube_context])

    return args


def build_kubectl_cmd(
    subcommand: str,
    v_args: "list[str] | str" = "",
    kubeconfig: str = "",
    settings=None,
) -> list[str]:
    """Build a raw kubectl command (no transport wrapper).

    Returns ``[kubectl, --kubeconfig, ..., subcommand, ...args]``.
    Transport wrapping (wiz/ssh) is handled by the transport layer.
    """
    if isinstance(v_args, str) and v_args:
        args_list = _split_args(v_args)
    elif isinstance(v_args, list):
        args_list = v_args
    else:
        args_list = []

    runtime_settings = settings or globals()["settings"]

    cmd = [runtime_settings.kubectl_path]
    cmd.extend(_build_kubectl_global_args(kubeconfig))
    cmd.append(subcommand)
    cmd.extend(args_list)
    return cmd


async def exec_kubectl_raw(
    subcommand: str,
    v_args: "list[str] | str" = "",
    kubeconfig: str = "",
    timeout: float = 30.0,
    stdin_data: str = "",
) -> CommandResult:
    """Execute kubectl via transport layer (lightweight internal checks).

    Use this for preflight, env_info, safety_check — internal calls that
    don't need LLM tool-call overhead.  For LLM-driven tool calls, use
    _kubectl_impl() instead.  ``stdin_data`` (when non-empty) is piped to
    the subprocess stdin — the declarative-apply seam for programmatic
    manifest delivery (``kubectl apply -f -``). The manifest-kind
    allowlist that guards the LLM apply face does NOT run here: internal
    callers deliver their own vetted manifests (e.g. the faultdrill CRD
    template — deliberately NOT whitelisted on the LLM face, design D2
    of openspec change ``faultdrill-cr-channel``).
    """
    cmd = build_kubectl_cmd(subcommand, v_args, kubeconfig)
    target = TransportTarget.from_state({})
    try:
        return await execute_via_transport(
            cmd, target, timeout=timeout, stdin_data=stdin_data,
            expect_profile=PROFILE_K8S,
        )
    except ToolGuardError as exc:
        return CommandResult(exit_code=-1, stdout="", stderr=f"guard rejected: {exc}")
    except ToolTimeoutError:
        return CommandResult(exit_code=-1, stdout="", stderr=f"timed out after {timeout}s")
    except FileNotFoundError:
        return CommandResult(exit_code=-1, stdout="", stderr="kubectl/wiz not found")
    except Exception as exc:
        return CommandResult(exit_code=-1, stdout="", stderr=str(exc))


def display_cmd(cmd: list[str]) -> str:
    """Return a human/LLM-facing command string.

    Delegates to the transport layer to strip any transport wrapper
    (wiz/ssh) and show only the inner semantic command.
    """
    target = TransportTarget.from_state({})
    return display_via_transport(cmd, target)


@dataclass(frozen=True)
class QueryOutcome:
    """Tri-state result of one kubectl READ (error ≠ empty ≠ value).

    B81 root fix (case #39 first shot): the claim-discovery channels ran
    on a bare-``str`` contract where ANY failure returned ``""`` — an error
    (malformed command, transport down) was value-collapsed onto "query
    succeeded, found nothing". The guard then honestly-but-wrongly froze
    an empty claim set and banned a legitimate occupant channel. Read-side
    callers consume THIS shape instead: ``ok`` carries the query's fate,
    ``stdout`` the payload, ``error`` the diagnosis. The fail-closed
    DECISION stays with the caller (an empty/failed set still anchors
    nothing), but the REASON is distinguishable at the log layer —
    ``.words`` is empty for both, ``.ok`` is not.
    """

    ok: bool
    stdout: str = ""
    error: str = ""

    @property
    def text(self) -> str:
        """Stripped payload; empty unless ``ok`` (fail-closed by attribute)."""
        return self.stdout.strip() if self.ok else ""

    @property
    def words(self) -> tuple[str, ...]:
        """Whitespace-split payload tokens; empty unless ``ok``."""
        return tuple(self.text.split())


async def query_kubectl(
    args: list[str],
    kubeconfig: str = "",
    *,
    log_name: str = "kubectl query",
) -> QueryOutcome:
    """Run ONE kubectl ``get`` through the transport; never raises (B81).

    Single production point for guard-side reads. ``args`` is the
    subcommand body AFTER ``get`` (kind, names, ``-n``, ``-l``, ``-o`` …).
    Failures surface at WARNING with the diagnosis (``log_name`` prefixes
    every line) so a silent empty set is never mistaken for a healthy
    empty answer again — the log layer is where error and empty were
    first required to be distinguishable.

    The transport import is resolved INSIDE the function on purpose:
    tests monkeypatch ``chaos_agent.transports.execute_via_transport``
    at the module attribute, which a top-level ``from``-import would
    have already copied into this module's namespace.
    """
    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )

    cmd = build_kubectl_cmd("get", args, kubeconfig=kubeconfig)
    target = TransportTarget.from_state({})
    try:
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl,
            expect_profile=PROFILE_K8S,
        )
    except Exception as e:  # noqa: BLE001 — best-effort read by contract
        logger.warning("%s failed (exception): %s", log_name, e)
        return QueryOutcome(ok=False, error=f"exception: {e}")
    if result.exit_code != 0:
        detail = (result.stderr or "").strip()[:200]
        logger.warning(
            "%s failed (exit=%s): %s", log_name, result.exit_code, detail,
        )
        return QueryOutcome(
            ok=False, error=f"exit={result.exit_code}: {detail}",
        )
    return QueryOutcome(ok=True, stdout=result.stdout.strip())


def _is_json_output(v_args: str) -> bool:
    """Check whether v_args requests JSON output (-o json, not jsonpath or yaml)."""
    parts = _split_args(v_args)
    for i, part in enumerate(parts):
        if part == "-o" and i + 1 < len(parts):
            fmt = parts[i + 1]
            return fmt == "json"
        if part.startswith("-o=") or part.startswith("-ojson"):
            fmt = part.split("=", 1)[-1] if "=" in part else part[2:]
            return fmt == "json"
    return False


@tool
async def kubectl(
    subcommand: str,
    v_args: str = "",
    stdin_data: str = "",
    kubeconfig: str = "",
) -> str:
    """Phase 2 (execution): full kubectl incl. mutation. Pick `subcommand`,
    pass remaining CLI args as `v_args`. NOT Phase 1 — use ``kubectl_read``.

    When to use:
      - Any inspection/mutation; probing inside containers/on nodes.
      - Non-workload resources (PV/PVC/Secret/ConfigMap) via ``apply`` +
        YAML in ``stdin_data``; workload creation blocked except the
        recovery-carrier ``run`` shape.
      - Recovery-carrier RBAC objects (sa/clusterrole/clusterrolebinding)
        via imperative ``create`` — the ``apply -f -`` manifest whitelist
        admits no RBAC kinds.

    Inputs:
      - subcommand: get|describe|top|logs|exec|delete|patch|set|scale|
        cordon|uncordon|taint|label|annotate|drain|debug|create|apply|
        run; ``edit``/``replace`` unavailable — use ``patch``.
      - v_args: shell-quoted args (recipes: `kubectl-recipes.md`). Single-
        quote any arg with spaces or double quotes (jsonpath, `-p` JSON):
        `-o 'jsonpath={range...}'`, `-p '{"spec":...}'`.
      - stdin_data: YAML for ``apply -f -`` (not via v_args/exec heredoc).
      - kubeconfig: path override; never --kubeconfig/--context/--cluster
        in v_args (auto-stripped). Connection identity is owned by the
        runtime channel — passing a cluster name cannot work.

    Output: stdout, or "Error: ..." on non-zero exit.

    Side effects: read verbs none; all else mutates cluster state.

    Constraints:
      - v_args is two-layered. BEFORE `--`: kubectl args go straight
        into argv, no shell — `|`/`;`/`&&`/`>`/`$()` are inert there
        (use `-l/--selector`, `--field-selector`, `-o jsonpath`).
        AFTER `--` (exec): `sh -c '<script>'` hands the script to the
        container's shell — heredocs, redirects, pipes, `&&` all work
        there (skill recipes use this form).
      - `exec` rejects `-l/--selector` at the kubectl layer (error with
        fix guidance) — resolve the pod via `get` first.
      - `debug node/<node>`: MUST append `-- sleep 3600`; never `-it`;
        host paths under `/host/...`; host mutation needs
        `--profile=sysadmin` + pullable image (recipes).
      - One-shot debug CMD (`-- CMD`, no sleep) is PROBE-only: HARD 120s
        cap, then auto-cleaned. Sustained loops → host systemd-run service.
      - `drain` refuses `--force`/`--disable-eviction`; recover via
        `uncordon`.
      - `run`: recovery-carrier shape ONLY (timer-host pod:
        `drill-rc-*` + whitelisted image + `--restart=Never
        --command -- sleep N`; `--overrides` admits
        `spec.serviceAccountName` only) — guard refuses every other
        shape (see `references/carrier/recovery-carrier.md`).
      - Unknown-flag error → `--help`; do NOT guess and retry.
    """
    return await _kubectl_impl(subcommand, v_args, kubeconfig, stdin_data=stdin_data)


#: Appended to an empty ``get`` result that carried a label selector.
#: DOUBLE-DUTY marker: the replan-review guard
#: (``agent.nodes.execute.execute_loop._target_absence_proven_in_epoch``)
#: matches this exact text as its framework-generated empty-set receipt —
#: the model cannot write ToolMessages, so this string is a structural
#: proof anchor, not just UX guidance. Rewording it silently blinds the
#: guard; keep both sides in sync.
EMPTY_SELECTOR_HINT = (
    "💡 No resources matched the label selector. "
    "Try running without -l to discover available pods, "
    "then inspect their actual labels with: "
    "kubectl(subcommand='get', v_args='pod <name> -n <ns> -o jsonpath={.metadata.labels}')"
)


async def _kubectl_impl(
    subcommand: str,
    v_args: str = "",
    kubeconfig: str = "",
    stdin_data: str = "",
) -> str:
    """Shared kubectl execution logic used by both kubectl and kubectl_read."""
    # Transport target drives channel selection downstream; stdin_data rides
    # along via execute_via_transport, which folds it into the command on
    # channels without a native stdin pipe (wiz) — no channel-specific
    # rejection here any more (task-349ccf5d deadlock).
    _target = TransportTarget.from_state({})

    processed_args: list[str] = []

    if v_args:
        # Tokenize FIRST, then apply kubectl-level hygiene to tokens BEFORE
        # the "--" separator only. String-level regex rewrites here once
        # matched ``ls -l /proc/$(cat ...)`` INSIDE a quoted exec payload and
        # amputated `` -l /proc/$(cat``, corrupting the script the container
        # received (inject-17617837): flag-shaped text past "--" is payload,
        # never a kubectl flag.
        processed_args = _split_args(v_args)
        # R45: the hygiene boundary is pflag's TRUE separator, not the
        # first standalone ``--`` — a ``--`` in a value-taking flag's
        # value slot (``-c --``) is that flag's VALUE, and flag-shaped
        # text between it and the true separator is still the kubectl
        # FLAG region (an embedded ``--context prod`` there must be
        # stripped — K7 channel-owned connection identity). A line with
        # no true separator keeps the legacy boundary.
        from chaos_agent.tools._readonly_facts import exec_separator_index

        sep_idx = exec_separator_index(processed_args)
        if sep_idx is None:
            sep_idx = (
                processed_args.index("--")
                if "--" in processed_args
                else len(processed_args)
            )

        # Defensive: strip --kubeconfig embedded at the kubectl layer by LLM
        # mistake (the tool has a dedicated 'kubeconfig' parameter). A
        # --kubeconfig past "--" targets a NESTED kubectl inside the exec
        # payload and must survive verbatim.
        cleaned: list[str] = []
        dropped_flags: list[str] = []
        i = 0
        while i < sep_idx:
            tok = processed_args[i]
            # --kubeconfig has a dedicated parameter; --context/--cluster are
            # NOT parameterized at all (the runtime channel owns connection
            # identity, K7). At the kubectl layer they can only be LLM
            # mistakes — stripped the same way. Past "--" (exec payload)
            # these strings are script text and survive verbatim.
            if tok in ("--kubeconfig", "--context", "--cluster"):
                i += 2 if i + 1 < sep_idx else 1
                dropped_flags.append(tok)
                continue
            if tok.startswith(("--kubeconfig=", "--context=", "--cluster=")):
                i += 1
                dropped_flags.append(tok.split("=", 1)[0])
                continue
            cleaned.append(tok)
            i += 1
        if dropped_flags:
            logger.warning(
                "Connection flags %s must not be embedded in v_args "
                "(kubeconfig has a dedicated parameter; context/cluster are "
                "runtime-channel owned). The embedded values have been removed.",
                sorted(set(dropped_flags)),
            )
        processed_args = cleaned + processed_args[sep_idx:]

        # kubectl exec targets ONE explicit pod — selector flags belong to
        # `get`, not `exec`. Reject BEFORE dispatch (fail fast, reason + fix);
        # never silently rewrite a command the caller did not send.
        if subcommand == "exec":
            hit = next(
                (
                    t for t in cleaned
                    if t in ("-l", "--selector") or t.startswith(("-l=", "--selector="))
                ),
                None,
            )
            if hit is not None:
                return (
                    "Error: kubectl exec does not support -l/--selector "
                    "(exec targets one explicit pod name, not a selector).\n"
                    "Fix: resolve the pod first — "
                    "kubectl get pods -n <ns> -l <selector> "
                    "-o jsonpath='{.items[0].metadata.name}' — then exec by pod name:\n"
                    "kubectl(subcommand='exec', v_args='<pod-name> -n <ns> -- <command>')"
                )

    debug_namespace = ""
    _debug_start_ts = 0.0
    _pre_ec_names: set[str] | None = None
    if subcommand == "debug":
        # Wall-clock anchor BEFORE dispatch: the parse-failure discovery
        # fallback filters candidates by creationTimestamp recency.
        _debug_start_ts = time.time()
        debug_namespace = _namespace_from_args(processed_args)
        if not debug_namespace:
            debug_namespace = await _resolve_effective_namespace(
                kubeconfig,
            )
            # R45: inject before the TRUE separator — the same value-slot
            # rule as the hygiene boundary above (a ``--`` in a flag's
            # value slot is that flag's VALUE). A line with no true
            # separator keeps the legacy boundary.
            from chaos_agent.tools._readonly_facts import exec_separator_index

            separator = exec_separator_index(processed_args)
            if separator is None and "--" in processed_args:
                separator = processed_args.index("--")
            if separator is not None:
                processed_args[separator:separator] = ["-n", debug_namespace]
            else:
                processed_args.extend(["-n", debug_namespace])
        # Snapshot the pod's ephemeral containers BEFORE dispatch so the
        # container THIS call creates can be attributed afterwards (the status
        # list is alphabetical; spec order is creation order).
        _pre_target_pod = _debug_target_pod_name(processed_args)
        if _pre_target_pod:
            _pre_ec_names = await _ephemeral_spec_names(
                _pre_target_pod, debug_namespace, kubeconfig,
            )

    # Auto-inject/normalize --timeout for kubectl exec blade create commands.
    # Must happen BEFORE build_kubectl_cmd so --timeout is in processed_args.
    if subcommand == "exec" and v_args and re.search(r"\bblade\s+create\b", v_args):
        _fault_match = re.search(
            r"blade\s+create\s+k8s\s+(pod|node|container)-(\w+)\s+(\w+)", v_args
        )
        _scope, _fault_target, _action = (
            (_fault_match.group(1), _fault_match.group(2), _fault_match.group(3))
            if _fault_match else (None, None, None)
        )
        from chaos_agent.utils.fault_type import ensure_min_duration, normalize_timeout_flag
        # normalize_timeout_flag canonicalizes every spelling ChaosBlade
        # accepts (``--timeout=30``, ``--timeout 30``, duplicates, ``s``
        # suffix) into a single ``--timeout <value>`` pair — same pattern
        # as blade_create/blade_python_create. Without it the equals form
        # escaped canonicalization (no parseable pair to read).
        _timeout_value = normalize_timeout_flag(processed_args)
        if _timeout_value is None:
            effective_timeout = ensure_min_duration(None, _scope, _fault_target, _action)
            processed_args.extend(["--timeout", str(effective_timeout)])
            logger.info(
                f"Auto-injected --timeout {effective_timeout}s into "
                f"kubectl exec blade create command"
            )
        else:
            try:
                _current_int = int(_timeout_value)
            except (ValueError, TypeError):
                _current_int = 0
            _effective = ensure_min_duration(_timeout_value, _scope, _fault_target, _action)
            if _effective != _current_int:
                _timeout_idx = processed_args.index("--timeout")
                processed_args[_timeout_idx + 1] = str(_effective)
                logger.info(
                    f"Normalized --timeout from {_timeout_value}s to {_effective}s "
                    f"for {_scope}-{_fault_target}-{_action}"
                )

    cmd = build_kubectl_cmd(subcommand, processed_args, kubeconfig)

    # exec/debug subcommands use longer timeout (container commands may be slow;
    # debug needs to pull images and create ephemeral containers)
    timeout = settings.timeout_kubectl_exec if subcommand in ("exec", "debug") else settings.timeout_kubectl

    try:
        result = await execute_via_transport(
            cmd, _target, timeout=timeout, stdin_data=stdin_data,
            # Both LLM-facing kubectl tools funnel through here, and both need
            # cluster access. A host-profile channel (ssh / kubewiz_host) gives
            # a shell on one machine and cannot serve cluster operations.
            expect_profile=profile_for_tool("kubectl"),
        )
    except Exception as e:
        # Surface the raw signal without a "failed" verdict. For a self-severing
        # injection (e.g. node network isolation) THIS exec times out ON SUCCESS;
        # editorializing it as "failed" misleads the LLM. Keep the "Error:" prefix
        # (the framework's failure-marker contract used by downstream detection)
        # but let the raw text — e.g. "Command timed out after 10s" — speak.
        return apply_output_safety_valve(
            f"Error: kubectl {subcommand}: {e}", kind="error"
        )

    if result.exit_code != 0:
        # kubewiz 模式下错误信息在 stdout，直接模式在 stderr；两者都非空时
        # （如 jsonpath 半截渲染占 stdout、真实报错在 stderr）必须合并，
        # or 语义会把 kubectl 的实际错误解释丢掉，模型只能盲猜自修复。
        _err_parts = [s.strip() for s in (result.stdout, result.stderr) if s and s.strip()]
        error_detail = "\n".join(_err_parts) if _err_parts else "(no output)"
        # A node-debugger pod that completed mid-probing: its keep-alive sleep
        # expired while the model was still exec-ing probes through it. The
        # documented convention is `-- sleep 3600` precisely to prevent this,
        # but the convention is prompt-level — the model can (and #31 did)
        # pass a short sleep. Point at the fix instead of leaving a bare
        # kubectl error for the generic reminder loop to chew on.
        # The debug-pod check is a scoping condition, not an error matcher:
        # the same "completed pod" error on a BUSINESS pod must NOT get the
        # keep-alive guidance (its Completed is a normal lifecycle end, not
        # an expired probe channel) — the prescription only fits debug pods.
        # Boundary of legitimacy: this stays ADVICE appended to the raw
        # error (feedback-loop form — the model keeps full error text, exit
        # code, and the choice of what to do next). It must never grow into
        # a gate: no retry-blocking, no action-stripping, no forced rebuild.
        # Its license to exist is the fail-safety above — on a kubectl
        # wording change it silently degrades to the bare error; a gate
        # built on the same string match would instead fail by blocking
        # correct actions. Program matching may inform the model's
        # judgment; it must not replace it.
        if (
            subcommand == "exec"
            and "node-debugger-" in (v_args or "")
            and _COMPLETED_POD_EXEC_ERR_SUBSTRINGS[0] in error_detail
            and (
                _COMPLETED_POD_EXEC_ERR_SUBSTRINGS[1] in error_detail
                or _COMPLETED_POD_EXEC_ERR_SUBSTRINGS[2] in error_detail
            )
        ):
            error_detail += (
                "\n\nThe debug pod's keep-alive has ended (phase Completed/"
                "Succeeded): its `-- sleep N` expired while probes were still "
                "running through it. The documented convention is `-- sleep "
                "3600` for exactly this reason. Recreate the debug pod "
                "(kubectl debug node/<node> --image=<image> -- sleep 3600) "
                "to continue exec-based probing."
            )
        # Report the exit code + raw output verbatim; no "failed" verdict word.
        # No routine truncation at the tool layer: governance is the
        # compactor's job (it caches oversized messages in full). The safety
        # valve below only fires on runaway output (>64KB) and keeps both
        # ends — the error verdict typically lives at the tail.
        return apply_output_safety_valve(
            f"Error: kubectl {subcommand} (exit {result.exit_code}): {error_detail}",
            kind="error",
        )

    output = apply_output_safety_valve(result.stdout, kind="success-output")

    # Append large output hint for get subcommand with JSON output.
    # Skipped when the safety valve has already fired (original output
    # over the 64KB ceiling): the valve's shared notice already carries
    # the same narrowing strategies (kind "success-output" embeds them),
    # it reports the honest ORIGINAL size, and appending ~300B here would
    # push the returned message past the ceiling the valve just enforced
    # — exactly the overrun the valve's budget contract forbids.
    if (
        subcommand == "get"
        and _is_json_output(v_args)
        and settings.kubectl_max_output_bytes > 0
        and len(result.stdout.encode("utf-8", errors="replace"))
        <= TOOL_OUTPUT_SAFETY_VALVE_BYTES
    ):
        output_bytes = len(output.encode("utf-8", errors="replace"))
        if output_bytes > settings.kubectl_max_output_bytes:
            size_kb = output_bytes // 1024
            output += (
                f"\n\n⚠️ LARGE_OUTPUT: Output is large ({size_kb}KB). Narrow the scope using:\n"
                f"- Use --field-selector to filter (e.g., --field-selector spec.nodeName=<node>)\n"
                f"- Use -o name to get resource names only\n"
                f"- Specify a resource name to query a single resource\n"
                f"- Use -o jsonpath to extract specific fields"
            )

    # (The old post-hoc "-l was removed from your command" warning is gone:
    # selector flags at the kubectl layer are now rejected before dispatch,
    # and payload tokens were never ours to touch.)

    # Debug pod lifecycle — creation alone is not execution readiness. Resolve
    # the authoritative namespace/UID/node and fail early on image pull errors.
    if subcommand == "debug":
        _debug_ns = debug_namespace or _namespace_from_args(processed_args) or "default"

        # Pod-scoped debug (``kubectl debug <pod> --target=<c> ...``) attaches an
        # EPHEMERAL CONTAINER to an existing pod. kubectl prints no container
        # name on stdout — it is only in the target pod's
        # ``ephemeralContainerStatuses``. The target pod is the USER'S workload:
        # it must never be treated as a created debug pod (no delete on
        # cleanup). Node-scoped debug (``node/<node>``) keeps the original path.
        _target_pod = _debug_target_pod_name(processed_args)
        if _target_pod:
            ec_state, container_name, tgt_meta, ec_detail = (
                await _wait_for_ephemeral_container(
                    _target_pod, _debug_ns, kubeconfig,
                    pre_existing=_pre_ec_names,
                    dispatch_ts=_debug_start_ts,
                )
            )
            if not container_name:
                # Distinguish "not created" from "created but not parsed": tell
                # the model exactly where the name lives so it does not loop.
                return (
                    "Error: kubectl debug attached no ephemeral container to "
                    f"pod '{_target_pod}'. Raw output: {output}\n"
                    "If the debug command reported 'Targeting container ...', the "
                    "ephemeral container WAS created but its name is only in "
                    "``.status.ephemeralContainerStatuses`` (kubectl does not "
                    "print it). Read it with: kubectl(subcommand='get', "
                    f"v_args='pod {_target_pod} -n {_debug_ns} -o "
                    "jsonpath={.status.ephemeralContainerStatuses[*].name}')."
                )
            if ec_state == "terminated":
                _logs_hint = (
                    f"kubectl(subcommand='logs', v_args='{_target_pod} -n {_debug_ns} "
                    f"-c {container_name}')"
                )
                if ec_detail.startswith("exit 0"):
                    return (
                        f"Ephemeral container '{container_name}' on pod "
                        f"'{_target_pod}' ran to completion ({ec_detail}) — for a "
                        "one-shot probe this is SUCCESS; the output is in its logs: "
                        f"{_logs_hint}. If the command was meant to keep running "
                        "(e.g. a fault chain with a sleep), it ended EARLY — read "
                        "the logs to see where it stopped. The container is gone "
                        "as an exec target; do not exec into it."
                    )
                return (
                    f"Error: ephemeral container '{container_name}' on pod "
                    f"'{_target_pod}' terminated before becoming usable "
                    f"({ec_detail}). Read its logs for the cause: {_logs_hint}. "
                    "Do NOT delete the target pod — it is the user's workload; "
                    "the ephemeral container is bound to its lifecycle."
                )
            if ec_state != "running":
                return (
                    f"Error: ephemeral container '{container_name}' on pod "
                    f"'{_target_pod}' did not start: {ec_detail}. "
                    "This can also be a visibility race — the container may "
                    "actually have run or finished: verify ground truth with "
                    f"kubectl(subcommand='logs', v_args='{_target_pod} -n "
                    f"{_debug_ns} -c {container_name}') and a fresh 'get pod' "
                    "status read BEFORE retrying. "
                    "Do NOT delete the target pod — it is the user's workload; "
                    "the ephemeral container is bound to its lifecycle. Retry "
                    "with a pullable image if this was an image-pull failure."
                )
            _profile = _extract_debug_profile(v_args)
            meta_payload = {
                **tgt_meta,
                "name": _target_pod,
                "namespace": _debug_ns,
                "ephemeral_container": container_name,
                "ready": True,
                "cleaned": False,
                "debug_profile": _profile,
            }
            meta_tag = json.dumps(meta_payload, ensure_ascii=True, separators=(",", ":"))
            output += (
                f"\n\n[debug-pod-meta: {meta_tag}]"
                f"\n[debug-pod-ns: {_debug_ns}]"
                f"\nThe ephemeral container is running. Exec into it with: "
                f"kubectl(subcommand='exec', v_args='{_target_pod} -n {_debug_ns} "
                f"-c {container_name} -- <command>')."
                "\nIt shares the target pod's namespaces (network/pid); operate on "
                "eth0 there to affect exactly this pod. There is NOTHING to clean "
                "up — an ephemeral container is removed only when the pod is "
                "recreated; do NOT delete the pod."
            )
            return output

        # Single parsing source — shared with baseline/verifier/recover
        # (lazy import: _debug_pod imports build_kubectl_cmd from here).
        from chaos_agent.agent.nodes.execute._debug_pod import (
            discover_created_debug_pod,
            parse_debug_pod_name,
        )
        _debug_pod = parse_debug_pod_name(output)
        if not _debug_pod:
            # `debug --help` (or any flag-only invocation without a node/pod
            # target) is a documentation request, not a pod creation: the
            # tools' own guidance teaches "Unknown-flag error → ``--help``",
            # and routing that through the pod-name parse yields the actively
            # misleading "create may never have executed" error on top of the
            # help text (#30 msg 18: one wasted digestion round). A help
            # invocation never carries the ``--`` command separator — that
            # co-check keeps the short-circuit off real create forms.
            if "--" not in processed_args and any(
                a in processed_args for a in ("--help", "-h")
            ):
                return output
            # Discovery fallback (node scope only): one live get-pods filtered
            # by spec.nodeName + node-debugger- prefix + recency. Only runs on
            # the parse-failure path; the normal path pays zero extra cost.
            _node = _debug_target_node_name(processed_args)
            if _node:
                _debug_pod = await discover_created_debug_pod(
                    _node, _debug_ns, _debug_start_ts,
                    kubeconfig,
                )
                if _debug_pod:
                    output += (
                        f"\n(kubectl printed no pod name; discovered "
                        f"'{_debug_pod}' on node '{_node}' via live lookup.)"
                    )
        if not _debug_pod:
            # Parse AND discovery both failed. Exit 0 + no pod name means the
            # create may never have executed at all (transport drop,
            # API-server reject without event). Telling the model to retry
            # the SAME command is what burned the k3 budget three rounds in
            # a row (task-29848471).
            return (
                "Error: kubectl debug returned exit 0 but no debug pod name "
                "could be identified. The create may never have executed — "
                "this is NOT evidence the command is wrong. Do NOT retry the "
                "same command blindly; first verify the current cluster state "
                "or use a different path to reach the target. "
                f"Raw output: {output}"
            )
        _profile = _extract_debug_profile(v_args)

        # ---- One-shot COMMAND mode (`debug ... -- CMD`): the pod runs CMD
        # once and terminates. condition=Ready is never true there, so waiting
        # for it is a guaranteed false negative — poll the terminal phase and
        # report the COMMAND's exit code instead (task-29848471 false alarm).
        if _debug_has_oneshot_command(processed_args):
            terminal, metadata, terminal_error = await _wait_for_debug_pod_terminal(
                _debug_pod, _debug_ns, kubeconfig,
            )
            if metadata.get("namespace"):
                _debug_ns = metadata["namespace"]
            logs_tail = await _debug_pod_logs_tail(
                _debug_pod, _debug_ns, kubeconfig,
            )
            # A finished one-shot has served its purpose — remove it whether
            # the command succeeded or not; task-level cleanup remains the
            # second safety net.
            cleaned = await _delete_created_debug_pod(
                _debug_pod, _debug_ns, kubeconfig,
            )
            if terminal:
                _exit = metadata.get("exit_code")
                if _exit is None and metadata.get("phase") == "Failed":
                    _exit = 1  # Failed pod without an exitCode — non-zero by definition
                elif _exit is None and metadata.get("phase") == "Succeeded":
                    # containerStatuses can lag behind the phase; a Succeeded
                    # pod is exit 0 by definition (else it would be Failed).
                    _exit = 0
                meta_payload = {
                    **metadata,
                    "name": metadata.get("name") or _debug_pod,
                    "namespace": _debug_ns,
                    "ready": _exit == 0,
                    "cleaned": cleaned,
                    "debug_profile": _profile,
                    "oneshot": True,
                }
                meta_tag = json.dumps(meta_payload, ensure_ascii=True, separators=(",", ":"))
                _logs_section = f"\nCommand output (logs tail):\n{logs_tail}" if logs_tail else ""
                if _exit == 0:
                    return (
                        f"{output}\n\n[debug-pod-meta: {meta_tag}]"
                        f"\n[debug-pod-ns: {_debug_ns}]"
                        f"\nOne-shot debug command completed with exit_code=0."
                        f"{_logs_section}\n"
                        "The debug pod has been removed; there is NOTHING to clean up."
                    )
                # A non-zero exit is NOT automatically a failure: existence/
                # residue pre-checks report absence THROUGH non-zero exits
                # (exit 2 "No such file", exit 4 "could not be found") — the
                # skill corpus legislates that as the expected PASS form. The
                # "Error:" prefix would also ignite the framework's RUNTIME
                # EVIDENCE reminder ("real-world outcome: unknown"), a digestion
                # round the model pays before continuing (#31: the same exit-4
                # pre-check read cleanly through kubectl_read in planning, then
                # tripped this wrapper in execute). State the exit neutrally,
                # surface the logs, let the model judge.
                return (
                    f"One-shot debug command completed with exit_code={_exit} (non-zero).\n"
                    f"[debug-pod-meta: {meta_tag}]\n"
                    f"The debug pod has been removed.{_logs_section}"
                    "\nA non-zero exit is not automatically a failure: for "
                    "existence/residue pre-checks it is the expected PASS form "
                    "(e.g. 'No such file', 'could not be found'); judge from "
                    "the command output above."
                )
            # Budget expired before termination — leave the pod for follow-up.
            meta_payload = {
                **metadata,
                "name": metadata.get("name") or _debug_pod,
                "namespace": _debug_ns,
                "ready": False,
                "cleaned": cleaned,
                "debug_profile": _profile,
                "oneshot": True,
            }
            meta_tag = json.dumps(meta_payload, ensure_ascii=True, separators=(",", ":"))
            return (
                f"Error: {terminal_error}.\n"
                f"[debug-pod-meta: {meta_tag}]\n"
                + (
                    "The still-running pod was cleaned up automatically."
                    if cleaned
                    else f"Cleanup failed; delete pod {_debug_pod} -n {_debug_ns} when done."
                )
                # Budget-expiry fix direction: without it the model must
                # self-diagnose the carrier mistake (inject-59b289a6 burned
                # ~150s + a fragmented fault window before re-arming).
                + "\nOne-shot debug pods are probes (120s hard cap), not loop "
                "carriers: a long-running/sustained payload must be hosted "
                "as a systemd transient service on the host (systemd-run), "
                "armed via a short one-shot command that returns immediately."
            )

        # ---- INTERACTIVE mode (`debug ... -- sleep N` style, or no `--`):
        # the pod must become Ready so the caller can exec into it.
        ready, metadata, ready_error = await _wait_for_created_debug_pod(
            _debug_pod, _debug_ns, kubeconfig,
        )
        if metadata.get("namespace"):
            _debug_ns = metadata["namespace"]
        cleaned = False
        if not ready:
            cleaned = await _delete_created_debug_pod(
                _debug_pod, _debug_ns, kubeconfig,
            )
        # _profile was extracted above the oneshot branch; debug-pod-meta
        # carries it for carrier resolution and diagnostics.
        meta_payload = {
            **metadata,
            "name": metadata.get("name") or _debug_pod,
            "namespace": _debug_ns,
            "ready": ready,
            "cleaned": cleaned,
            "debug_profile": _profile,
        }
        meta_tag = json.dumps(meta_payload, ensure_ascii=True, separators=(",", ":"))
        if not ready:
            return (
                f"Error: {ready_error}.\n"
                f"[debug-pod-meta: {meta_tag}]\n"
                "The pod object exists but is not executable. Do NOT call kubectl exec. "
                + (
                    "The failed pod was cleaned up automatically."
                    if cleaned
                    else f"Cleanup failed; delete pod {_debug_pod} -n {_debug_ns}."
                )
            )
        output += (
            f"\n\n[debug-pod-meta: {meta_tag}]"
            f"\n[debug-pod-ns: {_debug_ns}]"
            "\nThe debug pod is Ready. Clean it up after use with: "
            f"kubectl(subcommand='delete', v_args='pod {_debug_pod} -n {_debug_ns}')."
        )

    # Label discovery hint for empty get results with label selector.
    # Two empty-set forms, one per transport reality:
    #   - server-side channels (kubewiz) return a literally empty stdout;
    #   - a local CLI (kubeconfig channel) prints the table printer's
    #     "No resources found in <ns> namespace." line (exit 0 — see
    #     kubernetes/kubectl#1596), which is equally an empty MATCH SET.
    # Without the second form the hint — and the replan-review absence
    # proof anchored on it — is dead code on direct connections.
    # startswith() covers the version variants ("No resources found.",
    # "... in <ns> namespace.", all-namespaces scans).
    _stripped_output = output.strip()
    if (
        subcommand == "get"
        and (not _stripped_output or _stripped_output.startswith("No resources found"))
        and ("-l " in v_args or "--selector " in v_args)
    ):
        output += f"\n\n{EMPTY_SELECTOR_HINT}"

    return output


# ── Phase 1 read-only kubectl flavour ──────────────────────────────────
#
# Background (task-ce9647931ce1): planning-phase agent_loop had the full
# ``kubectl`` bound, and the LLM — once it撞 the ``blade_create`` black-
# list — pivoted to ``kubectl exec <chaosblade-controller-pod> -- blade
# create ...`` to inject anyway. The whole point of the agent_loop →
# safety_check → confirmation_gate → execute_loop pipeline is that
# planning has zero side effects, so the user's reject at
# confirmation_gate actually leaves the cluster untouched.
#
# Mitigation strategy (multi-layer, see design plan):
#   - Layer A (THIS): physically remove mutation subcommands from the
#     Phase 1 tool surface. The LLM cannot call what's not in the schema.
#   - Layer D: ToolNode error handler refuses to list "try one of [...]"
#     alternatives that would re-suggest the bypass.
#   - Layer F: a phase1_screener as last-resort runtime check.
#
# The ``Literal`` type below is enforced by LangChain's tool argument
# validation; passing any other subcommand returns a Pydantic
# ValidationError that ToolNode catches and surfaces via the Layer D
# error handler.
READONLY_SUBCOMMANDS: tuple[str, ...] = (
    "get", "describe", "top", "logs",
    "version", "cluster-info", "api-resources", "explain", "auth",
    "exec", "debug",
)


@tool
async def kubectl_read(
    subcommand: Literal[
        "get", "describe", "top", "logs",
        "version", "cluster-info", "api-resources", "explain", "auth",
        "exec", "debug",
    ],
    v_args: str = "",
    kubeconfig: str = "",
) -> str:
    """READ-ONLY kubectl — the observation tool for every read-only phase.

    Read-only BY ENFORCEMENT: read verbs always; ``exec``/``debug``
    probes only; mutating inner commands are REJECTED — fault INJECTION
    is Phase 2.

    When to use:
      - Read-only inspection: `get`/`describe`/`top`/`logs`/
        `api-resources`/`explain`/`auth can-i`.
      - Read-only probes inside a pod (``exec``): ONE single command
        after ``--`` (e.g. ``-- which stress-ng``, ``-- df -h``).
      - Node host fs/kernel: ``debug node/<node> --image=<cluster-image>
        -- sleep 60`` then exec into it (paths ``/host/...``). In PLANNING,
        verify the image carries your plan's binaries BEFORE committing.

    Inputs:
      - subcommand: Literal-enforced.
      - v_args: same shape as ``kubectl``; single-quote any arg containing
        spaces — especially a whole ``-o jsonpath=...`` template (unquoted
        literal text like ``capacity={...}`` gets word-split remotely).
      - kubeconfig: path override. No context/cluster parameters —
        connection identity is owned by the runtime channel.

    Output: same as the full ``kubectl`` tool (stdout / "Error: ...").

    Side effects: none on cluster state (`debug`'s probe Pod auto-cleaned).

    Constraints:
      - exec inner: a read-only probe — one command, a pipeline, or a
        `;`/`&&`/`||`-chained list where EVERY segment is a read-only probe
        (B46). Redirects/substitution/background/heredocs fail closed.
      - exec: no ``-l/--selector`` at the kubectl layer (rejected with
        guidance — resolve the pod via `get`); no ``-it``; SHORT
        keep-alive for debug (``-- sleep 60``).
      - Unknown-flag error → ``--help`` in v_args rather than guessing.
    """
    # Belt-and-braces: even if Literal validation is bypassed, reject any
    # subcommand outside the read-only set at runtime.
    if subcommand not in READONLY_SUBCOMMANDS:
        return (
            f"Error: kubectl_read does not accept subcommand '{subcommand}'.\n"
            f"kubectl_read is read-only by enforcement. Allowed subcommands: "
            f"{', '.join(READONLY_SUBCOMMANDS)}.\n"
            f"Mutation subcommands (delete/patch/scale/...) run in Phase 2 via "
            f"the full kubectl tool after your plan is approved."
        )
    # ``exec`` / ``debug`` inner command must be a read-only probe — the shared
    # classifier judges it (same vocabulary the guard-scope screeners use), and
    # a rejection carries the SPECIFIC reason so the model can self-correct.
    if subcommand in ("exec", "debug"):
        from chaos_agent.tools.readonly import (
            READONLY_PROBE_FIX_HINT,
            kubectl_exec_rejection_reason,
        )

        reason = kubectl_exec_rejection_reason(v_args)
        if reason is not None:
            return (
                f"Error: kubectl_read rejected this {subcommand} — its inner "
                f"command is not read-only: {reason}.\n"
                f"kubectl_read only runs read-only probes. "
                f"{READONLY_PROBE_FIX_HINT}"
            )
    # Call the shared implementation directly — NOT kubectl.ainvoke(), which
    # would emit a nested on_tool_start event (duplicate TUI tool card).
    return await _kubectl_impl(subcommand, v_args, kubeconfig)
