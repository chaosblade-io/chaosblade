"""Tests for ``chaos_agent.agent.nodes.verify._metric_extractor`` (E2 Phase 1).

The extractor is the shared kernel for three call sites — baseline
collection, Layer 2 verification, and verdict cross-check. These tests
pin the per-format parser contracts AND the backward-compat behaviour
required by ``_verifier_hints._extract_baseline_key_metrics``.
"""
from __future__ import annotations

import json

from chaos_agent.agent.nodes.verify._metric_extractor import (
    disk_target_paths,
    extract_baseline_metrics,
    extract_metrics,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec


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
# Target-driven df extraction (D4 / B2) — the declared injection mount path
# is the authoritative key, not a static k8s-shape whitelist. Additive over
# the overlay/nodefs aliases so the root-fs k8s domain stays byte-identical.
# ---------------------------------------------------------------------------

# Bare ``df`` a dedicated-partition drill would print: the root overlay AND
# the /dev/loop0 partition mounted at /drill-disk. The old whitelist kept
# only the overlay row (5%) and silently dropped the target partition — the
# B2 bug (baseline reported the root fs as if it were the target, cross_check
# confessed an "extractor coverage gap").
_DF_DRILL_DISK = (
    "Filesystem      Size  Used Avail Use% Mounted on\n"
    "overlay         453G   21G  432G   5% /\n"
    "/dev/loop0      2.0G 1.6G  194M  90% /drill-disk\n"
)


class TestTargetDrivenDfUsage:
    def test_partition_target_produces_its_own_key(self):
        """B2 root-fix: a /drill-disk partition target yields
        ``Disk usage (/drill-disk)`` from its OWN df row (90%), while the
        root overlay alias is STILL produced (additive, byte-identical)."""
        result = extract_metrics(
            "shell", "df", _DF_DRILL_DISK, ("/drill-disk",),
        )
        assert result["Disk usage (/drill-disk)"] == "90% (1.6G/2.0G)"
        # Additive: the alias pass is untouched.
        assert result["Disk usage (overlay)"] == "5% (21G/453G)"

    def test_exact_mount_wins_over_root_prefix(self):
        """``/`` is a path-prefix of ``/drill-disk``, but the EXACT mount
        match must win — otherwise the target would resolve to the root
        overlay (the very impersonation B2 forbids)."""
        result = extract_metrics(
            "shell", "df -h", _DF_DRILL_DISK, ("/drill-disk",),
        )
        # The target key carries the partition's 90%, NOT the root's 5%.
        assert result["Disk usage (/drill-disk)"] == "90% (1.6G/2.0G)"

    def test_longest_prefix_for_subdir_target(self):
        """A sub-directory target on a dedicated parent mount resolves to
        the LONGEST covering mount (``/var/log``), not the root ``/``."""
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay         100G   10G   90G  10% /\n"
            "/dev/vdb1        50G   40G   10G  80% /var/log\n"
        )
        result = extract_metrics(
            "shell", "df -h", stdout, ("/var/log/app",),
        )
        assert result["Disk usage (/var/log)"] == "80% (40G/50G)"
        assert result["Disk usage (overlay)"] == "10% (10G/100G)"

    def test_root_fs_target_on_overlay_is_byte_identical(self):
        """k8s pod-fill ``--path /tmp`` where /tmp sits on the overlay root:
        the covering row is alias-covered, so NO mount-named duplicate key is
        added and the result is byte-identical to the pre-D4 behaviour."""
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay          50G   13G   38G  26% /\n"
        )
        result = extract_metrics("kubectl", "df -h", stdout, ("/tmp",))
        assert result == {"Disk usage (overlay)": "26% (13G/50G)"}

    def test_target_mounted_on_overlay_fs_is_byte_identical(self):
        """``--path /tmp`` where df reports the mount AS /tmp on an overlay
        filesystem — still alias-covered (fs startswith overlay), no new key.
        Mirrors the existing ``df -k /tmp`` contract."""
        stdout = (
            "Filesystem      1K-blocks      Used Available Use% Mounted on\n"
            "overlay         123456789  37384168  80888384  32% /tmp\n"
        )
        result = extract_metrics(
            "kubectl", "df -k /tmp", stdout, ("/tmp",),
        )
        assert result == {"Disk usage (overlay)": "32% (37384168/123456789)"}

    def test_host_nodefs_target_is_byte_identical(self):
        """A ``/host`` target is alias-covered (nodefs) — no ``Disk usage
        (/host)`` duplicate."""
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/vda3       100G   42G   58G  42% /host\n"
        )
        result = extract_metrics("kubectl", "df -h /host", stdout, ("/host",))
        assert result == {"Disk usage (nodefs)": "42% (42G/100G)"}

    def test_no_target_paths_keeps_legacy_whitelist_only(self):
        """Default (no target_paths): the /drill-disk row is NOT extracted —
        proving every existing caller stays byte-identical."""
        result = extract_metrics("shell", "df", _DF_DRILL_DISK)
        assert result == {"Disk usage (overlay)": "5% (21G/453G)"}

    def test_coverage_gap_warns_and_never_impersonates(self, caplog):
        """A declared target with NO covering df row (df captured an
        unrelated mount, no ``/`` prefix) → explicit coverage-gap warning,
        NO fabricated target key, and the root fs is NOT relabeled as the
        target."""
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sdb1        50G   40G   10G  80% /mnt/other\n"
        )
        with caplog.at_level("WARNING"):
            result = extract_metrics(
                "shell", "df -h", stdout, ("/drill-disk",),
            )
        assert "Disk usage (/drill-disk)" not in result
        # /mnt/other is not overlay/nodefs and not the target — no impersonation
        assert "Disk usage (overlay)" not in result
        assert any(
            "coverage gap" in r.getMessage() for r in caplog.records
        ), "expected an explicit extractor-coverage-gap warning"

    def test_root_prefix_present_is_not_a_gap(self, caplog):
        """When ``/`` IS present, a target with no exact mount resolves to the
        root overlay (a legitimate sub-dir-on-root target) — that is NOT a
        coverage gap, so no warning fires (keeps the k8s domain quiet)."""
        stdout = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay          50G   13G   38G  26% /\n"
        )
        with caplog.at_level("WARNING"):
            result = extract_metrics("kubectl", "df -h", stdout, ("/tmp",))
        assert result == {"Disk usage (overlay)": "26% (13G/50G)"}
        assert not any(
            "coverage gap" in r.getMessage() for r in caplog.records
        )


