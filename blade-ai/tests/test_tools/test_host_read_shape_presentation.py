"""Layer 1 of ``host-read-readonly-shape-presentation`` — the ``host_read``
presentation face (docstring + rejection-remedy hint) must state the SAME
binary-agnostic SHAPE rule its sibling ``host_inject`` already states, NOT a
closed per-binary allow-list mirrored from ``readonly.py``.

Root cause (O-C4-1, live 2026-10-29 python Case #4 run1 ``inject-eb7c189d``):
``host_read``'s docstring enumerated a CLOSED read-only binary allow-list
(``df / ps / ls / cat / top / iostat / free / ss / netstat / ip show /
systemctl status / …``) that OMITTED ``blade status`` — even though the
authority ``readonly.py::_h_blade`` + ``_BLADE_READONLY_VERBS`` has admitted
``blade status/query/version`` since Task-5193538b. The model read the list,
concluded ``blade is probably not in that whitelist … the classifier would
likely reject`` (run1_readtimeout.log line 329/529/624/722), self-vetoed the
admissible ``host_read blade status``, and burned ~230 lines of indirect port
inference (environ / hook mtime / /proc/net/tcp). This is the exact sibling of
O-C2-1's ``curl is likely rejected`` detour — same shape-vs-binary model
mismatch, on the presentation face O-C2-1 never touched (it fixed host_inject
only). The archived capability ``readonly-capability-presentation`` Req 2
already forbids ANY presentation face from carrying a private word-list copy;
this closes the host_read half.

These tests pin INVARIANTS (779169a1 five-piece), not frozen literals:
  ① consistency — the face states the SHAPE rule (as host_inject does);
  ② single-source hot-tracking — every dual-use read verb the face NAMES is
     admitted by the LIVE authority (``is_readonly_host_command``), and its
     mutating side is refused, so the prose can never silently claim an
     admission the judge does not honour;
  ③ old-value negative + dual-surface non-resurrection — the closed allow-list
     fingerprint is gone from BOTH the docstring and the rejection hint;
  ④ named-example presence floor — the hot-tracked example (blade status) is
     actually named, so ② is never vacuous.
"""

from __future__ import annotations

import inspect
import re

from chaos_agent.tools.host_cmd import host_inject, host_read
from chaos_agent.tools.readonly import is_readonly_host_command


def _tool_source(tool) -> str:
    """Source of the underlying callable of a LangChain StructuredTool.

    ``inspect.getsource`` on the tool object fails (it is a StructuredTool, not
    a function); the @tool-decorated callable lives on ``.coroutine`` (async) or
    ``.func``. That callable's source spans BOTH presentation faces of the tool:
    the docstring (rendered to the LLM as ``.description``) and the runtime
    rejection-remedy hint in the body — the two surfaces a single closed
    allow-list used to be copied into.
    """
    fn = getattr(tool, "coroutine", None) or getattr(tool, "func", None)
    assert fn is not None, f"could not resolve the callable behind {tool!r}"
    return inspect.getsource(fn)


_HOST_READ_SRC = _tool_source(host_read)
_FLAT = re.sub(r"\s+", " ", _HOST_READ_SRC)
# The description the LLM actually reads (the docstring as surfaced to schema) —
# pinned alongside the raw source so the test covers the true presentation face.
_DESC_FLAT = re.sub(r"\s+", " ", host_read.description or "")

# The dual-use read-verb shapes the face NAMES as admitted (hot-tracked against
# the live authority below). ``blade status`` is the O-C4-1 instance: the exact
# experiment-inspection command run1's model self-vetoed.
_NAMED_READONLY_SHAPES = {
    "blade status": "blade status --type prepare --status Running",
    "iptables -L": "iptables -L -n",
    "systemctl status": "systemctl status nginx",
}

# The mutating side of the SAME dual-use binaries — the face's shape rule claims
# these are refused; pin that the authority agrees (so the rule is honest in
# both directions, not just permissive).
_MUTATING_SHAPES = [
    "blade create python redis returnValue --return-value null --cmd GET",
    "blade destroy abc123",
    "blade prepare python --port 9527",
    "blade revoke abc123",
    "iptables -A INPUT -j DROP",
    "systemctl stop nginx",
]

# The closed per-binary allow-list fingerprints that MUST NOT resurrect — one
# per surface (docstring spacing ``df / ps / ls`` vs hint spacing ``df/ps/ls``).
_CLOSED_ALLOWLIST_FINGERPRINTS = [
    "the leading binary must be a read-only diagnostic (df",
    "df/ps/ls/cat/top/iostat/free/ss/netstat",
]


