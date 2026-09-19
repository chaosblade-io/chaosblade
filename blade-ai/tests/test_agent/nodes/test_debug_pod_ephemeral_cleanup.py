"""parse_debug_pod_info must never report the TARGET pod of an ephemeral
``kubectl debug`` as a cleanup candidate (task-5193538b), and must surface
the tool's own ``cleaned`` declaration so scanners skip already-removed
pods (#31: 18 redundant NotFound deletes).

A POD-scoped ``kubectl debug`` attaches an ephemeral container to the
TARGET pod — no probe pod is created — yet the ``[debug-pod-meta]`` tag
still carries the target pod's name/namespace. Both cleanup paths
(planning cleanup + verifier finalize) feed this parser, so a pod-scope
debug used to queue the FAULT TARGET for deletion.

A one-shot ``kubectl_read debug`` probe pod is auto-removed by the tool
itself (meta ``cleaned: true``) and never enters the artifact registry,
so the ONLY signal that saves it from a redundant NotFound delete is the
third return element below.
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
    assert parse_debug_pod_info(content) == ("", "", False)


def test_regular_debug_pod_meta_still_parses():
    content = _content_with_meta({
        "name": "node-debugger-node-a-abc12",
        "namespace": "kubewiz",
    })
    assert parse_debug_pod_info(content) == (
        "node-debugger-node-a-abc12", "kubewiz", False,
    )


def test_ephemeral_flag_falsy_falls_back_to_normal_parse():
    content = _content_with_meta({
        "name": "node-debugger-node-a-abc12",
        "namespace": "kubewiz",
        "ephemeral_container": "",
    })
    assert parse_debug_pod_info(content) == (
        "node-debugger-node-a-abc12", "kubewiz", False,
    )


def test_oneshot_meta_cleaned_true_is_surfaced():
    """A one-shot probe pod the tool already removed must report
    cleaned=True — this is what keeps scanners from re-deleting it."""
    content = _content_with_meta({
        "name": "node-debugger-node-a-abc12",
        "namespace": "default",
        "cleaned": True,
        "oneshot": True,
    })
    assert parse_debug_pod_info(content) == (
        "node-debugger-node-a-abc12", "default", True,
    )


def test_oneshot_meta_cleaned_false_stays_cleanable():
    """Auto-cleanup failed (meta ``cleaned: false``) — the pod still
    exists, so the scanner must keep it deletable."""
    content = _content_with_meta({
        "name": "node-debugger-node-a-abc12",
        "namespace": "default",
        "cleaned": False,
        "oneshot": True,
    })
    assert parse_debug_pod_info(content) == (
        "node-debugger-node-a-abc12", "default", False,
    )


def test_name_pattern_fallback_without_meta_reports_uncleaned():
    """Old tasks emit no meta tag — the name-pattern fallback path has no
    cleaned signal and must conservatively return False."""
    content = (
        "Creating debugging pod node-debugger-node-b-zz9xy with container "
        "debugger on node node-b.\n[debug-pod-ns: kubewiz]"
    )
    assert parse_debug_pod_info(content) == (
        "node-debugger-node-b-zz9xy", "kubewiz", False,
    )
