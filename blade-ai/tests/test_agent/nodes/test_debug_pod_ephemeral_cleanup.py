"""parse_debug_pod_info must never report the TARGET pod of an ephemeral
``kubectl debug`` as a cleanup candidate (task-5193538b).

A POD-scoped ``kubectl debug`` attaches an ephemeral container to the
TARGET pod — no probe pod is created — yet the ``[debug-pod-meta]`` tag
still carries the target pod's name/namespace. Both cleanup paths
(planning cleanup + verifier finalize) feed this parser, so a pod-scope
debug used to queue the FAULT TARGET for deletion.
"""

import json

from chaos_agent.agent.nodes.execute._debug_pod import parse_debug_pod_info


def _content_with_meta(metadata: dict) -> str:
    return (
        "$ kubectl debug -it pod/target --image=ubuntu\n"
        f"[debug-pod-meta: {json.dumps(metadata)}]"
    )


def test_ephemeral_debug_returns_empty():
    content = _content_with_meta({
        "name": "kone-runtime-5b69b7b8bd-6swrx",
        "namespace": "ark-system",
        "ephemeral_container": "debugger",
    })
    assert parse_debug_pod_info(content) == ("", "")


def test_regular_debug_pod_meta_still_parses():
    content = _content_with_meta({
        "name": "node-debugger-node-a-abc12",
        "namespace": "kubewiz",
    })
    assert parse_debug_pod_info(content) == (
        "node-debugger-node-a-abc12", "kubewiz",
    )


def test_ephemeral_flag_falsy_falls_back_to_normal_parse():
    content = _content_with_meta({
        "name": "node-debugger-node-a-abc12",
        "namespace": "kubewiz",
        "ephemeral_container": "",
    })
    assert parse_debug_pod_info(content) == (
        "node-debugger-node-a-abc12", "kubewiz",
    )
