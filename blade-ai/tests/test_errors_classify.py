"""Tests for the layered error classification (Patch B).

The legacy ``should_auto_replan`` is kept compatible by re-implementing
it on top of :func:`classify_error`. Both APIs are exercised here.
"""

from __future__ import annotations

import pytest

from chaos_agent.errors import (
    ErrorAction,
    ErrorClass,
    classify_error,
    should_auto_replan,
)


# ---------------------------------------------------------------------------
# Direct ErrorClass classification
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_empty_returns_unknown(self):
        r = classify_error("")
        assert r.error_class == ErrorClass.UNKNOWN
        assert r.action == ErrorAction.END_FAILED
        assert r.matched_pattern is None

    def test_none_returns_unknown(self):
        r = classify_error(None)
        assert r.error_class == ErrorClass.UNKNOWN
        assert r.action == ErrorAction.END_FAILED

    # ── INFRA_TRANSIENT (the case that motivated Patch B) ───────────────
    def test_bad_file_descriptor_is_transient(self):
        # The kubeconfig-handle bug from the user-reported turn
        msg = (
            "Error: blade create failed (exit 1): {\"code\":63061,...} "
            "dial tcp 47.238.146.166:6443: connect: bad file descriptor"
        )
        r = classify_error(msg)
        assert r.error_class == ErrorClass.INFRA_TRANSIENT
        assert r.action == ErrorAction.SHORT_RETRY
        assert r.matched_pattern == "bad file descriptor"

    def test_timeout_is_transient(self):
        r = classify_error("dial tcp: i/o timeout")
        assert r.error_class == ErrorClass.INFRA_TRANSIENT
        assert r.action == ErrorAction.SHORT_RETRY

    def test_connection_refused_is_transient(self):
        r = classify_error("connect: connection refused")
        assert r.error_class == ErrorClass.INFRA_TRANSIENT
        assert r.action == ErrorAction.SHORT_RETRY

    def test_dns_lookup_is_transient(self):
        r = classify_error("dial tcp: lookup foo.bar: no such host")
        assert r.error_class == ErrorClass.INFRA_TRANSIENT

    # ── INFRA_PERSISTENT ────────────────────────────────────────────────
    def test_diskpressure_is_persistent(self):
        msg = "0/3 nodes are available: 1 node(s) had untolerated taint DiskPressure"
        r = classify_error(msg)
        assert r.error_class == ErrorClass.INFRA_PERSISTENT
        assert r.action == ErrorAction.END_FAILED

    def test_evicted_is_persistent(self):
        # Pod evicted — ChaosBlade Agent on a DiskPressure'd node
        r = classify_error("pod otel-c-tool-w2qv9 status Evicted")
        assert r.error_class == ErrorClass.INFRA_PERSISTENT
        assert r.action == ErrorAction.END_FAILED

    # ── AUTH_DENIED ─────────────────────────────────────────────────────
    def test_permission_denied_is_auth(self):
        r = classify_error("permission denied: cannot list pods")
        assert r.error_class == ErrorClass.AUTH_DENIED
        assert r.action == ErrorAction.ASK_USER

    def test_x509_is_auth(self):
        r = classify_error("x509: certificate has expired")
        assert r.error_class == ErrorClass.AUTH_DENIED
        assert r.action == ErrorAction.ASK_USER

    # ── TARGET_GONE ─────────────────────────────────────────────────────
    def test_resource_not_found_is_target_gone(self):
        r = classify_error("Error: resource not found in default")
        assert r.error_class == ErrorClass.TARGET_GONE
        assert r.action == ErrorAction.REPLAN

    def test_no_matches_for_kind(self):
        r = classify_error('error: the server doesn\'t have a resource type "foo"; no matches for kind')
        assert r.error_class == ErrorClass.TARGET_GONE

    # ── USER_CONFIG ─────────────────────────────────────────────────────
    def test_unknown_flag_is_user_config(self):
        # The blade CLI version-incompat scenario the legacy code handled
        r = classify_error("unknown flag: --namespace")
        assert r.error_class == ErrorClass.USER_CONFIG
        assert r.action == ErrorAction.REPLAN

    def test_invalid_parameter(self):
        r = classify_error("invalid parameter cpu_percent: must be 1-100")
        assert r.error_class == ErrorClass.USER_CONFIG
        assert r.action == ErrorAction.REPLAN

    # ── QUOTA_EXCEEDED ──────────────────────────────────────────────────
    def test_quota(self):
        r = classify_error('exceeded quota: cpu="50" used="48"')
        assert r.error_class == ErrorClass.QUOTA_EXCEEDED
        assert r.action == ErrorAction.ASK_USER

    # ── UNKNOWN fallthrough ─────────────────────────────────────────────
    def test_completely_novel_error_falls_to_unknown(self):
        r = classify_error("zzzz some entirely new failure shape never seen before")
        assert r.error_class == ErrorClass.UNKNOWN
        assert r.action == ErrorAction.END_FAILED

    def test_priority_auth_beats_other_overlaps(self):
        # ``forbidden: deadline exceeded`` could theoretically match
        # both AUTH and TRANSIENT; auth must win because of rule order.
        r = classify_error("forbidden: rbac deadline exceeded")
        assert r.error_class == ErrorClass.AUTH_DENIED


# ---------------------------------------------------------------------------
# Legacy should_auto_replan compatibility
# ---------------------------------------------------------------------------


class TestShouldAutoReplanLegacy:
    """The boolean API must keep returning the same answers it did
    before Patch B for inputs that *both* classifiers know about."""

    def test_target_gone_returns_true(self):
        assert should_auto_replan("resource not found in cluster") is True
        assert should_auto_replan("no matches for kind Pod") is True

    def test_user_config_returns_true(self):
        assert should_auto_replan("unknown flag: --foo") is True
        assert should_auto_replan("invalid parameter") is True

    def test_auth_returns_false(self):
        assert should_auto_replan("permission denied") is False
        assert should_auto_replan("forbidden") is False

    def test_transient_returns_false(self):
        # Replan is for "different plan would help"; transient blip
        # doesn't qualify — let SHORT_RETRY handle it.
        assert should_auto_replan("connection refused") is False
        assert should_auto_replan("bad file descriptor") is False

    def test_unknown_returns_false(self):
        assert should_auto_replan("") is False
        assert should_auto_replan("zzzz") is False
