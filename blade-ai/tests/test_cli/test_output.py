"""Tests for CLI output formatting."""

import json

import pytest

from chaos_agent.cli.output import format_output, OutputFormat


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
        """yaml format should produce valid YAML output when PyYAML is available."""
        data = {"code": 0, "message": "success"}
        result = format_output(data, "yaml")
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


class TestOutputFormatValidation:
    """Tests for invalid format handling and enum support."""

    def test_invalid_format_raises_valueerror(self):
        """Unknown format strings raise ValueError instead of silently returning JSON."""
        with pytest.raises(ValueError, match="Unsupported output format"):
            format_output({"key": "value"}, "xml")

    def test_typo_format_raises_valueerror(self):
        """Common typos like 'ymal' raise ValueError."""
        with pytest.raises(ValueError, match="Unsupported output format"):
            format_output({"key": "value"}, "ymal")

    def test_yaml_without_pyyaml_raises_valueerror(self):
        """When PyYAML is unavailable, requesting yaml raises a clear error."""
        import chaos_agent.cli.output as out_mod

        original = out_mod.HAS_YAML
        out_mod.HAS_YAML = False
        try:
            with pytest.raises(ValueError, match="PyYAML"):
                format_output({"key": "value"}, "yaml")
        finally:
            out_mod.HAS_YAML = original

    def test_output_format_enum_json(self):
        """OutputFormat.json enum value works with format_output."""
        data = {"key": "value"}
        result = format_output(data, OutputFormat.json)
        parsed = json.loads(result)
        assert parsed == data

    def test_output_format_enum_yaml(self):
        """OutputFormat.yaml enum value works with format_output."""
        data = {"code": 0, "message": "success"}
        result = format_output(data, OutputFormat.yaml)
        assert "code" in result
        assert "success" in result

    def test_output_format_enum_values(self):
        """OutputFormat enum has correct string values."""
        assert OutputFormat.json.value == "json"
        assert OutputFormat.yaml.value == "yaml"
