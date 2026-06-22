"""Tests for _verifier_layer1.py — Layer 1 verification parsing."""

import json

import pytest
from langchain_core.messages import ToolMessage

from chaos_agent.agent.nodes._verifier_layer1 import (
    _parse_blade_status_output,
    _parse_blade_query_k8s_output,
    _find_blade_query_in_messages,
    _map_query_k8s_to_layer1,
    _QueryK8sResult,
)


class TestParseBladeStatusOutput:
    def test_running(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Running"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "passed"
        assert not expired

    def test_success_status(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Success"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "passed"

    def test_destroyed_expired(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Destroyed"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert expired is True
        assert "expired" in details.lower()

    def test_revoked_expired(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Revoked"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert expired is True

    def test_api_failure(self):
        raw = json.dumps({"code": 500, "success": False, "result": {}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert not expired

    def test_non_dict_result_means_success(self):
        raw = json.dumps({"code": 200, "success": True, "result": "abc123uid"})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "passed"

    def test_non_json_fallback_running(self):
        raw = "Status: Running, everything is fine"
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "passed"

    def test_non_json_fallback_no_match(self):
        raw = "Error: something went wrong"
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"

    def test_transient_please_wait(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"Status": "Initialized", "Error": "please wait, preparing"},
        })
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "warning"
        assert "transient" in details.lower()

    def test_unknown_status(self):
        raw = json.dumps({"code": 200, "success": True, "result": {"Status": "Unknown"}})
        status, details, expired = _parse_blade_status_output(raw)
        assert status == "failed"
        assert not expired


class TestParseBladeQueryK8sOutput:
    def test_all_success(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"statuses": [
                {"name": "pod-1", "success": True, "state": "Running"},
                {"name": "pod-2", "success": True, "state": "Running"},
            ]},
        })
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "passed"
        assert r.affected_count == 2

    def test_some_failed(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"statuses": [
                {"name": "pod-1", "success": True},
                {"name": "pod-2", "success": False},
            ]},
        })
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "failed"

    def test_expired_state(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"statuses": [
                {"name": "exp-1", "state": "Destroyed", "success": True},
            ]},
        })
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "failed"
        assert r.expired is True

    def test_empty_input(self):
        r = _parse_blade_query_k8s_output("")
        assert r.status == "unknown"

    def test_error_not_found(self):
        r = _parse_blade_query_k8s_output("Error: not found")
        assert r.status == "unknown"
        assert "CRD" in r.details

    def test_non_json(self):
        r = _parse_blade_query_k8s_output("this is not json")
        assert r.status == "unknown"

    def test_api_error_not_found(self):
        raw = json.dumps({"code": 63061, "success": False, "error": "resource not found"})
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "unknown"
        assert "not found" in r.details.lower()

    def test_no_statuses_but_success(self):
        raw = json.dumps({
            "code": 200, "success": True,
            "result": {"success": True},
        })
        r = _parse_blade_query_k8s_output(raw)
        assert r.status == "passed"


class TestFindBladeQueryInMessages:
    def test_finds_matching_message(self):
        uid = "abc-123-xyz"
        content = json.dumps({"success": True, "result": {"uid": uid, "status": "Running"}})
        messages = [
            ToolMessage(content="unrelated", name="kubectl", tool_call_id="tc1"),
            ToolMessage(content=content, name="kubectl", tool_call_id="tc2"),
        ]
        assert _find_blade_query_in_messages(messages, uid) == content

    def test_no_match(self):
        messages = [
            ToolMessage(content="no blade data", name="kubectl", tool_call_id="tc1"),
        ]
        assert _find_blade_query_in_messages(messages, "uid-999") == ""

    def test_wrong_uid(self):
        content = json.dumps({"success": True, "result": {"uid": "other-uid"}})
        messages = [
            ToolMessage(content=content, name="kubectl", tool_call_id="tc1"),
        ]
        assert _find_blade_query_in_messages(messages, "wanted-uid") == ""

    def test_empty_messages(self):
        assert _find_blade_query_in_messages([], "uid") == ""


class TestMapQueryK8sToLayer1:
    def test_passed(self):
        q = _QueryK8sResult("passed", "all ok", [], 2, False)
        r = _map_query_k8s_to_layer1(q, "{}", "pod-1", "original")
        assert r.status == "passed"

    def test_expired(self):
        q = _QueryK8sResult("failed", "expired", [], 1, True)
        r = _map_query_k8s_to_layer1(q, "{}", "pod-1", "discovery")
        assert r.status == "failed"
        assert r.expired is True

    def test_failed_not_expired(self):
        q = _QueryK8sResult("failed", "some failure", [], 1, False)
        r = _map_query_k8s_to_layer1(q, "{}", "pod-1", "original")
        assert r.status == "failed"
        assert r.expired is False
