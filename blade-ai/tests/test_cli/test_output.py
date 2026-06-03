"""Tests for CLI output formatting."""

import json

from chaos_agent.cli.output import format_output


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
        """yaml format should produce valid output (yaml if available, else json)."""
        data = {"code": 0, "message": "success"}
        result = format_output(data, "yaml")
        # Either yaml or json - both should contain the key
        assert "code" in result
        assert "success" in result

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
