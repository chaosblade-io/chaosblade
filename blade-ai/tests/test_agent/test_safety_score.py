"""Tests for safety_score multi-dimensional scoring."""

from datetime import datetime

import pytest

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.agent.safety_score import (
    DEFAULT_WEIGHTS,
    DimensionScore,
    SafetyScore,
    _score_blast_radius,
    _score_frequency,
    _score_time,
    _score_topology,
    compute_safety_score,
    maybe_escalate_status,
)


def _spec(**kw) -> FaultSpec:
    """Minimal valid FaultSpec for tests; overrideable per-test."""
    defaults = dict(
        namespace="default",
        scope="pod",
        names=("my-pod",),
        blade_target="cpu",
        blade_action="fullload",
        duration_seconds=60,
    )
    defaults.update(kw)
    return FaultSpec(**defaults)


class TestBlastRadius:
    def test_pod_single_target(self):
        s = _score_blast_radius(_spec(scope="pod", names=("p1",)))
        assert s.value == 30  # pod base 30, 1 target +0

    def test_deployment_single(self):
        s = _score_blast_radius(_spec(scope="deployment", names=("app",)))
        assert s.value == 50

    def test_node_scope(self):
        s = _score_blast_radius(_spec(scope="node", names=("n1",)))
        assert s.value == 70

    def test_cluster_scope_caps_at_100(self):
        s = _score_blast_radius(_spec(scope="cluster", names=("c1",)))
        assert s.value == 100

    def test_unknown_scope_falls_to_default(self):
        s = _score_blast_radius(_spec(scope="weird", names=("x",)))
        assert s.value == 30

    def test_multiple_targets_increase_score(self):
        small = _score_blast_radius(_spec(scope="pod", names=("a",)))
        medium = _score_blast_radius(_spec(scope="pod", names=tuple(f"p{i}" for i in range(5))))
        big = _score_blast_radius(_spec(scope="pod", names=tuple(f"p{i}" for i in range(25))))
        assert small.value < medium.value < big.value
        assert big.value == min(100, 30 + 40)

    def test_namespace_wide(self):
        s = _score_blast_radius(_spec(scope="pod", names=(), labels={}))
        # is_namespace_wide=True → +30 modifier
        assert s.value == 60
        assert "namespace-wide" in s.explanation

    def test_empty_scope_returns_zero(self):
        """Regression: empty scope must not be inflated by namespace-wide
        fallback (it was previously scoring 60 via default scope=30 +
        is_namespace_wide=+30, falsely indicating medium risk for a
        degenerate spec)."""
        s = _score_blast_radius(_spec(scope="", names=(), labels={}))
        assert s.value == 0
        assert "no scope" in s.explanation.lower()


class TestFrequency:
    def test_no_conflicts_first_attempt(self):
        s = _score_frequency({})
        assert s.value == 0
        assert "no conflicts" in s.explanation

    def test_active_conflicts(self):
        s = _score_frequency({"conflict_uids": ["u1", "u2"]})
        assert s.value == 40  # 2 * 20

    def test_conflict_count_capped_at_60(self):
        s = _score_frequency({"conflict_uids": ["u"] * 10})
        # 10 * 20 = 200, capped at 60
        assert s.value == 60

    def test_first_retry_adds_30(self):
        s = _score_frequency({"pipeline_attempt": 1})
        assert s.value == 30

    def test_third_retry_adds_50_total(self):
        s = _score_frequency({"pipeline_attempt": 3})
        assert s.value == 50  # 30 + 20

    def test_combined_capped_at_100(self):
        s = _score_frequency({"conflict_uids": ["u"] * 5, "pipeline_attempt": 3})
        # 60 + 30 + 20 = 110 → 100
        assert s.value == 100


