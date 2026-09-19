"""Tests for ``chaos_agent.agent.nodes.verify._metric_extractor`` (E2 Phase 1).

The extractor is the shared kernel for three call sites — baseline
collection, Layer 2 verification, and verdict cross-check. These tests
pin the per-format parser contracts AND the backward-compat behaviour
required by ``_verifier_hints._extract_baseline_key_metrics``.
"""
from __future__ import annotations

import json

from chaos_agent.agent.nodes.verify._metric_extractor import (
    extract_baseline_metrics,
    extract_metrics,
)


# ---------------------------------------------------------------------------
# extract_metrics — command-dispatch entry point
# ---------------------------------------------------------------------------


class TestEntryPointDispatch:
    def test_empty_stdout_returns_empty(self):
        assert extract_metrics("kubectl", "describe pod x", "") == {}
        assert extract_metrics("kubectl", "describe pod x", "   \n") == {}

    def test_empty_command_returns_empty(self):
        # No command means no dispatch matches.
        assert extract_metrics("kubectl", "", "anything") == {}

    def test_unknown_command_returns_empty(self):
        assert extract_metrics(
            "kubectl", "version --client", "Client Version: v1.30.0",
        ) == {}

    def test_never_raises_on_garbage_input(self):
        # Defensive: extractor must never raise — caller pattern is to
        # use ``{}`` as the "no signal" sentinel.
        for bad in ("\x00\x01\x02", "{ not json", "\n" * 1000, "𠮷" * 100):
            extract_metrics("kubectl", "describe pod x", bad)


# ---------------------------------------------------------------------------
# df -h
# ---------------------------------------------------------------------------


class TestDfH:
    def test_overlay_root_partition(self):
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay          50G   13G   38G  26% /\n"
            "tmpfs            64M     0   64M   0% /dev\n"
        )
        result = extract_metrics("kubectl", "exec mypod -- df -h", stdout)
        assert result == {"Disk usage (overlay)": "26% (13G/50G)"}

    def test_host_nodefs(self):
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/vda3       100G   42G   58G  42% /host\n"
        )
        result = extract_metrics("kubectl", "exec mypod -- df -h /host", stdout)
        assert result == {"Disk usage (nodefs)": "42% (42G/100G)"}

    def test_both_partitions(self):
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay          50G   13G   38G  26% /\n"
            "/dev/vda3       100G   42G   58G  42% /host\n"
        )
        result = extract_metrics("kubectl", "df -h", stdout)
        assert result == {
            "Disk usage (overlay)": "26% (13G/50G)",
            "Disk usage (nodefs)": "42% (42G/100G)",
        }

    def test_header_only_returns_empty(self):
        stdout = "Filesystem      Size  Used Avail Use% Mounted on\n"
        assert extract_metrics("kubectl", "df -h", stdout) == {}

    def test_df_k_dispatch_and_kb_units(self):
        """The disk cases' primary evidence is ``df -k`` (KB precision,
        #9 quality bar) — dispatch must fire on any standalone ``df``
        token, not just the -h literal. Same 6-column shape; the value
        carries raw 1K-block numbers."""
        stdout = (
            "Filesystem      1K-blocks      Used Available Use% Mounted on\n"
            "overlay         123456789  37384168  80888384  32% /tmp\n"
        )
        result = extract_metrics("kubectl", "exec mypod -- df -k /tmp", stdout)
        assert result == {"Disk usage (overlay)": "32% (37384168/123456789)"}

    def test_plain_df_token_fires(self):
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay          50G   13G   38G  26% /\n"
        )
        assert extract_metrics("kubectl", "df /tmp", stdout) == {
            "Disk usage (overlay)": "26% (13G/50G)"
        }

    def test_filename_like_token_does_not_fire(self):
        # "df.txt" has no whitespace/end boundary after "df" — must not
        # dispatch even when the payload itself looks like df output.
        stdout = "overlay 50G 13G 38G 26% /\n"
        assert extract_metrics("kubectl", "cat df.txt", stdout) == {}

    def test_inode_mode_rejected(self):
        # df -i: "IUse%" is inode usage, not disk usage — no signal
        # beats a mislabeled one.
        stdout = (
            "Filesystem Inodes IUsed IFree IUse% Mounted on\n"
            "overlay 6553600 103487 6450113 2% /\n"
        )
        assert extract_metrics("kubectl", "df -i /", stdout) == {}

    def test_type_column_layout_rejected(self):
        # df -Th: extra Type column shifts Use% off position 4 — the
        # per-row "%"-suffix guard drops it rather than misparse.
        stdout = (
            "Filesystem Type Size Used Avail Use% Mounted on\n"
            "overlay overlay 50G 13G 38G 26% /\n"
        )
        assert extract_metrics("kubectl", "df -Th /", stdout) == {}


