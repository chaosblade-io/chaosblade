"""Multi-strategy ChaosBlade UID extraction.

Real-world `blade create` output appears in many shapes — clean JSON, JSON
buried in a kubectl-exec stderr preamble, pretty-printed multi-line JSON,
mixed code 200 / 54000 responses, and occasionally raw `chaosblade-*`
resource names. A single regex or `json.loads` is brittle against this.

This module exposes a single entry point, `extract_blade_uid(text)`, that
walks three increasingly-permissive strategies in order. The first to
return a UID wins; the semantically-aware strategy runs first so that a
code-54000 response with `success=false` is correctly rejected (the CRD
exists but the experiment failed — extracting its UID would mislead the
verifier).

Strategy order:
  1. JSON-aware (`json.JSONDecoder.raw_decode`):
     - Walks every `{` in `text`, parses JSON segments, applies the
       blade_create response semantics:
         * code=200 + success=true        → return result
         * code=54000 + success!=False    → return result.uid
         * code=54000 + success=False     → reject AND block fallbacks
           (the injection failed — extracting the UID would be misleading).
  2. Loose regex on `"result"` / `"uid"` UUID-shaped fields — catches
     malformed JSON that the parser bailed on (e.g., truncated stdout,
     unescaped quotes from kubectl-exec wrapping).
  3. ChaosBlade resource pattern `chaosblade-[a-f0-9]+` — last resort
     for cases where blade emitted a resource name instead of a UID
     (e.g. `kubectl get chaosblades` echo).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Standard UUID shape (8-4-4-4-12 hex) embedded in a JSON-style key.
_UUID_RE = re.compile(
    r'"(?:result|uid)"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"'
)

# ChaosBlade resource-name fallback (used when blade emits a resource ref
# rather than a UID — e.g. `chaosblade-1234abcd...`).
_CHAOSBLADE_RESOURCE_RE = re.compile(r'\b(chaosblade-[a-f0-9]{8,})\b')

# Sentinel returned by strategy 1 to mean "saw a 54000+success=false
# response — do NOT fall back to looser strategies." Any non-None, non-str
# object works; an object literal makes identity checks unambiguous.
_FAILED_54000_SENTINEL = object()


def extract_blade_uid(text: str) -> Optional[str]:
    """Extract a ChaosBlade UID from arbitrary tool output.

    Returns the UID string on success, or None if no usable UID was found
    (including the case where a 54000 response indicates the injection
    actually failed — callers must treat None as "no live experiment").
    """
    if not isinstance(text, str) or not text:
        return None

    uid = _strategy_json_aware(text)
    if uid is _FAILED_54000_SENTINEL:
        # Known-failed injection; do NOT fall back to looser strategies.
        return None
    if uid is not None:
        return uid

    uid = _strategy_regex(text)
    if uid is not None:
        return uid

    return _strategy_chaosblade_resource(text)


def _strategy_json_aware(text: str):
    """Walk every `{` in `text`, parse JSON segments, apply blade semantics.

    Returns one of:
      - str: a UID extracted from a recognized success or 54000 response.
      - None: no JSON object yielded a usable UID.
      - _FAILED_54000_SENTINEL: encountered a 54000+success=false response;
        caller MUST refuse to extract a UID from this output.
    """
    decoder = json.JSONDecoder()
    scan_from = 0
    saw_failed_54000 = False

    while True:
        idx = text.find("{", scan_from)
        if idx < 0:
            break
        try:
            data, end_idx = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            scan_from = idx + 1
            continue

        if isinstance(data, dict):
            if data.get("success") is True and data.get("code") == 200:
                result = data.get("result")
                if isinstance(result, str) and result:
                    return result

            if data.get("code") == 54000:
                result = data.get("result")
                if isinstance(result, dict):
                    uid = result.get("uid")
                    if isinstance(uid, str) and uid:
                        if data.get("success") is False:
                            logger.info(
                                "blade_uid extraction: 54000 + success=false, "
                                "treating as failed injection (uid=%s ignored)",
                                uid,
                            )
                            saw_failed_54000 = True
                        else:
                            logger.debug(
                                "blade_uid extraction: 54000 + success=%s, "
                                "extracted uid=%s", data.get("success"), uid,
                            )
                            return uid

        scan_from = end_idx

    if saw_failed_54000:
        return _FAILED_54000_SENTINEL
    return None


def _strategy_regex(text: str) -> Optional[str]:
    """Find the first UUID-shaped value of `result` or `uid` in `text`."""
    match = _UUID_RE.search(text)
    if match:
        return match.group(1)
    return None


def _strategy_chaosblade_resource(text: str) -> Optional[str]:
    """Find a `chaosblade-<hex>` resource name as a last-resort identifier."""
    match = _CHAOSBLADE_RESOURCE_RE.search(text)
    if match:
        return match.group(1)
    return None
