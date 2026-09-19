"""UID-shape legislation: jurisdiction scan + consistency matrix.

Rounds 16-20 each fixed a shape-dialect drift by patching the anchors the
round happened to look at; round-21's probe found the 8th recurrence
OUTSIDE the chaosblade provider package (memory/compactor.py's two
survival-context anchors and the side_effect conflict-check fallback each
hand-copied a pre-legislation dialect). The meta-root-cause: the
legislation lived as verify.py's PRIVATE convention with ZERO machine
enforcement — "every capturing anchor composes the source" was a claim
each round re-asserted by enumerating anchors, never a property the tree
could not lose.

Round-21 installed two machines (constants public in the carrier-agnostic
arbitration layer ``agent/providers/uid_shapes.py``):

1. **The jurisdiction scan** — every regex anywhere in ``src/chaos_agent``
   whose pattern contains a hex character class must compose the
   legislation constants (``HEX16_UID_SHAPE`` / ``DASHED_UUID_SHAPE`` /
   ``UID_SHAPE_ALTERNATION``) at the AST level, or be registered in the
   ``EXEMPT`` ledger below with a reason. A NEW hand-copied hex regex in
   ANY file fails this scan: the repair surface is discovered by machine,
   closing the "enumerate the repair surface" meta-pattern at its root.

2. **The consistency matrix** — every module-level ``re.Pattern`` in the
   governed modules whose compiled pattern embeds a legislated shape must
   be registered here with its expected domain (``hex16`` / ``full``) and
   a carrier (the minimal surface the anchor mines). The battery probes
   every face with the same shapes; an unregistered new anchor fails; a
   registered-but-vanished anchor fails.

Round-22's adversarial re-review of those machines found four gaps, all
fixed here (the machines are themselves part of the governed surface):

- The hex-class DETECTOR enumerated five literal spellings — itself a
  hand-copied dialect of the detection predicate (round-22 Q2). A
  ``[A-F0-9]`` class, a kwarg-passed ``pattern=`` or an ``re.split``
  call escaped the jurisdiction entirely. The detector now PARSES the
  class: anything that admits only hex digits AND at least one hex
  letter is governed, in every spelling.
- The matrix's module list was a hardcoded 4-module tuple while its
  docstring claimed "discovery is by reflection, not enumeration"
  (round-22 Q5). Discovery is now AST-based: any module with a
  module-level ``re.compile`` composing the legislation is found,
  imported and reflected.
- The battery had no mixed-case shape (round-22 Q6): an edge guard that
  rejects lowercase-only could truncate ``deadbeef00000001ABCDEF`` into a
  16-hex fake on every face with every test green. Mixed-case shapes are
  now refused everywhere.
- The edge-guard spelling (``(?![a-fA-F0-9])``) was hand-typed at every
  consumer (round-22 Q1): round-21's FALLBACK_UID_RE drifted casing
  exactly this way. The guards are now legislated constants
  (``HEX_HEAD_GUARD`` / ``HEX_TAIL_GUARD``) like the shapes are.

Round-23's cascade audit found the machines themselves leaking on the
POSITION axis (R4): a function-body ``re.compile(HEX16_UID_SHAPE...)``
was simultaneously exempt (Machine 1: it composes the source) and
invisible (Machine 2: its universe is module-level anchors) — a
battery-free anchor licensed forever. The module-level requirement is
now itself legislated and machine-checked: a legislated pattern MUST
live as a module-level assignment, where discovery reflects it into
the matrix and the battery polices its edges and domain.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import re
import tempfile

import pytest

from chaos_agent.agent.providers.chaosblade import verify

# ---------------------------------------------------------------------------
# Machine 1: the jurisdiction scan
# ---------------------------------------------------------------------------

RE_FUNC_NAMES = {
    "compile",
    "search",
    "match",
    "fullmatch",
    "findall",
    "finditer",
    "sub",
    "subn",
    "split",
}
LEGISLATION_NAMES = {
    "HEX16_UID_SHAPE",
    "DASHED_UUID_SHAPE",
    "UID_SHAPE_ALTERNATION",
    "_UID_SHAPE_ALTERNATION",
}
_HEX_CHARS = "0123456789abcdefABCDEF"
_HEX_LETTERS = "abcdefABCDEF"

SRC_ROOT = pathlib.Path(__file__).resolve().parents[3] / "src" / "chaos_agent"

# Deliberate exemptions from the jurisdiction: hex-class regexes that are
# NOT experiment-UID shapes. Every entry must carry a reason; an entry
# whose site no longer matches is pruned loudly (test fails) so the ledger
# cannot rot. (Currently empty: every hand-copy found so far was fixed,
# not exempted.)
EXEMPT: dict[tuple[str, int], str] = {}


def _iter_char_classes(pattern_text: str):
    """Yield the body of every bracket character class in a regex text.

    Handles ``[...]`` with an optional leading ``^``, a leading literal
    ``]`` and backslash escapes; a class that never closes yields nothing
    (it is malformed regex — not this machine's problem).
    """
    i, n = 0, len(pattern_text)
    while i < n:
        if pattern_text[i] == "[":
            j = i + 1
            if j < n and pattern_text[j] == "^":
                j += 1
            if j < n and pattern_text[j] == "]":
                j += 1
            while j < n and pattern_text[j] != "]":
                if pattern_text[j] == "\\":
                    j += 1
                j += 1
            if j < n:
                yield pattern_text[i + 1 : j]
                i = j + 1
                continue
        i += 1


def _class_is_hex(body: str) -> bool:
    """True when the class admits ONLY hex digits AND at least one hex LETTER.

    The general definition of what the jurisdiction governs (round-22 Q2):
    a hand-copied UID dialect's class is a hex-digit class —
    ``[a-f0-9]``, ``[0-9a-f]``, ``[a-fA-F0-9]``, ``[A-F0-9]``,
    ``[abcdef0-9]``, ``[a-f\\d]``, ``[\\da-f]``, in EVERY spelling, not
    the five literals the round-21 detector enumerated (the enumeration
    was itself a hand-copied dialect of the detection predicate — pure
    uppercase classes and ``\\d`` composites escaped it wholesale).
    Name classes (``[a-z0-9]``, ``[\\w]``) admit non-hex characters and
    are NOT governed; pure-digit classes (``[0-9]``, ``[\\d]``) admit no
    hex letter and are not UID shapes either. Unrecognized escapes are
    conservatively treated as admitting non-hex (the site is not flagged;
    exotic spellings are vanishingly rare in hand-copies).
    """
    has_hex_letter = False
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "\\":
            if i + 1 >= n:
                return False
            esc = body[i + 1]
            if esc == "d":
                i += 2
                continue
            if esc in _HEX_CHARS:
                if esc in _HEX_LETTERS:
                    has_hex_letter = True
                i += 2
                continue
            return False
        if ch == "-" and 0 < i < n - 1 and body[i + 1] != "]":
            i += 1
            continue
        if i + 2 < n and body[i + 1] == "-":
            lo, hi = ord(ch), ord(body[i + 2])
            if hi < lo:
                return False
            for o in range(lo, hi + 1):
                c = chr(o)
                if c not in _HEX_CHARS:
                    return False
                if c in _HEX_LETTERS:
                    has_hex_letter = True
            i += 3
            continue
        if ch not in _HEX_CHARS:
            return False
        if ch in _HEX_LETTERS:
            has_hex_letter = True
        i += 1
    return has_hex_letter


def _carries_hex_class(pattern_text: str) -> bool:
    return any(_class_is_hex(body) for body in _iter_char_classes(pattern_text))


def _fold_str(node: ast.AST, names: dict[str, str]) -> str | None:
    """Best-effort constant-fold a string expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_str(node.left, names)
        right = _fold_str(node.right, names)
        if left is not None and right is not None:
            return left + right
    return None


def _pattern_expr(call: ast.Call) -> ast.AST | None:
    """The pattern expression of an ``re.*`` call: first positional arg,
    else the ``pattern=`` keyword (round-22 Q2: kwarg-passed patterns
    escaped the round-21 scanner, which read ``node.args[0]`` only)."""
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "pattern":
            return kw.value
    return None


def _scan_hex_regex_sites(root: pathlib.Path) -> list[tuple[str, int, str]]:
    """Every ``re.*`` call site whose pattern carries a hex character class.

    Returns ``(relative_path, lineno, folded_pattern)`` tuples for sites
    whose pattern text (after folding local string constants) contains a
    hex character class — the sites the legislation must govern.
    """
    sites: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        # String constants assigned to a bare name at MODULE level — the
        # scope the round-23 position legislation fixes: an anchor's
        # pattern is compiled at module level, so a name it references is
        # only resolvable when assigned at module level too (a
        # function-body assignment is not even bound when the module
        # compiles). Round-22's "ANY nesting level" over-capture was the
        # discovery predicate smuggling its own scoping dialect: it fed
        # function-body names into this scan while Machine 2's
        # module-level universe never saw the anchors those names built
        # — the R4 escape. Function-body compile sites of ANY pattern
        # (hand-copied or legislated) are now a violation of their own
        # (test_legislated_patterns_live_at_module_level), so the scan
        # no longer needs the nested-name net to reach them.
        names: dict[str, str] = {}
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                folded = _fold_str(node.value, {})
                if isinstance(folded, str):
                    names[node.targets[0].id] = folded
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else ""
            if fname not in RE_FUNC_NAMES:
                continue
            pattern = _pattern_expr(node)
            if pattern is None:
                continue
            used = {n.id for n in ast.walk(pattern) if isinstance(n, ast.Name)}
            fragments = [
                v.value
                for v in ast.walk(pattern)
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ]
            # Names resolved through the file's string-constant table
            fragments += [
                names[n.id]
                for n in ast.walk(pattern)
                if isinstance(n, ast.Name) and n.id in names
            ]
            text = " ".join(fragments)
            if _carries_hex_class(text) and not (used & LEGISLATION_NAMES):
                rel = str(path.relative_to(root.parent.parent))
                sites.append((rel, node.lineno, text[:120]))
    return sites


def test_jurisdiction_scan_no_hand_copied_hex_regexes() -> None:
    """Every hex-class regex in src/chaos_agent composes the legislation.

    This is the round-21 root fix's enforcement: the repair surface of the
    UID-shape legislation is "every hex-capturing regex in the codebase",
    which no round 16-20 enumerated — because only a machine can. A failure
    here means a NEW hex-class regex was hand-copied somewhere in
    src/chaos_agent: compose it from HEX16_UID_SHAPE / DASHED_UUID_SHAPE /
    UID_SHAPE_ALTERNATION (agent/providers/uid_shapes.py), or — if it is
    genuinely not an experiment-UID shape — register it in EXEMPT above
    with a reason.
    """
    sites = _scan_hex_regex_sites(SRC_ROOT)
    violations = [
        f"{rel}:{lineno}  {text}"
        for rel, lineno, text in sites
        if (rel, lineno) not in EXEMPT
    ]
    assert not violations, (
        "hex-class regexes outside the UID-shape legislation (compose the "
        "constants from agent/providers/uid_shapes.py or register an EXEMPT "
        "entry with a reason):\n  " + "\n  ".join(violations)
    )


def test_jurisdiction_exempt_ledger_is_live() -> None:
    """Every EXEMPT entry still points at a real hex-regex site.

    An exemption that no longer matches anything is stale bookkeeping —
    it would silently stop meaning anything. Fail loudly so the ledger
    gets pruned.
    """
    sites = {(rel, lineno) for rel, lineno, _ in _scan_hex_regex_sites(SRC_ROOT)}
    stale = sorted(set(EXEMPT) - sites)
    assert not stale, f"stale EXEMPT entries (site no longer scanned): {stale}"


def _call_composes_legislation(call: ast.Call) -> bool:
    """True when the call is an ``re.compile`` whose pattern references a
    legislation constant name."""
    func = call.func
    fname = func.attr if isinstance(func, ast.Attribute) else ""
    if fname != "compile":
        return False
    pattern = _pattern_expr(call)
    if pattern is None:
        return False
    used = {n.id for n in ast.walk(pattern) if isinstance(n, ast.Name)}
    return bool(used & LEGISLATION_NAMES)


def _non_module_level_legislation_compiles(
    root: pathlib.Path,
) -> list[tuple[str, int]]:
    """Legislation-composing ``re.compile`` call sites OUTSIDE module level.

    Round-23 R4: Machine 2's universe is module-level assignments, so a
    function-body ``re.compile(HEX16_UID_SHAPE...)`` is single-sourced
    (Machine 1 exempts it for composing the source) yet battery-free
    (Machine 2 cannot see it). The position itself is therefore part of
    the legislation — the two machines can no longer disagree about
    what counts as an anchor: every legal anchor is discovered, every
    discovered anchor is legal.
    """
    sites: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        module_level: set[int] = set()
        for stmt in tree.body:
            if (
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Call)
                and _call_composes_legislation(stmt.value)
            ):
                module_level.add(stmt.value.lineno)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_composes_legislation(node)
                and node.lineno not in module_level
            ):
                rel = str(path.relative_to(root.parent.parent))
                sites.append((rel, node.lineno))
    return sites


