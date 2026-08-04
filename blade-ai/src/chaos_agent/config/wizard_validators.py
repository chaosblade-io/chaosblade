"""Wizard validation primitives — public, UI-agnostic.

Extracted from ``tui/renderers/onboarding.py`` so the same validation
logic backs three different UI front-ends without divergence:

  · ``tui/renderers/onboarding.py``      — legacy Python Rich wizard
  · ``cli/commands/config_wizard.py``    — standalone CLI subcommand
  · ``server/routes/wizard.py``          — HTTP endpoints the TS Ink
                                            wizard calls during onboarding

Every public callable here is pure-ish (depends only on stdlib +
``openai`` SDK + ``kubectl`` subprocess) and never touches Rich /
prompt_toolkit / Ink. ValidationResult is a plain dataclass with a
trivial ``to_dict()`` so the HTTP layer can return it verbatim.

Model presets live here too: the wizard's "recommended models" radio
list is part of validation surface (we test "is this model id supported
by the endpoint") so the list itself belongs alongside the validators.
TS wizard pulls the list via ``GET /api/v1/wizard/model-presets``
rather than hard-coding a copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── ValidationResult ───────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Outcome of a single validation call.

    ``status`` is one of ``"ok" | "warn" | "error"``. ``block`` is
    advisory for UI consumers — true means the user shouldn't be allowed
    to advance past this field. ``metadata`` carries optional structured
    extras (e.g. discovered kube contexts, number of models returned)
    that the UI can surface beyond the human-readable message.
    """

    status: str = "ok"
    message: str = ""
    block: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "block": self.block,
            "metadata": self.metadata,
        }


# ── Model presets ──────────────────────────────────────────────────────


# Curated short-list of LLMs the wizard offers as radio options.
#
# Order matters — first item is the wizard's default selection.
# Adding a new preset is intentionally just an edit here; no TS
# release needed because the TS wizard fetches this via
# ``GET /api/v1/wizard/model-presets``.
#
# ``id`` is the model identifier the LLM SDK will see.
# ``label`` is the radio row's primary text (CJK ok).
# ``vendor`` is a short attribution tag.
# ``hint`` is the dim right-column note (one short phrase).
MODEL_PRESETS: list[dict] = [
    {
        "id": "qwen3-max-preview",
        "label": "qwen3-max-preview",
        "vendor": "Alibaba",
        "hint": "Best for Chinese · strongest reasoning",
    },
    {
        "id": "deepseek-v4-pro",
        "label": "deepseek-v4-pro",
        "vendor": "DeepSeek",
        "hint": "Great value · strong reasoning",
    },
    {
        "id": "glm-5.1",
        "label": "glm-5.1",
        "vendor": "Zhipu AI",
        "hint": "Balanced for Chinese",
    },
    {
        "id": "qwen3.6-plus",
        "label": "qwen3.6-plus",
        "vendor": "Alibaba",
        "hint": "Fast · good value",
    },
    {
        "id": "claude-opus-4-7",
        "label": "claude-opus-4-7",
        "vendor": "Anthropic",
        "hint": "Global flagship",
    },
]


def get_model_presets() -> list[dict]:
    """Return a shallow copy so callers can't mutate the source list."""
    return [dict(p) for p in MODEL_PRESETS]


# ── Config file health check ──────────────────────────────────────────


def check_config_file_health() -> str | None:
    """Detect a corrupt config file BEFORE the wizard fires.

    Returns a human-readable error message describing the problem if
    ``~/.blade-ai/config.json`` exists but cannot be parsed; ``None``
    when the file is absent (first-time user, wizard should fire) or
    valid.

    Surfacing this distinction matters because every JSON read in the
    codebase currently swallows ``json.JSONDecodeError`` and falls back
    to an empty dict — so a malformed config silently degrades into
    "looks like the user never configured anything", trapping the user
    in a wizard loop or producing downstream 401/403 errors with no
    pointer to the real cause.
    """
    path = Path(os.path.expanduser("~/.blade-ai/config.json"))
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"Cannot read ~/.blade-ai/config.json: {e}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return (
            f"~/.blade-ai/config.json is not valid JSON: "
            f"{e.msg} (line {e.lineno}, column {e.colno}). "
            f"Common cause: a string field is missing its double quotes "
            f"(e.g. kubewiz_token, kubewiz_profile)."
        )
    if not isinstance(data, dict):
        return "The top level of ~/.blade-ai/config.json must be a JSON object {...}"
    return None