# ---------------------------------------------------------------------------
# kubectl describe pod
# ---------------------------------------------------------------------------


class TestDescribePod:
    def test_restart_count_and_ready(self):
        stdout = (
            "Name:         my-pod\n"
            "Status:       Running\n"
            "Containers:\n"
            "  app:\n"
            "    Restart Count:  8\n"
            "    Ready:          True\n"
            "Conditions:\n"
            "  Type              Status\n"
            "  Ready             True\n"
        )
        result = extract_metrics("kubectl", "describe pod my-pod", stdout)
        assert result["RestartCount"] == "8"
        assert result["Pod Ready"] == "True"

    def test_oomkilled_last_termination(self):
        stdout = (
            "Last State:     Terminated\n"
            "  Reason:       OOMKilled\n"
            "  Exit Code:    137\n"
        )
        result = extract_metrics("kubectl", "describe pod x", stdout)
        assert result["Last termination reason"] == "OOMKilled"

    def test_evicted_termination(self):
        stdout = "Last State:\n  Reason: Evicted\n"
        result = extract_metrics("kubectl", "describe pod x", stdout)
        assert result["Last termination reason"] == "Evicted"

    def test_ready_false(self):
        stdout = "Ready             False\n"
        result = extract_metrics("kubectl", "describe pod x", stdout)
        assert result["Pod Ready"] == "False"

    def test_describe_po_alias(self):
        # `describe po` (the short form) must also dispatch.
        stdout = "Restart Count:  3\n"
        result = extract_metrics("kubectl", "describe po my-pod", stdout)
        assert result["RestartCount"] == "3"


# ---------------------------------------------------------------------------
# kubectl get pod -o json
# ---------------------------------------------------------------------------