def test_legislated_patterns_live_at_module_level() -> None:
    """A legislation-composing re.compile MUST be a module-level anchor
    (round-23 R4).

    A function-body compile of the legislation is the R4 escape: exempt
    from Machine 1 (it composes the source) and invisible to Machine 2
    (module-level universe) — a battery-free anchor whose edges, domain
    and truncation behavior nothing ever polices. Promote it to a
    module-level assignment; the matrix reflects it there and the
    battery takes over.
    """
    sites = _non_module_level_legislation_compiles(SRC_ROOT)
    assert not sites, (
        "legislation-composing re.compile calls OUTSIDE module level "
        "(promote the anchor to a module-level assignment so the matrix "
        "battery polices it there):\n  "
        + "\n  ".join(f"{p}:{ln}" for p, ln in sites)
    )


def test_position_legislation_catches_function_body_anchor() -> None:
    """Sandbox: a function-body legislated anchor is flagged; the same
    pattern at module level is not (round-23 R4).

    The discovery predicate no longer carries its own scoping dialect —
    "module-level" is one spelling shared by the legislation, the scan
    and the discovery, not a limit each restates independently.
    """
    function_body = (
        "import re\n"
        "from chaos_agent.agent.providers.uid_shapes import "
        "HEX16_UID_SHAPE\n"
        "def build():\n"
        "    return re.compile(HEX16_UID_SHAPE)\n"
    )
    module_level = (
        "import re\n"
        "from chaos_agent.agent.providers.uid_shapes import "
        "HEX16_UID_SHAPE\n"
        "X = re.compile(HEX16_UID_SHAPE)\n"
    )
    for snippet, expect in ((function_body, 1), (module_level, 0)):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "pkg"
            root.mkdir()
            (root / "mod.py").write_text(snippet)
            sites = _non_module_level_legislation_compiles(root)
            assert len(sites) == expect, (
                f"position legislation misjudged {snippet!r}: {sites}"
            )