# ── Essential-config completeness check ───────────────────────────────


# Fields the wizard treats as "required to launch blade-ai". Same set as
# the legacy ``cli/commands/config_check.py::REQUIRED_FIELDS`` and the
# (now-deprecated) TS-side ``utils/configGate.ts::isConfigSufficient``.
# Kept here so every callsite (HTTP /needs-setup, CLI config-check,
# any future check) reads from one constant.
ESSENTIAL_CONFIG_FIELDS: tuple[tuple[str, str], ...] = (
    # (config-file key, env-var name)
    ("llm_api_key", "BLADE_AI_LLM_API_KEY"),
    ("model_name", "BLADE_AI_MODEL_NAME"),
    ("api_base_url", "BLADE_AI_API_BASE_URL"),
)


def _read_config_file() -> dict:
    """Read ``~/.blade-ai/config.json`` as a dict; ``{}`` on any failure.

    Deliberately bypasses ``chaos_agent.config.settings`` — the wizard's
    "user filled this field?" question is about the *literal source*,
    not the resolved value. ``settings`` has built-in defaults for
    ``model_name`` and ``api_base_url``, so asking it whether they're
    set always returns true; that hides the fact that the user never
    chose a model and short-circuits the wizard.
    """
    path = Path(os.path.expanduser("~/.blade-ai/config.json"))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def missing_essential_config() -> list[str]:
    """Return field names that have NO source providing a value.

    A field is "present" iff its env var is non-empty *or* its
    config-file entry is a non-empty string. Mirrors the resolution
    order ``pydantic-settings`` would apply (env > file > default), but
    excludes the code-default fallback — the whole point of the wizard
    gate is to detect "user hasn't filled this yet" before the default
    masks the gap.

    Returns ``[]`` when all essential fields are present (wizard not
    needed) or the list of missing field names (wizard should fire).
    """
    file_cfg = _read_config_file()
    missing: list[str] = []
    for field_name, env_name in ESSENTIAL_CONFIG_FIELDS:
        env_val = (os.environ.get(env_name) or "").strip()
        if env_val:
            continue
        file_val = file_cfg.get(field_name)
        if isinstance(file_val, str) and file_val.strip():
            continue
        missing.append(field_name)
    return missing


def needs_essential_setup() -> bool:
    """Convenience wrapper — ``True`` if the wizard should fire."""
    return len(missing_essential_config()) > 0


# ── URL validation ─────────────────────────────────────────────────────


