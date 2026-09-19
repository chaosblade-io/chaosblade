"""Tests for chaos_agent.mcp.effect — advisory read/write label resolver.

Pins the precedence (operator config > server annotation > unspecified)
and the ``destructiveHint``-wins-over-``readOnlyHint`` caution rule.
These labels are advisory only: nothing here gates execution.
"""

from chaos_agent.mcp.effect import (
    EFFECT_DESTRUCTIVE,
    EFFECT_READONLY,
    EFFECT_UNSPECIFIED,
    SOURCE_ANNOTATION,
    SOURCE_CONFIG,
    SOURCE_UNSPECIFIED,
    effect_from_annotations,
    resolve_tool_effect,
)


class _Ann:
    """Stand-in for an mcp ToolAnnotations object (attribute access)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestEffectFromAnnotations:
    def test_none_is_unspecified(self):
        assert effect_from_annotations(None) == EFFECT_UNSPECIFIED

    def test_empty_dict_is_unspecified(self):
        assert effect_from_annotations({}) == EFFECT_UNSPECIFIED

    def test_readonly_hint_dict(self):
        assert effect_from_annotations({"readOnlyHint": True}) == EFFECT_READONLY

    def test_destructive_hint_dict(self):
        assert effect_from_annotations({"destructiveHint": True}) == EFFECT_DESTRUCTIVE

    def test_destructive_wins_over_readonly(self):
        ann = {"readOnlyHint": True, "destructiveHint": True}
        assert effect_from_annotations(ann) == EFFECT_DESTRUCTIVE

    def test_false_hints_are_unspecified(self):
        # Explicitly-false hints declare nothing → unspecified.
        assert effect_from_annotations({"readOnlyHint": False}) == EFFECT_UNSPECIFIED
        assert effect_from_annotations({"destructiveHint": False}) == EFFECT_UNSPECIFIED

    def test_idempotent_only_is_unspecified(self):
        # idempotentHint/openWorldHint alone carry no read/write signal.
        assert effect_from_annotations({"idempotentHint": True}) == EFFECT_UNSPECIFIED

    def test_object_annotations_supported(self):
        assert effect_from_annotations(_Ann(readOnlyHint=True)) == EFFECT_READONLY
        assert effect_from_annotations(_Ann(destructiveHint=True)) == EFFECT_DESTRUCTIVE


class TestResolveToolEffect:
    def test_config_readonly_wins_over_destructive_annotation(self):
        eff, src = resolve_tool_effect(EFFECT_READONLY, {"destructiveHint": True})
        assert (eff, src) == (EFFECT_READONLY, SOURCE_CONFIG)

    def test_config_destructive_wins_over_readonly_annotation(self):
        eff, src = resolve_tool_effect(EFFECT_DESTRUCTIVE, {"readOnlyHint": True})
        assert (eff, src) == (EFFECT_DESTRUCTIVE, SOURCE_CONFIG)

    def test_no_config_falls_back_to_annotation(self):
        eff, src = resolve_tool_effect(None, {"destructiveHint": True})
        assert (eff, src) == (EFFECT_DESTRUCTIVE, SOURCE_ANNOTATION)

    def test_no_config_no_annotation_is_unspecified(self):
        eff, src = resolve_tool_effect(None, None)
        assert (eff, src) == (EFFECT_UNSPECIFIED, SOURCE_UNSPECIFIED)

    def test_invalid_config_value_ignored_falls_to_annotation(self):
        # config.py rejects bad values at load, but the resolver must not
        # trust an out-of-band invalid value — it falls through.
        eff, src = resolve_tool_effect("bogus", {"readOnlyHint": True})
        assert (eff, src) == (EFFECT_READONLY, SOURCE_ANNOTATION)

    def test_invalid_config_value_and_no_annotation_is_unspecified(self):
        eff, src = resolve_tool_effect("bogus", None)
        assert (eff, src) == (EFFECT_UNSPECIFIED, SOURCE_UNSPECIFIED)
