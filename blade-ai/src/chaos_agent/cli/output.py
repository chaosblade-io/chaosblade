"""Output formatting: JSON/YAML output for CLI commands."""

import json

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def format_output(data: dict, output_format: str = "json") -> str:
    """Format response data as JSON or YAML.

    Args:
        data: Response dict (the full envelope)
        output_format: "json" or "yaml"

    Returns:
        Formatted string
    """
    if output_format == "yaml" and HAS_YAML:
        return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    else:
        return json.dumps(data, indent=2, ensure_ascii=False)
