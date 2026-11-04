"""Tests for the bundled k8s-chaos-skills inject_io_hang.py script.

The script fills the gap left by ChaosBlade's disk experiments (burn / fill
only, no io_hang — chaosblade-io/chaosblade#1034). Its shell fragments are
pure functions precisely so the dangerous ordering invariants can be pinned
here: the recovery script and the watchdog must be in place on the node
*before* IO is suspended, and recovery must drop the delay before it tries
to unmount.
"""

import argparse
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "k8s-chaos-skills"
    / "scripts"
    / "inject_io_hang.py"
)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("inject_io_hang", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode=returncode, stdout=stdout, stderr=stderr)


def _args(**overrides):
    defaults = dict(
        node="node-1", path="/data", kubeconfig="/tmp/kubeconfig", action="inject",
        mode="fsfreeze", namespace="default", image="busybox:1.36", timeout=300,
        delay_ms=600000, size_mb=512, mount_target="", probe_seconds=5, ttl=600,
        keep_pod=False, force=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


class TestNaming:
    def test_pod_name_is_a_valid_dns1123_label(self, mod):
        for node in ["cn-hangzhou.10.0.0.1", "Node_With_UPPER", "x" * 80]:
            name = mod.pod_name(node)
            assert len(name) <= 63
            assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name), name

    def test_names_are_deterministic_and_distinct(self, mod):
        assert mod.pod_name("node-a") == mod.pod_name("node-a")
        assert mod.pod_name("node-a") != mod.pod_name("node-b")

    def test_scenario_id_separates_mode_and_path(self, mod):
        assert mod.scenario_id("fsfreeze", "/data") == mod.scenario_id("fsfreeze", "/data")
        assert mod.scenario_id("fsfreeze", "/data") != mod.scenario_id("dm-delay", "/data")
        assert mod.scenario_id("fsfreeze", "/data") != mod.scenario_id("fsfreeze", "/var/log")

    def test_state_and_recover_files_live_on_tmpfs(self, mod):
        # /run is tmpfs: it stays writable while the target filesystem is frozen.
        sid = mod.scenario_id("fsfreeze", "/data")
        assert mod.state_file(sid).startswith("/run/")
        assert mod.recover_file(sid).startswith("/run/")


# ---------------------------------------------------------------------------
# Privileged pod
# ---------------------------------------------------------------------------


class TestPodManifest:
    def test_manifest_can_reach_the_host_namespaces(self, mod):
        m = mod.build_pod_manifest("p", "ns", "node-1", "busybox:1.36", 600)
        assert m["spec"]["nodeName"] == "node-1"
        assert m["spec"]["hostPID"] is True
        assert m["spec"]["containers"][0]["securityContext"]["privileged"] is True
        assert m["spec"]["containers"][0]["command"] == ["sleep", "600"]
        assert m["spec"]["tolerations"] == [{"operator": "Exists"}]
        assert m["spec"]["restartPolicy"] == "Never"

    def test_node_exec_enters_pid1_mount_namespace(self, mod):
        args = mod.node_exec_args("ns", "pod-x", "echo hi")
        assert args[:6] == ["exec", "pod-x", "-n", "ns", "--", "nsenter"]
        assert "--target" in args and "1" in args
        assert "--mount" in args
        assert args[-3:] == ["sh", "-c", "echo hi"]


# ---------------------------------------------------------------------------
# fsfreeze mode
# ---------------------------------------------------------------------------