class TestTime:
    def test_business_hour_weekday_short_duration(self):
        ctx = {"current_time": datetime(2026, 5, 26, 14, 0)}  # Tuesday 14:00
        s = _score_time(_spec(duration_seconds=30), ctx)
        # 50 (biz) + 20 (weekday) + 0 (<60s) = 70
        assert s.value == 70
        assert "business hours" in s.explanation
        assert "weekday" in s.explanation

    def test_off_hours_weekend(self):
        ctx = {"current_time": datetime(2026, 5, 24, 3, 0)}  # Sunday 03:00
        s = _score_time(_spec(duration_seconds=30), ctx)
        # 0 (off) + 0 (weekend) + 0 (short)
        assert s.value == 0

    def test_permanent_injection(self):
        ctx = {"current_time": datetime(2026, 5, 26, 14, 0)}
        s = _score_time(_spec(duration_seconds=0), ctx)
        # 50 + 20 + 30 = 100
        assert s.value == 100
        assert "permanent" in s.explanation

    def test_long_duration_bonus(self):
        ctx = {"current_time": datetime(2026, 5, 26, 14, 0)}
        s = _score_time(_spec(duration_seconds=900), ctx)  # 15 min
        assert s.value == 50 + 20 + 20

    def test_extended_hours(self):
        ctx = {"current_time": datetime(2026, 5, 26, 20, 0)}  # Tuesday 20:00
        s = _score_time(_spec(duration_seconds=30), ctx)
        # 20 (extended) + 20 (weekday) = 40
        assert s.value == 40

    def test_tz_aware_utc_override_normalised_to_beijing(self):
        """Regression: a caller passing tz-aware UTC datetime must be
        normalised to Beijing wall-clock before reading .hour. Without
        normalisation, UTC 08:00 = Beijing 16:00 (business hours), but
        ``.hour`` on the raw UTC value would read 8 (off-hours) and
        score the wrong way."""
        from datetime import timezone

        utc_8am_tuesday = datetime(2026, 5, 26, 8, 0, tzinfo=timezone.utc)
        s = _score_time(_spec(duration_seconds=30), {"current_time": utc_8am_tuesday})
        # Beijing wall-clock 16:00 Tue = business (50) + weekday (20) = 70
        assert s.value == 70
        assert "16" in s.explanation
        assert "business hours" in s.explanation

    def test_uses_beijing_tz_when_no_override(self, monkeypatch):
        """Regression: default clock must come from BEIJING_TZ so that
        a server running in UTC still scores using Beijing wall-clock
        (project-wide convention from chaos_agent.utils.time).
        Earlier version used bare ``datetime.now()`` which would have
        let the server's process TZ leak into the score.
        """
        from chaos_agent.utils.time import BEIJING_TZ
        from datetime import timezone

        # Freeze "now" to a known instant: 2026-05-26T08:00:00Z = 16:00
        # in Beijing time (a weekday business-hours moment). We pin the
        # module-level datetime.now to return this instant when called
        # with the BEIJING_TZ argument, mimicking what runtime does.
        fixed_utc = datetime(2026, 5, 26, 8, 0, tzinfo=timezone.utc)
        from chaos_agent.agent import safety_score as mod

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D401 — stub
                return fixed_utc.astimezone(tz) if tz else fixed_utc

        monkeypatch.setattr(mod, "datetime", _Clock)

        s = _score_time(_spec(duration_seconds=30), {})
        # Beijing wall-clock = 16:00 (business hours) + weekday → 50+20=70
        assert s.value == 70
        assert "business hours" in s.explanation
        assert "16" in s.explanation


class TestTopology:
    def test_no_markers(self):
        s = _score_topology(_spec(namespace="default", names=("svc",), scope="pod"), {})
        assert s.value == 0
        assert "no critical markers" in s.explanation

    def test_production_namespace(self):
        s = _score_topology(_spec(namespace="prod", names=("svc",), scope="pod"), {})
        assert s.value == 40
        assert "production" in s.explanation

    def test_critical_name(self):
        s = _score_topology(_spec(namespace="default", names=("api-gateway",), scope="pod"), {})
        assert s.value == 30
        assert "api-gateway" in s.explanation

    def test_cluster_scoped(self):
        s = _score_topology(_spec(namespace="", names=("n1",), scope="node"), {})
        assert s.value == 30
        assert "cluster-scoped" in s.explanation

    def test_combined(self):
        s = _score_topology(
            _spec(namespace="production", names=("auth-db",), scope="pod"),
            {},
        )
        # 40 (production ns) + 30 (auth+db match first) = 70
        assert s.value == 70

    def test_deep_k8s_signal_applied(self):
        s = _score_topology(
            _spec(namespace="default", names=("svc",), scope="pod"),
            {"topology_deep_signal": (20, "1 replica SPOF")},
        )
        assert s.value == 20
        assert "1 replica SPOF" in s.explanation

    def test_invalid_deep_signal_ignored(self):
        s = _score_topology(
            _spec(namespace="default", names=("svc",), scope="pod"),
            {"topology_deep_signal": "not-a-tuple"},
        )
        assert s.value == 0

    def test_deep_signal_bool_bonus_rejected(self):
        """Regression: ``isinstance(True, int)`` is True in Python — the
        type check must reject bool tuples so a caller accidentally
        passing ``(True, "msg")`` doesn't silently add +1."""
        s = _score_topology(
            _spec(namespace="default", names=("svc",), scope="pod"),
            {"topology_deep_signal": (True, "should be rejected")},
        )
        assert s.value == 0
        assert "should be rejected" not in s.explanation

    # ── Substring false-positive regression (task-f8320b6ff844) ──────

    def test_random_hash_does_not_falsely_match_db(self):
        """Regression: a Pod whose ReplicaSet hash contains ``bdb`` /
        ``db`` (e.g. ``accounting-6fbdb464c7-qn2vr`` from
        task-f8320b6ff844) must NOT be flagged as a database.

        Previously ``_score_topology`` used ``"db" in name.lower()``
        which matched any hash with two consecutive ``d``/``b`` chars
        — for the 5-7 char random hex hash K8s assigns to every
        ReplicaSet pod, the collision probability for ``db`` alone is
        roughly 2-3% → silent false-positive on a non-trivial fraction
        of all inject targets, inflating safety_score by +30 topology
        (which is 0.30 weight → +9 on overall).
        """
        s = _score_topology(
            _spec(
                namespace="cms-demo",
                names=("accounting-6fbdb464c7-qn2vr",),
                scope="pod",
            ),
            {},
        )
        assert s.value == 0
        assert "critical component" not in s.explanation
        assert "no critical markers" in s.explanation

    def test_token_match_still_fires_on_real_critical_pod(self):
        """Mutation safeguard: a tightening to exact-token matching
        must not over-tighten — a real database pod whose name segment
        IS ``db`` still has to fire."""
        s = _score_topology(
            _spec(namespace="default", names=("postgres-db-0",), scope="pod"),
            {},
        )
        assert s.value == 30
        assert "critical component" in s.explanation

    def test_random_hash_does_not_falsely_match_api(self):
        """Companion to db: ``api`` is the other most-collidable short
        pattern. ``chapinski-7b89c-xyz`` (made-up; no real ``api``
        token, just incidentally contains the letters) must not match."""
        s = _score_topology(
            _spec(namespace="default", names=("chapinski-7b89c-xyz",), scope="pod"),
            {},
        )
        assert s.value == 0
        assert "no critical markers" in s.explanation

    def test_token_boundaries_dot_and_underscore(self):
        """K8s names commonly use ``.`` (e.g. nodes ``cn-hongkong.10.0.1.60``)
        and ``_`` in some custom resources — both must be treated as
        token boundaries the same as ``-``."""
        s = _score_topology(
            _spec(namespace="default", names=("svc.api.internal",), scope="pod"),
            {},
        )
        assert s.value == 30
        assert "critical component" in s.explanation


