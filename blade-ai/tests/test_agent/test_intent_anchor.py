"""B76 ① — explicit node anchor extraction from user intent text.

CLI NL mode previously left FaultSpec identity empty and let lazy derivation
decide it from probe-command ORDER (task inject-5552c6e4: a tool-health probe
locked scope=pod, an observation-target probe locked a Pod label, three
REJECT_DRIFT rejections followed). The anchor extraction pins the ONE signal
that outranks every probe: a node the user named themselves.
"""

from types import SimpleNamespace

from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.spec.intent_anchor import extract_explicit_node_anchor

# The verbatim intent of the failing run — must anchor exactly one node.
_R4_INTENT = (
    "模拟节点宕机：在节点 cn-shanghai-cloudspe.25.209.71.189 上切断该节点与 API Server "
    "的网络通信，观察节点 NotReady 状态变化与节点上 Pod 的驱逐重建行为"
)


def test_anchors_verbatim_node_down_intent():
    assert extract_explicit_node_anchor(_R4_INTENT) == (
        "cn-shanghai-cloudspe.25.209.71.189",
    )


def test_anchors_english_prepositional_form_and_strips_punctuation():
    assert extract_explicit_node_anchor(
        "Inject network loss on node worker-1, observe for 300s"
    ) == ("worker-1",)


def test_no_anchor_without_preposition():
    # "节点 NotReady 状态" mentions 节点 but not in "在节点 X 上" form — a bare
    # mention must NOT capture "NotReady" as a node name.
    assert extract_explicit_node_anchor(
        "观察节点 NotReady 状态变化与节点上 Pod 的驱逐重建行为"
    ) == ()


def test_no_anchor_for_pod_or_prose_contexts():
    assert extract_explicit_node_anchor("在 Pod frontend-abc 上注入 CPU 满载") == ()
    assert extract_explicit_node_anchor("对 default 命名空间的应用做演练") == ()
    # Chinese fragments fail the DNS-subdomain shape even in anchor position.
    assert extract_explicit_node_anchor("在节点 生产核心节点 上注入故障") == ()
    # CamelCase words are not k8s names.
    assert extract_explicit_node_anchor("on node NotReady for a while") == ()


def test_multiple_anchors_dedupe_preserving_first_mention():
    text = "在节点 cn-a.1.2.3 上、节点 cn-a.1.2.3 与 cn-b.4.5.6 中同时注入"
    assert extract_explicit_node_anchor(text) == ("cn-a.1.2.3",)


def test_empty_or_missing_text_yields_no_anchor():
    assert extract_explicit_node_anchor("") == ()
    assert extract_explicit_node_anchor(None) == ()  # type: ignore[arg-type]


# ── from_cli_nl integration ─────────────────────────────────────────────


def test_from_cli_nl_prefills_node_identity_from_anchor():
    spec = FaultSpec.from_cli_nl(input_text=_R4_INTENT)
    assert spec.scope == "node"
    assert spec.names == ("cn-shanghai-cloudspe.25.209.71.189",)
    assert spec.labels == {}
    assert spec.user_description == _R4_INTENT


def test_from_cli_nl_without_anchor_stays_on_lazy_derivation_path():
    spec = FaultSpec.from_cli_nl(input_text="对 default 命名空间的应用做演练")
    assert spec.scope == ""
    assert spec.names == ()
    assert spec.labels == {}


def test_from_cli_nl_keeps_tuning_kwargs_alongside_anchor():
    spec = FaultSpec.from_cli_nl(
        input_text=_R4_INTENT,
        kwargs={"duration": 240, "params": {"percent": "100"}},
    )
    assert spec.scope == "node"
    assert spec.names == ("cn-shanghai-cloudspe.25.209.71.189",)
    assert spec.duration_seconds == 240
    assert spec.params == {"percent": "100"}


# ── P2-1: HTTP NL parity — the anchor belongs to the text, not the transport ──


def _http_nl_request(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        input=text, scope="", target_name="", labels=None, namespace="",
        target="", action="", params=None, params_flags=None, duration=0,
    )


def test_http_nl_anchors_same_identity_as_cli_nl():
    """The same anchored text must yield the same identity from both NL
    entry points — an HTTP NL request feeds the same agent_loop
    lazy-derivation path (route_pipeline_start), so a CLI-only anchor would
    leave the HTTP channel racing probe ORDER exactly like r4 did
    (B76 review P2-1: probe-verified — the two constructors previously
    disagreed on the same text)."""
    cli_spec = FaultSpec.from_cli_nl(input_text=_R4_INTENT)
    http_spec = FaultSpec.from_http_request(_http_nl_request(_R4_INTENT))
    assert (http_spec.scope, http_spec.names, http_spec.labels) == (
        cli_spec.scope, cli_spec.names, cli_spec.labels,
    )
    assert http_spec.scope == "node"
    assert http_spec.names == ("cn-shanghai-cloudspe.25.209.71.189",)
    assert http_spec.source == "http_nl"


def test_http_nl_without_anchor_stays_empty():
    http_spec = FaultSpec.from_http_request(
        _http_nl_request("对 default 命名空间的应用做演练"),
    )
    assert http_spec.scope == ""
    assert http_spec.names == ()


def test_http_half_structured_request_keeps_explicit_fields():
    """The anchor fills gaps, it never overrides a stated choice: a request
    that carries explicit identity fields but fails the 5-field structured
    test (here: namespace missing) keeps those fields untouched — even when
    its input text happens to name a node."""
    half = SimpleNamespace(
        input=_R4_INTENT, scope="node",
        target_name="some-other-node", labels=None, namespace="",  # ← fails structured
        target="network", action="loss", params=None, params_flags=None, duration=0,
    )
    spec = FaultSpec.from_http_request(half)
    assert spec.names == ("some-other-node",)
    assert spec.source == "http_nl"
