"""Regression tests for the ChaosBlade Operator preflight check.

Pinned by chaosblade-io/chaosblade#1339: the check used to report
``passed`` (with a bogus version donated by an unrelated image) when NO
real operator existed, because it identified candidates by a loose
``"chaosblade" in name`` substring and let any ready look-alike satisfy
readiness.

Root-cause fix under test: identification is now POSITIVE (operator image
match, or exact deployment name) and happens BEFORE readiness is
evaluated; a missing ``availableReplicas`` counts as 0 instead of
vanishing; the version extractor has no unrelated-image fallback.
"""
import json
import sys
from types import SimpleNamespace

import pytest

from chaos_agent.preflight import (
    _extract_operator_image_version,
    _is_operator_deployment,
    _operator_replicas_ready,
    _scan_deployments_json,
    _scan_deployments_jsonpath,
    check_chaosblade_operator,
)

# The real submodule: ``chaos_agent.tools`` re-exports a StructuredTool
# also named ``kubectl``, which shadows the submodule attribute on the
# package — resolve the module through sys.modules so patching hits the
# exact object preflight's lazy import reads from.
_KUBECTL_MOD = sys.modules["chaos_agent.tools.kubectl_cli"]

# ── Unit: scanners normalize missing replicas, never filter names ─────


class TestScanDeploymentsJsonpath:
    def test_missing_replicas_is_zero_not_dropped(self):
        rows = _scan_deployments_jsonpath("chaosblade-box||example/box:20260529\n")
        assert rows == [("chaosblade-box", "0", ["example/box:20260529"])]

    def test_no_name_filtering_at_scan_stage(self):
        rows = _scan_deployments_jsonpath("nginx|3|nginx:1.25\n")
        assert rows == [("nginx", "3", ["nginx:1.25"])]

    def test_multi_container_images(self):
        rows = _scan_deployments_jsonpath(
            "chaosblade-operator|1|ghcr.io/chaosblade-io/chaosblade-operator:1.7.4,sidecar:2,\n"
        )
        assert rows == [
            ("chaosblade-operator", "1",
             ["ghcr.io/chaosblade-io/chaosblade-operator:1.7.4", "sidecar:2"])
        ]


class TestScanDeploymentsJson:
    @staticmethod
    def _item(name, avail, images):
        item = {"metadata": {"name": name},
                "spec": {"template": {"spec": {"containers": [
                    {"image": img} for img in images]}}}}
        if avail is not None:  # None → API omits the field (omitempty)
            item["status"] = {"availableReplicas": avail}
        return item

    def test_absent_field_is_zero(self):
        doc = {"items": [self._item("chaosblade-box", None, ["example/box:20260529"])]}
        rows = _scan_deployments_json(json.dumps(doc))
        assert rows == [("chaosblade-box", "0", ["example/box:20260529"])]

    def test_malformed_json_returns_nothing(self):
        assert _scan_deployments_json("not json") == []


# ── Unit: positive identification ──────────────────────────────────────


class TestOperatorIdentification:
    def test_image_is_the_decisive_signal(self):
        assert _is_operator_deployment(
            "whatever-renamed", ["ghcr.io/chaosblade-io/chaosblade-operator:1.7.4"])

    def test_exact_name_is_secondary_signal(self):
        assert _is_operator_deployment("chaosblade-operator", ["custom-registry/op:v2"])

    @pytest.mark.parametrize("name", [
        "chaosblade-box",                       # ChaosBlade Box itself
        "chaosblade-space-exploration-mysql",   # unrelated app, issue repro
        "my-chaosblade-dashboard",
    ])
    def test_substring_name_alone_is_never_enough(self, name):
        assert not _is_operator_deployment(name, ["mysql:8.4"])

    def test_box_images_do_not_match(self):
        assert not _is_operator_deployment(
            "chaosblade-box", ["chaosblade-box-museum:1.0", "chaosblade-box-dashboard:1.0"])


# ── Unit: version extraction has no unrelated-image fallback ──────────


class TestVersionExtraction:
    def test_operator_image_semver(self):
        assert _extract_operator_image_version(
            ["ghcr.io/chaosblade-io/chaosblade-operator:1.7.4"]) == "1.7.4"

    def test_v_prefix_stripped(self):
        assert _extract_operator_image_version(
            ["registry.cn/chaosblade-operator:v1.7.4-amd64"]) == "1.7.4"

    def test_registry_host_with_port(self):
        assert _extract_operator_image_version(
            ["host:5000/chaosblade-operator:1.7.4"]) == "1.7.4"

    def test_no_fallback_to_unrelated_image(self):
        # The exact #1339 shape: only unrelated images present.
        assert _extract_operator_image_version(
            ["example/box:20260529", "mysql:8.4"]) == ""

    def test_operator_image_preferred_over_sidecar(self):
        assert _extract_operator_image_version(
            ["sidecar:2.0", "ghcr.io/chaosblade-io/chaosblade-operator:1.7.4"]) == "1.7.4"