class TestAggregation:
    def test_default_weights_sum_to_one(self):
        assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9

    def test_low_risk_overall(self):
        ctx = {"current_time": datetime(2026, 5, 24, 3, 0)}  # off-hour weekend
        score = compute_safety_score(
            _spec(scope="pod", namespace="dev", names=("test",), duration_seconds=30),
            ctx,
        )
        assert score.overall < 30
        assert score.level == "low"

    def test_critical_overall(self):
        ctx = {
            "current_time": datetime(2026, 5, 26, 14, 0),
            "conflict_uids": ["u1", "u2", "u3"],
            "pipeline_attempt": 3,
        }
        score = compute_safety_score(
            _spec(
                scope="node",
                namespace="prod",
                names=("api-server",),
                duration_seconds=0,
            ),
            ctx,
        )
        assert score.overall >= 80
        assert score.level == "critical"

    def test_to_dict_round_trip(self):
        ctx = {"current_time": datetime(2026, 5, 26, 10, 0)}
        score = compute_safety_score(_spec(), ctx)
        d = score.to_dict()
        assert d["overall"] == score.overall
        assert d["level"] == score.level
        assert d["blast_radius"]["value"] == score.blast_radius.value
        assert "explanation" in d["blast_radius"]

    def test_custom_weights(self):
        ctx = {"current_time": datetime(2026, 5, 26, 10, 0)}
        # Weight blast_radius 100% → overall equals blast_radius value
        score = compute_safety_score(
            _spec(),
            ctx,
            weights={"blast_radius": 1.0, "topology": 0, "frequency": 0, "time": 0},
        )
        assert score.overall == score.blast_radius.value

    def test_overall_capped_at_100_with_misweighted_dict(self):
        """Regression: partial weights dict falls back to DEFAULT for
        missing keys, which can push the raw sum past 100. Overall must
        cap there so escalation thresholds stay meaningful."""
        ctx = {
            "current_time": datetime(2026, 5, 26, 14, 0),
            "conflict_uids": ["u1", "u2", "u3"],
            "pipeline_attempt": 3,
        }
        score = compute_safety_score(
            _spec(scope="cluster", namespace="prod", names=("api-db",), duration_seconds=0),
            ctx,
            # Only blast_radius=1.0 specified — the other 3 dims silently
            # fall back to DEFAULT (sum 0.6), pushing the raw weighted
            # sum well above 100 when every dimension scores high.
            weights={"blast_radius": 1.0},
        )
        assert 0 <= score.overall <= 100
        assert score.level in ("low", "medium", "high", "critical")


class TestEscalation:
    def test_rejected_never_changes(self):
        assert maybe_escalate_status("rejected", 99) == "rejected"

    def test_confirm_required_never_downgrades(self):
        assert maybe_escalate_status("confirm_required", 10) == "confirm_required"

    def test_safe_upgrades_to_warning(self):
        assert maybe_escalate_status("safe", 75) == "warning"

    def test_safe_upgrades_to_confirm(self):
        assert maybe_escalate_status("safe", 95) == "confirm_required"

    def test_warning_upgrades_to_confirm(self):
        assert maybe_escalate_status("warning", 95) == "confirm_required"

    def test_warning_stays_below_confirm_thresh(self):
        assert maybe_escalate_status("warning", 80) == "warning"

    def test_safe_stays_below_warning_thresh(self):
        assert maybe_escalate_status("safe", 50) == "safe"

    def test_custom_thresholds(self):
        assert maybe_escalate_status("safe", 60, warning_thresh=50, confirm_thresh=80) == "warning"
        assert maybe_escalate_status("safe", 85, warning_thresh=50, confirm_thresh=80) == "confirm_required"