class TestDiskTargetPaths:
    """``disk_target_paths`` — a thin adapter delegating to the FaultSpec,
    which is the single authority mapping a disk fault to its declared mount
    path (D4/B2).

    Fixtures use the REAL runtime spec shapes, not the idealised
    ``params={"path": ...}`` shape the earlier version assumed. A/A run
    inject-0242c9d6 proved the NL host disk-fill carries the mount in
    ``names`` with ``params={}``; the old params-only fixtures passed while
    the live run silently dropped the /drill-disk row.
    """

    def test_real_host_shape_mount_in_names(self):
        # THE regression this fix targets: real NL host disk-fill stores the
        # mount in names, params empty. Must still resolve the target.
        spec = FaultSpec(
            scope="host",
            fault_target="disk",
            fault_action="fill",
            names=("/drill-disk",),
            params={},
        )
        assert disk_target_paths(spec) == ("/drill-disk",)

    def test_explicit_params_path_takes_precedence(self):
        # Structured (-p path=) declaration wins over names.
        spec = FaultSpec(
            scope="host",
            fault_target="disk",
            fault_action="fill",
            names=("/other",),
            params={"path": "/drill-disk", "percent": "90"},
        )
        assert disk_target_paths(spec) == ("/drill-disk",)

    def test_node_disk_fill_resource_name_is_not_a_path(self):
        # node/pod disk-fill names a resource, not a mount → no absolute-path
        # entry → empty, so the df parser stays on its additive alias path.
        spec = FaultSpec(
            scope="node",
            fault_target="disk",
            fault_action="fill",
            names=("node-1",),
            params={"percent": "90"},
        )
        assert disk_target_paths(spec) == ()

    def test_non_disk_fault_gated_off(self):
        # A cpu fault declares no disk target even if a stray path is present.
        spec = FaultSpec(
            scope="pod",
            fault_target="cpu",
            fault_action="fullload",
            params={"cpu-percent": "80", "path": "/data"},
        )
        assert disk_target_paths(spec) == ()

    def test_blank_params_path_falls_back_to_names(self):
        spec = FaultSpec(
            scope="host",
            fault_target="disk",
            fault_action="fill",
            names=("/drill-disk",),
            params={"path": "   "},
        )
        assert disk_target_paths(spec) == ("/drill-disk",)

    def test_none_spec_is_safe(self):
        assert disk_target_paths(None) == ()

    def test_object_without_authority_is_safe(self):
        # Duck-typed: anything lacking disk_mount_targets yields no target.
        assert disk_target_paths({"path": "/drill-disk"}) == ()


class TestBaselineTargetThreading:
    """``extract_baseline_metrics`` threads target_paths so the baseline key
    metric lands on the TARGET filesystem (B2's wrong-target baseline)."""

    def test_baseline_keys_on_target_partition(self):
        baseline = {
            "observations": [{
                "command": "df", "stdout": _DF_DRILL_DISK,
                "description": "disk baseline", "exit_code": 0,
            }],
        }
        result = extract_baseline_metrics(
            baseline, "disk", "fill", ("/drill-disk",),
        )
        # The target partition survives the disk-fault filter ("Disk usage"
        # prefix) alongside the additive overlay alias.
        assert result["Disk usage (/drill-disk)"] == "90% (1.6G/2.0G)"
        assert result["Disk usage (overlay)"] == "5% (21G/453G)"


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
            "Containers:\n"
            "  app:\n"
            "    Last State:     Terminated\n"
            "      Reason:       OOMKilled\n"
            "      Exit Code:    137\n"
        )
        result = extract_metrics("kubectl", "describe pod x", stdout)
        assert result["Last termination reason"] == "OOMKilled"

    def test_evicted_termination(self):
        stdout = (
            "Containers:\n"
            "  app:\n"
            "    Last State:\n"
            "      Reason: Evicted\n"
        )
        result = extract_metrics("kubectl", "describe pod x", stdout)
        assert result["Last termination reason"] == "Evicted"

    def test_ready_false(self):
        stdout = "Ready             False\n"
        result = extract_metrics("kubectl", "describe pod x", stdout)
        assert result["Pod Ready"] == "False"

    def test_describe_po_alias(self):
        # `describe po` (the short form) must also dispatch.
        stdout = "Containers:\n  app:\n    Restart Count:  3\n"
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
# kubectl get deployment/node -o json (m7: non-pod scopes)
# ---------------------------------------------------------------------------