async def validate_api_url(url: Optional[str]) -> ValidationResult:
    """Shape check only — does NOT make a network call.

    Network reachability is intentionally deferred to the API-key step
    where we have to talk to the endpoint anyway (a separate URL ping
    would double the wizard's wait time without adding signal). Shape
    rules:
      · non-empty
      · starts with ``http://`` or ``https://``
    """
    url = (url or "").strip()
    if not url:
        return ValidationResult(
            status="error", message="URL cannot be empty", block=True,
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        return ValidationResult(
            status="error",
            message="URL must start with http:// or https://",
            block=True,
        )
    return ValidationResult(status="ok", message="Format is valid")


# ── API key validation (live call) ─────────────────────────────────────


async def validate_api_key(
    api_key: Optional[str],
    base_url: Optional[str],
    model: Optional[str] = None,
) -> ValidationResult:
    """Live ``models.list()`` against ``base_url``.

    Behaviour mirrors the original ``_validate_api_key`` in
    ``tui/renderers/onboarding.py`` (which now wraps this). Returns
    ``error+block`` for clear 401 / invalid-key signals; ``warn`` (not
    block) for soft failures so the user can still proceed and re-check
    at first ``/run``. The ``model`` parameter — when supplied — flips
    ``metadata.has_target`` based on whether the endpoint's model list
    contains it; the UI can show "supports your chosen model" inline.
    """
    key = (api_key or "").strip()
    if not key:
        return ValidationResult(
            status="error", message="API key cannot be empty", block=True,
        )
    base = (base_url or "").strip()
    if not base:
        # Without a base URL we can't even pick the right SDK endpoint.
        return ValidationResult(
            status="error",
            message="Missing the API base URL (fill in the previous step first)",
            block=True,
        )

    try:
        import openai  # type: ignore
    except ImportError:
        return ValidationResult(
            status="warn",
            message="The openai package is not installed, so live validation is unavailable (accepted)",
            block=False,
        )

    client = openai.AsyncOpenAI(api_key=key, base_url=base, timeout=5.0)
    try:
        listed = await asyncio.wait_for(client.models.list(), timeout=5.0)
    except asyncio.TimeoutError:
        return ValidationResult(
            status="warn",
            message="Validation timed out, possibly a network issue; accepted (will be confirmed on the first /run)",
            block=False,
        )
    except Exception as e:
        msg = str(e).lower()
        if (
            "401" in msg
            or "unauthorized" in msg
            or "invalid" in msg
            or "authentication" in msg
        ):
            return ValidationResult(
                status="error",
                message=(
                    f"401 rejected: endpoint {base} does not accept this key. "
                    "Check that the key and the API base URL from the previous step "
                    "come from the same provider."
                ),
                block=True,
            )
        if "404" in msg:
            return ValidationResult(
                status="warn",
                message=f"The endpoint has no /models ({type(e).__name__}); accepted",
                block=False,
            )
        return ValidationResult(
            status="warn",
            message=f"Validation error: {type(e).__name__} (accepted)",
            block=False,
        )

    # Parse the model list (SDK returns objects with .id attr).
    model_ids: list[str] = []
    try:
        if hasattr(listed, "data"):
            model_ids = [m.id for m in listed.data if hasattr(m, "id")]
        else:
            # Pagination iterator path — best-effort, take first 200.
            collected = []
            async for m in listed:  # type: ignore
                if hasattr(m, "id"):
                    collected.append(m.id)
                if len(collected) >= 200:
                    break
            model_ids = collected
    except Exception:
        model_ids = []

    has_target: Optional[bool] = None
    if model and model_ids:
        has_target = model in model_ids

    metadata = {"model_count": len(model_ids)}
    if has_target is not None:
        metadata["has_target"] = has_target
        metadata["target_model"] = model

    message_parts = [f"Validation passed · {len(model_ids)} model(s) returned"]
    if has_target is True:
        message_parts.append(f"includes the target {model} ✓")
    elif has_target is False:
        message_parts.append(f"⚠ does not include {model}")
    return ValidationResult(
        status="ok" if has_target is not False else "warn",
        message=" · ".join(message_parts),
        block=False,
        metadata=metadata,
    )


# ── Kubeconfig validation ──────────────────────────────────────────────


async def validate_kubeconfig(path: Optional[str]) -> ValidationResult:
    """File-existence check + best-effort context discovery.

    Discovery failure is degraded to "no contexts found" rather than an
    error so the wizard can still skip the kube-context step gracefully.
    The discovered list is returned in ``metadata.contexts`` for the UI
    to populate the next radio step.
    """
    path = (path or "").strip()
    if not path:
        return ValidationResult(
            status="warn",
            message="Not specified; kubectl default behaviour will be used",
            block=False,
            metadata={"contexts": []},
        )
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        return ValidationResult(
            status="warn",
            message=f"File does not exist: {expanded} (accepted; adjustable after startup)",
            block=False,
            metadata={"contexts": [], "expanded_path": expanded},
        )
    contexts = await discover_kube_contexts(expanded)
    return ValidationResult(
        status="ok",
        message=f"Found {len(contexts)} context(s)",
        block=False,
        metadata={"contexts": contexts, "expanded_path": expanded},
    )


async def discover_kube_contexts(kubeconfig_path: str) -> list[str]:
    """Run ``kubectl --kubeconfig <path> config get-contexts -o name``.

    Pure best-effort: missing kubectl, missing file, timeout, or a
    non-zero exit all collapse to an empty list. The caller (validate or
    a UI radio-options builder) decides what to do with it.
    """
    from chaos_agent.utils.blade_paths import resolve_exec_path
    kubectl_bin = resolve_exec_path("kubectl")
    if not os.path.dirname(kubectl_bin):
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            kubectl_bin,
            f"--kubeconfig={kubeconfig_path}",
            "config",
            "get-contexts",
            "-o",
            "name",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return []
        if proc.returncode != 0:
            return []
        return [
            ln.strip()
            for ln in stdout.decode("utf-8", "replace").splitlines()
            if ln.strip()
        ]
    except Exception:
        return []