def test_jurisdiction_scan_catches_every_dialect_spelling() -> None:
    """The detector PARSES classes; no spelling escapes (round-22 Q2).

    The round-21 detector matched five literal spellings — a
    pure-uppercase class, a ``\\d`` composite and a kwarg-passed pattern
    all escaped. Each snippet below is scanned in a sandbox and MUST be
    flagged.
    """
    snippets = [
        "import re\nX = re.compile(r'[A-F0-9]{16}')\n",
        "import re\nX = re.compile(r'[0-9A-F]{16}')\n",
        "import re\nX = re.compile(pattern=r'[0-9a-f]{16}')\n",
        "import re\nparts = re.split(r'[a-f\\d]+', text)\n",
        "import re\nX = re.compile(r'[abcdef0123456789]{16}')\n",
    ]
    for snippet in snippets:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "pkg"
            root.mkdir()
            (root / "mod.py").write_text(snippet)
            sites = _scan_hex_regex_sites(root)
            assert sites, f"scanner missed: {snippet!r}"

    # Name classes and digit-only classes are NOT UID dialects — the
    # detector must not drown the signal in false positives.
    benign = [
        "import re\nX = re.compile(r'[a-z0-9-]+')\n",
        "import re\nX = re.compile(r'[0-9]{2,5}')\n",
        "import re\nX = re.compile(r'\\w+')\n",
    ]
    for snippet in benign:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "pkg"
            root.mkdir()
            (root / "mod.py").write_text(snippet)
            sites = _scan_hex_regex_sites(root)
            assert not sites, f"scanner over-captures: {snippet!r}"