class TestFsfreezeScripts:
    def test_recovery_is_staged_before_the_freeze(self, mod):
        sid = mod.scenario_id("fsfreeze", "/data")
        script = mod.fsfreeze_inject_script(sid, "/data", "/data", 300)
        recover_pos = script.index(mod.recover_file(sid))
        freeze_pos = script.index("fsfreeze -f")
        assert recover_pos < freeze_pos, "freeze must come last, after the safety net"
        assert script.index("sleep 300") < freeze_pos

    def test_inject_script_freezes_the_mountpoint_not_the_path(self, mod):
        sid = mod.scenario_id("fsfreeze", "/data/sub")
        script = mod.fsfreeze_inject_script(sid, "/data/sub", "/data", 300)
        assert "fsfreeze -f /data\n" in script

    def test_recover_body_unfreezes_and_clears_state(self, mod):
        sid = mod.scenario_id("fsfreeze", "/data")
        body = mod.fsfreeze_recover_body(sid, "/data")
        assert "fsfreeze -u /data" in body
        assert mod.state_file(sid) in body
        assert mod.recover_file(sid) in body

    def test_watchdog_runs_the_same_recover_script(self, mod):
        sid = mod.scenario_id("fsfreeze", "/data")
        wd = mod.watchdog_script(sid, 120)
        assert "sleep 120" in wd
        assert mod.recover_file(sid) in wd
        assert "setsid" in wd or "nohup" in wd

    def test_watchdog_is_opt_out_only(self, mod):
        wd = mod.watchdog_script(mod.scenario_id("fsfreeze", "/data"), 0)
        assert "sleep" not in wd
        assert "watchdog disabled" in wd

    def test_detach_survives_pod_teardown(self, mod):
        out = mod.detach("sleep 1")
        assert "setsid" in out and "nohup" in out
        assert "</dev/null" in out


# ---------------------------------------------------------------------------
# dm-delay mode
# ---------------------------------------------------------------------------


class TestDmDelayScripts:
    def test_hang_script_loads_the_requested_delay(self, mod):
        sid = mod.scenario_id("dm-delay", "/data")
        script = mod.dm_delay_hang_script(sid, "/dev/loop3", "1048576", 600000)
        assert f"dmsetup suspend {mod.dm_name(sid)}" in script
        assert "0 1048576 delay /dev/loop3 0 600000" in script
        assert script.index("reload") < script.index("resume")

    def test_recover_zeroes_the_delay_before_unmounting(self, mod):
        sid = mod.scenario_id("dm-delay", "/data")
        body = mod.dm_delay_recover_body(
            sid, "/dev/loop3", "1048576", "/data/.img", "/mnt/target"
        )
        zero_pos = body.index("delay /dev/loop3 0 0")
        umount_pos = body.index("umount")
        remove_pos = body.index("dmsetup remove")
        # With the delay still loaded, umount/remove would block on in-flight IO.
        assert zero_pos < umount_pos < remove_pos

    def test_recover_releases_every_resource_it_created(self, mod):
        sid = mod.scenario_id("dm-delay", "/data")
        body = mod.dm_delay_recover_body(
            sid, "/dev/loop3", "1048576", "/data/.img", "/mnt/target"
        )
        for expected in ["losetup -d /dev/loop3", "rm -f /data/.img", "/mnt/target",
                         mod.state_file(sid), mod.recover_file(sid)]:
            assert expected in body


# ---------------------------------------------------------------------------
# Write probe
# ---------------------------------------------------------------------------


class TestWriteProbe:
    def test_probe_is_bounded_and_cleans_up(self, mod):
        script = mod.write_probe_script("/data", 5)
        assert "timeout 5 dd" in script
        assert "conv=fsync" in script
        assert "probe_exit=$?" in script
        assert "rm -f /data/.chaos-io-hang-probe" in script

    def test_probe_target_never_doubles_the_separator(self, mod):
        assert "//" not in mod.write_probe_script("/data/", 5).replace("if=/dev/zero", "")


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------