class TestReplicasReady:
    def test_empty_is_not_ready(self):
        assert not _operator_replicas_ready("")

    def test_zero_token_fails(self):
        assert not _operator_replicas_ready("1 0")

    def test_all_positive(self):
        assert _operator_replicas_ready("1 2")


# ── End-to-end: the four scenarios from issue #1339 ────────────────────

# The issue's reproduction fleet, jsonpath format (kubectl/kubeconfig):
# no real operator anywhere; box is unavailable (field absent), the
# unrelated mysql is ready with a numeric-looking tag.
ISSUE_FLEET_JSONPATH = (
    "chaosblade-box||example/box:20260529\n"
    "chaosblade-space-exploration-mysql|1|mysql:8.4\n"
)


def _kubectl_result(stdout, exit_code=0, stderr=""):
    return SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=stderr)


@pytest.fixture
def k8s_channel(monkeypatch):
    """Force the kubectl/kubeconfig (jsonpath) code path."""
    monkeypatch.setattr("chaos_agent.preflight._is_host_scope_channel", lambda: False)
    monkeypatch.setattr("chaos_agent.preflight._is_kubewiz_channel", lambda: False)
    return monkeypatch


async def _run(monkeypatch, stdout, exit_code=0):
    async def fake_exec(*args, **kwargs):
        return _kubectl_result(stdout, exit_code)
    monkeypatch.setattr(_KUBECTL_MOD, "exec_kubectl_raw", fake_exec)
    return await check_chaosblade_operator()


class TestIssue1339Scenarios:
    async def test_unrelated_lookalikes_no_longer_pass(self, k8s_channel):
        """The exact repro: previously `passed / v20260529`, must now fail
        with 'not deployed' — the ready mysql cannot satisfy the check and
        the box image cannot donate a version."""
        r = await _run(k8s_channel, ISSUE_FLEET_JSONPATH)
        assert not r.passed
        assert "not deployed" in r.message
        assert "20260529" not in r.message

    async def test_same_fleet_via_kubewiz_json_path(self, monkeypatch):
        """The JSON parser path must reach the same verdict."""
        monkeypatch.setattr("chaos_agent.preflight._is_host_scope_channel", lambda: False)
        monkeypatch.setattr("chaos_agent.preflight._is_kubewiz_channel", lambda: True)
        doc = {"items": [
            {"metadata": {"name": "chaosblade-box"},
             "spec": {"template": {"spec": {"containers": [{"image": "example/box:20260529"}]}}}},
            {"metadata": {"name": "chaosblade-space-exploration-mysql"},
             "status": {"availableReplicas": 1},
             "spec": {"template": {"spec": {"containers": [{"image": "mysql:8.4"}]}}}},
        ]}
        r = await _run(monkeypatch, json.dumps(doc))
        assert not r.passed
        assert "not deployed" in r.message

    async def test_identified_but_unavailable_operator_fails(self, k8s_channel):
        """Operator present (by image) but zero available replicas —
        absent field must count as 0, not vanish."""
        stdout = "chaosblade-operator||ghcr.io/chaosblade-io/chaosblade-operator:1.7.4\n"
        r = await _run(k8s_channel, stdout)
        assert not r.passed
        assert "not ready" in r.message

    async def test_genuine_ready_operator_passes_with_real_version(self, k8s_channel):
        stdout = "chaosblade-operator|1|ghcr.io/chaosblade-io/chaosblade-operator:1.7.4\n"
        r = await _run(k8s_channel, stdout)
        assert r.passed
        assert r.message == "v1.7.4"

    async def test_renamed_image_install_reports_ready_without_version(self, k8s_channel):
        """Exact-name identification with a custom image: ready passes,
        but no bogus version is fabricated."""
        stdout = "chaosblade-operator|1|custom-registry/op:v2\n"
        r = await _run(k8s_channel, stdout)
        assert r.passed
        assert r.message == "ready"

    async def test_operator_among_unrelated_noise(self, k8s_channel):
        """Real operator + unrelated chaosblade-named noise: verdict and
        version come from the identified workload only."""
        stdout = (
            "chaosblade-space-exploration-mysql|1|mysql:8.4\n"
            "chaosblade-operator|1|ghcr.io/chaosblade-io/chaosblade-operator:1.7.4\n"
        )
        r = await _run(k8s_channel, stdout)
        assert r.passed
        assert r.message == "v1.7.4"

    async def test_kubectl_failure_still_reports_not_deployed(self, k8s_channel):
        r = await _run(k8s_channel, "", exit_code=1)
        assert not r.passed
        assert "not deployed" in r.message
