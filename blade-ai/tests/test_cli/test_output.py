"""Tests for CLI output formatting."""

import json

import pytest

from chaos_agent.cli import output as output_mod
from chaos_agent.cli.output import OutputFormat, format_output


class TestFormatOutput:
    def test_json_format(self):
        data = {"code": 0, "message": "success", "data": {"key": "value"}}
        result = format_output(data, "json")
        parsed = json.loads(result)
        assert parsed == data

    def test_json_with_unicode(self):
        data = {"message": "中文测试"}
        result = format_output(data, "json")
        assert "中文测试" in result

    def test_json_indent(self):
        data = {"key": "value"}
        result = format_output(data, "json")
        # Should be indented
        assert "\n" in result

    def test_yaml_format(self):
        """yaml format should produce real YAML, not a JSON fallback."""
        data = {"code": 0, "message": "success"}
        result = format_output(data, "yaml")
        # YAML renders "key: value"; the result is not valid JSON.
        assert "code: 0" in result
        assert "message: success" in result
        with pytest.raises(json.JSONDecodeError):
            json.loads(result)

    def test_yaml_via_enum_member(self):
        """Passing the OutputFormat enum member works the same as the string."""
        data = {"code": 0}
        assert format_output(data, OutputFormat.YAML) == format_output(data, "yaml")

    def test_empty_dict(self):
        result = format_output({}, "json")
        parsed = json.loads(result)
        assert parsed == {}

    def test_nested_dict(self):
        data = {"data": {"nested": {"key": "value"}}, "list": [1, 2, 3]}
        result = format_output(data, "json")
        parsed = json.loads(result)
        assert parsed["data"]["nested"]["key"] == "value"
        assert parsed["list"] == [1, 2, 3]

    def test_default_format_is_json(self):
        data = {"key": "value"}
        result = format_output(data)
        # Default should be json
        parsed = json.loads(result)
        assert parsed == data

    def test_unknown_format_raises(self):
        """An unrecognized format is rejected instead of silently returning JSON."""
        with pytest.raises(ValueError, match="Unsupported output format"):
            format_output({"key": "value"}, "xml")

    def test_yaml_without_pyyaml_raises(self, monkeypatch):
        """Requesting yaml without PyYAML installed raises a clear error."""
        monkeypatch.setattr(output_mod, "HAS_YAML", False)
        with pytest.raises(ValueError, match="PyYAML"):
            format_output({"key": "value"}, "yaml")


class TestOutputFormat:
    def test_members(self):
        assert OutputFormat.JSON.value == "json"
        assert OutputFormat.YAML.value == "yaml"

    def test_str_equality(self):
        # str subclass: compares equal to its string value
        assert OutputFormat.JSON == "json"