# ---------------------------------------------------------------------------
# Machine 2: the consistency matrix
# ---------------------------------------------------------------------------

HEX16 = "deadbeef00000001"
HEX32 = "c" * 32
DASHED = "a1b2c3d4-1111-2222-3333-444455556666"
HEX40 = "a" * 40
HEX8 = "1234abcd"
UPPER16 = "ABCDEF0123456789"
JUNK = "not-a-uid"
HYPHENS = "-------"
# Mixed-case shapes (round-22 Q6): a casing-limited edge guard can carve
# the lowercase run out of these — the battery refuses them on EVERY face,
# so no future guard drift stays green.
MIXED_TRAILER = HEX16 + "ABCDEF"
MIXED_LEADER = "ABCDEF" + HEX16


def _module_name_for(path: pathlib.Path) -> str:
    rel = path.relative_to(SRC_ROOT).with_suffix("")
    return "chaos_agent." + ".".join(rel.parts)


def _module_has_legislated_anchor(tree: ast.Module) -> bool:
    """A module-level ``NAME = re.compile(<legislation>)`` assignment."""
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
        ):
            continue
        func = node.value.func
        fname = func.attr if isinstance(func, ast.Attribute) else ""
        if fname != "compile":
            continue
        pattern = _pattern_expr(node.value)
        if pattern is None:
            continue
        used = {n.id for n in ast.walk(pattern) if isinstance(n, ast.Name)}
        if used & LEGISLATION_NAMES:
            return True
    return False


