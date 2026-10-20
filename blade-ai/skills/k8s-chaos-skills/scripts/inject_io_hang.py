#!/usr/bin/env python3
"""磁盘 IO 挂起（io_hang）注入脚本 — 补齐 chaosblade-io/chaosblade#1034 缺失的场景。

ChaosBlade 的 disk 实验只有 `burn`（IO 压力）和 `fill`（空间占用），没有
`io_hang`（IO 请求长时间不返回）。本脚本用内核原生能力在目标节点上补齐该场景：

- `fsfreeze` 模式（默认）：冻结 `--path` 所在文件系统，之后所有写 IO 与 fsync
  全部阻塞在内核里，适合让运行中的应用真正卡在 write/fsync 上。
- `dm-delay` 模式：loop 设备 + device-mapper `delay` 目标构造一个「IO 迟迟不完成」
  的块设备并挂载，适合验证挂载点级别的存储后端无响应，不影响节点其它文件系统。

两种模式都通过一个绑定到目标节点的特权 Pod 进入宿主机命名空间执行，并且在注入前
先把恢复脚本和看门狗落到宿主机 tmpfs 上：`--timeout` 到点自动恢复，避免脚本中断、
网络断开或 Pod 被回收导致节点被永久冻结。

用法:
    python inject_io_hang.py --node <node> --path <path> --kubeconfig <path> \
        [--action inject|recover|status] [--mode fsfreeze|dm-delay] \
        [--timeout 300] [--delay-ms 600000] [--size-mb 512] [--namespace default]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys

POD_NAME_PREFIX = "chaos-io-hang"
DEFAULT_IMAGE = "busybox:1.36"
STATE_DIR = "/run/chaos-io-hang"

# 冻结根文件系统会连带冻结 kubelet 与容器运行时，节点将无法被恢复 —— 默认拒绝。
PROTECTED_MOUNTPOINTS = ("/",)

_KUBECONFIG: str = ""


# ---------------------------------------------------------------------------
# kubectl / 宿主机命令执行
# ---------------------------------------------------------------------------


def run_kubectl(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--kubeconfig", _KUBECONFIG] + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="kubectl timeout")


def node_exec_args(namespace: str, pod: str, script: str) -> list[str]:
    """构造在宿主机命名空间里执行 shell 片段的 kubectl exec 参数。"""
    return [
        "exec", pod, "-n", namespace, "--",
        "nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid",
        "--", "sh", "-c", script,
    ]


def node_run(namespace: str, pod: str, script: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return run_kubectl(node_exec_args(namespace, pod, script), timeout=timeout)


# ---------------------------------------------------------------------------
# 命名
# ---------------------------------------------------------------------------


def _digest(*parts: str) -> str:
    return hashlib.sha1(":".join(parts).encode()).hexdigest()[:8]


def pod_name(node: str) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", node.lower()).strip("-")[:20].strip("-")
    return f"{POD_NAME_PREFIX}-{safe or 'node'}-{_digest(node)}"


def scenario_id(mode: str, path: str) -> str:
    """同一个 (mode, path) 组合对应稳定的状态文件名，便于 recover/status 复用。"""
    tail = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")[-24:].strip("-")
    return f"{mode}-{tail or 'root'}-{_digest(mode, path)}"


def state_file(sid: str) -> str:
    return f"{STATE_DIR}/{sid}.env"


def recover_file(sid: str) -> str:
    return f"{STATE_DIR}/{sid}.recover.sh"


def dm_name(sid: str) -> str:
    return f"{POD_NAME_PREFIX}-{_digest(sid)}"


# ---------------------------------------------------------------------------
# Pod 清单
# ---------------------------------------------------------------------------


def build_pod_manifest(name: str, namespace: str, node: str, image: str, ttl: int) -> dict:
    """特权 Pod：绑定目标节点 + hostPID，用于 nsenter 进入宿主机命名空间。"""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": POD_NAME_PREFIX, "chaosblade.io/scenario": "disk-io-hang"},
        },
        "spec": {
            "nodeName": node,
            "hostPID": True,
            "hostIPC": True,
            "hostNetwork": True,
            "restartPolicy": "Never",
            "tolerations": [{"operator": "Exists"}],
            "terminationGracePeriodSeconds": 0,
            "containers": [{
                "name": "agent",
                "image": image,
                "command": ["sleep", str(ttl)],
                "securityContext": {"privileged": True},
            }],
        },
    }


def ensure_pod(namespace: str, node: str, image: str, ttl: int) -> tuple[str, str]:
    """创建（或复用）特权 Pod，返回 (pod 名, 错误信息)。"""
    name = pod_name(node)
    manifest = json.dumps(build_pod_manifest(name, namespace, node, image, ttl))
    cmd = ["kubectl", "--kubeconfig", _KUBECONFIG, "apply", "-f", "-"]
    try:
        r = subprocess.run(cmd, input=manifest, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return name, "kubectl apply timeout"
    if r.returncode != 0:
        return name, f"create privileged pod failed: {r.stderr.strip()}"

    w = run_kubectl(["wait", "--for=condition=Ready", f"pod/{name}", "-n", namespace,
                     "--timeout=90s"], timeout=120)
    if w.returncode != 0:
        return name, f"privileged pod not ready: {w.stderr.strip() or w.stdout.strip()}"
    return name, ""


def delete_pod(namespace: str, name: str) -> None:
    run_kubectl(["delete", "pod", name, "-n", namespace, "--ignore-not-found",
                 "--grace-period=0", "--force"])


# ---------------------------------------------------------------------------
# 宿主机 shell 片段（纯函数，便于单测）
# ---------------------------------------------------------------------------


def detach(inner: str) -> str:
    """把 inner 以脱离当前会话的方式放到后台 —— Pod 被回收后仍然存活。"""
    quoted = shlex.quote(inner)
    return (f"if command -v setsid >/dev/null 2>&1; then "
            f"setsid sh -c {quoted} </dev/null >/dev/null 2>&1 & "
            f"else nohup sh -c {quoted} </dev/null >/dev/null 2>&1 & fi")


def watchdog_script(sid: str, timeout: int) -> str:
    """看门狗：--timeout 秒后自动执行恢复脚本，防止节点被永久挂起。"""
    if timeout <= 0:
        return "echo 'watchdog disabled (--timeout 0), manual recover required'"
    inner = f"sleep {timeout}; sh {shlex.quote(recover_file(sid))}"
    return detach(inner)


def write_state_script(sid: str, fields: dict[str, str], recover_body: str) -> str:
    """把状态文件与恢复脚本写到宿主机 tmpfs（/run 不会被 freeze 影响）。"""
    lines = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return "\n".join([
        f"mkdir -p {shlex.quote(STATE_DIR)}",
        f"cat > {shlex.quote(state_file(sid))} <<'CHAOS_STATE_EOF'\n{lines}\nCHAOS_STATE_EOF",
        f"cat > {shlex.quote(recover_file(sid))} <<'CHAOS_RECOVER_EOF'\n{recover_body}\nCHAOS_RECOVER_EOF",
        f"chmod +x {shlex.quote(recover_file(sid))}",
    ])


def fsfreeze_recover_body(sid: str, mountpoint: str) -> str:
    mp = shlex.quote(mountpoint)
    return "\n".join([
        "#!/bin/sh",
        f"fsfreeze -u {mp} 2>/dev/null",
        f"rm -f {shlex.quote(state_file(sid))} {shlex.quote(recover_file(sid))}",
    ])


def fsfreeze_inject_script(sid: str, path: str, mountpoint: str, timeout: int) -> str:
    """先落恢复脚本与看门狗，最后才冻结 —— 顺序反了就可能失去恢复手段。"""
    fields = {"mode": "fsfreeze", "path": path, "mountpoint": mountpoint,
              "timeout": str(timeout)}
    return "\n".join([
        "set -e",
        write_state_script(sid, fields, fsfreeze_recover_body(sid, mountpoint)),
        watchdog_script(sid, timeout),
        f"fsfreeze -f {shlex.quote(mountpoint)}",
        f"echo frozen={shlex.quote(mountpoint)}",
    ])


def dm_delay_recover_body(sid: str, loop: str, sectors: str,
                          img: str, mount_target: str) -> str:
    name = dm_name(sid)
    table_zero = shlex.quote(f"0 {sectors} delay {loop} 0 0")
    return "\n".join([
        "#!/bin/sh",
        # 先把延迟清零，否则 umount/remove 会卡在还没完成的 IO 上
        f"dmsetup suspend --noflush --nolockfs {name} 2>/dev/null",
        f"dmsetup reload {name} --table {table_zero} 2>/dev/null",
        f"dmsetup resume {name} 2>/dev/null",
        f"umount {shlex.quote(mount_target)} 2>/dev/null || umount -l {shlex.quote(mount_target)} 2>/dev/null",
        f"dmsetup remove {name} 2>/dev/null",
        f"losetup -d {shlex.quote(loop)} 2>/dev/null",
        f"rm -f {shlex.quote(img)}",
        f"rmdir {shlex.quote(mount_target)} 2>/dev/null",
        f"rm -f {shlex.quote(state_file(sid))} {shlex.quote(recover_file(sid))}",
    ])


def dm_delay_hang_script(sid: str, loop: str, sectors: str, delay_ms: int) -> str:
    """把 dm 表的延迟调到 delay_ms —— 挂载完成之后才做，否则 mount 自己会卡住。"""
    name = dm_name(sid)
    table = shlex.quote(f"0 {sectors} delay {loop} 0 {delay_ms}")
    return "\n".join([
        "set -e",
        f"dmsetup suspend {name}",
        f"dmsetup reload {name} --table {table}",
        f"dmsetup resume {name}",
    ])


def write_probe_script(target: str, seconds: int) -> str:
    """写探针：IO 被挂起时 dd 会超时（timeout 返回 124）。"""
    probe = shlex.quote(f"{target.rstrip('/')}/.chaos-io-hang-probe")
    return "\n".join([
        f"timeout {seconds} dd if=/dev/zero of={probe} bs=4k count=1 conv=fsync "
        f"</dev/null >/dev/null 2>&1; echo probe_exit=$?",
        f"rm -f {probe} 2>/dev/null || true",
    ])


# ---------------------------------------------------------------------------
# 动作实现
# ---------------------------------------------------------------------------


def fail(result: dict, error: str) -> None:
    result["status"] = "failed"
    result["error"] = error
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(1)


def resolve_mountpoint(namespace: str, pod: str, path: str) -> tuple[str, str]:
    r = node_run(namespace, pod, f"findmnt -n -o TARGET --target {shlex.quote(path)}")
    if r.returncode != 0:
        return "", f"resolve mountpoint failed: {r.stderr.strip() or r.stdout.strip()}"
    mp = r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
    if not mp:
        return "", f"path {path} does not exist on the node"
    return mp, ""


def do_inject_fsfreeze(args, namespace: str, pod: str, result: dict) -> dict:
    mp, err = resolve_mountpoint(namespace, pod, args.path)
    if err:
        fail(result, err)
    result["mountpoint"] = mp

    if mp in PROTECTED_MOUNTPOINTS and not args.force:
        fail(result,
             f"refusing to freeze {mp}: it backs kubelet and the container runtime, "
             f"the node would become unrecoverable. Target a dedicated mount (PV / data "
             f"disk), use --mode dm-delay, or pass --force if you accept the blast radius")

    sid = scenario_id("fsfreeze", args.path)
    result["scenario_id"] = sid

    exists = node_run(namespace, pod, f"test -f {shlex.quote(state_file(sid))} && echo yes || echo no")
    if exists.stdout.strip() == "yes" and not args.force:
        fail(result, f"io_hang already injected for {args.path} (state {state_file(sid)}); "
                     f"run --action recover first, or pass --force")

    r = node_run(namespace, pod, fsfreeze_inject_script(sid, args.path, mp, args.timeout))
    if r.returncode != 0:
        fail(result, f"fsfreeze failed: {r.stderr.strip() or r.stdout.strip()}")

    result["status"] = "success"
    result["message"] = (
        f"Filesystem {mp} frozen on node {args.node}: writes and fsync now hang. "
        f"Auto-recover in {args.timeout}s." if args.timeout > 0 else
        f"Filesystem {mp} frozen on node {args.node}: writes and fsync now hang. "
        f"Watchdog disabled — recover manually with --action recover."
    )
    return result


def do_inject_dm_delay(args, namespace: str, pod: str, result: dict) -> dict:
    sid = scenario_id("dm-delay", args.path)
    name = dm_name(sid)
    img = f"{args.path.rstrip('/')}/.{sid}.img"
    target = args.mount_target or f"/mnt/{sid}"
    result.update({"scenario_id": sid, "dm_device": name, "backing_file": img,
                   "mount_target": target})

    exists = node_run(namespace, pod, f"test -f {shlex.quote(state_file(sid))} && echo yes || echo no")
    if exists.stdout.strip() == "yes" and not args.force:
        fail(result, f"io_hang already injected for {args.path} (state {state_file(sid)}); "
                     f"run --action recover first, or pass --force")

    # 1. 准备 loop 设备
    r = node_run(namespace, pod,
                 f"set -e\ntest -d {shlex.quote(args.path)}\n"
                 f"truncate -s {args.size_mb}M {shlex.quote(img)}\n"
                 f"losetup --find --show {shlex.quote(img)}")
    if r.returncode != 0:
        fail(result, f"losetup failed: {r.stderr.strip() or r.stdout.strip()}")
    loop = r.stdout.strip().splitlines()[-1].strip()
    result["loop_device"] = loop

    r = node_run(namespace, pod, f"blockdev --getsz {shlex.quote(loop)}")
    if r.returncode != 0 or not r.stdout.strip():
        node_run(namespace, pod, f"losetup -d {shlex.quote(loop)}; rm -f {shlex.quote(img)}")
        fail(result, f"blockdev --getsz failed: {r.stderr.strip() or r.stdout.strip()}")
    sectors = r.stdout.strip().splitlines()[-1].strip()
    result["sectors"] = sectors

    recover_body = dm_delay_recover_body(sid, loop, sectors, img, target)

    # 2. 先以 0 延迟建表、格式化、挂载，并落下恢复脚本与看门狗
    setup = "\n".join([
        "set -e",
        f"dmsetup create {name} --table {shlex.quote(f'0 {sectors} delay {loop} 0 0')}",
        f"mkfs.ext4 -F -q /dev/mapper/{name}",
        f"mkdir -p {shlex.quote(target)}",
        f"mount /dev/mapper/{name} {shlex.quote(target)}",
        write_state_script(sid, {
            "mode": "dm-delay", "path": args.path, "dm_device": name, "loop": loop,
            "sectors": sectors, "backing_file": img, "mount_target": target,
            "delay_ms": str(args.delay_ms), "timeout": str(args.timeout),
        }, recover_body),
        watchdog_script(sid, args.timeout),
    ])
    r = node_run(namespace, pod, setup, timeout=240)
    if r.returncode != 0:
        node_run(namespace, pod, recover_body)
        fail(result, f"dm-delay setup failed: {r.stderr.strip() or r.stdout.strip()}")

    # 3. 最后把延迟拉满，此刻起该挂载点的 IO 才开始挂起
    r = node_run(namespace, pod, dm_delay_hang_script(sid, loop, sectors, args.delay_ms))
    if r.returncode != 0:
        node_run(namespace, pod, recover_body)
        fail(result, f"dm-delay reload failed: {r.stderr.strip() or r.stdout.strip()}")

    result["status"] = "success"
    result["message"] = (
        f"Mount {target} on node {args.node} now hangs every IO for {args.delay_ms}ms "
        f"(dm-delay {name} over {loop}). Point the workload's volume at {target} to "
        f"observe the hang. "
        + (f"Auto-recover in {args.timeout}s." if args.timeout > 0
           else "Watchdog disabled — recover manually with --action recover.")
    )
    return result


def do_recover(args, namespace: str, pod: str, result: dict) -> dict:
    sid = scenario_id(args.mode, args.path)
    result["scenario_id"] = sid
    rf = shlex.quote(recover_file(sid))
    r = node_run(namespace, pod, f"test -f {rf} && sh {rf} && echo recovered || echo missing",
                 timeout=180)
    out = r.stdout.strip()
    if "missing" in out or r.returncode != 0:
        # fsfreeze 兜底：状态文件丢失时仍尽力解冻 --path 所在文件系统
        if args.mode == "fsfreeze":
            mp, err = resolve_mountpoint(namespace, pod, args.path)
            if not err:
                node_run(namespace, pod, f"fsfreeze -u {shlex.quote(mp)} 2>/dev/null || true")
                result["status"] = "success"
                result["mountpoint"] = mp
                result["message"] = (f"No state file for {args.path}; issued a best-effort "
                                     f"fsfreeze -u on {mp}")
                return result
        fail(result, f"no recovery state for mode={args.mode} path={args.path} "
                     f"(expected {recover_file(sid)}); nothing to recover")

    result["status"] = "success"
    result["message"] = f"Recovered io_hang (mode={args.mode}) for {args.path} on node {args.node}"
    return result


def do_status(args, namespace: str, pod: str, result: dict) -> dict:
    sid = scenario_id(args.mode, args.path)
    result["scenario_id"] = sid
    r = node_run(namespace, pod, f"cat {shlex.quote(state_file(sid))} 2>/dev/null || true")
    state = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            state[k.strip()] = v.strip()
    result["injected"] = bool(state)
    result["state"] = state

    probe_target = state.get("mount_target") or state.get("mountpoint") or args.path
    p = node_run(namespace, pod, write_probe_script(probe_target, args.probe_seconds),
                 timeout=args.probe_seconds + 60)
    exit_code = None
    for line in p.stdout.splitlines():
        if line.startswith("probe_exit="):
            exit_code = line.split("=", 1)[1].strip()
    result["write_probe"] = {"target": probe_target, "exit_code": exit_code,
                             "hanging": exit_code not in (None, "0")}
    result["status"] = "success"
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="磁盘 IO 挂起（io_hang）注入")
    parser.add_argument("--node", required=True, help="目标节点名称")
    parser.add_argument("--path", required=True,
                        help="fsfreeze 模式：要冻结的路径；dm-delay 模式：存放 loop 背景文件的目录")
    parser.add_argument("--kubeconfig", required=True, help="kubeconfig 路径")
    parser.add_argument("--action", default="inject", choices=["inject", "recover", "status"])
    parser.add_argument("--mode", default="fsfreeze", choices=["fsfreeze", "dm-delay"])
    parser.add_argument("--namespace", default="default", help="特权 Pod 所在命名空间")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="特权 Pod 镜像（需含 nsenter）")
    parser.add_argument("--timeout", type=int, default=300,
                        help="看门狗自动恢复时间（秒），0 = 关闭（需手动恢复）")
    parser.add_argument("--delay-ms", type=int, default=600000,
                        help="dm-delay 模式的单次 IO 延迟（毫秒）")
    parser.add_argument("--size-mb", type=int, default=512,
                        help="dm-delay 模式 loop 背景文件大小（MB）")
    parser.add_argument("--mount-target", default="",
                        help="dm-delay 模式的挂载点，默认 /mnt/<scenario_id>")
    parser.add_argument("--probe-seconds", type=int, default=5,
                        help="status 写探针的超时秒数")
    parser.add_argument("--ttl", type=int, default=600, help="特权 Pod 存活秒数")
    parser.add_argument("--keep-pod", action="store_true", help="执行结束后保留特权 Pod")
    parser.add_argument("--force", action="store_true",
                        help="跳过保护性检查（冻结根文件系统 / 覆盖已有注入）")
    args = parser.parse_args()

    global _KUBECONFIG
    _KUBECONFIG = args.kubeconfig

    result: dict = {"status": "failed", "action": args.action, "mode": args.mode,
                    "node": args.node, "path": args.path}

    pod, err = ensure_pod(args.namespace, args.node, args.image, args.ttl)
    result["pod"] = pod
    if err:
        if not args.keep_pod:
            delete_pod(args.namespace, pod)
        fail(result, err)

    try:
        if args.action == "recover":
            result = do_recover(args, args.namespace, pod, result)
        elif args.action == "status":
            result = do_status(args, args.namespace, pod, result)
        elif args.mode == "fsfreeze":
            result = do_inject_fsfreeze(args, args.namespace, pod, result)
        else:
            result = do_inject_dm_delay(args, args.namespace, pod, result)
    finally:
        if not args.keep_pod:
            delete_pod(args.namespace, pod)

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
