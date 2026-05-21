"""Tests for chaos_agent.utils.coerce defensive type coercion."""

from __future__ import annotations

import pytest

from chaos_agent.utils.coerce import (
    coerce_to_dict,
    coerce_to_list,
    coerce_to_str,
    coerce_to_int,
)


# ---------------------------------------------------------------------------
# coerce_to_dict
# ---------------------------------------------------------------------------


class TestCoerceToDict:
    def test_dict_passthrough(self):
        assert coerce_to_dict({"a": "1"}) == {"a": "1"}
        assert coerce_to_dict({}) == {}

    def test_none_and_empty(self):
        assert coerce_to_dict(None) == {}
        assert coerce_to_dict("") == {}
        assert coerce_to_dict([]) == {}

    def test_json_object_string(self):
        assert coerce_to_dict('{"app":"nginx"}') == {"app": "nginx"}
        assert coerce_to_dict('{"a":1,"b":"x"}') == {"a": 1, "b": "x"}

    def test_label_selector_string(self):
        assert coerce_to_dict("app=nginx") == {"app": "nginx"}
        assert coerce_to_dict("app=nginx,tier=front") == {
            "app": "nginx",
            "tier": "front",
        }
        # whitespace tolerated
        assert coerce_to_dict("app = nginx, tier = front") == {
            "app": "nginx",
            "tier": "front",
        }

    def test_label_selector_skips_malformed_pieces(self):
        # ``app=nginx`` is valid, ``broken`` has no ``=`` and is skipped
        assert coerce_to_dict("app=nginx,broken") == {"app": "nginx"}

    def test_list_of_dicts_takes_first(self):
        assert coerce_to_dict([{"a": "1"}, {"b": "2"}]) == {"a": "1"}

    def test_list_of_kv_strings(self):
        assert coerce_to_dict(["app=nginx", "tier=front"]) == {
            "app": "nginx",
            "tier": "front",
        }

    def test_unparseable_returns_empty(self, caplog):
        # bare int / unstructured str / etc.
        assert coerce_to_dict(123) == {}
        assert coerce_to_dict("just some random text") == {}
        # Warning should have been emitted
        assert any(
            "coerce_to_dict" in r.getMessage() for r in caplog.records
        )

    def test_never_raises(self):
        # Must NOT raise even on truly weird inputs
        class Weird:
            pass

        assert coerce_to_dict(Weird()) == {}
        assert coerce_to_dict(object()) == {}


# ---------------------------------------------------------------------------
# coerce_to_list
# ---------------------------------------------------------------------------


class TestCoerceToList:
    def test_list_passthrough(self):
        assert coerce_to_list(["a", "b"]) == ["a", "b"]
        assert coerce_to_list([]) == []

    def test_tuple_to_list(self):
        assert coerce_to_list(("a", "b")) == ["a", "b"]

    def test_none_empty(self):
        assert coerce_to_list(None) == []
        assert coerce_to_list("") == []

    def test_json_array_string(self):
        assert coerce_to_list('["a","b"]') == ["a", "b"]

    def test_comma_separated(self):
        assert coerce_to_list("a,b,c") == ["a", "b", "c"]
        assert coerce_to_list(" a , b , c ") == ["a", "b", "c"]

    def test_single_value_string(self):
        assert coerce_to_list("only") == ["only"]

    def test_unknown_returns_empty(self, caplog):
        assert coerce_to_list(42) == []
        assert any(
            "coerce_to_list" in r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# coerce_to_str
# ---------------------------------------------------------------------------


class TestCoerceToStr:
    def test_str_passthrough(self):
        assert coerce_to_str("hello") == "hello"
        assert coerce_to_str("") == ""

    def test_none_returns_default(self):
        assert coerce_to_str(None) == ""
        assert coerce_to_str(None, default="x") == "x"

    def test_int_float(self):
        assert coerce_to_str(42) == "42"
        assert coerce_to_str(3.14) == "3.14"

    def test_bool(self):
        assert coerce_to_str(True) == "true"
        assert coerce_to_str(False) == "false"

    def test_unknown_returns_default(self, caplog):
        assert coerce_to_str([1, 2], default="x") == "x"
        assert any(
            "coerce_to_str" in r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# coerce_to_int
# ---------------------------------------------------------------------------


class TestCoerceToInt:
    def test_int_passthrough(self):
        assert coerce_to_int(42) == 42

    def test_float_truncated(self):
        assert coerce_to_int(3.7) == 3
        assert coerce_to_int(-2.5) == -2

    def test_str_int(self):
        assert coerce_to_int("100") == 100
        assert coerce_to_int(" 42 ") == 42
        assert coerce_to_int("-3") == -3

    def test_str_float(self):
        assert coerce_to_int("3.7") == 3

    def test_none_returns_default(self):
        assert coerce_to_int(None) == 0
        assert coerce_to_int(None, default=99) == 99

    def test_bool_rejected(self):
        # bool is technically int subclass; we deliberately reject it
        assert coerce_to_int(True, default=99) == 99
        assert coerce_to_int(False, default=99) == 99

    def test_unparseable(self, caplog):
        assert coerce_to_int("abc", default=5) == 5
        assert any(
            "coerce_to_int" in r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Regression: the exact bug from baseline_capture
# ---------------------------------------------------------------------------


def test_regression_baseline_labels_str_form():
    """Reproduces the original ``'str' object has no attribute 'items'``
    bug: the LLM emitted labels as a k8s-selector string instead of a
    dict, and ``label_selector = "-l " + ",".join(f"{k}={v}" for k, v
    in labels.items())`` raised because str has no ``.items()``."""
    target = {
        "namespace": "default",
        "names": ["pod-a"],
        "labels": "app=nginx,tier=front",  # ← LLM-emitted str
    }
    labels = coerce_to_dict(
        target.get("labels"), context="regression_test:labels"
    )
    # Must be a dict that supports .items()
    assert labels == {"app": "nginx", "tier": "front"}
    rendered = "-l " + ",".join(f"{k}={v}" for k, v in labels.items())
    assert "app=nginx" in rendered
    assert "tier=front" in rendered


def test_regression_target_full_payload_str():
    """Even the entire ``target`` field could in theory arrive as a
    JSON-encoded string. ``coerce_to_dict`` should handle it."""
    raw = '{"namespace":"default","names":["pod-a"]}'
    target = coerce_to_dict(raw, context="regression_test:target")
    assert target == {"namespace": "default", "names": ["pod-a"]}