class TestHostReadStatesShapeRule:
    """① consistency: host_read states the SHAPE model, like host_inject."""

    def test_docstring_states_admission_by_shape_not_binary(self):
        assert "BY COMMAND SHAPE" in _FLAT or "by command SHAPE" in _FLAT, (
            "host_read must state admission is BY COMMAND SHAPE (the "
            "binary-agnostic model the authority applies), not a per-binary "
            "allow-list — the O-C4-1 root cause."
        )
        assert "not by binary" in _FLAT.lower(), (
            "host_read must state admission is by shape, NOT by binary identity."
        )
        # The LLM-facing description (not just the raw source) carries the rule.
        assert "SHAPE" in _DESC_FLAT and "not by binary" in _DESC_FLAT.lower(), (
            "the description rendered to the LLM omits the shape rule — the "
            "presentation face the model actually reads still lacks it."
        )

    def test_sibling_faces_agree_on_the_shape_model(self):
        # The whole point: host_inject was fixed by O-C2-1 to state the shape
        # rule; host_read must not contradict it from the same file. Both faces
        # carry the "SHAPE, not … binary" model.
        inject_flat = re.sub(r"\s+", " ", _tool_source(host_inject))
        assert "by command SHAPE, not binary" in inject_flat.lower() or (
            "SHAPE" in inject_flat and "not binary" in inject_flat.lower()
        ), "re-anchor: host_inject no longer states the shape rule O-C2-1 added."
        assert "SHAPE" in _FLAT and "not by binary" in _FLAT.lower(), (
            "host_read diverges from its sibling host_inject — one states the "
            "shape rule, the other a closed binary list (the O-C4-1 mismatch)."
        )

    def test_rejection_hint_also_states_shape_rule(self):
        # Dual-surface: the remedy hint the model reads ON rejection must state
        # the same rule, so a model that DID try and got a different command
        # refused learns the shape model, not a stale binary list.
        assert "admitted by SHAPE, not by binary" in _FLAT, (
            "the host_read rejection-remedy hint must state the shape rule too "
            "(dual-surface consistency, 779169a1 ③)."
        )


class TestHostReadNamedShapesTrackAuthority:
    """② single-source hot-tracking + ④ named-example presence floor."""

    def test_named_read_shapes_are_present_in_the_face(self):
        # ④ presence floor: if a named example is dropped from the face, the
        # hot-track below would pass vacuously — fail loud instead.
        for named in _NAMED_READONLY_SHAPES:
            assert named in _FLAT, (
                f"host_read no longer names {named!r} as an admitted dual-use "
                f"read shape; the hot-tracking assertion would be vacuous. "
                f"Re-anchor this test."
            )
        # blade status is the specific O-C4-1 instance that must be named.
        assert "blade status" in _FLAT, (
            "host_read must name `blade status` — the exact dual-use read verb "
            "the closed allow-list omitted, causing the O-C4-1 detour."
        )

    def test_every_named_read_shape_is_admitted_by_the_authority(self):
        for named, probe in _NAMED_READONLY_SHAPES.items():
            assert is_readonly_host_command(probe), (
                f"host_read names {named!r} as an admitted read-only shape, but "
                f"the live authority rejects {probe!r} — the presentation and "
                f"readonly.py have drifted; re-check the host_read face."
            )

    def test_blade_read_verbs_admitted_mutating_verbs_refused(self):
        # The core O-C4-1 fact: blade status/query/version ARE read-only per the
        # authority (Task-5193538b), while create/destroy/prepare/revoke are not.
        for probe in (
            "blade status --type prepare --status Running",
            "blade status --uid abc123",
            "blade status --type create",
            "blade query",
            "blade version",
        ):
            assert is_readonly_host_command(probe), (
                f"authority no longer admits {probe!r} — if blade status was "
                f"reclassified, the host_read face must be re-checked (this is "
                f"the admission run1's model wrongly assumed would be rejected)."
            )
        for probe in _MUTATING_SHAPES:
            assert not is_readonly_host_command(probe), (
                f"authority now ADMITS the mutating shape {probe!r} — the shape "
                f"rule the face states (mutating forms refused) would be a lie."
            )


class TestClosedAllowListDoesNotResurrect:
    """③ old-value negative on BOTH surfaces."""

    def test_no_closed_per_binary_allow_list(self):
        for fingerprint in _CLOSED_ALLOWLIST_FINGERPRINTS:
            flat_fp = re.sub(r"\s+", " ", fingerprint)
            assert flat_fp not in _FLAT, (
                f"the closed per-binary allow-list {fingerprint!r} resurrected "
                f"in host_read — it omits blade status and re-creates O-C4-1."
            )

    def test_mutation_reintroducing_allow_list_is_caught(self):
        # Self-check that ③ has teeth: a source string carrying the old
        # docstring fingerprint must be detected by the same assertion logic.
        regression = (
            "the leading binary must be a read-only diagnostic "
            "(df / ps / ls / cat / top / iostat / free / ss / netstat)"
        )
        flat_regression = re.sub(r"\s+", " ", regression)
        assert re.sub(r"\s+", " ", _CLOSED_ALLOWLIST_FINGERPRINTS[0]) in flat_regression, (
            "the fingerprint matcher no longer matches the old allow-list shape "
            "— test ③ would pass vacuously. Re-anchor."
        )
