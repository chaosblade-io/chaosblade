from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.agent.nodes._store_sync import _extract_db_fields


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
