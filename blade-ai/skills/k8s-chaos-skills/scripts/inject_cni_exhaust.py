#!/usr/bin/env python3
"""CNI IP 耗尽注入脚本 — 在目标节点动态计算并创建 Pod 耗尽可用 IP/ENI 资源。

自动查询节点的 IP pool 大小和当前 Pod 数，计算精确的副本数：
填满 IP pool 但不超过 pod capacity，确保触发 CNI 分配失败而非 OutOfpods。

用法:
    python inject_cni_exhaust.py --namespace <ns> --node <node> --kubeconfig <path>
"""

import argparse
import json
import subprocess
import sys


DEPLOYMENT_NAME = "chaos-ip-exhaust"

_KUBECONFIG: str = ""


def run_kubectl(args: list[str]) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--kubeconfig", _KUBECONFIG] + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="kubectl timeout")


def cleanup(namespace: str):
    run_kubectl(["delete", "deployment", DEPLOYMENT_NAME, "-n", namespace,
                 "--ignore-not-found", "--grace-period=0", "--force"])


def safe_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def query_node_capacity(node: str) -> tuple[int, int, int]:
    """查询节点的 pod capacity、IP pool 大小、当前 Pod 数。"""
    r = run_kubectl(["get", "node", node, "-o",
                     "jsonpath={.status.allocatable.pods}"])
    pod_capacity = safe_int(r.stdout) if r.returncode == 0 else 0

    r = run_kubectl(["get", "node", node, "-o",
                     "jsonpath={.metadata.annotations.k8s\\.aliyun\\.com/max-available-ip}"])
    ip_pool = safe_int(r.stdout) if r.returncode == 0 else 0

    # 计算节点上所有非终态 Pod（Running + Pending + ContainerCreating 都占 pod slot）
    r = run_kubectl(["get", "pods", "--all-namespaces",
                     f"--field-selector=spec.nodeName={node}",
                     "-o", "jsonpath={range .items[*]}{.status.phase}{'\\n'}{end}"])
    current_pods = 0
    if r.returncode == 0 and r.stdout.strip():
        for phase in r.stdout.strip().splitlines():
            if phase.strip() not in ("Succeeded", "Failed"):
                current_pods += 1

    return pod_capacity, ip_pool, current_pods


def fail(result: dict, error: str):
    result["error"] = error
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="CNI IP 耗尽注入")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--node", required=True, help="目标节点名称")
    parser.add_argument("--kubeconfig", required=True, help="kubeconfig 路径")
    parser.add_argument("--replicas", type=int, default=0,
                        help="副本数（默认 0 = 自动计算）")
    args = parser.parse_args()

    global _KUBECONFIG
    _KUBECONFIG = args.kubeconfig

    result = {"status": "failed", "deployment": DEPLOYMENT_NAME,
              "namespace": args.namespace, "node": args.node}

    # 1. 查询节点容量
    pod_capacity, ip_pool, current_pods = query_node_capacity(args.node)
    result["node_info"] = {
        "pod_capacity": pod_capacity,
        "ip_pool": ip_pool,
        "current_pods": current_pods,
    }

    if ip_pool <= 0:
        fail(result,
             f"无法获取节点 IP pool 大小（annotation k8s.aliyun.com/max-available-ip 缺失）。"
             f"pod_capacity={pod_capacity}, current_pods={current_pods}")

    # 2. 计算副本数
    if args.replicas > 0:
        replicas = args.replicas
    else:
        ip_remaining = ip_pool - current_pods
        pod_slots_remaining = pod_capacity - current_pods

        if ip_remaining <= 0:
            fail(result,
                 f"节点 IP 已耗尽（current_pods={current_pods} >= ip_pool={ip_pool}）")

        # 填满 IP pool，预留 2 个 pod slot 给目标应用重建
        replicas = min(ip_remaining, pod_slots_remaining - 2)
        if replicas <= 0:
            fail(result,
                 f"无法计算安全副本数：ip_remaining={ip_remaining}, "
                 f"pod_slots_remaining={pod_slots_remaining}")

    result["replicas"] = replicas

    # 3. 创建 deployment（直接绑定 nodeName，避免 pods 临时调度到其他节点）
    r = run_kubectl([
        "create", "deployment", DEPLOYMENT_NAME,
        "--image=busybox",
        "--replicas=0",
        "-n", args.namespace,
        "--", "sleep", "3600",
    ])
    if r.returncode != 0:
        fail(result, f"create failed: {r.stderr.strip()}")

    # 4. 先绑定 nodeName 再扩容（确保所有 pod 直接调度到目标节点）
    patch = json.dumps({"spec": {
        "replicas": replicas,
        "template": {"spec": {"nodeName": args.node}},
    }})
    r = run_kubectl(["patch", "deployment", DEPLOYMENT_NAME,
                     "-n", args.namespace, "-p", patch])
    if r.returncode != 0:
        cleanup(args.namespace)
        fail(result, f"patch failed: {r.stderr.strip()}")

    result["status"] = "success"
    result["message"] = (
        f"Deployment {DEPLOYMENT_NAME} created with {replicas} replicas on node {args.node}. "
        f"Node: pod_capacity={pod_capacity}, ip_pool={ip_pool}, current_pods={current_pods}. "
        f"IP pool will be exhausted, {pod_capacity - current_pods - replicas} pod slots remain."
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
