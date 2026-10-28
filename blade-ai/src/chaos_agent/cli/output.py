"""Output formatting: JSON/YAML output for CLI commands."""

import json
from enum import Enum

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class OutputFormat(str, Enum):
    """Supported CLI output formats.

    Subclasses ``str`` so Typer renders/validates it as a plain choice and
    the value compares equal to the corresponding string ("json"/"yaml").
    """

    JSON = "json"
    YAML = "yaml"


def format_output(data: dict, output_format: str = OutputFormat.JSON) -> str:
    """Format response data as JSON or YAML.

    Args:
        data: Response dict (the full envelope)
        output_format: "json" or "yaml" (or an OutputFormat member)

    Returns:
        Formatted string

    Raises:
        ValueError: If output_format is not a supported format, or if "yaml"
            is requested but PyYAML is not installed.
    """
    try:
        fmt = OutputFormat(output_format)
    except ValueError:
        supported = ", ".join(f.value for f in OutputFormat)
        raise ValueError(
            f"Unsupported output format: {output_format!r}. Supported formats: {supported}."
        )

    if fmt is OutputFormat.YAML:
        if not HAS_YAML:
            raise ValueError(
                "YAML output requires PyYAML, which is not installed. "
                "Install it with 'pip install pyyaml'."
            )
        return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return json.dumps(data, indent=2, ensure_ascii=False)
