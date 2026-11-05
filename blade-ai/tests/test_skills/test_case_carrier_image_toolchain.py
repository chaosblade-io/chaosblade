"""Sentinel: every recovery-carrier image named in the docs must be a sanctioned
curl-capable image — a MEMBER of the runtime allowlist, not merely "not busybox".

Root cause this sentinel pins (Case #87 W-87-2, inject-c28f25f5): the carrier
standard ``references/carrier/recovery-carrier.md`` §4 states the hard toolchain
requirement ``sh+sleep+curl`` (every arming payload issues authenticated
``curl -X DELETE/PATCH`` against the apiserver), yet its own worked example
command AND the static default allowlist note both reached for ``busybox:1.36``.
busybox ships the ``wget`` applet but **not** ``curl`` — so a carrier built from
the verbatim example arms fine, then at timer-fire time the subshell hits
``curl: not found``, the error is swallowed into ``/tmp/restore.log`` (``2>&1``,
``-sf``), the DELETE is never issued, and the fault silently persists past its
window. For a ``failurePolicy=Fail`` webhook (#87) that means admission stays
blocked indefinitely. The failure is delayed + unobserved because the autonomous
carrier has no watcher at fire time.

Root-fix upgrade (彻底根治): the first version of this sentinel asserted a
NEGATIVE property — "the image must not contain ``busybox``" — which an equally
curl-less image (``alpine:3.19``) slips straight past, and whose test name
(``..._is_curl_capable``) overclaimed what it actually checked. This version
asserts the POSITIVE member-level invariant against the SINGLE authoritative
source: every ``drill-rc-`` carrier image named in the standard or any catalogue
case must be a member of ``settings.recovery_carrier_allowed_images`` — the very
allowlist the ToolGuard / screener shape predicate
(``_recovery_carrier_shape_failure`` condition #4) enforces at dispatch — or an
explicit ``<...>`` placeholder that defers to a §9-discovered node-cached image.
Doc and runtime can then no longer drift apart:

- name an image in a doc without sanctioning it in the allowlist → red HERE;
- sanction a curl-less image in the allowlist → the dispatch gate refuses it
  (see ``test_guard.py::test_run_verb_refused_curl_less_carrier_image``).

Victim/probe Pods (``drill-*-victim`` / ``drill-probe``) legitimately use
busybox — they only ``sleep`` and never issue curl — so the invariant is scoped
to the ``drill-rc-`` recovery-carrier signature only.
"""

from __future__ import annotations

import pathlib
import re

from chaos_agent.config.settings import settings

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_CATALOGUE_DIR = _SKILLS_DIR / "k8s-chaos-skills" / "references" / "catalogue"
_CARRIER_STANDARD = (
    _SKILLS_DIR / "k8s-chaos-skills" / "references" / "carrier" / "recovery-carrier.md"
)

# A recovery-carrier creation line: ``kubectl run drill-rc-<hash> ... --image=X``.
# Anchored on the ``drill-rc-`` carrier signature so victim/probe busybox Pods
# (which never carry curl) are never matched.
_CARRIER_IMAGE_RE = re.compile(r"drill-rc-<hash>.*?--image=(\S+)")


def _allowed_carrier_images() -> frozenset[str]:
    """The authoritative configured carrier allowlist (single source).

    The SAME setting the dispatch shape predicate consults — reading it here
    (rather than a hardcoded ``{curlimages/...}`` copy) is precisely what makes
    the doc and the runtime unable to drift apart.
    """
    return frozenset(
        img.strip()
        for img in str(settings.recovery_carrier_allowed_images or "").split(",")
        if img.strip()
    )


def _carrier_images(text: str) -> list[str]:
    """Every image specified on a ``drill-rc-<hash>`` carrier-creation line."""
    return [
        m.group(1)
        for line in text.splitlines()
        for m in [_CARRIER_IMAGE_RE.search(line)]
        if m
    ]


def _is_placeholder(image: str) -> bool:
    """An explicit ``<...>`` token defers to the allowlist.

    The standard intentionally shows ``--image=<允许集内镜像>`` for the case
    where the planner substitutes a §9-discovered node-cached image instead of
    hardcoding one; a placeholder is sanctioned by construction.
    """
    return image.startswith("<") and image.endswith(">")


def _unsanctioned(images: list[str]) -> list[str]:
    """Images that are neither allowlist members nor explicit placeholders."""
    allowed = _allowed_carrier_images()
    return [
        img for img in images
        if not _is_placeholder(img) and img not in allowed
    ]


def test_standard_carrier_example_is_sanctioned() -> None:
    """The standard's worked carrier example must name a sanctioned image.

    Positive member-level: the image must be IN the authoritative allowlist (or
    a placeholder), not merely "not busybox" — an off-allowlist curl-less image
    (alpine, distroless) is caught just as busybox is.
    """
    text = _CARRIER_STANDARD.read_text(encoding="utf-8", errors="replace")
    images = _carrier_images(text)
    assert images, (
        "the carrier standard lost its `drill-rc-<hash> --image=` worked "
        "example — the toolchain-consistency tooth can no longer read it; "
        "re-pin the example command"
    )
    bad = _unsanctioned(images)
    assert not bad, (
        f"recovery-carrier.md demonstrates carrier image(s) {bad} that are not "
        "members of the authoritative allowlist "
        f"(settings.recovery_carrier_allowed_images = "
        f"{sorted(_allowed_carrier_images())}). Every drill-rc carrier must run "
        "a curl-capable image (§4 sh+sleep+curl): an unsanctioned image is "
        "either curl-less (busybox/alpine → `curl: not found` swallowed at fire "
        "time, fault silently persists — W-87-2) or is refused outright by the "
        "dispatch shape gate. Use an allowlist member or a §9-discovered "
        "node-cached image."
    )


def test_no_case_carrier_image_is_unsanctioned() -> None:
    """No catalogue case may name a drill-rc carrier image off the allowlist.

    Unconditional — NOT gated on "the doc happens to mention curl": a
    ``drill-rc-`` carrier's §4 recovery payload is ALWAYS curl-based, so every
    named carrier image must be sanctioned regardless of the surrounding prose.
    (The old ``..._uses_busybox_with_curl_payload`` gate let a curl-less carrier
    through whenever the case file omitted the literal word "curl".)
    """
    offenders: list[str] = []
    for path in sorted(_CATALOGUE_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        bad = _unsanctioned(_carrier_images(text))
        if bad:
            offenders.append(f"{path.name}: drill-rc carrier image {bad}")
    assert not offenders, (
        "These cases name a drill-rc recovery-carrier image that is not a member "
        "of the authoritative allowlist "
        f"({sorted(_allowed_carrier_images())}). A carrier built from an "
        "unsanctioned image is refused at dispatch, or — if curl-less — arms "
        "then dies `curl: not found` at fire time, the DELETE never issues, and "
        f"the fault persists past its window (W-87-2): {offenders}"
    )
