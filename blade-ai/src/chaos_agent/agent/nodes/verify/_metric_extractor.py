"""Structured metric extraction from kubectl / shell tool outputs.

Pure functions: ``extract_metrics(tool_name, command, stdout) -> dict``.
Used by three call sites:

  1. Baseline collection (pre-injection snapshot) — historical caller,
     was inlined in ``_verifier_hints._extract_baseline_key_metrics``.
  2. Layer 2 verification — PreReasoningHook prepends a short
     ``[Auto-extracted]`` summary to each new ToolMessage's content
     BEFORE ``tool_compactor`` truncates it to 1KB. The summary lives
     in the HEAD so it survives ``truncate_text``'s head-only retention.
  3. Cross-check in ``_parse_verification_result`` — compares
     extractor's ground truth against the numbers the LLM cites in
     evidence, to catch hallucinated baseline→post deltas.

No state, no side effects, never raises. ``{}`` on any failure — the
caller decides what to do with the absence of data.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Standalone ``df`` token — catches ``df``, ``df -h``, ``df -k /tmp``;
# rejects ``diff`` and ``df.txt`` (no whitespace/end boundary after "df").
_DF_TOKEN = re.compile(r"(?:^|\s)df(?:\s|$)")

# Standalone ``dd`` token — catches ``dd if=/dev/zero of=/x bs=1M``,
# ``exec <pod> -- dd ...`` (the value-joined command carries `` dd ``);
# rejects ``add``/``ddress``/``/tmp/dd``/``dd-xyz`` (no whitespace-or-start
# boundary before, no whitespace-or-end boundary after). This is only the
# ACTIVATION gate; ``_parse_dd_timing`` keys on the OUTPUT format, so a
# command that merely contains a ``dd`` token but produced no transfer
# summary yields nothing (the cabb5702 guard against literal-mention noise).
_DD_TOKEN = re.compile(r"(?:^|\s)dd(?:\s|$)")

# Standalone ``get`` token — catches ``get pods -n default
# --no-headers``, ``get sa,role,rolebinding -n default``; rejects
# ``forget`` and "budget" (no whitespace/end boundary after "get").
_GET_TOKEN = re.compile(r"(?:^|\s)get(?:\s|$)")

# Output forms that rewrite the kubectl table into something that is
# NOT a line-per-resource table (JSON/YAML/custom-columns/jsonpath).
# ``-o name`` and ``-o wide`` ARE tables — not excluded.
_NON_TABLE_OUTPUT = re.compile(
    r"-o\s*(json|yaml|custom-columns)|-ojson|--output=(json|yaml)|jsonpath"
)

# kubectl table headers are ALL-CAPS tokens (NAME, READY, STATUS,
# UP-TO-DATE, RESTARTS, AGE …) — a data row can't match this: pod
# names are lowercase hashes, IPs contain dots, AGE suffixes ("23d")
# and restart counts ("0/1" or "0") mix lowercase/slash.
_TABLE_HEADER_TOKEN = re.compile(r"^[A-Z0-9_-]+$")

# jsonpath output form — ``-o jsonpath=…`` / ``--output=jsonpath=…``, in
# EVERY standard kubectl output-flag spelling (``-o jsonpath``, ``-ojsonpath``,
# ``-o=jsonpath``, ``--output=jsonpath``, ``--output jsonpath``) AND with an
# optional shell quote between the flag and the value (``-o 'jsonpath=…'`` /
# ``-o "jsonpath=…"``). The quoted spelling is the DOMINANT real form: a
# template with ``{"\n"}`` field separators MUST be shell-quoted, so the LLM
# emits ``-o 'jsonpath=partition={…}'`` — a gate that only tolerated the
# unquoted spelling silently starved every production labeled-jsonpath read
# (extract_metrics → ``{}``), so those metrics never reached the timeline.
# Widening the ACTIVATION gate is safe: the anti-positional guard is the
# SEPARATE ``_LABELED_JSONPATH_PAIR`` template check below (a quoted template
# still carries its ``label={…}`` pairs), so no column-position guessing is
# admitted (memory 786c2f53). Note the suppression gate ``_NON_TABLE_OUTPUT``
# already recognized the quoted form via its bare ``jsonpath`` alternative —
# the activation/suppression asymmetry was the bug.
_JSONPATH_OUTPUT = re.compile(
    r"(?:-o|--output)\s*=?\s*['\"]?jsonpath", re.IGNORECASE
)

# The template body that follows ``jsonpath=``. Extracted so the label
# gate below scans ONLY the template, never the ``jsonpath=`` token
# itself — otherwise a bare-value form ``-o jsonpath={.spec…partition}``
# would false-positive (``jsonpath=`` looks like ``label={``). NOT DOTALL:
# a real command string is single-line (``hook._command_from_parent`` joins
# args with spaces), so ``.+`` stopping at the first actual newline keeps a
# reconstructed multi-command blob from leaking trailing prose into the
# gate. The template's ``{"\n"}`` separators are LITERAL backslash-n, not
# real newlines, so they survive the single-line capture.
_JSONPATH_TEMPLATE = re.compile(r"jsonpath=(.+)", re.IGNORECASE)

# A self-describing label immediately before a jsonpath expression. Two
# shapes are admitted, both carrying an EXPLICIT ``label=``:
#   1. newline/space form — ``partition={.spec…}`` (``word={`);
#   2. pipe-literal form — ``{.metadata.name}{"|partition="}{.spec…}``
#      where the label sits inside a quoted jsonpath string literal
#      (``{"…partition="}``), the shape the LLM uses for pipe-delimited
#      readbacks (D15 修法 2). Without (2) the baseline read was dropped
#      ({}), starving the timeline to a single-phase snapshot (Mode B).
# The command gate requires ≥1 such labeled pair before the output is read
# as key=value metrics — this is what separates a self-describing template
# (safe to read BY LABEL) from a positional/bare-value template
# (``{.a}|{.b}`` — no ``label=`` literal, still refused), which is never
# guessed by output column position (memory 786c2f53).
_LABELED_JSONPATH_PAIR = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*=\{"            # newline/space form: partition={…}
    r"|\{[\"'][^\"']*[A-Za-z_][A-Za-z0-9_]*=[\"']"  # pipe literal: {"|partition="}
)

# A ``label=value`` pair in the OUTPUT of a labeled jsonpath template.
# The value runs until whitespace OR a pipe: jsonpath templates separate
# fields with ``{"\n"}``, spaces, or ``{"|…"}`` pipe literals, so both are
# safe terminators (``mypod|partition=0|ready=3`` must split into
# ``partition=0`` / ``ready=3``, not swallow ``0|ready=3``). An empty value
# (``partition=`` — field absent) is dropped, matching the downstream
# _numeric_series/_distinct_values blank-skip.
_JSONPATH_LABELED_VALUE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s|]*)")

# A real kubectl jsonpath SCALAR value (partition number, replica count,
# revision hash, DNS-1123 name, percentage, phase, ``true:NoSchedule``) is a
# run of ASCII alphanumerics plus a small safe punctuation set. Anything
# outside it — CJK / full-width punctuation / backtick / brace / quote —
# means the "value" is leaked PROSE or an echoed jsonpath TEMPLATE fragment,
# not a metric. Dropping those keeps garbage keys (real run inject-7c25e537
# iteration-8 produced ``'Partition': '0`。'``, ``'Dedicated'``, ``'Ecs'``,
# ``'Jsonpath': "'replicas={…}"``) out of the timeline (D15 修法 2 guardrail).
# Annotation-JSON values (``{"k":"v"}``) are also dropped — they are
# non-numeric and consumed by no rule, so nothing is lost.
_JSONPATH_VALUE_OK = re.compile(r"^[A-Za-z0-9._/:%+-]*$")

# camelCase word boundary, to humanize a jsonpath label into a metric
# name (``currentRevision`` → ``Current revision``) consistent with the
# human-readable names the other parsers emit ("Spec replicas", "Node
# Ready"). Vocabulary-free: pure algorithmic splitting.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_metrics(
    tool_name: str,
    command: str,
    stdout: str,
    target_paths: Sequence[str] = (),
) -> dict[str, str]:
    """Extract structured metrics from a tool result.

    Dispatch is COMMAND-based for the kubectl family: a generic
    ``kubectl exec`` can run df, top, describe, or cat /proc/... and
    each needs a different parser, so only the command text
    disambiguates. The ONE tool-name-routed branch is ``http_probe`` —
    a dedicated typed probe whose result is born-structured JSON the
    framework itself produced, so the tool name (not a reconstructed
    command substring) is the authoritative routing key and the branch
    returns early. Every other tool falls through to the command-
    substring branches below.

    Args:
        tool_name: Tool identifier (e.g. ``"kubectl"``, ``"shell"``).
            Also the routing key for the ``http_probe`` early-return
            branch; for all other tools it is a diagnostic log tag only.
        command: The actual command/subcommand text (e.g. ``"describe
            pod xyz"``, ``"df -h"``, ``"exec ... cat /proc/diskstats"``).
        stdout: Raw command output. Empty input returns ``{}``.
        target_paths: Declared injection target mount path(s) — the
            authoritative key for TARGET-DRIVEN ``df`` extraction (D4/B2),
            derived from the fault_spec ``--path`` via
            :func:`disk_target_paths`. Empty (default) keeps the df parser
            on its additive overlay/nodefs alias behaviour only, so every
            caller that has no fault_spec context stays byte-identical.

    Returns:
        Dict of ``metric_name -> human_value`` (e.g.
        ``{"RestartCount": "8"}``). Empty when nothing matches.
    """
    if not stdout or not stdout.strip():
        return {}

    cmd_lower = (command or "").lower()
    metrics: dict[str, str] = {}

    # ── http_probe (typed HTTP probe tool) — TOOL-NAME dispatch, the
    #    sanctioned future use of the tool_name axis (see the docstring:
    #    "reserved for future use, e.g. routing vendor-specific outputs").
    #    Every other branch below is COMMAND-substring driven because a
    #    generic ``kubectl exec`` can run df/top/describe/cat and only the
    #    command text disambiguates. ``http_probe`` is the opposite: a
    #    dedicated tool whose result is born-structured JSON the framework
    #    itself produced, so the tool name — not a reconstructed command
    #    substring — is the authoritative routing key. Exclusive early
    #    return: the JSON carries no other command shape to parse.
    #    ``host_http_probe`` is the HOST-delivered sibling: it emits the SAME
    #    born-structured JSON (identical canonical metric keys), so it routes
    #    through the SAME parser by name — the tool-name axis admits both L7
    #    probes without a command-substring guess.
    if tool_name in ("http_probe", "host_http_probe"):
        metrics.update(_parse_http_probe(stdout))
        if metrics:
            logger.debug(
                "extract_metrics(%s, %r) → %s",
                tool_name, command[:60] if command else "", metrics,
            )
        return metrics

    # ── df family (host or container fs via kubectl exec) — ``df``,
    #    ``df -h``, ``df -k /tmp``: any standalone ``df`` token. The
    #    disk cases' primary evidence command is ``df -k`` (KB precision,
    #    mandated by the #9 quality bar over Use%'s coarse granularity) —
    #    the old "-h"-only literal left every verify-phase ``df -k`` call
    #    out of the metric timeline (no auto-summary, no cross-check
    #    ground truth for the primary evidence channel).
    #    ``target_paths`` (D4/B2) makes the df parser TARGET-DRIVEN: the
    #    declared injection mount path — not a static k8s-shape whitelist —
    #    is the authoritative key, so a dedicated partition target
    #    (``/drill-disk`` on a loop device) produces its own metric instead
    #    of being silently dropped in favour of the root overlay.
    if _DF_TOKEN.search(cmd_lower):
        metrics.update(_parse_df_usage(stdout, target_paths))

    # ── kubectl describe pod / po
    if "describe pod" in cmd_lower or "describe po " in cmd_lower:
        metrics.update(_parse_describe_pod(stdout))

    # ── kubectl get pod -o json (API server's authoritative shape)
    if ("get pod" in cmd_lower or "get po " in cmd_lower) and (
        "-o json" in cmd_lower or "-ojson" in cmd_lower or "--output=json" in cmd_lower
    ):
        metrics.update(_parse_get_pod_json(stdout))

    # ── kubectl get deployment/node -o json — the workload/node scopes'
    #    authoritative shape (m7, r68 review; measured: dispatch was
    #    pod-only so a deployment-scope ``get -o json`` — the r43 live
    #    case's mechanism readback form — produced NO structured metrics
    #    while the pod form did). Same -o json gate as the pod branch.
    if "-o json" in cmd_lower or "-ojson" in cmd_lower or "--output=json" in cmd_lower:
        if "get deployment" in cmd_lower or "get deploy " in cmd_lower:
            metrics.update(_parse_get_deployment_json(stdout))
        elif "get node" in cmd_lower or "get no " in cmd_lower:
            metrics.update(_parse_get_node_json(stdout))

    # ── kubectl get <resource> -o jsonpath=label={…} — self-describing
    #    labeled jsonpath, the LLM's dominant structured-readback form
    #    (measured: 70 jsonpath reads vs 0 ``-o json`` for StatefulSet
    #    across 23 tasks). It previously produced NO metrics —
    #    ``_NON_TABLE_OUTPUT`` only made the table parser SKIP it, no
    #    parser picked it up. Strict command gate: fires only when the
    #    template carries ≥1 explicit ``label={…}`` pair, so positional /
    #    bare-value templates are never guessed by output column position
    #    (memory 786c2f53). Mutually exclusive with the table branch below
    #    (that one excludes jsonpath via ``_NON_TABLE_OUTPUT``).
    if _JSONPATH_OUTPUT.search(cmd_lower):
        _tmpl = _JSONPATH_TEMPLATE.search(command or "")
        if _tmpl and _LABELED_JSONPATH_PAIR.search(_tmpl.group(1)):
            metrics.update(_parse_labeled_jsonpath(stdout))

    # ── kubectl top pod/node
    if "top pod" in cmd_lower or "top node" in cmd_lower:
        metrics.update(_parse_kubectl_top(stdout))

    # ── /proc/diskstats (write throughput)
    if "diskstats" in cmd_lower:
        metrics.update(_parse_diskstats(stdout))

    # ── /proc/stat (CPU iowait)
    if "/proc/stat" in cmd_lower:
        metrics.update(_parse_proc_stat(stdout))

    # ── du -s (target path size)
    if re.search(r"\bdu\b", cmd_lower) and ("-s" in cmd_lower or "-sh" in cmd_lower):
        metrics.update(_parse_du_sh(stdout))

    # ── dd transfer summary. POSIX puts dd's elapsed+rate line on STDERR
    #    (host_cmd / kubectl merge it under "--- STDERR ---"). Before this
    #    branch the extractor had NO dd parser, so any dd observation produced
    #    {} and never reached metric_observations. This is a GENERAL capability
    #    completion (the timeline is no longer blind to dd's rate/elapsed), NOT
    #    a case-criterion gap fix: the sanctioned disk-IO cases judge via
    #    ``iostat`` %util/w/s/iowait (``/proc/diskstats`` delta fallback), and
    #    dd appears there only as the injection burn payload and as a one-shot
    #    pre-injection environment probe (baseline throughput). So lifting dd's
    #    own timing is marginal — chiefly anti-truncation for that env-probe
    #    baseline if the raw output is later compacted — not a criterion the
    #    cross-check would otherwise miss. dd has NO typed probe tool to route
    #    through (unlike curl's http_probe), so a format-keyed parser here is
    #    the legitimate home — it does NOT violate the "no reverse-parsing
    #    hand-written shell" principle that reserves L7 HTTP to the typed probe.
    #    Gate on a standalone ``dd`` token; the parser keys on the OUTPUT
    #    format, so a ``cat`` of a file that merely mentions dd yields nothing.
    if _DD_TOKEN.search(cmd_lower):
        metrics.update(_parse_dd_timing(stdout))

    # ── kubectl logs (error pattern counts — feeds E4 side-effect detection)
    if " logs " in f" {cmd_lower} " or cmd_lower.startswith("logs "):
        metrics.update(_parse_kubectl_logs(stdout))

    # ── kubectl get <resource> — table data-row count. The one fact most
    #    often lost to the tool-compactor's 1KB historical demotion:
    #    #13-R's 92-pod namespace listing (9785B) demoted to a 1018B head
    #    lost the very number the plan was built around, costing 226s of
    #    re-probing. The [Auto-extracted] head carrying the count
    #    survives that demotion (head-only retention).
    if _GET_TOKEN.search(cmd_lower) and not _NON_TABLE_OUTPUT.search(cmd_lower):
        metrics.update(_parse_get_table_rows(stdout))

    if metrics:
        logger.debug(
            "extract_metrics(%s, %r) → %s",
            tool_name, command[:60] if command else "", metrics,
        )
    return metrics


# ---------------------------------------------------------------------------
# Per-format parsers — small, focused, return {} when nothing matches
# ---------------------------------------------------------------------------


# Canonical metric keys the ``http_probe`` tool emits, in a stable order. Kept
# in sync with ``tools/http_probe_cli._W_NAME_TO_METRIC`` — the tool's JSON, the
# keys the LLM sees (and cites in ``metric_claims``), and the keys this parser
# puts on the observation timeline must all agree byte-for-byte, because the
# deterministic cross-check does an EXACT-key lookup (no substring guessing).
_HTTP_PROBE_METRIC_KEYS = (
    "http_code",
    "time_connect",
    "time_starttransfer",
    "size_download",
    "exit_code",
)

# Capture-layer key prefixes ``_parse_http_probe`` adds when the probe ran in a
# ``capture`` mode beyond ``metadata``. Same byte-for-byte contract as above:
# the LLM cites ``header.<name>=<value>`` / ``body=<text>`` in its
# ``metric_claims`` and the cross-check does an exact-key lookup against these.
_HTTP_PROBE_HEADER_PREFIX = "header."
_HTTP_PROBE_BODY_KEY = "body"


def _flatten_headers(headers_raw: str) -> dict[str, str]:
    """Flatten curl's ``%{header_json}`` object into ``header.<lower-name>`` keys.

    ``http_probe`` (capture=headers/all) carries the raw ``header_json`` string:
    a JSON object mapping each lower-cased header name to a LIST of values
    (``{"x-drill-injected": ["true"], ...}``). This lifts every header onto a
    ``header.<name>`` timeline key so an LLM claim like
    ``header.x-drill-injected=true`` hits the deterministic cross-check's
    exact-key lookup. A single-value list yields the bare value; a multi-value
    list is comma-joined (HTTP list convention). Volatile headers (Date, Age…)
    are flattened too but simply never cited in a claim, so they cannot seed a
    spurious mismatch. Non-JSON / non-object input yields ``{}``.
    """
    try:
        data = json.loads(headers_raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for name, values in data.items():
        if isinstance(values, list):
            joined = ",".join(str(v) for v in values)
        elif values is None:
            continue
        else:
            joined = str(values)
        if joined == "":
            continue
        out[f"{_HTTP_PROBE_HEADER_PREFIX}{str(name).lower()}"] = joined
    return out


def _parse_http_probe(stdout: str) -> dict[str, str]:
    """Read metrics from the ``http_probe`` tool's born-structured JSON.

    ``http_probe`` returns a JSON object (provenance + the canonical metric
    keys). This lifts the metric fields straight out — no reverse-parsing of
    hand-written shell, which is the entire point of routing the L7 HTTP
    observation through a typed probe instead of an LLM-authored ``curl``.
    Values are stringified verbatim (the numeric cross-check parses them
    downstream); empty/absent keys are skipped so a partial probe never seeds a
    spurious ``""`` metric. Non-JSON / non-object input yields ``{}``.

    Beyond the five metadata keys, a ``capture=headers``/``all`` probe carries a
    raw ``headers`` (curl ``header_json``) string that is flattened to
    ``header.<name>`` keys (see :func:`_flatten_headers`), and a
    ``capture=body``/``all`` probe carries a ``body`` string lifted verbatim
    under the ``body`` key — so a header verdict (Case 1.7) or a body-tampering
    verdict (Case 1.6) reaches the timeline as an exact-match claim target
    without the LLM ever hand-writing a ``curl -D -`` / plain body curl.
    """
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key in _HTTP_PROBE_METRIC_KEYS:
        value = data.get(key)
        if value is None or value == "":
            continue
        out[key] = str(value)
    headers_raw = data.get("headers")
    if isinstance(headers_raw, str) and headers_raw:
        out.update(_flatten_headers(headers_raw))
    body = data.get("body")
    if isinstance(body, str) and body != "":
        out[_HTTP_PROBE_BODY_KEY] = body
    return out


def disk_target_paths(spec: Any) -> tuple[str, ...]:
    """The declared disk-fault target mount path(s), delegated to the
    ``FaultSpec`` — which is the SINGLE authority for this fact (D4/B2).

    This is a thin None-safe adapter over ``FaultSpec.disk_mount_targets``;
    the resolution logic lives on the spec that owns the fact, NOT here and
    NOT smeared across downstream fields. (An earlier version read
    ``params["path"]`` directly and declared it authoritative — but A/A run
    inject-0242c9d6 proved the real NL host disk-fill leaves ``params`` empty
    and stores the mount in ``names``, so keying on ``params["path"]``
    silently dropped the ``/drill-disk`` row. See ``disk_mount_targets`` for
    the corrected precedence.)

    ChaosBlade's disk fill/burn ``--path`` flag is the injection's own
    declaration of WHICH filesystem it saturates; that declaration — not a
    static enumeration of k8s-era mount shapes — is the ground truth the df
    evidence must be keyed on. The old whitelist (``/``→overlay,
    ``/host``→nodefs) silently dropped any other partition.

    Empty for a non-disk fault, a spec that is None, or when no path is
    declared — the df parser then keeps only its additive overlay/nodefs
    alias behaviour, so every root-fs k8s target stays byte-identical.
    """
    resolver = getattr(spec, "disk_mount_targets", None)
    if resolver is None:
        return ()
    return tuple(resolver)


def _df_alias_key(mount: str, filesystem: str) -> str | None:
    """Canonical alias metric key for a root/host df row, else ``None``.

    The k8s-era shapes the timeline's downstream consumers hardcode by
    literal name (``_FILL_USAGE_KEYS``, case frontmatter
    ``declared_expected``, the PINNED deterministic rules):
      - ``/`` or an ``overlay*`` filesystem → "Disk usage (overlay)"
      - ``/host``                          → "Disk usage (nodefs)"
    A dedicated partition target (``/drill-disk``) matches neither, so it is
    keyed by its own mount instead (see ``_parse_df_usage``).
    """
    if mount == "/" or filesystem.startswith("overlay"):
        return "Disk usage (overlay)"
    if mount == "/host":
        return "Disk usage (nodefs)"
    return None


def _normalise_mount(path: str) -> str:
    """Strip a trailing slash so ``/drill-disk/`` matches ``/drill-disk``;
    the root ``/`` is preserved as-is."""
    p = (path or "").strip()
    if len(p) > 1 and p.endswith("/"):
        return p.rstrip("/") or "/"
    return p


def _best_df_row_for(
    target_path: str,
    rows: list[tuple[str, str, str]],
) -> tuple[str, str, str] | None:
    """The df row whose mount best covers ``target_path``.

    Exact-mount match wins — a dedicated partition target such as
    ``/drill-disk`` has its own row (the B2 case the old whitelist dropped).
    Otherwise the LONGEST mount that is a path-prefix of the target
    (``/var/log`` for ``/var/log/app``; ``/`` for ``/tmp``): a sub-directory
    target's disk usage IS its parent mount's usage, so the parent row
    legitimately covers it. ``None`` only when NO row is a prefix at all (df
    captured nothing relevant → a real coverage gap). Resolves the design's
    Open Question: exact preferred, longest-prefix fallback.
    """
    target = _normalise_mount(target_path)
    if not target:
        return None
    for row in rows:
        if _normalise_mount(row[0]) == target:
            return row
    best: tuple[str, str, str] | None = None
    best_len = -1
    for row in rows:
        m = _normalise_mount(row[0])
        if not m:
            continue
        base = m.rstrip("/")  # "/" → "", "/var/log" → "/var/log"
        if target == m or target.startswith(base + "/"):
            if len(m) > best_len:
                best, best_len = row, len(m)
    return best


def _parse_df_usage(
    stdout: str,
    target_paths: Sequence[str] = (),
) -> dict[str, str]:
    """``df`` family (``df``, ``df -h``, ``df -k``): disk usage keyed by the
    DECLARED injection target (D4/B2), with additive root/host aliases.

    Same 6-column shape across the byte-usage variants — only the units
    differ (1K-blocks vs human); the value keeps the raw
    ``used/total`` strings, so a ``-k`` reading lands as
    ``"32% (37384168/123456789)"``.

    Two ADDITIVE passes over the same rows:
      1. Alias pass (UNCHANGED — byte-identical to pre-D4): ``/`` or an
         ``overlay*`` filesystem → "Disk usage (overlay)"; ``/host`` →
         "Disk usage (nodefs)". These literal keys are hardcoded by the
         deterministic rule chain (``_FILL_USAGE_KEYS``), case frontmatter and
         the PINNED rule tests, so they are NEVER renamed or removed.
      2. Target-driven pass: for each declared ``target_paths`` entry, the
         best-covering df row (exact mount, else longest-prefix) is emitted as
         "Disk usage (<mount>)" — UNLESS that row is already alias-covered, so
         a root-fs target (``/tmp`` on overlay) adds no new key and the k8s
         domain stays byte-identical. A dedicated partition target
         (``/drill-disk``) is not alias-covered, so it produces its own key
         instead of being dropped.

    When a declared target has NO covering row at all (df captured nothing
    relevant — a genuine coverage gap) a warning is logged and NOTHING is
    emitted for it: the parser MUST NOT relabel a root-fs metric as the target
    (the silent impersonation the spec forbids).

    Rejected formats (no signal beats a mislabeled signal):
      - ``df -i`` — inode table: "IUse%" is not disk usage (header
        carries "Inodes")
      - ``df -T*`` — extra Type column shifts Use% off position; the
        per-row "%"-suffix check drops those rows
    """
    if "Inodes" in stdout:  # df -i header token — inode mode, not bytes
        return {}
    metrics: dict[str, str] = {}
    rows: list[tuple[str, str, str]] = []  # (mount, filesystem, value)
    for line in stdout.strip().splitlines():
        parts = line.strip().split()
        if len(parts) < 6:
            continue
        if parts[0] == "Filesystem":
            continue
        use_pct, mount = parts[4], parts[5]
        if not use_pct.endswith("%"):
            continue  # column layout shifted (e.g. df -T) — refuse to guess
        value = f"{use_pct} ({parts[2]}/{parts[1]})"
        rows.append((mount, parts[0], value))
        # Pass 1 — additive alias (byte-identical to the pre-D4 behaviour).
        alias_key = _df_alias_key(mount, parts[0])
        if alias_key is not None:
            metrics[alias_key] = value

    # Pass 2 — target-driven (D4/B2): the declared target path is the key.
    for target_path in target_paths or ():
        row = _best_df_row_for(target_path, rows)
        if row is None:
            logger.warning(
                "df evidence has no row covering declared disk target %r "
                "(mounts seen: %s) — extractor coverage gap; NOT relabeling "
                "a root-fs metric as the target",
                target_path,
                ", ".join(m for m, _, _ in rows) or "(none)",
            )
            continue
        mount, filesystem, value = row
        if _df_alias_key(mount, filesystem) is not None:
            # Covering fs is the root/host alias — pass 1 already emitted its
            # canonical key. A mount-named duplicate (e.g. "Disk usage (/)"
            # for a /tmp-on-overlay target) would break byte-identical, so the
            # alias key stands as the target metric.
            continue
        metrics.setdefault(f"Disk usage ({mount})", value)
    return metrics


# Top-level section header of a ``kubectl describe`` block print — a line
# that is ONLY a title (``Containers:``, ``Init Containers:``,
# ``Ephemeral Containers:``, ``Conditions:`` …). Field lines always carry a
# value after the colon, so a value-less ``Name:``-at-line-end can never be
# a field. Matched on the RAW line: stripping first would erase the
# top-level-vs-indented distinction the section tracking depends on.
_DESCRIBE_SECTION = re.compile(r"^([A-Za-z][A-Za-z ]*):[ \t]*$")


def _parse_describe_pod(stdout: str) -> dict[str, str]:
    """``kubectl describe pod``: restart count, container ID, ready state,
    termination reason.

    Section-aware. ``Restart Count`` / ``Container ID`` / termination
    ``Reason`` lines repeat under EVERY container section — business
    ``Containers:``, ``Init Containers:``, and, once a pod-scoped
    ``kubectl debug`` has EVER run against the pod, a permanently
    accumulating ``Ephemeral Containers:`` section (ephemeral containers
    survive until the pod is recreated; a reused drill target accumulated
    26 dead debuggers — 577 ephemeral lines after the 22-line business
    section). Old last-wins logic read the LAST ephemeral container's
    values as the pod's metrics (live case inject-3dae7b4f: baseline
    RestartCount 0 / debugger container ID vs business truth 329). Only
    the business ``Containers:`` section feeds those metrics now.
    ``Pod Ready`` is section-independent: its pattern matches the
    top-level Conditions table (``Ready   True``, no colon), never the
    per-container ``Ready:`` field — so it keeps scanning the whole
    output.
    """
    metrics: dict[str, str] = {}
    in_business_section = False
    for raw_line in stdout.splitlines():
        section = _DESCRIBE_SECTION.match(raw_line)
        if section:
            in_business_section = section.group(1) == "Containers"
            continue
        if not in_business_section:
            continue
        s = raw_line.strip()
        # ``Restart Count:  8`` (the variable-whitespace form 'describe' uses)
        if "Restart Count" in s:
            try:
                metrics["RestartCount"] = str(int(s.split()[-1]))
            except (ValueError, IndexError):
                pass
        # ``Container ID:  containerd://a1b2…`` — the last token is the
        # runtime-qualified ID. A change across the timeline is the
        # deterministic kill signature (process kill → container
        # replacement). Last occurrence wins within the business section
        # (multi-container pods: the rule layer sees the last container).
        if "Container ID" in s:
            _cid = s.split()[-1] if s.split() else ""
            if _cid and "://" in _cid:
                metrics["Container ID"] = _cid
        # Last termination reason
        if "Reason:" in s:
            for kw in ("OOMKilled", "Error", "Completed", "Evicted"):
                if kw in s:
                    metrics["Last termination reason"] = kw
                    break
    # Pod Ready — top-level Conditions table only (see docstring).
    for raw_line in stdout.splitlines():
        m = re.search(r"^\s*Ready\s+(True|False)\b", raw_line)
        if m:
            metrics["Pod Ready"] = m.group(1)
    return metrics


def _parse_get_pod_json(stdout: str) -> dict[str, str]:
    """``kubectl get pod -o json``: canonical structured pod state.

    More reliable than ``describe pod`` parsing — same fields but from
    the API server's authoritative JSON instead of human-formatted
    text. When both run, the JSON path wins on metric collisions
    because ``extract_metrics`` runs them in dict-update order with
    describe earlier.
    """
    metrics: dict[str, str] = {}
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return metrics

    items = (
        data.get("items", []) if isinstance(data, dict) and data.get("kind") == "PodList"
        else [data]
    )

    for pod in items:
        if not isinstance(pod, dict):
            continue
        status = pod.get("status") or {}
        cs_list = status.get("containerStatuses") or []
        if cs_list:
            cs = cs_list[0]  # first container — caller can re-call per-container
            if "restartCount" in cs:
                metrics["RestartCount"] = str(cs.get("restartCount", 0))
            # Container replacement signature (see _parse_describe_pod);
            # empty containerID (pod not yet started) is skipped.
            _cid = cs.get("containerID") or ""
            if _cid:
                metrics["Container ID"] = _cid
            if "ready" in cs:
                metrics["Pod Ready"] = "True" if cs["ready"] else "False"
            terminated = (cs.get("lastState") or {}).get("terminated") or {}
            if terminated.get("reason"):
                metrics["Last termination reason"] = terminated["reason"]
        # Pod-level phase (Running / Pending / Failed / Succeeded / Unknown)
        if status.get("phase"):
            metrics["Pod phase"] = status["phase"]
        # Top-level Ready condition (overrides cs.ready when set; more
        # authoritative for "pod overall ready" semantics)
        for cond in status.get("conditions") or []:
            if isinstance(cond, dict) and cond.get("type") == "Ready":
                metrics["Pod Ready"] = cond.get("status", "Unknown")
    return metrics


def _parse_get_deployment_json(stdout: str) -> dict[str, str]:
    """``kubectl get deployment -o json``: workload replica state.

    m7 (r68 review): the r43 live case's scope — its mechanism readback
    (``get deployment -o json``) previously produced NO structured
    metrics because the JSON dispatch was pod-only. Fields follow the
    workload drills' judgement vocabulary (replica mismatch, HPA cap,
    scaled-down): spec.replicas vs status.readyReplicas/replicas/
    unavailableReplicas.

    r68 self-review C1 (measured in .b4tmp/r68_review_c1_probe.py): the
    nameless form ``get deployments -o json`` returns the GENERIC List
    shape (``{"kind": "List", "items": […]}`` — unlike pods' dedicated
    ``PodList`` kind) and used to yield ``{}`` while the pod parser's
    List precedent yielded metrics. Unrolled the same way: per-item
    extraction, key collisions last-wins (a multi-deployment listing
    reports the LAST item — same policy as the pod parser).
    """
    metrics: dict[str, str] = {}
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return metrics
    if not isinstance(data, dict):
        return metrics
    items = data.get("items", []) if data.get("kind") == "List" else [data]
    for dep in items:
        if not isinstance(dep, dict):
            continue
        spec = dep.get("spec") or {}
        status = dep.get("status") or {}
        if "replicas" in spec:
            metrics["Spec replicas"] = str(spec["replicas"])
        for field in ("readyReplicas", "availableReplicas", "unavailableReplicas", "replicas"):
            if field in status:
                metrics[f"Status {field}"] = str(status[field])
    return metrics


def _parse_get_node_json(stdout: str) -> dict[str, str]:
    """``kubectl get node -o json``: node condition state.

    m7 (r68 review): the node drills' judgement vocabulary is the
    conditions array (MemoryPressure / DiskPressure / Ready / …) — the
    JSON form is the API server's authoritative shape.

    r68 self-review C1: same generic-List unroll as the deployment
    parser (nameless ``get nodes -o json``) — per-item extraction,
    key collisions last-wins.
    """
    metrics: dict[str, str] = {}
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return metrics
    if not isinstance(data, dict):
        return metrics
    items = data.get("items", []) if data.get("kind") == "List" else [data]
    for node in items:
        if not isinstance(node, dict):
            continue
        for cond in (node.get("status") or {}).get("conditions") or []:
            if isinstance(cond, dict) and cond.get("type"):
                metrics[f"Node {cond['type']}"] = str(cond.get("status", "Unknown"))
    return metrics


def _humanize_jsonpath_label(label: str) -> str:
    """``currentRevision`` → ``Current revision``; ``partition`` → ``Partition``.

    Split on camelCase boundaries, lowercase the words, capitalize the
    first letter — a deterministic, vocabulary-free humanization matching
    the metric-name convention the other parsers use.
    """
    words = _CAMEL_BOUNDARY.sub(" ", label).split()
    if not words:
        return label
    joined = " ".join(w.lower() for w in words)
    return joined[:1].upper() + joined[1:]


def _parse_labeled_jsonpath(stdout: str) -> dict[str, str]:
    """Self-describing labeled jsonpath output → named metrics.

    The command gate (in ``extract_metrics``) has already confirmed the
    template carries explicit ``label={…}`` pairs, so the OUTPUT is
    ``label=value`` text (fields separated by ``{"\n"}`` or spaces). Each
    pair becomes a metric named by humanizing the label, so the
    deterministic-rule layer reads the same human-readable names the other
    parsers produce (e.g. ``Partition``, ``Current revision``).

    Deliberately GENERAL — no preset vocabulary: whatever labels the LLM's
    template emitted are captured. A label the rule doesn't recognize is
    simply never read; a DRIFTED label (``readyReplicas=`` ↔ ``ready=``)
    makes the rule's expected metric absent → the rule abstains to
    ``unknown`` (design D11). Positional / bare-value forms are refused at
    the command gate, never guessed here.

    Duplicate labels (multi-pod listings sharing a selector): last wins —
    the same collision policy as the pod/deployment/node JSON parsers.
    """
    metrics: dict[str, str] = {}
    for m in _JSONPATH_LABELED_VALUE.finditer(stdout):
        value = m.group(2)
        if not value:  # empty (field absent) → skip; downstream skips blanks too
            continue
        if not _JSONPATH_VALUE_OK.match(value):
            # CJK / full-width punctuation / backtick / brace / quote → the
            # "value" is leaked PROSE or an echoed jsonpath TEMPLATE fragment,
            # not a metric scalar (real run inject-7c25e537 iteration-8:
            # ``'Partition': '0`。'``, ``'Dedicated'``, ``'Ecs'``,
            # ``'Jsonpath': "'replicas={…}"``). Drop it — no rule consumes a
            # non-numeric prose value, so nothing is lost (D15 修法 2).
            continue
        metrics[_humanize_jsonpath_label(m.group(1))] = value
    return metrics


def _parse_kubectl_top(stdout: str) -> dict[str, str]:
    """``kubectl top pod/node``: current CPU/Memory usage.

    Returns only the FIRST data row's metrics. For multi-pod tables the
    caller is responsible for splitting by pod name before calling.
    """
    metrics: dict[str, str] = {}
    lines = stdout.strip().splitlines()
    if len(lines) < 2:
        return metrics

    # Header row(s) get skipped — kubectl top can echo header multiple
    # times if the output spans namespaces.
    for line in lines:
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        if parts[0].upper() in ("NAME", "POD", "NODE"):
            continue
        cpu_field = parts[1]
        mem_field = parts[2]
        # CPU in millicores ("250m") or cores ("0.5"); memory in Mi/Gi.
        if cpu_field and (cpu_field[-1] in "mn" or cpu_field.replace(".", "").isdigit()):
            metrics["CPU usage"] = cpu_field
        if mem_field.lower().endswith(("mi", "gi", "ki")):
            metrics["Memory usage"] = mem_field
        break  # first data row only
    return metrics


def _parse_diskstats(stdout: str) -> dict[str, str]:
    """``/proc/diskstats``: per-device write sector counters.

    All physical devices (loop/ram/sr/fd excluded) are reported — the
    injection landing device depends on the case: root-disk overlay
    injections (pod-disk burn/fill with ``--path /``) land on the system
    disk (vda/sda, often a partition like vda3) while PVC-backed ones land
    on the data disk. A hardcoded device pick (the old vdb/sdb guess)
    silently mis-baselines root-disk injections — Case #10 evidence: IO
    burn landed on vda3 while the extracted baseline metric pointed at
    vdb, leaving the baseline layer blind to the actual landing device.
    Downstream consumers select the device to compare (the
    ``_filter_metrics_by_fault`` prefix match keeps every
    ``Disk writes (...)`` key for disk faults).

    Field positions per ``Documentation/iostats.txt``:
      [0] major  [1] minor  [2] device-name  …  [9] sectors-written
    """
    metrics: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        parts = line.strip().split()
        if len(parts) < 11:
            continue
        name = parts[2]
        # zram excluded alongside loop/ram: RAM-backed swap — its writes
        # are memory ops, not disk traffic.
        if name.startswith(("loop", "ram", "zram", "sr", "fd")):
            continue
        metrics[f"Disk writes ({name})"] = f"{parts[9]} sectors"
    return metrics


def _parse_proc_stat(stdout: str) -> dict[str, str]:
    """``/proc/stat`` first ``cpu`` line: aggregate iowait percentage.

    Skips per-CPU rows (``cpu0`` etc.) — the first row is the
    cluster-wide aggregate, which is what we want for "is the node
    waiting on IO?"
    """
    for line in stdout.strip().splitlines():
        if line.startswith("cpu ") and not line.startswith("cpu0"):
            parts = line.strip().split()
            if len(parts) >= 6:
                try:
                    idle = int(parts[4])
                    iowait = int(parts[5])
                    pct = round(iowait / max(idle + iowait, 1) * 100, 1)
                    return {"CPU iowait %": f"{pct}%"}
                except (ValueError, ZeroDivisionError):
                    pass
    return {}


def _parse_du_sh(stdout: str) -> dict[str, str]:
    """``du -s[h]``: first column of first row is target path size."""
    for line in stdout.strip().splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0]:
            return {"Target path size": parts[0]}
    return {}


# dd's transfer summary — the throughput line POSIX puts on STDERR. Grounded
# in the real GNU coreutils / busybox dd forms:
#   GNU coreutils: ``524288 bytes (524 kB, 512 KiB) copied, 0.0012 s, 437 MB/s``
#   busybox:       ``524288 bytes (512.0KB) copied, 0.001234 seconds, 425.0MB/s``
# The ``(…)`` size aside contains COMMAS, so the pre-``copied`` gap is a
# non-greedy ``.*?`` — deliberately single-line (NO re.DOTALL) so the gap can
# never span into unrelated output (the cabb5702 DOTALL-greed guard). Elapsed
# unit covers ``s`` / ``sec(s)`` / ``second(s)``; rate covers the SI/IEC
# ``B/s`` family (``MB/s``, ``MiB/s``, ``kB/s``, ``GB/s``, busybox ``MB/s``
# with no space). The rate is optional: ``status=none`` prints nothing (→ no
# match → {}) and a truncated transfer may omit it, so elapsed alone still
# yields T_base.
_DD_TIMING_LINE = re.compile(
    r"(?P<bytes>\d+)\s+bytes?\b.*?copied,\s*"
    r"(?P<elapsed>\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b"
    r"(?:\s*,\s*(?P<rate>\d+(?:\.\d+)?\s*[KMGT]?i?B/s))?",
    re.IGNORECASE,
)


def _parse_dd_timing(text: str) -> dict[str, str]:
    """``dd``'s transfer summary → elapsed / rate / bytes on the timeline.

    Scans EVERY line of the (merged) tool content, so the summary is found
    whether it rides raw stdout or the ``--- STDERR ---`` section host_cmd /
    kubectl merge in. Returns the FIRST timing line's numbers — a probe runs
    one dd, and a loop-form payload's repeated lines are all the same shape
    (the head one is as good as any for the before/after ratio). Keys are NEW
    and additive ("Transfer elapsed (s)" / "Transfer rate" / "Transfer
    bytes"): no PINNED deterministic rule or case ``declared_expected`` binds
    them today (a60bab47 — additive, never a rename), so they enter the
    timeline + ``[Auto-extracted]`` head without disturbing any consumer.
    Empty when no ``copied,`` line is present (``status=none``, a non-dd
    command whose text merely mentions dd, or a transfer that died first).
    """
    for line in text.splitlines():
        m = _DD_TIMING_LINE.search(line)
        if not m:
            continue
        out: dict[str, str] = {}
        nbytes = m.group("bytes")
        elapsed = m.group("elapsed")
        rate = m.group("rate")
        if nbytes:
            out["Transfer bytes"] = nbytes
        if elapsed:
            out["Transfer elapsed (s)"] = elapsed
        if rate:
            # Normalize the internal space (``437 MB/s`` → ``437 MB/s`` kept
            # verbatim; ``425.0MB/s`` likewise) — the value is stringified for
            # the head prefix and the numeric cross-check parses the leading
            # number, so the unit suffix is carried for the LLM to read.
            out["Transfer rate"] = rate.strip()
        if out:
            return out
    return {}


# Patterns whose mere presence (count > 0) in logs is a side-effect
# signal. The verifier verdict cross-check uses these to flag the
# "no errors observed" hallucination — if extractor saw 12 OOMKilled
# log lines but LLM claims "no errors observed", that's contradiction.
_ERR_PATTERNS = (
    "OOMKilled", "CrashLoopBackOff", "panic:", "connection refused",
    "no space left", "DiskPressure", "MemoryPressure", "Evicted",
)


def _parse_kubectl_logs(stdout: str) -> dict[str, str]:
    """``kubectl logs``: count occurrences of known error patterns.

    Returns one metric per pattern that appeared at least once. Empty
    when logs are clean — explicit "0 errors" would mislead the verdict
    cross-check into asserting a positive ground truth where the
    extractor really has no signal.
    """
    counts: dict[str, int] = {}
    for pattern in _ERR_PATTERNS:
        n = stdout.count(pattern)
        if n > 0:
            counts[pattern] = n
    return {
        f"Log: {pattern} count": str(n)
        for pattern, n in counts.items()
    }


def _parse_get_table_rows(stdout: str) -> dict[str, str]:
    """``kubectl get <resource>`` table listing: number of data rows.

    The count is the fact most often lost to the tool-compactor's 1KB
    historical demotion — a 92-pod namespace listing (9785B) demoted to
    a 1018B head loses the very number the plan was built around
    (#13-R: 226s of re-probing to recover it). The [Auto-extracted]
    head carrying ``List rows=92`` survives that demotion.

    Format contract (deliberately narrow, kubectl-only):
      - header row: every whitespace token matches ``[A-Z0-9_-]+``
        (NAME, READY, UP-TO-DATE …) → data rows = non-empty lines − 1
      - ``--no-headers`` / ``-o name`` listings: no header → data
        rows = non-empty lines
      - empty listings (``No resources found`` / the kubectl tool's
        ``💡 No resources matched`` hint) and error envelopes
        (``Error: …``) carry no row signal → ``{}``

    A data row that happens to be all-caps tokens would overcount by
    one header; accepted — kubectl data rows (lowercase hashes, IPs,
    ages, timestamps) never matched that shape in practice.
    """
    if stdout.lstrip().startswith("Error:"):
        return {}
    lower = stdout.lower()
    if "no resources found" in lower or "no resources matched" in lower:
        return {}
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return {}
    first_tokens = lines[0].split()
    if all(_TABLE_HEADER_TOKEN.match(t) for t in first_tokens):
        rows = len(lines) - 1
    else:
        rows = len(lines)
    if rows <= 0:
        return {}
    return {"List rows": str(rows)}


# ---------------------------------------------------------------------------
# Baseline integration — backward-compat wrapper used by
# ``_verifier_hints._extract_baseline_key_metrics``.
# ---------------------------------------------------------------------------


def extract_baseline_metrics(
    baseline: dict[str, Any] | None,
    fault_target: str,
    fault_action: str,
    target_paths: Sequence[str] = (),
) -> dict[str, str]:
    """Walk a baseline ``observations`` list and aggregate metrics.

    Each observation dict carries ``{"command": str, "stdout": str,
    "description": str, "exit_code": int}``. We use ``command`` as the
    primary dispatch key (matches what the LLM actually ran) and fall
    back to ``description`` for fixtures / older traces that didn't
    record the raw command.

    ``target_paths`` (D4/B2) is the declared disk-fault mount path (via
    :func:`disk_target_paths`) threaded down to the df parser so the
    baseline key metric is keyed on the TARGET filesystem, not the root
    overlay the old whitelist fell back to.

    The returned dict is filtered to fault-relevant metrics so the
    Layer 2 prompt only sees what the LLM should compare against (e.g.
    a disk-fill fault doesn't need iowait noise from the pod's CPU
    counter).
    """
    metrics: dict[str, str] = {}
    if not baseline:
        return metrics

    for obs in baseline.get("observations", []) or []:
        if obs.get("exit_code") != 0:
            continue
        stdout = obs.get("stdout", "") or ""
        if not stdout:
            continue
        # Dispatch key: prefer recorded command, fall back to description.
        cmd = obs.get("command") or obs.get("description", "") or ""
        metrics.update(extract_metrics("baseline", cmd, stdout, target_paths))

    return _filter_metrics_by_fault(metrics, fault_target, fault_action)


# ---------------------------------------------------------------------------
# Fault-type filter — keeps Layer 2 prompt focused on what matters
# (preserves the legacy behaviour from _verifier_hints._filter_metrics_by_fault)
# ---------------------------------------------------------------------------

_ALWAYS_KEEP = frozenset({
    "RestartCount", "Pod Ready", "Pod phase",
    "Last termination reason", "Target path size",
    # Container replacement signature — same tier as RestartCount (pod
    # identity facts, relevant to every fault family's timeline).
    "Container ID",
})

_FAULT_METRICS: dict[str, frozenset[str]] = {
    "disk": frozenset({"Disk usage", "Disk writes", "CPU iowait", "Target path size"}),
    "cpu": frozenset({"CPU usage", "Memory usage", "CPU iowait"}),
    "mem": frozenset({"Memory usage", "CPU usage"}),
    "network": frozenset({"Memory usage", "CPU usage"}),
    "process": frozenset({"RestartCount", "Pod Ready"}),
}


def _filter_metrics_by_fault(
    metrics: dict[str, str],
    fault_target: str,
    fault_action: str,
) -> dict[str, str]:
    """Keep only fault-relevant metrics (prefix-match on metric name)."""
    target_key = (fault_target or "").lower()
    if target_key not in _FAULT_METRICS:
        # Unknown fault: pass-through with always-keep set merged in.
        return dict(metrics)

    relevant: set[str] = set(_ALWAYS_KEEP)
    for prefix in _FAULT_METRICS[target_key]:
        for k in metrics:
            if k.lower().startswith(prefix.lower()):
                relevant.add(k)
    return {k: v for k, v in metrics.items() if k in relevant}


# ---------------------------------------------------------------------------
# D17 — value-presence fingerprint (truth-side coverage-gap breaker)
# ---------------------------------------------------------------------------
# The observation timeline only carries what ``extract_metrics``' FIXED
# per-format vocabulary parses; free-form exec output (ping, /proc/net/*,
# ``tc -s qdisc``, ss, ``iptables -L``) yields ``{}`` and never enters the
# timeline, so cross_check's exact-key lookup cannot corroborate those
# claims (D12's unknown-key warn-only branch — absence is symmetric between
# a coverage gap and a hallucination). The fingerprint is a FORMAT-AGNOSTIC
# harvest of the numeric tokens in the RAW tool output, captured at hook time
# (before the 1KB compactor truncation) and stored per ``tool_call_id``.
#
# ONE harvest, TWO views — because the two inferences cross_check draws from it
# need OPPOSITE biases, and a single context-free token set cannot serve both.
# That conflation was the defect: a digit run inside a hex experiment UID
# (``932`` of ``932eaa6bd493bf54``) or an ISO date (``-10`` of ``2026-10-27``,
# where the sign is really a separator) satisfied the same membership test a
# genuine metric satisfies, so an incidental collision certified a hallucinated
# value as REAL and thereby suppressed T3's fabrication ruling.
#   ``tokens``         context-free, deliberately OVER-inclusive. Serves the
#                      ABSENCE proof, which must never narrow: a ``kind=raw``
#                      claim anchored to a COMPLETE harvest that provably lacks
#                      the value is a fabrication (T3, may downgrade). Dropping a
#                      real number here would manufacture a false accusation —
#                      the D12 failure this whole design exists to avoid.
#   ``numeric_fields`` the subset the output delimits as standalone numbers
#                      (``369``, ``30%``, ``8001:``, ``3005ms``, ``0b``). Serves
#                      the PRESENCE certification (T2, never downgrades).
#                      Under-harvest here is safe by construction: it can only
#                      turn a positive "corroborated" into an honest "abstain",
#                      never into a fabrication — so T3's trigger set is
#                      byte-identical whether or not this view exists.
# Fail-open everywhere: no fingerprint, an incomplete harvest, an unmappable
# source, or an embedded-only hit all abstain to the honest coverage-gap
# warning.

# A bare numeric literal: optional sign, digits, optional fraction, optional
# exponent. Deliberately format-agnostic — no unit awareness, because the whole
# point of ``tokens`` is to harvest values the per-format parsers do NOT
# understand. Trailing units (%, ms, Mi) fall outside the match boundary.
# POSITION awareness is applied separately, and ONLY to the ``numeric_fields``
# view (see ``_is_presented_as_number``) — leaving ``tokens`` exhaustive is what
# keeps T3's absence proof sound.
_FINGERPRINT_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")

# Generous cap on unique tokens per observation. Beyond it the output is so
# number-dense that value-presence is near-guaranteed (T2 fail-open) and
# absence is unprovable, so the entry is marked incomplete and T3 abstains.
# The netem / tc / df / describe outputs this targets are orders of magnitude
# smaller; the cap only guards pathological dumps from bloating checkpoint
# state. Completeness is what makes T3's absence proof sound, so an entry
# over the cap MUST NOT be used to downgrade.
_FINGERPRINT_TOKEN_CAP = 2000


def _is_ident_char(ch: str) -> bool:
    """Identifier-continuation character: alphanumeric or ``_``.

    ``str.isalnum()`` alone would admit ``_``-joined runs (``max_open_files``),
    slicing a "number" out of an identifier.
    """
    return ch.isalnum() or ch == "_"


# Punctuation that JOINS numeric components into one compound literal rather
# than delimiting a reported value: ``07:34:40``, ``2026-10-27``, ``1.5.2``,
# ``4pkts/336bytes``.
_NUMERIC_JOINERS = frozenset(":-./")


def _is_presented_as_number(content: str, start: int, end: int) -> bool:
    """Whether the numeric match ``content[start:end]`` is a number the OUTPUT
    presents as a number, rather than a fragment of a longer literal.

    D17's only position-aware step, feeding ONLY the ``numeric_fields`` view —
    ``tokens`` stays context-free so T3's absence proof remains exhaustive.
    One principle, two kinds of fragment:

    (a) EMBEDDED IN A WORD. LEFT must be string start or a non-identifier
        character; RIGHT must be string end, a non-identifier character
        (``369 ``, ``30%``, ``8001:``, ``4pkts/336bytes``), or a PURELY
        ALPHABETIC run ending at a non-identifier boundary — the unit shapes
        ``3005ms``, ``0b``, ``1.7G``. A run continuing into digits is an
        identifier, not a unit, which excludes the ``932`` of
        ``932eaa6bd493bf54``. The LEFT rule also rejects a sign that is really
        an in-token separator: ``2026-10-27`` matches ``-10`` whose sign is
        preceded by the digit ``6``, so that negative token is a date artifact
        (a genuine ``delta: -5`` or ``(-3)`` is delimiter-preceded and passes).

    (b) INTERIOR OF A COMPOUND LITERAL — flanked by ``_NUMERIC_JOINERS`` on
        BOTH sides, e.g. the ``34`` of ``07:34:40``. A reported value is
        delimited by its context, not wedged between two joiners. The end
        components (``07``, ``40``) stay fields, which is the conservative
        direction: over-admitting a component can only abstain-ward, never
        accusation-ward.

        ONE carve-out: a DECIMAL component flanked by ``/`` on both sides is an
        entry of a VALUE LIST, not a compound-literal component — the ping
        summary ``rtt min/avg/max/mdev = 1008.584/1012.525/1019.390/4.092 ms``
        reports four independent measurements, so its interior ``avg``/``max``
        are genuine reported values. Real ``/``-joined compound literals are
        integer-component (dates ``2026/10/27``), as are the other joiners
        (``07:34:40``, ``1.5.2``), so requiring a decimal point separates the
        two cleanly. Demoting the ping avg/max is exactly the "legitimate
        corroboration lost" D17 forbids — its raison d'etre is to prove ping
        values from raw output. The carve-out is fail-open safe: widening
        ``numeric_fields`` can only move a claim corroborated-ward, never
        accusation-ward (T3 keys on the unchanged exhaustive ``tokens``).

    Returns False for an out-of-range slice, so a stale span can never certify.
    """
    if not (0 <= start < end <= len(content)):
        return False
    left = content[start - 1] if start > 0 else ""
    if left and _is_ident_char(left):
        return False
    if end >= len(content):
        return True
    right = content[end]
    if left in _NUMERIC_JOINERS and right in _NUMERIC_JOINERS:
        # A DECIMAL component flanked by '/' on both sides is an entry of a
        # VALUE LIST (ping rtt min/avg/max/mdev = 1008.584/1012.525/...), not a
        # compound-literal component: real '/'-joined compound literals are
        # integer-component dates (2026/10/27), and ':' / '.' build integer
        # literals too (07:34:40, 1.5.2). Requiring the decimal point thus
        # separates the two cleanly. Demoting the ping avg/max would lose a
        # legitimate corroboration — the very thing D17 exists to prove. Safe:
        # widening numeric_fields can only move a claim corroborated-ward, never
        # accusation-ward (T3 keys on the unchanged exhaustive tokens).
        if left == "/" and right == "/" and "." in content[start:end]:
            return True
        return False  # interior component of a compound literal
    if not _is_ident_char(right):
        return True
    j = end
    while j < len(content) and content[j].isalpha():
        j += 1
    if j == end:
        return False  # '_' or a digit → identifier continuation, not a unit
    return j >= len(content) or not _is_ident_char(content[j])


def canonicalize_number(value: Any) -> str | None:
    """Canonical string for a numeric value/token so a claim's ``post``
    (int/float) matches a harvested text token (``'369'``, ``'369.0'``,
    ``'0.053'``).

    Integral values drop the decimal point; fractions strip trailing zeros at
    fixed precision (so float noise does not split ``0.053`` from ``0.0530``).
    Returns ``None`` for non-numeric / NaN / inf input — the caller abstains
    rather than guessing. Shared by the harvest side (hook) and the compare
    side (cross_check) so both agree on the canonical form; a mismatch there
    would silently starve every value-presence match.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / ±inf
        return None
    if f == int(f) and abs(f) < 1e16:
        return str(int(f))
    return f"{f:.10f}".rstrip("0").rstrip(".")


def harvest_evidence_fingerprint(
    content: str,
    tool_name: str = "",
    command: str = "",
) -> dict:
    """Format-agnostic value fingerprint of a RAW tool output (D17).

    Returns ``{"tokens": [...], "numeric_fields": [...], "complete": bool,
    "tool_name": str, "command": str}`` — JSON-serializable (LISTs, not sets, so
    they survive checkpoint serialization). ``tokens`` are ALL canonicalized
    unique numeric literals: the deliberately over-inclusive corpus that T3's
    absence proof keys on. ``numeric_fields`` is the subset the output delimits
    as standalone numbers (``_is_presented_as_number``): the ONLY corpus that may
    certify a claimed value as REAL (T2). ``complete`` is False when the
    unique-token count exceeded ``_FINGERPRINT_TOKEN_CAP`` (T3 MUST abstain on an
    incomplete harvest — it cannot prove absence). ``command`` enables per-source
    anchoring of a ``kind=raw`` claim. Never raises; always a well-formed dict
    (the consumer keys its guard on ``isinstance(fp, dict)``, and empty lists are
    a valid numberless observation).
    """
    text = content or ""
    tokens: set[str] = set()
    fields: set[str] = set()
    for m in _FINGERPRINT_NUM_RE.finditer(text):
        canon = canonicalize_number(m.group(0))
        if canon is None:
            continue
        tokens.add(canon)
        # numeric_fields ⊆ tokens by construction, so it inherits the cap and
        # needs no separate completeness notion.
        if _is_presented_as_number(text, m.start(), m.end()):
            fields.add(canon)
    complete = len(tokens) <= _FINGERPRINT_TOKEN_CAP
    ordered = sorted(tokens)
    ordered_fields = sorted(fields)
    if not complete:
        ordered = ordered[:_FINGERPRINT_TOKEN_CAP]
        ordered_fields = ordered_fields[:_FINGERPRINT_TOKEN_CAP]
    return {
        "tokens": ordered,
        "numeric_fields": ordered_fields,
        "complete": complete,
        "tool_name": tool_name or "",
        "command": command or "",
    }


__all__ = [
    "extract_metrics",
    "extract_baseline_metrics",
    "disk_target_paths",
    "canonicalize_number",
    "harvest_evidence_fingerprint",
]