class TestRootFilesystemGuard:
    def test_refuses_to_freeze_root_without_force(self, mod, monkeypatch, capsys):
        monkeypatch.setattr(mod, "node_run", lambda *a, **k: _completed(stdout="/\n"))

        with pytest.raises(SystemExit) as exc:
            mod.do_inject_fsfreeze(_args(path="/"), "default", "pod-x", {"status": "failed"})

        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert "refusing to freeze /" in payload["error"]
        assert "/" in mod.PROTECTED_MOUNTPOINTS

    def test_force_overrides_the_guard(self, mod, monkeypatch, capsys):
        calls: list[str] = []

        def fake_node_run(namespace, pod, script, timeout=120):
            calls.append(script)
            if script.startswith("findmnt"):
                return _completed(stdout="/\n")
            if script.startswith("test -f"):
                return _completed(stdout="no\n")
            return _completed(stdout="frozen=/\n")

        monkeypatch.setattr(mod, "node_run", fake_node_run)
        result = mod.do_inject_fsfreeze(
            _args(path="/", force=True), "default", "pod-x", {"status": "failed"}
        )
        assert result["status"] == "success"
        assert any("fsfreeze -f /" in c for c in calls)

    def test_refuses_to_inject_twice_over_the_same_path(self, mod, monkeypatch, capsys):
        def fake_node_run(namespace, pod, script, timeout=120):
            if script.startswith("findmnt"):
                return _completed(stdout="/data\n")
            if script.startswith("test -f"):
                return _completed(stdout="yes\n")
            return _completed()

        monkeypatch.setattr(mod, "node_run", fake_node_run)
        with pytest.raises(SystemExit):
            mod.do_inject_fsfreeze(_args(), "default", "pod-x", {"status": "failed"})
        assert "already injected" in json.loads(capsys.readouterr().out)["error"]

    def test_missing_path_fails_before_any_freeze(self, mod, monkeypatch, capsys):
        monkeypatch.setattr(mod, "node_run", lambda *a, **k: _completed(stdout="\n"))
        with pytest.raises(SystemExit):
            mod.do_inject_fsfreeze(_args(path="/nope"), "default", "pod-x", {"status": "failed"})
        assert "does not exist" in json.loads(capsys.readouterr().out)["error"]


# ---------------------------------------------------------------------------
# recover / status
# ---------------------------------------------------------------------------


class TestRecoverAndStatus:
    def test_recover_runs_the_stored_script(self, mod, monkeypatch):
        seen: list[str] = []

        def fake_node_run(namespace, pod, script, timeout=120):
            seen.append(script)
            return _completed(stdout="recovered\n")

        monkeypatch.setattr(mod, "node_run", fake_node_run)
        result = mod.do_recover(_args(action="recover"), "default", "pod-x", {"status": "failed"})
        assert result["status"] == "success"
        assert mod.recover_file(mod.scenario_id("fsfreeze", "/data")) in seen[0]

    def test_recover_falls_back_to_unfreeze_when_state_is_lost(self, mod, monkeypatch):
        scripts: list[str] = []

        def fake_node_run(namespace, pod, script, timeout=120):
            scripts.append(script)
            if "missing" in script:
                return _completed(stdout="missing\n")
            if script.startswith("findmnt"):
                return _completed(stdout="/data\n")
            return _completed()

        monkeypatch.setattr(mod, "node_run", fake_node_run)
        result = mod.do_recover(_args(action="recover"), "default", "pod-x", {"status": "failed"})
        assert result["status"] == "success"
        assert any("fsfreeze -u /data" in s for s in scripts)

    def test_status_reports_a_hanging_probe(self, mod, monkeypatch):
        def fake_node_run(namespace, pod, script, timeout=120):
            if script.startswith("cat "):
                return _completed(stdout="mode=fsfreeze\nmountpoint=/data\ntimeout=300\n")
            return _completed(stdout="probe_exit=124\n")

        monkeypatch.setattr(mod, "node_run", fake_node_run)
        result = mod.do_status(_args(action="status"), "default", "pod-x", {"status": "failed"})
        assert result["injected"] is True
        assert result["state"]["mountpoint"] == "/data"
        assert result["write_probe"] == {"target": "/data", "exit_code": "124", "hanging": True}

    def test_status_reports_a_healthy_probe(self, mod, monkeypatch):
        def fake_node_run(namespace, pod, script, timeout=120):
            if script.startswith("cat "):
                return _completed(stdout="")
            return _completed(stdout="probe_exit=0\n")

        monkeypatch.setattr(mod, "node_run", fake_node_run)
        result = mod.do_status(_args(action="status"), "default", "pod-x", {"status": "failed"})
        assert result["injected"] is False
        assert result["write_probe"]["hanging"] is False