_GOVERNED_CACHE: dict[str, object] | None = None


def _governed_modules() -> dict[str, object]:
    """Every module with a module-level legislation-composing anchor.

    Round-22 Q5: the round-21 machine hardcoded a 4-module tuple while
    claiming "discovery is by reflection, not enumeration" — a fifth
    module composing the legislation was invisible to the matrix. The
    module list is now itself discovered: any module whose module level
    compiles a pattern from the legislation constants is imported and
    reflected.
    """
    global _GOVERNED_CACHE
    if _GOVERNED_CACHE is None:
        found: dict[str, object] = {}
        for path in sorted(SRC_ROOT.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            if _module_has_legislated_anchor(tree):
                mod_name = _module_name_for(path)
                found[mod_name] = importlib.import_module(mod_name)
        _GOVERNED_CACHE = found
    return _GOVERNED_CACHE


# Registered faces: (module, attr) -> (domain, carrier).
#   domain — "hex16" (bare lowercase hex16 ONLY: the birth-side / conflict
#            vocabulary) or "full" (hex16 | dashed legacy: the
#            destroy-side / survival-context vocabulary).
#   carrier — builds the minimal surface the anchor mines around a shape.
# Keys are full dotted module names (round-22 Q5: discovery returns full
# names; two modules named utils.py must never collide).
_V = "chaos_agent.agent.providers.chaosblade.verify"
_U = "chaos_agent.agent.providers.uid_shapes"
_C = "chaos_agent.agent.providers.chaosblade.cli_python"
_F = "chaos_agent.agent.nodes.side_effect._conflict_check"
MATRIX: dict[tuple[str, str], tuple[str, object]] = {
    (_V, "_UID_SHAPE_RE"): ("full", lambda s: s),
    (_V, "_UUID_RE"): ("full", lambda s: f'"result": "{s}"'),
    # Round-29 K4 — the JSON-blind fallback's plural collector (same
    # key-value face discipline as _UUID_RE, plural reach).
    (_V, "_BLIND_BIRTH_UID_RE"): ("full", lambda s: f'"result": "{s}"'),
    (_V, "_CHAOSBLADE_RESOURCE_RE"): (
        "hex16",
        lambda s: f"chaosblade-{s}",
    ),
    (_V, "FAILED_CREATE_UID_RE"): ("hex16", lambda s: f"UID: {s}"),
    (_V, "RAW_FAILED_CREATE_UID_RE"): (
        "hex16",
        lambda s: f'"uid": "{s}"',
    ),
    (_V, "UID_SHAPE_GATE"): ("full", lambda s: s),
    (_C, "PY_FAILED_CREATE_UID_RE"): (
        "hex16",
        lambda s: f'"uid": "{s}"',
    ),
    (_U, "UID_SHAPE_GATE"): ("full", lambda s: s),
    (_F, "FALLBACK_UID_RE"): ("hex16", lambda s: s),
}


def _discover_legislated_patterns() -> dict[tuple[str, str], re.Pattern]:
    """Module-level Patterns embedding a legislated shape, by (mod, attr).

    Discovery is by reflection over the AST-discovered governed modules:
    any NEW module-level anchor that composes the legislation is found
    automatically — and then fails the matrix-completeness test until it
    is registered.
    """
    found: dict[tuple[str, str], re.Pattern] = {}
    for mod_name, mod in _governed_modules().items():
        for attr, value in vars(mod).items():
            if not isinstance(value, re.Pattern):
                continue
            if (
                verify.HEX16_UID_SHAPE in value.pattern
                or verify.DASHED_UUID_SHAPE in value.pattern
            ):
                found[(mod_name, attr)] = value
    return found


def _matrix_face(key: tuple[str, str]) -> tuple[re.Pattern, object]:
    mod_name, attr = key
    mod = _governed_modules()[mod_name]
    return getattr(mod, attr), MATRIX[key][1]


def _captured(match: re.Match) -> str:
    """The mined value: capture group 1 when the anchor captures, else the match.

    Gate-style anchors (_UID_SHAPE_RE, UID_SHAPE_GATE, FALLBACK_UID_RE)
    have no capture group — for them the whole match IS the mined value.
    """
    return match.group(1) if match.re.groups else match.group(0)


def test_matrix_registration_is_complete() -> None:
    """Every legislated anchor is registered; every registration is live.

    The two directions together force every future face into the matrix:
    a new anchor composes the legislation → discovery finds it → the
    unregistered side fails; a registered anchor is deleted → the stale
    side fails. Round-22 Q5: the module universe is discovered, so a NEW
    module composing the legislation enters the machine's jurisdiction
    the moment it is written.
    """
    discovered = set(_discover_legislated_patterns())
    registered = set(MATRIX)
    missing = sorted(discovered - registered)
    stale = sorted(registered - discovered)
    assert not missing, (
        "module-level anchors embedding a legislated shape but NOT "
        "registered in the matrix (add a MATRIX entry with the face's "
        "domain and carrier):\n  " + "\n  ".join(map(str, missing))
    )
    assert not stale, (
        "registered faces that no longer exist as legislated anchors:\n  "
        + "\n  ".join(map(str, stale))
    )


@pytest.mark.parametrize(
    "key",
    sorted(MATRIX),
    ids=lambda k: f"{k[0].rsplit('.', 1)[-1]}.{k[1]}",
)
def test_matrix_shape_battery(key: tuple[str, str]) -> None:
    """One battery, every face: identical shapes, identical rulings.

    Accept: 16-hex and 32-hex everywhere; dashed only on ``full`` faces.
    Refuse: 40-hex (sha256), 8-hex (K8s suffix), uppercase, junk,
    hyphen-noise — everywhere, on every face, no exceptions — and the
    MIXED-CASE shapes (round-22 Q6): no face may carve the lowercase run
    out of a longer hex-ish token, the truncation channel a
    lowercase-only edge guard leaves open.
    """
    domain, carrier_fn = MATRIX[key]
    pattern, carrier = _matrix_face(key)
    mod_short, attr = key

    for accept_shape in (HEX16, HEX32):
        m = pattern.search(carrier(accept_shape))
        assert m is not None and _captured(m) == accept_shape, (
            f"{mod_short}.{attr} must accept the legal shape "
            f"{accept_shape[:8]}... (len {len(accept_shape)})"
        )

    dashed_expect = domain == "full"
    m = pattern.search(carrier(DASHED))
    if dashed_expect:
        assert m is not None and _captured(m) == DASHED, (
            f"{mod_short}.{attr} is a full-domain face and must keep the "
            f"dashed legacy spelling"
        )
    else:
        assert m is None, (
            f"{mod_short}.{attr} is a hex16-only face and must refuse the "
            f"dashed K8s-object vocabulary (domain split)"
        )

    for refuse_shape, why in (
        (HEX40, "40-hex sha256 shape"),
        (HEX8, "8-hex K8s suffix"),
        (UPPER16, "uppercase hex16"),
        (MIXED_TRAILER, "mixed-case token (lowercase run + uppercase trailer)"),
        (MIXED_LEADER, "mixed-case token (uppercase leader + lowercase run)"),
        (JUNK, "junk token"),
        (HYPHENS, "hyphen noise"),
    ):
        m = pattern.search(carrier(refuse_shape))
        assert m is None, (
            f"{mod_short}.{attr} must refuse the {why} "
            f"({refuse_shape[:16]!r}...): matched "
            f"{(m.group(1) if m.re.groups else m.group(0))[:32]!r}"
        )


def test_matrix_resource_face_strips_prefix() -> None:
    """The resource-name face captures the SUFFIX, never the prefixed string.

    (round-20 R1's pinning, kept in the matrix so the face cannot regress
    independently of it.)
    """
    m = verify._CHAOSBLADE_RESOURCE_RE.search(f"chaosblade-{HEX16}")
    assert m is not None and m.group(1) == HEX16


def test_edge_guards_are_single_sourced() -> None:
    """The guard spellings are legislated constants, never re-typed
    (round-22 Q1: round-21's FALLBACK_UID_RE drifted the casing of a
    hand-typed edge while every shape check stayed green)."""
    from chaos_agent.agent.providers import uid_shapes

    # Every governed pattern's lookaround text equals the legislated
    # spellings where a lookaround appears at all.
    guard_users = {
        "chaos_agent.agent.providers.chaosblade.verify": "FAILED_CREATE_UID_RE",
        "chaos_agent.agent.nodes.side_effect._conflict_check": "FALLBACK_UID_RE",
    }
    for mod_name, attr in guard_users.items():
        pat = getattr(_governed_modules()[mod_name], attr).pattern
        assert uid_shapes.HEX_TAIL_GUARD in pat, (
            f"{mod_name}.{attr} must compose HEX_TAIL_GUARD"
        )
    fallback = getattr(
        _governed_modules()["chaos_agent.agent.nodes.side_effect._conflict_check"],
        "FALLBACK_UID_RE",
    ).pattern
    assert uid_shapes.HEX_HEAD_GUARD in fallback


def test_extractor_refuses_structured_failure_receipts() -> None:
    """round-22 Q4b: a structured refusal blocks the shape-only fallbacks.

    The JSON-aware strategy saw the receipt and judged it un-licensable
    (code=500 failed-create, the 54000 terminal failure) — the regex /
    resource fallbacks exist for JSON-BLIND output (truncated stdout,
    unescaped quotes) and must not re-admit a UID the structured verdict
    already refused. The pre-round-22 chain blocked the 54000 spelling
    only (the r20-Q8 sentinel); a code=500 receipt's UID fell straight
    through to the fallback and laundered a failed experiment's UID into
    the live slot.
    """
    assert (
        verify.extract_experiment_uid(
            '{"code":200,"success":true,"result":"deadbeef00000001"}'
        )
        == HEX16
    )
    assert (
        verify.extract_experiment_uid(
            '{"code":500,"success":false,"result":"deadbeef00000001"}'
        )
        is None
    )
    assert (
        verify.extract_experiment_uid(
            '{"code":54000,"success":false,"result":"deadbeef00000001",'
            '"error":"exec failed"}'
        )
        is None
    )
    # JSON-blind output keeps its fallback lane
    assert (
        verify.extract_experiment_uid(f'wrapped: "result":"{HEX16}" trailing junk')
        == HEX16
    )
    # A non-receipt JSON object does NOT claim jurisdiction over the text
    assert (
        verify.extract_experiment_uid(f'{{"apiVersion":"v1"}} "result":"{HEX16}"')
        == HEX16
    )


def test_survival_context_delegates_uid_lifecycle_to_registry() -> None:
    """End-to-end (round-22 Q4): the survival context carries lifecycle
    legislation because it DELEGATES to the registry seam.

    A create receipt survives compaction. A destroyed uid (retired in the
    very state dict the function receives — the pre-round-22 miner never
    read it), a failed-create receipt and a prose mention do NOT: the
    round-21 anchors promoted all of them into "Active experiment_uid"
    in the post-compaction recovery message.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from chaos_agent.memory import compactor

    created = [
        ToolMessage(
            content='{"code":200,"success":true,"result":"deadbeef00000001"}',
            name="blade_create",
            tool_call_id="tc-r22-a",
        )
    ]
    ctx = compactor.extract_critical_context(created, {})
    assert ctx.get("active_experiment_uid") == HEX16

    destroyed = [
        ToolMessage(
            content='{"code":200,"success":true,"result":"deadbeef00000001"}',
            name="blade_destroy",
            tool_call_id="tc-r22-b",
        )
    ]
    ctx = compactor.extract_critical_context(
        destroyed, {"retired_experiment_uids": [HEX16]}
    )
    assert "active_experiment_uid" not in ctx

    failed = [
        ToolMessage(
            content='{"code":500,"success":false,"result":"deadbeef00000001"}',
            name="blade_create",
            tool_call_id="tc-r22-c",
        )
    ]
    ctx = compactor.extract_critical_context(failed, {})
    assert "active_experiment_uid" not in ctx

    prose = [HumanMessage(content=f"please re-check experiment_uid: {HEX16}")]
    ctx = compactor.extract_critical_context(prose, {})
    assert "active_experiment_uid" not in ctx

    # State fallback: garbage refused (Q3 read-side gate), legal accepted
    ctx = compactor.extract_critical_context(
        [], {"experiment_uid": "uid-e1-placeholder"}
    )
    assert "active_experiment_uid" not in ctx
    ctx = compactor.extract_critical_context([], {"experiment_uid": HEX16})
    assert ctx.get("active_experiment_uid") == HEX16

    # State fallback, death quadrant (round-23 R1): the slot is
    # last-write-wins and never cleared on destroy, so a legal-shape
    # corpse in the slot + the retired ledger is the post-destroy STEADY
    # state of this fallback's input. The round-22 shape-only gate waved
    # it through ("Active experiment_uid: <dead>"); the read side now
    # judges BOTH axes — shape AND liveness — through the same
    # live_liability_uids primitive every other read face of the slot
    # uses (owned − retired − proven-destroyed; the slot self-proves
    # provenance via the durable-record evidence source).
    ctx = compactor.extract_critical_context(
        [], {"experiment_uid": HEX16, "retired_experiment_uids": [HEX16]}
    )
    assert "active_experiment_uid" not in ctx, (
        "a retired uid in the state slot must not survive compaction as "
        "the active experiment"
    )
    # ...and neither may a message-proven destroy: the destroy evidence
    # lives in the very messages being compacted away, so the read gate
    # must consult them (live_liability_uids subtracts proven deaths).
    destroyed_pair = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "blade_destroy",
                "args": {"uid": HEX16},
                "id": "tc-r23-x",
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content='{"code":200,"success":true,"result":"success"}',
            name="blade_destroy",
            tool_call_id="tc-r23-x",
        ),
    ]
    ctx = compactor.extract_critical_context(
        destroyed_pair,
        {"experiment_uid": HEX16, "messages": destroyed_pair},
    )
    assert "active_experiment_uid" not in ctx, (
        "a message-proven destroyed uid must not survive compaction as "
        "the active experiment"
    )


def test_conflict_fallback_reports_whole_identities() -> None:
    """The conflict-check fallback surfaces whole UIDs, never truncations.

    (round-21 S3: the pre-fix dialect reported TWO fake 16-char UIDs for a
    40-hex sha256 shape and split a legal 32-hex UID into two identical
    fakes. round-22 Q1: mixed-case tokens are never carved either — the
    guards reject case-adjacency, not just lowercase-adjacency.)
    """
    from chaos_agent.agent.nodes.side_effect import _conflict_check

    assert _conflict_check.FALLBACK_UID_RE.findall(HEX40) == []
    assert _conflict_check.FALLBACK_UID_RE.findall(HEX32) == [HEX32]
    assert _conflict_check.FALLBACK_UID_RE.findall(f"exp {HEX16} running") == [HEX16]
    assert _conflict_check.FALLBACK_UID_RE.findall(MIXED_TRAILER) == []
    assert _conflict_check.FALLBACK_UID_RE.findall(MIXED_LEADER) == []
