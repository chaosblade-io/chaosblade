"""Chaos Engineering Agent for Kubernetes fault injection."""

# Source-of-truth literal for the Python side. ``importlib.metadata``
# would be tempting, but it normalizes per PEP 440 — pre-release
# strings get rewritten on read (e.g. ``X.Y.Z-alpha.N`` becomes
# ``X.Y.ZaN``). A literal here keeps Python / npm / /doctor strings
# byte-equal so users don't see a phantom mismatch. pyproject.toml
# stays canonical for wheel filenames; the M11 release.yml guards
# refuse to publish on any drift between pyproject.toml,
# tui/package.json, and this file.
__version__ = "0.1.0-alpha"
