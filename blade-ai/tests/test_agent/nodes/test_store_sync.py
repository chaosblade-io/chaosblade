import pytest

from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.nodes.store._store_sync import _extract_db_fields


def test_extract_db_fields_persists_fault_spec_and_legacy_projection():
    fault_spec = FaultSpec(
        namespace="prod",
        scope="pod",
        names=("pod-a", "pod-b"),
        labels={"app": "demo"},
        blade_target="network",
        blade_action="loss",
        params={"percent": "100"},
        source="test",
    ).to_dict()

    task_fields, detail_fields = _extract_db_fields({
        "task_id": "task-1",
        "fault_spec": fault_spec,
    })

    assert detail_fields["fault_spec"] == fault_spec
    assert detail_fields["target"] == {
        "namespace": "prod",
        "names": ["pod-a", "pod-b"],
        "labels": {"app": "demo"},
        "resource_type": "pod",
    }
    assert detail_fields["params"] == {"percent": "100"}
    assert task_fields["namespace"] == "prod"
    assert task_fields["target_name"] == "pod-a,pod-b"


# ---------------------------------------------------------------------------
# 投影不变量护栏
# ---------------------------------------------------------------------------
# 不变量：**经由 _store_sync 写库时**，凡带 fault_spec（规范形态）就必然投影出
# 遗留形态 target。可恢复列表的判据依赖这个投影链，一旦断裂，走 _store_sync
# 的真实注入会在库里只剩规范形态。
#
# ❗ 这条不变量只在 _store_sync 层成立，**不能钉在 TaskStore.upsert 层** ——
#   upsert 不做投影，"只有 fault_spec、没有 target"在那一层是合法状态
#   （cli/runner.py 的直接 upsert 就是这个形状，并由
#   test_task_store.TestQueryActive::test_canonical_fault_spec_alone_is_recoverable
#   断言其可恢复）。若在 upsert 层钉这条不变量，两个测试会语义冲突。


@pytest.mark.parametrize(
    "spec_kwargs,expected_resource_type",
    [
        # 按 names 选目标的 pod
        (dict(namespace="prod", scope="pod", names=("pod-a",),
              blade_target="network", blade_action="loss"), "pod"),
        # 按 labels 选目标（names 为空 → target_name 为空，但 target 必须有）
        (dict(namespace="prod", scope="pod", names=(), labels={"app": "demo"},
              blade_target="cpu", blade_action="fullload"), "pod"),
        # host 作用域（无 namespace、无 names）
        (dict(namespace="", scope="host", names=(),
              blade_target="cpu", blade_action="fullload"), "host"),
        # node 作用域（cluster-scoped）
        (dict(namespace="", scope="node", names=("node-1",),
              blade_target="disk", blade_action="fill"), "node"),
    ],
)
def test_fault_spec_always_projects_target(spec_kwargs, expected_resource_type):
    """任何形态的 fault_spec 都必须投影出非空 target（含 host / labels 类）。"""
    spec = FaultSpec(source="test", **spec_kwargs).to_dict()

    _, detail_fields = _extract_db_fields({"task_id": "task-1", "fault_spec": spec})

    assert detail_fields.get("target"), "带 fault_spec 却没投影出 target"
    assert detail_fields["target"]["resource_type"] == expected_resource_type
    assert detail_fields.get("fault_spec") == spec, "规范形态本身不得丢失"


def test_projection_does_not_override_explicit_target():
    """已显式给出 target 时不得被 fault_spec 覆盖（setdefault 语义）。"""
    spec = FaultSpec(
        namespace="from-spec", scope="pod", names=("from-spec",),
        blade_target="network", blade_action="loss", source="test",
    ).to_dict()
    explicit = {"namespace": "explicit", "names": ["explicit-pod"],
                "labels": {}, "resource_type": "pod"}

    _, detail_fields = _extract_db_fields({
        "task_id": "task-1", "fault_spec": spec, "target": explicit,
    })

    assert detail_fields["target"] == explicit


def test_no_fault_spec_means_no_synthesized_target():
    """没有 fault_spec 时不得凭空造出 target（否则幽灵行会被误判为可恢复）。"""
    _, detail_fields = _extract_db_fields({"task_id": "task-1",
                                          "skill_name": "pod-kill"})

    assert not detail_fields.get("target")
    assert not detail_fields.get("fault_spec")