class TestGetDeploymentNodeJson:
    def test_deployment_replica_state(self):
        # m7 (r68 review, measured): the dispatch was pod-only, so the
        # r43 live case's own scope (deployment mechanism readback)
        # produced NO structured metrics while the pod form did.
        dep = {
            "spec": {"replicas": 0},
            "status": {
                "readyReplicas": 0,
                "replicas": 0,
                "unavailableReplicas": 3,
            },
        }
        result = extract_metrics(
            "kubectl", "get deployment demo -n default -o json", json.dumps(dep),
        )
        assert result["Spec replicas"] == "0"
        assert result["Status readyReplicas"] == "0"
        assert result["Status unavailableReplicas"] == "3"
        assert result["Status replicas"] == "0"

    def test_deployment_deploy_alias_and_ojson(self):
        dep = {"spec": {"replicas": 2}, "status": {"readyReplicas": 2, "replicas": 2}}
        result = extract_metrics(
            "kubectl", "get deploy demo -ojson", json.dumps(dep),
        )
        assert result["Spec replicas"] == "2"
        assert result["Status readyReplicas"] == "2"

    def test_node_conditions(self):
        node = {
            "status": {"conditions": [
                {"type": "MemoryPressure", "status": "True"},
                {"type": "Ready", "status": "True"},
            ]},
        }
        result = extract_metrics(
            "kubectl", "get node worker-1 -o json", json.dumps(node),
        )
        assert result["Node MemoryPressure"] == "True"
        assert result["Node Ready"] == "True"

    def test_deployment_invalid_json_returns_empty(self):
        result = extract_metrics("kubectl", "get deployment d -o json", "{ broken")
        assert result == {}

    def test_deployment_without_json_flag_not_parsed(self):
        # The -o json gate stays authoritative: a table-form get must not
        # route into the JSON parser (it would find no dict and return
        # {} — the regression guard for the dispatch's shape).
        dep = {"spec": {"replicas": 0}, "status": {"replicas": 0}}
        result = extract_metrics(
            "kubectl", "get deployment d", json.dumps(dep),
        )
        assert "Spec replicas" not in result

    def test_deployment_generic_list_form_iterates(self):
        # r68 self-review C1 (measured in .b4tmp/r68_review_c1_probe.py):
        # the nameless ``get deployments -o json`` returns the GENERIC
        # List shape (kind="List" — unlike pods' dedicated PodList kind)
        # and used to yield {} while the pod parser's List precedent
        # yielded metrics. Unrolled per-item; key collisions last-wins
        # (same policy as the pod parser — a multi-deployment listing
        # reports the LAST item).
        dep_list = {
            "kind": "List",
            "items": [
                {"spec": {"replicas": 3},
                 "status": {"readyReplicas": 1, "replicas": 3}},
                {"spec": {"replicas": 2},
                 "status": {"readyReplicas": 2, "replicas": 2}},
            ],
        }
        result = extract_metrics(
            "kubectl", "get deployments -n default -o json",
            json.dumps(dep_list),
        )
        assert result == {
            "Spec replicas": "2",
            "Status readyReplicas": "2",
            "Status replicas": "2",
        }

    def test_node_generic_list_form_iterates(self):
        # r68 self-review C1: same generic-List unroll for nodes.
        node_list = {
            "kind": "List",
            "items": [
                {"status": {"conditions": [
                    {"type": "Ready", "status": "True"},
                    {"type": "MemoryPressure", "status": "True"},
                ]}},
            ],
        }
        result = extract_metrics(
            "kubectl", "get nodes -o json", json.dumps(node_list),
        )
        assert result["Node Ready"] == "True"
        assert result["Node MemoryPressure"] == "True"


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
# dd transfer summary — the throughput timing line POSIX puts on STDERR.
# Before this parser the extractor had NO dd branch, so any dd observation
# produced {} and never entered metric_observations. This is a GENERAL
# capability completion, not a case-criterion gap fix: the sanctioned disk-IO
# cases judge via iostat %util/w/s/iowait (dd is only the injection burn
# payload / a one-shot env probe), so lifting dd's own timing is marginal —
# chiefly anti-truncation for the env-probe baseline throughput. Harvesting the
# stderr timing line into keys puts it on the timeline AND the [Auto-extracted]
# HEAD prefix.
# ---------------------------------------------------------------------------


