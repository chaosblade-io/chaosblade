"""Experiment-UID shape legislation — the single-source vocabulary domain.

This module is the cross-package authority for the shape an experiment UID
may take (round-21). Rounds 19/20 legislated the shapes inside
``chaosblade/verify.py`` — but as a PRIVATE package convention: consumers
outside the carrier package cannot import a carrier subpackage at all (the
phase-11 import-boundary guard confines general-layer carrier imports to
the registry seam), so each hand-copied its own dialect and drifted
(``memory/compactor.py``'s two survival-context anchors admitted
hyphen-noise / 8-hex / 40-hex / uppercase shapes; the side_effect
conflict-check fallback truncated 40-hex tokens into fake UIDs and split
legal 32-hex UIDs into two identical fakes — the 8th "enumerate the
repair surface" recurrence, the first one crossing out of the provider
package).

The legislation therefore lives HERE, in the carrier-agnostic arbitration
layer beside ``message_scanning.py``: it must never import a concrete
carrier subpackage (the phase-11 guard sweeps this file), and every
hex-class regex anywhere in ``src/chaos_agent`` must compose these
constants or register an exemption with the source-level scan test
(``tests/test_agent/test_providers/test_uid_shape_legislation.py`` — the
repair surface is discovered by machine, not enumerated by hand).

Shape-domain split (legislated, not accidental):
  - hex16 (lowercase, 16-32): the birth-side / blade-experiment vocabulary.
  - dashed UUID (case-tolerant, 8-4-4-4-12): the destroy-face legacy
    compatibility spelling (K8s-object vocabulary on every birth-side
    anchor — round-16 ruling).
"""

import re

# The blade experiment UID: LOWERCASE hex, bounded 16-32 (round-17 S3:
# lowercase domain alignment + an upper bound that keeps 40-hex sha256
# shapes out while leaving headroom for longer UIDs).
HEX16_UID_SHAPE = r"[a-f0-9]{16,32}"

# The dashed-UUID legacy spelling (destroy-face compatibility, r14) — its
# own constant (round-21) so the alternation below is composed, never
# hand-inlined: a dashed-only consumer composes THIS constant.
DASHED_UUID_SHAPE = (
    r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}"
    r"-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
)

# The FULL accepted shape domain — one definition shared by every anchor
# whose input family spans both spellings (the JSON-aware fullmatch gate,
# the malformed-JSON fallback, the destroy-token gate, the compaction
# survival context). Faces that mine the SAME input family must accept the
# SAME domain (round-20 Q3: twins with different domains are drift).
UID_SHAPE_ALTERNATION = HEX16_UID_SHAPE + "|" + DASHED_UUID_SHAPE

# The compiled fullmatch gate over the full domain — one gate instance for
# every consumer that must VALIDATE a candidate string (verify.py's
# destroy-token / status-dict / durable read-side gates re-export it as
# ``_UID_SHAPE_RE``; the compaction survival context's state fallback
# validates through it too). A second hand-compiled gate is a second
# legislation — compose this one.
UID_SHAPE_GATE = re.compile(r"^(?:" + UID_SHAPE_ALTERNATION + r")$")

# Edge guards — the case-insensitive hex-charset double refusal that keeps
# a bounded shape from partial-matching a LONGER hex-ish token (round-22 Q1).
# Round-19 N3b legislated the right-edge spelling on the prose-wording
# anchors; round-21's FALLBACK_UID_RE then hand-copied a lowercase-ONLY
# variant of both edges, and a 16-hex run followed by an uppercase trailer
# was carved into a truncated, well-shaped fake UID (the N3b pre-fix
# disease recurring on the anchor that was supposed to have learned from
# it). The guards live HERE because the edges are part of the shape
# legislation, not per-anchor decoration: a hand-copied edge drift escapes
# every shape-domain check (the matrix battery only probes whole shapes),
# so the spelling itself must be single-sourced like the shapes are.
# Consumers needing an edge-less face (quote-anchored / ``\b``-anchored
# right edges) simply do not compose these.
HEX_TAIL_GUARD = r"(?![a-fA-F0-9])"
HEX_HEAD_GUARD = r"(?<![a-fA-F0-9])"
