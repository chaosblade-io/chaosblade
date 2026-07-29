"""Output formatting: JSON/YAML output for CLI commands."""

import json
from enum import Enum

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


class OutputFormat(str, Enum):
    """Supported CLI output formats."""

    json = "json"
    yaml = "yaml"


def format_output(data: dict, output_format: str = "json") -> str:
    """Format response data as JSON or YAML.

    Args:
        data: Response dict (the full envelope)
        output_format: "json" or "yaml" (also accepts :class:`OutputFormat`)

    Returns:
        Formatted string

    Raises:
        ValueError: If *output_format* is not ``"json"`` or ``"yaml"``,
            or if ``"yaml"`` is requested but PyYAML is not installed.
    """
    if output_format == "yaml":
        if not HAS_YAML:
            raise ValueError(
                "YAML output requires PyYAML. Install with: pip install pyyaml"
            )
        return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    elif output_format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)
    else:
        raise ValueError(
            f"Unsupported output format: '{output_format}'. Supported: json, yaml"
        )