class TestGetPodJson:
    def _pod(self, **overrides) -> dict:
        base = {
            "kind": "Pod",
            "metadata": {"name": "my-pod"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{
                    "name": "app",
                    "restartCount": 5,
                    "ready": True,
                    "lastState": {},
                }],
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        # Shallow merge of overrides into status
        base["status"].update(overrides)
        return base

    def test_single_pod_running_ready(self):
        stdout = json.dumps(self._pod())
        result = extract_metrics("kubectl", "get pod my-pod -o json", stdout)
        assert result["RestartCount"] == "5"
        assert result["Pod Ready"] == "True"
        assert result["Pod phase"] == "Running"

    def test_oomkilled_lifecycle(self):
        pod = self._pod()
        pod["status"]["containerStatuses"][0]["lastState"] = {
            "terminated": {"reason": "OOMKilled", "exitCode": 137},
        }
        stdout = json.dumps(pod)
        result = extract_metrics("kubectl", "get pod x -ojson", stdout)
        assert result["Last termination reason"] == "OOMKilled"

    def test_pod_list_iterates(self):
        pod_list = {"kind": "PodList", "items": [self._pod()]}
        stdout = json.dumps(pod_list)
        result = extract_metrics(
            "kubectl", "get pod -l app=foo --output=json", stdout,
        )
        assert result["RestartCount"] == "5"

    def test_invalid_json_returns_empty(self):
        result = extract_metrics("kubectl", "get pod x -o json", "{ broken")
        assert result == {}

    def test_top_level_ready_overrides_cs_ready(self):
        # Pod-level condition is more authoritative than per-container
        # ready — when both are set and conflict, top-level wins
        # because that's the K8s readiness probe answer.
        pod = self._pod()
        pod["status"]["containerStatuses"][0]["ready"] = True
        pod["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        stdout = json.dumps(pod)
        result = extract_metrics("kubectl", "get pod x -o json", stdout)
        assert result["Pod Ready"] == "False"


# ---------------------------------------------------------------------------
# kubectl top
# ---------------------------------------------------------------------------


class TestKubectlTop:
    def test_pod_first_row(self):
        stdout = (
            "NAME       CPU(cores)   MEMORY(bytes)\n"
            "my-pod     50m          120Mi\n"
        )
        result = extract_metrics("kubectl", "top pod my-pod", stdout)
        assert result == {"CPU usage": "50m", "Memory usage": "120Mi"}

    def test_node_row(self):
        stdout = (
            "NAME       CPU(cores)   MEMORY(bytes)\n"
            "node-1     1500m        4Gi\n"
        )
        result = extract_metrics("kubectl", "top node", stdout)
        assert result == {"CPU usage": "1500m", "Memory usage": "4Gi"}

    def test_no_data_returns_empty(self):
        # Header only — no metrics.
        stdout = "NAME       CPU(cores)   MEMORY(bytes)\n"
        assert extract_metrics("kubectl", "top pod x", stdout) == {}


# ---------------------------------------------------------------------------
# /proc/diskstats
# ---------------------------------------------------------------------------


class TestDiskstats:
    def test_all_physical_devices_reported(self):
        """Every non-virtual device gets a counter — the landing device
        depends on the case (root-disk overlay vs PVC data disk), so the
        extractor must not guess (Case #10: burn landed on vda3 while the
        old vdb/sdb hardcode pointed the baseline at the wrong disk)."""
        stdout = (
            " 252       0 vda 100 0 1000 200 50 0 500 100 0 300 300\n"
            " 252      16 vdb 200 0 2000 400 80 0 8888 200 0 600 600\n"
        )
        result = extract_metrics("kubectl", "exec x -- cat /proc/diskstats", stdout)
        assert result == {
            "Disk writes (vda)": "500 sectors",
            "Disk writes (vdb)": "8888 sectors",
        }

    def test_virtual_devices_excluded(self):
        stdout = (
            " 7       0 loop0 5 0 8 0 0 0 0 0 0 0 0 0\n"
            " 1       0 ram0 5 0 8 0 0 0 0 0 0 0 0 0\n"
            " 11      0 sr0 5 0 8 0 0 0 0 0 0 0 0 0\n"
            " 253       2 zram0 5 0 8 0 999 0 888 0 0 0 0 0\n"
        )
        # loop/ram/sr are virtual; zram is RAM-backed swap — its writes
        # are memory ops, not disk traffic.
        assert extract_metrics("kubectl", "cat /proc/diskstats", stdout) == {}


# ---------------------------------------------------------------------------
# /proc/stat
# ---------------------------------------------------------------------------


class TestProcStat:
    def test_iowait_percentage(self):
        # Format: cpu user nice system idle iowait irq softirq steal
        # Choose values where iowait/(idle+iowait) = 100/(900+100) = 10%
        stdout = "cpu  100 0 50 900 100 0 0 0\n"
        result = extract_metrics("kubectl", "cat /proc/stat", stdout)
        assert result == {"CPU iowait %": "10.0%"}

    def test_skips_per_cpu_rows(self):
        # Only the aggregate (first cpu line WITHOUT a digit suffix)
        # should be parsed.
        stdout = (
            "cpu  100 0 50 900 100 0 0 0\n"
            "cpu0 50 0 25 450 5 0 0 0\n"
        )
        result = extract_metrics("kubectl", "cat /proc/stat", stdout)
        assert result["CPU iowait %"] == "10.0%"


# ---------------------------------------------------------------------------
# du -sh
# ---------------------------------------------------------------------------


class TestDu:
    def test_first_row_size(self):
        stdout = "1.2G\t/data/cache\n"
        result = extract_metrics("kubectl", "exec x -- du -sh /data/cache", stdout)
        assert result == {"Target path size": "1.2G"}


# ---------------------------------------------------------------------------
# kubectl logs error patterns
# ---------------------------------------------------------------------------


class TestKubectlLogs:
    def test_oomkilled_count(self):
        stdout = (
            "ts=2024 level=info msg=hello\n"
            "ts=2024 level=warn msg=container OOMKilled by kernel\n"
            "ts=2024 level=warn msg=OOMKilled again\n"
        )
        result = extract_metrics("kubectl", "logs my-pod --tail=100", stdout)
        assert result == {"Log: OOMKilled count": "2"}

    def test_multiple_patterns(self):
        stdout = (
            "panic: runtime error\n"
            "connection refused\n"
            "panic: another\n"
        )
        result = extract_metrics("kubectl", "logs my-pod", stdout)
        assert result == {
            "Log: panic: count": "2",
            "Log: connection refused count": "1",
        }

    def test_clean_logs_returns_empty(self):
        # No explicit "0 errors" key — absence of signal != positive
        # ground truth (the cross-check phase would mis-rule with a 0).
        stdout = "ts=2024 level=info msg=all systems nominal\n"
        assert extract_metrics("kubectl", "logs my-pod", stdout) == {}


# ---------------------------------------------------------------------------
# kubectl get <resource> — table row count (#13-R: the 92-pod listing
# demoted to a 1018B head lost the count the plan was built around)
# ---------------------------------------------------------------------------


class TestGetTableRows:
    def test_no_headers_listing_counts_data_rows(self):
        # #13-R verbatim head: --no-headers pods listing, all-lowercase
        # data rows (lowercase hashes / 0/1 / AGE suffixes) — no header
        # to discount.
        stdout = (
            "chaosblade-operator-8564fd4f69-ck6pg   0/1   Init:ImagePullBackOff   0   23d\n"
            "chaosblade-tool-26xlq                  0/1   ImagePullBackOff        0   107d\n"
            "drill-mntopt-target-74f644bf8d-abcde   1/1   Running                  0   5d\n"
        )
        result = extract_metrics(
            "kubectl", "get pods -n default --no-headers", stdout,
        )
        assert result == {"List rows": "3"}

    def test_header_table_discounts_header_row(self):
        stdout = (
            "NAME               REFERENCE             TARGETS   MINPODS   MAXPODS   REPLICAS\n"
            "drill-hpa-target   Deployment/drill      50%/80%   1         10        1\n"
        )
        result = extract_metrics("kubectl", "get hpa -n default", stdout)
        assert result == {"List rows": "1"}

    def test_output_name_counts_lines(self):
        stdout = (
            "pod/chaosblade-operator-8564fd4f69-ck6pg\n"
            "pod/chaosblade-tool-26xlq\n"
        )
        result = extract_metrics("kubectl", "get pods -n default -o name", stdout)
        assert result == {"List rows": "2"}

    def test_no_resources_found_returns_empty(self):
        stdout = "No resources found in default namespace.\n"
        assert extract_metrics("kubectl", "get resourcequotas -n default", stdout) == {}

    def test_no_resources_matched_hint_returns_empty(self):
        # The kubectl tool-layer hint shape (#13-R msg[37]).
        stdout = (
            "\n\n💡 No resources matched the label selector. "
            "Try running without -l to discover available resources."
        )
        assert extract_metrics(
            "kubectl", "get sa,role,rolebinding -n default -l drill=rq", stdout,
        ) == {}

    def test_error_envelope_returns_empty(self):
        # NotFound error wrappers must not count as one data row.
        stdout = (
            "Error: kubectl get (exit 1): Error from server (NotFound): "
            "serviceaccounts \"drill-rc-rq300\" not found"
        )
        assert extract_metrics(
            "kubectl", "get sa drill-rc-rq300 -n default -o name", stdout,
        ) == {}

    def test_json_output_not_row_counted(self):
        stdout = '{"kind": "PodList", "items": []}'
        assert extract_metrics("kubectl", "get pods -n default -o json", stdout) == {}

    def test_jsonpath_output_not_row_counted(self):
        stdout = (
            "cn-shanghai.25.209.68.1: \n"
            "cn-shanghai.25.209.68.10: \n"
        )
        assert extract_metrics(
            "kubectl",
            "get nodes -o jsonpath='{range .items[*]}{.metadata.name}{\": \"}{.spec.taints}{\"\\n\"}{end}'",
            stdout,
        ) == {}

    def test_non_get_command_not_row_counted(self):
        stdout = "NAME   READY   STATUS\np1     1/1    Running\n"
        assert extract_metrics("kubectl", "can-i --list -n default", stdout) == {}
        assert extract_metrics("kubectl", "describe pod p1", stdout) == {}

    def test_single_resource_get_counts_one_row(self):
        stdout = (
            "NAME                  READY   UP-TO-DATE   AVAILABLE   AGE\n"
            "drill-mntopt-target   1/1     1            1           5d\n"
        )
        result = extract_metrics(
            "kubectl", "get deployment drill-mntopt-target -n default -o wide", stdout,
        )
        assert result == {"List rows": "1"}


# ---------------------------------------------------------------------------
# extract_baseline_metrics — backward-compat wrapper
# ---------------------------------------------------------------------------


class TestExtractBaselineMetricsWrapper:
    """Pins the contract ``_verifier_hints._extract_baseline_key_metrics``
    consumers rely on: a dict suitable for ``"\\n".join(k: v for ...)`
    rendering into the Layer 2 prompt, filtered to fault-relevant
    metrics."""

    def _baseline(self, *obs_specs: tuple[str, str, str]) -> dict:
        """Build a baseline dict with (command, stdout, description) rows."""
        return {
            "observations": [
                {"command": cmd, "stdout": out, "description": desc, "exit_code": 0}
                for cmd, out, desc in obs_specs
            ]
        }

    def test_empty_baseline_returns_empty(self):
        assert extract_baseline_metrics(None, "disk", "fill") == {}
        assert extract_baseline_metrics({}, "disk", "fill") == {}
        assert extract_baseline_metrics(
            {"observations": []}, "disk", "fill",
        ) == {}

    def test_failed_observation_skipped(self):
        baseline = {
            "observations": [{
                "command": "df -h", "stdout": "overlay 50G 13G 38G 26% /\n",
                "description": "...", "exit_code": 1,  # failed → skip
            }],
        }
        assert extract_baseline_metrics(baseline, "disk", "fill") == {}

    def test_disk_fault_keeps_disk_metrics(self):
        baseline = self._baseline(
            ("df -h", "overlay  50G  13G  38G  26% /\n", "Disk usage"),
            ("describe pod x", "Restart Count:  7\nReady             True\n",
             "Pod status"),
        )
        result = extract_baseline_metrics(baseline, "disk", "fill")
        assert "Disk usage (overlay)" in result
        assert "RestartCount" in result  # always-keep
        assert "Pod Ready" in result     # always-keep

    def test_cpu_fault_filters_disk_out(self):
        baseline = self._baseline(
            ("df -h", "overlay  50G  13G  38G  26% /\n", "..."),
            ("top pod x", "NAME CPU MEM\nmy-pod 250m 500Mi\n", "..."),
        )
        result = extract_baseline_metrics(baseline, "cpu", "fullload")
        # CPU fault: disk usage is noise → filtered out
        assert "Disk usage (overlay)" not in result
        # CPU/Memory should survive
        assert result.get("CPU usage") == "250m"

    def test_falls_back_to_description_when_command_missing(self):
        # Legacy fixtures might only have `description`, not `command`.
        # The wrapper must still dispatch correctly.
        baseline = {
            "observations": [{
                # No "command" key — wrapper falls back to description.
                "description": "df -h",
                "stdout": "overlay  50G  13G  38G  26% /\n",
                "exit_code": 0,
            }],
        }
        result = extract_baseline_metrics(baseline, "disk", "fill")
        assert result.get("Disk usage (overlay)") == "26% (13G/50G)"

    def test_unknown_fault_type_pass_through(self):
        # Unknown fault → no filtering, return all extracted metrics.
        baseline = self._baseline(
            ("df -h", "overlay 50G 13G 38G 26% /\n", "..."),
            ("top pod x", "NAME CPU MEM\nmy-pod 250m 500Mi\n", "..."),
        )
        result = extract_baseline_metrics(baseline, "weird-fault", "weird-action")
        assert "Disk usage (overlay)" in result
        assert "CPU usage" in result