class TestDdTransferTiming:
    def test_gnu_coreutils_form(self):
        # Real GNU coreutils dd form (observed verbatim in a drill log).
        stderr = (
            "8+0 records in\n"
            "8+0 records out\n"
            "524288 bytes (524 kB, 512 KiB) copied, 0.0012 s, 437 MB/s\n"
        )
        result = extract_metrics(
            "host_read", "dd if=/dev/zero bs=64k count=8", stderr
        )
        assert result == {
            "Transfer bytes": "524288",
            "Transfer elapsed (s)": "0.0012",
            "Transfer rate": "437 MB/s",
        }

    def test_busybox_form_seconds_and_no_space_rate(self):
        # busybox dd: "seconds" unit + rate with no internal space.
        stderr = "524288 bytes (512.0KB) copied, 0.001234 seconds, 425.0MB/s\n"
        result = extract_metrics(
            "kubectl", "exec p -n ns -- dd if=/dev/zero of=/x bs=1M count=100", stderr
        )
        assert result == {
            "Transfer bytes": "524288",
            "Transfer elapsed (s)": "0.001234",
            "Transfer rate": "425.0MB/s",
        }

    def test_timing_survives_under_merged_stderr_after_bulk_flood(self):
        """A dd-to-stdout shape: a bulk stdout flood, then the timing under the
        merge separator. The parser scans every line, so the tail-borne
        summary is found regardless of how much junk precedes it."""
        content = ("Z" * (60 * 1024)) + "\n--- STDERR ---\n" + (
            "524288 bytes (524 kB, 512 KiB) copied, 0.0012 s, 437 MB/s\n"
        )
        result = extract_metrics("host_read", "dd if=/dev/zero bs=64k count=8", content)
        assert result["Transfer elapsed (s)"] == "0.0012"
        assert result["Transfer rate"] == "437 MB/s"

    def test_rate_optional_when_transfer_truncated(self):
        # A transfer cut short may omit the rate; elapsed (T_base) still lands.
        stderr = "1048576 bytes (1 MB) copied, 2.5 s\n"
        result = extract_metrics("host_read", "dd if=/dev/zero of=/x bs=1M", stderr)
        assert result == {
            "Transfer bytes": "1048576",
            "Transfer elapsed (s)": "2.5",
        }

    def test_status_none_prints_nothing_returns_empty(self):
        # `dd status=none` suppresses the summary — no timing to harvest.
        assert extract_metrics("host_read", "dd if=/dev/zero status=none", "") == {}

    def test_prose_mentioning_dd_is_not_harvested(self):
        """Format-keyed guard (cabb5702): a ``dd`` token in the command with
        no actual ``copied,`` summary line yields nothing — the parser keys on
        the OUTPUT format, not the command name."""
        assert extract_metrics(
            "host_read", "dd if=/dev/zero of=/x", "run dd to fill the disk\n"
        ) == {}

    def test_non_dd_command_with_dd_looking_output_is_not_dispatched(self):
        """Activation-gate guard: a ``cat`` whose file happens to contain a
        dd timing line is NOT dispatched to the dd parser (no standalone
        ``dd`` token in the command)."""
        assert extract_metrics(
            "host_read", "cat /var/log/transfer.log",
            "524288 bytes (524 kB) copied, 0.0012 s, 437 MB/s\n",
        ) == {}

    def test_dd_token_gate_rejects_lookalikes(self):
        # ``dd`` must be a standalone token, not a substring of a name/path.
        # Scoped to the dd parser's OWN ``Transfer *`` keys: other command-family
        # branches may still legitimately fire on these strings (e.g. ``get pod``
        # → List rows), which is correct behaviour for THOSE parsers — asserting
        # a globally-empty dict would couple this gate test to unrelated branches.
        for cmd in ("get pod dd-xyz", "cat /tmp/dd", "kubectl add x", "address"):
            result = extract_metrics(
                "kubectl", cmd, "524288 bytes copied, 1 s, 2 MB/s\n"
            )
            assert not any(k.startswith("Transfer") for k in result), cmd


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
            ("describe pod x",
             "Containers:\n  app:\n    Restart Count:  7\n"
             "Ready             True\n",
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


# ---------------------------------------------------------------------------
# describe pod — section awareness (live case inject-3dae7b4f)
# ---------------------------------------------------------------------------


class TestDescribePodSectionAwareness:
    """``describe pod`` parsing must be immune to the Ephemeral Containers
    section. Once a pod-scoped ``kubectl debug`` has ever run against a
    target, the pod carries a permanently-accumulating ephemeral section
    (26 dead debuggers on the live target — 577 lines after the 22-line
    business section). Old last-wins logic read the LAST ephemeral
    container's RestartCount 0 / container ID as the pod's baseline
    (business truth: RestartCount 329)."""

    _BUSINESS_CID = "containerd://f69d92f1ea72"
    _EPHEMERAL_CID = "containerd://c0c6f9018f2d"

    def _describe(self):
        # Shape-faithful skeleton of the live target's describe output:
        # business section first, top-level Conditions table, then the
        # ephemeral graveyard. Ephemeral uses DIFFERENT values than the
        # business section so a section-blind parser cannot pass by luck.
        return (
            "Name:         drill-reorder-target\n"
            "Namespace:    default\n"
            "Status:       Running\n"
            "Containers:\n"
            "  target:\n"
            f"    Container ID:  {self._BUSINESS_CID}\n"
            "    Image:         registry/terway:v1.12.1\n"
            "    State:          Running\n"
            "    Ready:          True\n"
            "    Restart Count:  329\n"
            "    Last State:     Terminated\n"
            "      Reason:       Completed\n"
            "Conditions:\n"
            "  Type           Status\n"
            "  Initialized    True \n"
            "  Ready          True \n"
            "Ephemeral Containers:\n"
            "  debugger-b46sh:\n"
            f"    Container ID:  {self._EPHEMERAL_CID}\n"
            "    State:          Terminated\n"
            "      Reason:       OOMKilled\n"
            "    Ready:          False\n"
            "    Restart Count:  0\n"
        )

    def test_ephemeral_section_does_not_override_business_metrics(self):
        result = extract_metrics(
            "kubectl", "describe pod drill-reorder-target -n default",
            self._describe(),
        )
        assert result["RestartCount"] == "329"
        assert result["Container ID"] == self._BUSINESS_CID
        # Business Reason is Completed; the ephemeral Reason (OOMKilled)
        # must NOT win — it belongs to a dead debugger, not the pod.
        assert result["Last termination reason"] == "Completed"

    def test_pod_ready_read_from_top_level_conditions(self):
        # Top-level Conditions table (no colon after Ready) is the source;
        # per-container ``Ready:`` fields (with colon) never match, in the
        # business section or the ephemeral one.
        result = extract_metrics(
            "kubectl", "describe pod drill-reorder-target -n default",
            self._describe(),
        )
        assert result["Pod Ready"] == "True"

    def test_ephemeral_only_output_yields_no_misread_metrics(self):
        # Degenerate shape: business container list empty, only an
        # ephemeral section — nothing may be read as pod metrics.
        stdout = (
            "Name:         drill-reorder-target\n"
            "Containers:\n"
            "Ephemeral Containers:\n"
            "  debugger-b46sh:\n"
            f"    Container ID:  {self._EPHEMERAL_CID}\n"
            "    Restart Count:  0\n"
        )
        result = extract_metrics("kubectl", "describe pod x", stdout)
        assert "RestartCount" not in result
        assert "Container ID" not in result

    def test_init_containers_section_is_ignored_too(self):
        # Init containers restart once per boot by design — their restart
        # count is noise for fault-effect judgement, same class as the
        # ephemeral section.
        stdout = (
            "Containers:\n"
            "  app:\n"
            f"    Container ID:  {self._BUSINESS_CID}\n"
            "    Restart Count:  2\n"
            "Init Containers:\n"
            "  init-db:\n"
            "    Container ID:  containerd://initcid0000\n"
            "    Restart Count:  40\n"
        )
        result = extract_metrics("kubectl", "describe pod x", stdout)
        assert result["RestartCount"] == "2"
        assert result["Container ID"] == self._BUSINESS_CID

    def test_json_supplement_command_hits_structured_parser(self):
        """The Target-conditions supplement is now ``get -o json`` — the
        dispatch must route it to the structured parser (business
        containerStatuses first), locking the command-shape ↔ parser
        contract so a future wording change cannot silently break the
        hit."""
        pod_json = json.dumps({
            "kind": "Pod",
            "status": {
                "containerStatuses": [{
                    "restartCount": 329,
                    "containerID": self._BUSINESS_CID,
                    "ready": True,
                }],
                "phase": "Running",
            },
        })
        result = extract_metrics(
            "kubectl",
            "get pod drill-reorder-target -n default -o json"
            " --show-managed-fields=false",
            pod_json,
        )
        assert result["RestartCount"] == "329"
        assert result["Container ID"] == self._BUSINESS_CID
        assert result["Pod phase"] == "Running"


# ---------------------------------------------------------------------------
# Generic labeled-jsonpath key=value parser (mechanism-invariant-trust-boundary
# Phase 1). Strict command gate: fires only when the jsonpath template carries
# an explicit ``label={…}`` pair; positional / bare-value templates are refused
# rather than guessed by output column position (memory 786c2f53).
# ---------------------------------------------------------------------------


class TestLabeledJsonpath:
    # The #77 StatefulSet-partition readback in its REAL production shape
    # (task inject-92493ad8, verbatim): the ``{"\n"}`` field separators FORCE
    # the template to be shell-quoted, so the LLM emits ``-o 'jsonpath=…'``.
    # An earlier revision of this fixture used the UNQUOTED ``-o jsonpath=…``
    # spelling — cleaner than production, so it stayed green while the
    # activation gate ``_JSONPATH_OUTPUT`` silently starved EVERY real quoted
    # read to ``{}`` (the masking trap this fixture now pins shut). Keep it
    # QUOTED so the regression exercises the gate the way production does.
    # (Machine-specific kubeconfig path deliberately omitted.)
    _CMD_77 = (
        "get statefulset drill-sts-partition-target -n default -o "
        "'jsonpath=partition={.spec.updateStrategy.rollingUpdate.partition}"
        '{"\\n"}'
        "currentRevision={.status.currentRevision}"
        '{"\\n"}'
        "updateRevision={.status.updateRevision}"
        "'"
    )

    def test_quoted_jsonpath_gate_tolerates_every_real_spelling(self):
        # REGRESSION (extractor quote-gate gap): a template with {"\n"}
        # separators MUST be shell-quoted, so the DOMINANT production form is
        # ``-o 'jsonpath=…'``. The old activation gate only matched the
        # unquoted ``-o jsonpath`` spelling, so every real quoted read fell
        # through to ``{}`` and its metrics never reached the timeline. Pin
        # the gate on ALL standard kubectl output-flag spellings, quoted and
        # unquoted alike — the reverse assertion (each variant is NON-empty)
        # is what kills the bug rather than just the new happy path.
        tmpl = "partition={.spec.updateStrategy.rollingUpdate.partition}"
        stdout = "partition=2"
        for cmd in (
            f"get sts web -o jsonpath={tmpl}",         # unquoted (legacy)
            f"get sts web -o 'jsonpath={tmpl}'",       # single-quoted (dominant)
            f'get sts web -o "jsonpath={tmpl}"',       # double-quoted
            f"get sts web -ojsonpath={tmpl}",          # fused short flag
            f"get sts web --output=jsonpath={tmpl}",   # long flag
            f"get sts web --output 'jsonpath={tmpl}'",  # long flag + quote
        ):
            assert extract_metrics("kubectl", cmd, stdout) == {
                "Partition": "2"
            }, f"gate starved real spelling: {cmd}"

    def test_partition_readback_yields_named_metrics(self):
        stdout = (
            "partition=2\n"
            "currentRevision=7c99c5c4d\n"
            "updateRevision=6db7564c9d\n"
        )
        result = extract_metrics("kubectl", self._CMD_77, stdout)
        assert result == {
            "Partition": "2",
            "Current revision": "7c99c5c4d",
            "Update revision": "6db7564c9d",
        }

    def test_space_separated_single_line(self):
        cmd = "get sts web -o jsonpath=partition={.a} ready={.b} updated={.c}"
        stdout = "partition=2 ready=3 updated=1"
        assert extract_metrics("kubectl", cmd, stdout) == {
            "Partition": "2", "Ready": "3", "Updated": "1",
        }

    def test_value_is_not_hardcoded_to_two(self):
        # The parser is value-agnostic — N is declared per-case (design D3);
        # the extractor records whatever value appears (2, 5, 0 alike).
        stdout = "partition=5\ncurrentRevision=aaa\nupdateRevision=bbb\n"
        assert extract_metrics("kubectl", self._CMD_77, stdout)["Partition"] == "5"

    def test_partition_zero_is_captured(self):
        # 0 is a legal observation (baseline / no partitioning), not blank.
        cmd = "get sts web -o jsonpath=partition={.a}"
        assert extract_metrics("kubectl", cmd, "partition=0") == {"Partition": "0"}

    def test_label_drift_changes_metric_name(self):
        # readyReplicas= vs ready= humanize to DIFFERENT names, so a rule
        # looking for the canonical name finds it absent under drift and
        # abstains to unknown (design D11). The parser itself stays general.
        r1 = extract_metrics(
            "kubectl", "get sts web -o jsonpath=readyReplicas={.a}",
            "readyReplicas=3",
        )
        r2 = extract_metrics(
            "kubectl", "get sts web -o jsonpath=ready={.a}", "ready=3",
        )
        assert r1 == {"Ready replicas": "3"}
        assert r2 == {"Ready": "3"}
        assert "Ready replicas" not in r2  # drift → canonical name absent

    def test_bare_value_jsonpath_refused(self):
        # No label in the template → gate fails → {} (禁裸值). The
        # ``jsonpath=`` token itself must NOT be mistaken for a label.
        cmd = "get sts web -o jsonpath={.spec.updateStrategy.rollingUpdate.partition}"
        assert extract_metrics("kubectl", cmd, "2") == {}

    def test_positional_pipe_form_refused(self):
        # The unlabeled pipe form (design L10) must never be guessed by
        # output column position.
        cmd = "get sts web -o jsonpath={.spec.updateStrategy.type}|{.a}|{.b}"
        assert extract_metrics("kubectl", cmd, "RollingUpdate|0|7c99c5c4d") == {}

    def test_empty_value_dropped(self):
        # ``partition=`` (field absent) → dropped, not recorded as blank.
        cmd = "get sts web -o jsonpath=partition={.a}"
        assert extract_metrics("kubectl", cmd, "partition=") == {}

    def test_empty_stdout_returns_empty(self):
        assert extract_metrics("kubectl", self._CMD_77, "") == {}

    def test_non_jsonpath_command_does_not_fire(self):
        # A command with no jsonpath output form whose stdout happens to
        # contain '=' must not be parsed as labeled jsonpath.
        assert extract_metrics("kubectl", "version --client", "x=1") == {}

    def test_duplicate_label_last_wins(self):
        # Multi-object listing sharing a selector: last value wins (same
        # collision policy as the pod/deployment/node JSON parsers).
        cmd = "get statefulset web -o jsonpath=phase={.a}"
        stdout = "phase=Pending\nphase=Running\n"
        assert extract_metrics("kubectl", cmd, stdout) == {"Phase": "Running"}

    def test_never_raises_on_garbage_labeled_output(self):
        cmd = "get sts -o jsonpath=partition={.a}"
        for bad in ("\x00\x01", "partition=𠮷", "====", "partition==2", "\n" * 100):
            extract_metrics("kubectl", cmd, bad)

    def test_pipe_literal_labeled_form_yields_named_metrics(self):
        # D15 修法 2: the LLM's pipe-delimited readback shape puts the label
        # INSIDE a quoted jsonpath string literal — ``{"|partition="}`` — so
        # the stdout is ``web|partition=0|ready=3``. Before the gate widened
        # to admit the pipe-literal label, this baseline read was dropped to
        # {} and the timeline starved to a single-phase snapshot (Mode B).
        # The value now terminates at the pipe, so fields split correctly.
        cmd = (
            "get statefulset web -n default -o "
            "'jsonpath={.metadata.name}{\"|partition=\"}"
            "{.spec.updateStrategy.rollingUpdate.partition}"
            "{\"|ready=\"}{.status.readyReplicas}'"
        )
        stdout = "web|partition=0|ready=3"
        assert extract_metrics("kubectl", cmd, stdout) == {
            "Partition": "0", "Ready": "3",
        }

    def test_prose_and_template_contamination_values_dropped(self):
        # D15 修法 2 guardrail: a "value" carrying CJK / full-width
        # punctuation / backtick / quote / brace is leaked PROSE or an echoed
        # jsonpath TEMPLATE fragment, not a metric scalar — drop it so garbage
        # keys never enter the timeline. Real run inject-7c25e537 iteration-8
        # produced ``'Partition': '0`。'``, ``'Dedicated'``, ``'Ecs'``,
        # ``'Jsonpath': "'replicas={…}"`` from a reconstructed command blob.
        cmd = "get sts web -o 'jsonpath=partition={.a}'"
        assert extract_metrics("kubectl", cmd, "partition=0`。") == {}
        assert extract_metrics("kubectl", cmd, "partition=专用") == {}
        assert extract_metrics(
            "kubectl", cmd, "Jsonpath='replicas={.spec.replicas}'",
        ) == {}
        # a clean scalar in the same shape is still captured — the guard
        # drops contamination, not real values.
        assert extract_metrics("kubectl", cmd, "partition=0") == {"Partition": "0"}


# ---------------------------------------------------------------------------
# http_probe — typed L7 probe (L1): TOOL-NAME dispatch, born-structured JSON.
#
# Unlike every other extractor branch (command-text dispatch + reverse-parse of
# a shell's human output), http_probe is dispatched by tool NAME and lifts the
# metric keys straight out of the JSON the tool itself produced. This is the
# extractor half of closing the Case #84 coverage gap: a hand-written curl's
# http_code / time_connect never reached the observation timeline, so the
# deterministic cross-check logged a gap. The keys asserted here MUST match
# tools/http_probe_cli._W_NAME_TO_METRIC byte-for-byte (locked by
# test_http_probe_metric_keys_match_tool_contract below).
# ---------------------------------------------------------------------------


class TestHttpProbe:
    _CMD = "http_probe"  # dispatch is by tool_name, command text is irrelevant

    def test_full_json_extracts_all_canonical_keys(self):
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe",
            "origin_pod": "drill-http-client",
            "namespace": "default",
            "url": "http://svc/health",
            "http_code": "503",
            "time_connect": "0.004",
            "time_starttransfer": "0.011",
            "size_download": "0",
            "exit_code": "0",
        })
        assert extract_metrics("http_probe", self._CMD, stdout) == {
            "http_code": "503",
            "time_connect": "0.004",
            "time_starttransfer": "0.011",
            "size_download": "0",
            "exit_code": "0",
        }

    def test_partial_json_skips_empty_and_absent_keys(self):
        # A faulted probe (connection refused) reports exit_code but leaves the
        # timing fields empty — those must be SKIPPED, never seeded as "".
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "http_code": "000", "exit_code": "7",
            "time_connect": "", "time_starttransfer": "",
        })
        assert extract_metrics("http_probe", self._CMD, stdout) == {
            "http_code": "000", "exit_code": "7",
        }

    def test_non_json_stdout_returns_empty(self):
        # Defensive parity with the rest of the extractor: garbage in, {} out.
        assert extract_metrics("http_probe", self._CMD, "curl: not found") == {}
        assert extract_metrics("http_probe", self._CMD, "{ not json") == {}

    def test_json_non_object_returns_empty(self):
        assert extract_metrics("http_probe", self._CMD, "[1, 2, 3]") == {}
        assert extract_metrics("http_probe", self._CMD, '"a string"') == {}

    def test_dispatch_is_by_tool_name_not_command(self):
        # The SAME JSON body under a different tool_name must NOT trigger the
        # http parser (proves the dispatch key is tool_name, and the provenance
        # whitelist in memory/hook.py is what gates admission to the timeline).
        import json as _json

        stdout = _json.dumps({"http_code": "200", "exit_code": "0"})
        assert extract_metrics("kubectl", self._CMD, stdout) == {}

    def test_http_probe_metric_keys_match_tool_contract(self):
        # Cross-source consistency anchor: the extractor's key tuple and the
        # tool's write-out mapping must agree byte-for-byte, or the LLM cites a
        # key the cross-check cannot find in the timeline.
        from chaos_agent.agent.nodes.verify._metric_extractor import (
            _HTTP_PROBE_METRIC_KEYS,
        )
        from chaos_agent.tools.http_probe_cli import _W_NAME_TO_METRIC

        assert set(_HTTP_PROBE_METRIC_KEYS) == set(_W_NAME_TO_METRIC.values())

    # --- capture-layer keys (A3/A7): headers flatten + body lift -------------

    def test_headers_capture_flattens_to_header_keys(self):
        # capture=headers/all carries the raw header_json; the extractor
        # flattens it to header.<lower-name> keys so an LLM claim like
        # header.x-drill-injected=true hits the cross-check's exact-key lookup.
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "http_code": "200", "exit_code": "0",
            "capture": "headers",
            "headers": _json.dumps({
                "Content-Type": ["application/json"],
                "X-Drill-Injected": ["true"],
            }),
        })
        metrics = extract_metrics("http_probe", self._CMD, stdout)
        assert metrics["http_code"] == "200"
        assert metrics["header.content-type"] == "application/json"
        assert metrics["header.x-drill-injected"] == "true"

    def test_headers_multi_value_comma_joined_and_names_lowered(self):
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "http_code": "200",
            "headers": _json.dumps({"Set-Cookie": ["a=1", "b=2"]}),
        })
        metrics = extract_metrics("http_probe", self._CMD, stdout)
        assert metrics["header.set-cookie"] == "a=1,b=2"

    def test_headers_skip_none_and_empty_values(self):
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "http_code": "200",
            "headers": _json.dumps(
                {"X-Empty": [""], "X-Null": None, "X-Ok": ["1"]}),
        })
        metrics = extract_metrics("http_probe", self._CMD, stdout)
        assert "header.x-empty" not in metrics
        assert "header.x-null" not in metrics
        assert metrics["header.x-ok"] == "1"

    def test_headers_raw_non_json_yields_no_header_keys(self):
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "http_code": "200", "headers": "not json",
        })
        assert extract_metrics("http_probe", self._CMD, stdout) == {"http_code": "200"}

    def test_body_capture_lifted_verbatim_under_body_key(self):
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "http_code": "200", "exit_code": "0",
            "capture": "body", "body": '{"status":"TAMPERED"}',
        })
        metrics = extract_metrics("http_probe", self._CMD, stdout)
        assert metrics["body"] == '{"status":"TAMPERED"}'
        assert metrics["http_code"] == "200"

    def test_empty_body_not_seeded(self):
        import json as _json

        stdout = _json.dumps(
            {"tool": "http_probe", "http_code": "200", "body": ""})
        assert extract_metrics("http_probe", self._CMD, stdout) == {"http_code": "200"}

    def test_all_capture_carries_headers_and_body(self):
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "http_code": "200", "exit_code": "0",
            "capture": "all",
            "headers": _json.dumps({"X-Drill-Injected": ["true"]}),
            "body": "TAMPERED",
        })
        metrics = extract_metrics("http_probe", self._CMD, stdout)
        assert metrics["header.x-drill-injected"] == "true"
        assert metrics["body"] == "TAMPERED"

    def test_cross_check_exact_key_hit_on_anchor_ignores_volatile_date(self):
        # The whole point of flattening: a claim on the ANCHOR header resolves
        # by exact-key lookup against the timeline, while a volatile Date
        # header — flattened too, but never cited in a claim — cannot seed a
        # spurious mismatch (the cross-check only looks up keys the claim names,
        # it does not scan every timeline key for equality).
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "http_code": "200", "exit_code": "0",
            "capture": "headers",
            "headers": _json.dumps({
                "Date": ["Tue, 21 Oct 2026 00:00:00 GMT"],
                "X-Drill-Injected": ["true"],
            }),
        })
        timeline = extract_metrics("http_probe", self._CMD, stdout)
        # the anchor the LLM cites hits exactly...
        assert timeline["header.x-drill-injected"] == "true"
        # ...Date is present under its OWN key (so it never collides with the
        # anchor), but a claim dict naming only the anchor resolves cleanly and
        # never touches the volatile Date value.
        assert timeline["header.date"] == "Tue, 21 Oct 2026 00:00:00 GMT"
        claims = {"header.x-drill-injected": "true"}
        assert all(timeline.get(k) == v for k, v in claims.items())

    def test_resolve_provenance_is_not_lifted_as_a_metric(self):
        # The ``resolve`` provenance key (the --resolve DNS override the tool
        # echoes back for auditability) must NOT enter the observation timeline.
        # _parse_http_probe is a WHITELIST parser — it lifts ONLY the five
        # canonical metric keys plus headers/body — so an extra provenance string
        # is ignored by construction. This is exactly why the --resolve feature
        # needs ZERO extractor change: an Ingress entry probe routed through
        # http_probe(resolve=...) lands the SAME canonical http_code the
        # cross-check reads, and the override stays provenance-only.
        import json as _json

        stdout = _json.dumps({
            "tool": "http_probe", "origin_pod": "drill-http-client",
            "namespace": "default", "url": "http://drill-echo.example.com/",
            "capture": "metadata",
            "resolve": "drill-echo.example.com:80:22.10.85.9",
            "http_code": "503", "exit_code": "0",
        })
        metrics = extract_metrics("http_probe", self._CMD, stdout)
        assert metrics == {"http_code": "503", "exit_code": "0"}
        assert "resolve" not in metrics
        assert "url" not in metrics and "origin_pod" not in metrics
