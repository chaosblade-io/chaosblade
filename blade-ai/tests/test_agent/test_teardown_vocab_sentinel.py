"""Teardown≠mutation vocabulary sentinel (B76-R11, P3-evolved).

The teardown exemption ("a registered-vehicle delete is ASSET REMOVAL,
not fault injection") lived on the AGENT side of the vocabulary boundary
for rounds R5–R10 as a caller-side filter convention — and it leaked six
times (R5 screener, R6-1 channel A, R8-1 channel B, O-2 confirmation
guard, R10-1 replan evidence, O-3 step credit), each found only by
adversarial re-inspection. P3 (matcher threading) moved the exemption
INTO the vocabulary layer: the scan primitives take an ``is_teardown``
matcher and skip registered-vehicle teardown calls at CALL granularity
(mixed batches included); the neutral seam
(``execution_artifacts.make_teardown_matcher`` / ``issue_call_is_
registered_teardown``) single-sources the judgement. This test now turns
the POST-P3 convention into a STRUCTURAL invariant: any scope that loads
a watched mutation-vocabulary primitive must also load an
exemption-applied marker (thread the matcher, apply the predicate, or be
a registered, reasoned whitelist entry).

New consumption points that forget to thread the matcher fail here
BEFORE they can become the seventh door.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "chaos_agent"

# ---------------------------------------------------------------------------
# Vocabulary sets
# ---------------------------------------------------------------------------

#: BARE mutation-vocabulary / history-scan primitives. A function that
#: loads any of these consumes raw write-verb evidence and MUST apply the
#: teardown exemption itself (or be whitelisted with a reason).
WATCHED_PRIMITIVES = frozenset(
    {
        # the write-verb vocabulary, both spellings
        "KUBECTL_WRITE_SUBCOMMANDS",
        "inject_kubectl_subcommands",
        # raw history scanners (message_scanning layer). P3 gave each an
        # ``is_teardown`` matcher parameter — the call-level skip lives
        # INSIDE them now, so a caller's obligation narrowed from
        # "pre-filter messages" to "thread the matcher": loading the
        # scanner bare (matcher defaulted to None = RAW evidence) is still
        # the seventh-door shape.
        "scan_kubectl_mutation_index",
        "scan_native_issue_disproven",
        "scan_kubectl_injection_after_blade",
        # the HOST channel's native scanner (R23/G-7): same P3 contract
        # as the kubectl scanners above — the R23 threading gave it the
        # ``is_teardown`` matcher (an arm-first systemd-run timer is a
        # recovery registration, not native takeover evidence), so a
        # caller loading it bare re-opens the host twin of the seventh
        # door. Both live callers (host_shell provider detect /
        # injection_recency) thread the matcher.
        "scan_host_native_index",
        # (``scan_kubectl_mutation_attempted`` — the fourth armed scanner,
        # registered by R17/G-1 and then retired as dead code per the O-7
        # ruling: zero consumers anywhere in the repo — pinned by the
        # watch-set liveness tooth.)
        # (``scan_kubectl_blade_success`` is deliberately NOT watched: it
        # detects "kubectl exec ... blade create" DELIVERY receipts — a
        # teardown delete can never take that shape, so the primitive is
        # outside the teardown≠mutation exposure surface.)
        # raw executed-verb credit (provider layer)
        "_executed_kubectl_verbs",
        # the pure issue-time classifier (no filter inside — callers filter)
        "classify_issue_time_method",
        # the UNFILTERED epoch window (callers must thread the matcher or
        # apply a call-level skip themselves)
        "_epoch_bounded_messages",
        # the raw back-scan hook (provider-side, registry-dispatched). Its
        # ``is_teardown`` parameter carries the exemption — callers that
        # dispatch it bare re-open the R6-1 ghost door.
        "was_injection_attempted",
        # the step-credit CONSUMER entry (F-11, round-32e; F-12 resolved
        # honestly, P3): its teardown filter moved INSIDE (threaded into
        # the ``scan_step_actions`` hook) but remains OPTIONAL — the
        # ``is_teardown`` default is None = RAW credit. A guarded entry
        # must apply its filter UNCONDITIONALLY; a conditional one does
        # not qualify, so the entry STAYS WATCHED and a caller proves the
        # exemption by loading a marker (``make_teardown_matcher`` / an
        # ``is_teardown`` reference). Blanket-moving it to EXEMPTION_MARKERS
        # (the original F-12 plan) would re-open exactly the F-11
        # false-negative window: a forgetful caller passing no matcher —
        # the seventh door in its post-P3 shape — would pass silently.
        "build_injection_step_selfcheck",
    }
)

#: Markers proving the teardown exemption is APPLIED somewhere inside the
#: loading function — threading the P3 matcher (``make_teardown_matcher``
#: or an ``is_teardown`` reference), the shared predicate (single-sourced
#: in ``execution_artifacts`` since P3), one of the composite wrappers, or
#: one of the GUARDED ENTRY points whose own body already applies the
#: filter (so the caller is unconditionally safe).
EXEMPTION_MARKERS = frozenset(
    {
        # P3 matcher threading: the closure builder or the parameter
        # reference. ``is_teardown`` counts as a Name Load in
        # ``is_teardown=is_teardown`` threading positions — coarse
        # co-location (contract D below), pinned by the family teeth.
        "make_teardown_matcher",
        "is_teardown",
        # shared core predicate + its wrappers (single source since P3:
        # the neutral ``issue_call_is_registered_teardown`` and the
        # state-reading execute_loop twin ``_issue_call_...``)
        "is_vehicle_teardown_delete",
        "issue_call_is_registered_teardown",
        "_issue_call_is_registered_teardown",
        "_vehicle_delete_is_cleanup",
        # guarded entries: the filter is INSIDE these entry points
        # (round-32e S21 verified the bodies carry the exemption markers;
        # P3 re-verified — the filter is now threaded, not pre-stripped).
        "_issue_disproven_in_epoch",
        "_injection_attempted_this_contract",
    }
)

#: Registered open doors — every entry MUST carry a non-empty reason.
#: P3 (matcher threading) LANDED: the seven carrier-hook entries that
#: leaned on agent-side pre-filtering retired (the hooks thread the
#: matcher themselves now). What remains is the genuinely matcher-out-of-
#: reach residue: a test-only shim, a read-only evidence channel, the
#: channel-A pre-classification skip, and a declaration site.
WHITELIST: dict[tuple[str, str], str] = {
    (
        "agent/nodes/execute/_injection_detection.py",
        "_was_kubectl_injection_attempted",
    ): "test-only compatibility shim (docstring: no src caller remains); "
    "the vocabulary arrives via registry dispatch and the shim never feeds "
    "attribution consumers.",
    (
        "agent/nodes/execute/execute_loop.py",
        "_target_absence_proven_in_epoch",
    ): "target-absence exception channel: READ-ONLY evidence — the scanner "
    "hard-filters subcommand != 'get' (write verbs invisible to it), so a "
    "teardown delete can never register as evidence here.",
    (
        "agent/providers/k8s_native/provider.py",
        "issue_time_method",
    ): "issue-time classification runs AFTER the issue-loop caller's "
    "call-level teardown skip (R6-1) — the classifier sees only calls "
    "that already passed the exemption; pinned by the R6-1 teeth.",
    (
        "agent/providers/k8s_native/provider.py",
        "<class:K8sNativeProvider>",
    ): "vocabulary DECLARATION site (C/C2 scope registration): the "
    "class-body assignment ``inject_kubectl_subcommands = "
    "KUBECTL_WRITE_SUBCOMMANDS`` publishes the word set to instances — it "
    "binds names and never touches message history, so no exemption "
    "applies here.",
}

#: Message-scanning primitives REGISTERED OUT of the watch set, each
#: with the reason it sits outside the teardown≠mutation exposure surface
#: (the ``scan_kubectl_blade_success`` precedent — currently that blade-
#: delivery scanner lives in ``chaosblade/verify.py``, so the set is
#: empty; it exists so a future fifth primitive with a reasoned out-of-
#: scope argument has an honest home). The completeness tooth derives the
#: watch obligation from the SOURCE, so this registry is the ONLY escape
#: hatch — and its entries carry the same substantive-reason discipline
#: as WHITELIST.
PRIMITIVE_EXCLUSIONS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Checker (pure, testable)
# ---------------------------------------------------------------------------


def _loaded_names(func_node: ast.AST) -> set[str]:
    """Names an AST function body LOADS (bare names + attribute tails)."""
    names: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            names.add(node.attr)
    return names


def _pruned_body_aliases(node: ast.AST) -> dict[str, str]:
    """asname -> name for ImportFrom statements in one scope's WHOLE body.

    Block-inclusive (F-13, round-32h): imports nested inside Try/If/With
    statements belong to their enclosing scope exactly like top-level
    ones — ``try: from x import scan_... as _s except ImportError:`` is
    the natural optional-dependency idiom, and the old direct-body-only
    collection let a module/class/function CHAIN entry miss them (the
    function unit's own whole-subtree map rescued direct consumption —
    the H tooth's path — but nothing rescued the enclosing-chain view a
    NESTED consumer, the ``<module>`` unit, or a ``<class:...>`` unit
    resolves against).

    Pruned descent, source order: never descends into nested
    FunctionDef/AsyncFunctionDef/ClassDef — those are their own scopes
    (same pruning contract as ``_scope_load_names``) — and recursion is
    pre-order, so same-named aliases shadow in the order Python executes
    them, not in AST traversal order.
    """
    aliases: dict[str, str] = {}

    def _visit(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue  # nested scope — its own unit
            if isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.asname:
                        aliases[alias.asname] = alias.name
            _visit(child)

    _visit(node)
    return aliases


def _scope_alias_map(func_node: ast.AST) -> dict[str, str]:
    """asname -> name for every ImportFrom in a function's WHOLE subtree.

    Includes nested-function imports (over-broad by design: a nested
    import the outer scope cannot actually reach still only ADDS a
    resolution — errs toward reporting, never toward silence).

    Pre-order (F-13 second segment, round-32i): same-named aliases shadow
    in SOURCE order — Python rebinds at each import statement as written,
    so the last import IN SOURCE wins. ``ast.walk``'s breadth-first layer
    order instead let a deeper, earlier-in-source block import win the
    dict's last write over a shallower, later-in-source top-level one
    (deep layers are yielded last) — the mixed-order tooth's silent-miss
    shape. The recursion is deliberately still unpruned: the over-broad
    contract above is unchanged, only the visit order now matches
    execution order.
    """
    aliases: dict[str, str] = {}

    def _visit(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.asname:
                        aliases[alias.asname] = alias.name
            _visit(child)

    _visit(func_node)
    return aliases


def _collect_alias_map(tree: ast.AST) -> dict[str, str]:
    """TREE-LEVEL asname -> name map (single flat view, collisions lost).

    Kept only for the docstring history below; ``scan_source`` resolves
    aliases per-scope (R14-1) — see :func:`_enclosing_alias_map`.

    Alias imports are a NATURAL Python style (long-name abbreviation,
    name-conflict avoidance) — a new consumption point written this way
    must not silently bypass the sentinel (R12-1: ``from x import
    scan_kubectl_mutation_index as _s`` left the original name absent from
    every Load context, so the name scan missed it entirely). R12-1's
    first fix collected the map tree-wide into ONE dict — which silently
    DROPPED the entry whenever two legal scopes re-used the same alias
    for different sources (last write wins; the write ORDER decided
    between a miss and a false positive, disproving that version's
    "can only add hits" claim). R14-1 resolves per scope instead.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


def _enclosing_alias_map(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> dict[str, str]:
    """Per-scope alias map (R14-1): enclosing scopes first, near shadowing far.

    Walks the parent chain from ``node`` to the module root, collecting
    each enclosing scope's imports with the block-inclusive pruned sweep
    (F-13, round-32h — Try/If/With-nested imports count for their
    enclosing scope; distant scope first, nearer scope written later so
    it SHADOWS — Python's scoping semantics). For a function scope, the
    function's own subtree imports are laid on top (last write wins there
    too: a same-named double import inside one function shadows exactly
    as Python executes it).

    Scope-locality kills the R12-1 residual: two functions re-using one
    alias for different sources each resolve their OWN source — no
    cross-scope pollution in either direction (no dropped-entry miss, no
    order-dependent false positive).

    The chain STARTS at ``node`` itself, not at its parent: a Module or
    ClassDef unit's own direct imports are that unit's innermost scope
    (starting at the parent would silently lose every module-level alias
    for the ``<module>`` unit — the gap was real, pinned by the shadowing
    tooth's module-alias door).
    """
    chain: list[dict[str, str]] = []
    cur = node
    while cur is not None:
        if isinstance(cur, (ast.Module, ast.ClassDef)) or isinstance(
            cur, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            chain.append(_pruned_body_aliases(cur))
        cur = parents.get(cur)
    resolved: dict[str, str] = {}
    for scope_map in reversed(chain):  # farthest first → nearest overwrites
        resolved.update(scope_map)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        resolved.update(_scope_alias_map(node))
    return resolved


def _scope_load_names(node: ast.AST) -> set[str]:
    """Load names in ONE scope, without descending into nested scopes.

    Used for the module top-level and class bodies (C/C2): a nested
    function/method is its own unit and must not be double-counted here.
    Lambdas stay in the enclosing scope (a module-level lambda's body is
    part of that statement's expression — counting it errs safe).
    """
    names: set[str] = set()
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue  # nested scope — its own unit
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            names.add(child.id)
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            names.add(child.attr)
        stack.extend(ast.iter_child_nodes(child))
    return names


def scan_source(source: str, filename: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Violations in one source string: (filename, scope, watched hits).

    Threat model (deliberate, stated honestly): this sentinel catches the
    FORGETFUL author, not the malicious one — getattr-by-string, exec, or
    editing this very file all bypass it. Its job is to make "added a new
    vocabulary consumer, forgot the teardown exemption" fail at commit
    time in every natural coding shape (plain import, attribute access,
    as-alias at function or module level, rebinding, nesting, methods,
    and — since C/C2 — module top-level or class-body statements).

    Scope units (C/C2): functions/methods, PLUS the module top-level
    (``<module>``) and each class body (``<class:Name>``). The original
    function-only domain let a top-level ``_BASE = scan_...(...)`` boot
    value or a class attribute consume the vocabulary with no function
    ever loading a watched name — the seventh door born outside every
    scanned unit.

    Granularity contract (D, stated honestly): the exemption check is
    co-location at SCOPE granularity — an exemption marker merely LOADED
    somewhere in the same scope counts, even on an unrelated display
    branch. Proving the marker sits on the actual consumption path would
    need data-flow analysis, which is deliberately out of scope. The
    family teeth (behavior-level: ``TestIssueTimeTeardownAttribution``
    etc.) are the backstop for semantic-path drift.
    """
    violations: list[tuple[str, str, tuple[str, ...]]] = []
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    def _check(scope: str, names: set[str], node: ast.AST) -> None:
        # R12-1: resolve as-aliases back to their ORIGINAL names so the
        # vocabulary match (and the exemption match — one resolution for
        # both sides, or exemption parsing would lag watch parsing) cannot
        # be dodged by aliasing. R14-1: the map is SCOPE-AWARE — each unit
        # resolves against its OWN enclosing chain (see
        # ``_enclosing_alias_map``), so two legal scopes re-using one alias
        # for different sources cannot pollute each other in either
        # direction.
        alias_map = _enclosing_alias_map(node, parents)
        resolved = names | {
            alias_map[n] for n in names if n in alias_map
        }
        hits = sorted(resolved & WATCHED_PRIMITIVES)
        if hits and not (resolved & EXEMPTION_MARKERS):
            violations.append((filename, scope, tuple(hits)))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _check(node.name, _loaded_names(node), node)
        elif isinstance(node, ast.ClassDef):
            _check(f"<class:{node.name}>", _scope_load_names(node), node)
    _check("<module>", _scope_load_names(tree), tree)
    return violations


def teardown_armed_module_functions(source: str) -> set[str]:
    """Module-level function names in ``message_scanning`` source that
    declare an ``is_teardown`` parameter.

    The completeness tooth's SOURCE of truth: a scan primitive that takes
    the teardown matcher has self-declared teardown sensitivity (knife-1's
    own contract — the parameter exists BECAUSE the primitive is inside
    the exposure surface), so its name belongs in WATCHED_PRIMITIVES, or
    in PRIMITIVE_EXCLUSIONS with a reason when the primitive is argued
    outside it (the ``scan_kubectl_blade_success`` precedent). Manual
    enumeration alone missed the fourth primitive (R17/G-1: threaded by
    knife-1 yet unregistered — zero consumers today, so nothing failed;
    a future bare caller would have bypassed the sentinel silently).
    This derivation closes the registration gap at the source.
    """
    tree = ast.parse(source)
    armed: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if any(arg.arg == "is_teardown" for arg in params):
            armed.add(node.name)
    return armed


def teardown_armed_tree(root: Path) -> set[str]:
    """MODULE-LEVEL function names under ``root`` declaring ``is_teardown``.

    F-14's derivation source (tree-wide sibling of the completeness
    tooth's ``message_scanning``-only derivation — same R17/G-1 lesson,
    no hand enumeration: a primitive that takes the teardown matcher
    has self-declared teardown sensitivity by its own signature).
    Methods are deliberately OUT (``tree.body`` only): provider hook
    method names (``detect``, ``issue_disproven``, ``was_injection_
    attempted`` …) are armed in five provider classes and NOT armed in
    unrelated classes (``detect`` collides in ``_side_effect_detectors``) —
    name-level matching across classes would fabricate both misses and
    false demands; that surface stays with the scope sentinel and the
    family teeth.
    """
    armed: set[str] = set()
    for _path, source in iter_source_files(root):
        tree = ast.parse(source)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
            if any(arg.arg == "is_teardown" for arg in params):
                armed.add(node.name)
    return armed


def scan_raw_matcher_callsites(root: Path) -> list[tuple[str, str, str]]:
    """F-14 (round-32k): RAW call sites of teardown-armed module functions.

    Every src call site of a derived-armed name must thread ``is_teardown=``
    AT THE CALL — (relpath, enclosing scope, primitive) for each one that
    does not. Call granularity, deliberately BELOW contract D's scope
    co-location: the round-32k corpus proved scope-level markers cannot
    see W1 (matcher loaded and even built, the scan call made raw) or W4
    (a second armed primitive called raw inside an already-exempted
    scope) — both silent-pass shapes of the wide family-set exemption.
    Same honest threat model as ``scan_source``: getattr-by-string or
    exec dispatch escapes (src today carries the armed names as strings
    only in ``__all__`` exports); this catches the forgetful author's
    natural plain-call shape.
    """
    armed = teardown_armed_tree(root)
    raw: list[tuple[str, str, str]] = []
    for path, source in iter_source_files(root):
        rel = str(path.relative_to(root))
        tree = ast.parse(source)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            else:
                continue
            if callee not in armed:
                continue
            if any(kw.arg == "is_teardown" for kw in node.keywords):
                continue
            scope, cur = "<module>", parents.get(node)
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = cur.name
                    break
                if isinstance(cur, ast.ClassDef):
                    scope = f"<class:{cur.name}>"
                    break
                cur = parents.get(cur)
            raw.append((rel, scope, callee))
    return raw


def scan_delete_predicate_callsites(root: Path) -> list[tuple[str, str, bool]]:
    """P-9 (round-32p): every ``is_vehicle_teardown_delete`` call site
    with its ``v_args`` threading state — (relpath, scope, threaded).

    The teardown predicate answers from BOTH the ``effective`` anchor
    and the raw ``v_args`` string (G-4): the shared classifier's ``names``
    tuple is the drift check's single anchor — a comma-joined list stays
    ONE name and the space-separated batch members are DROPPED entirely.
    The round-32p probe pinned the fork on the space form: a MIXED batch
    (registered vehicle + unregistered victim) silently EXEMPTS a real
    mutation when the caller leans on the keyword's empty-string default,
    because the fallback then only sees the first (registered) name. No
    error, no red test — the code blind spot and the test blind spot
    coincide; the loud twin of this failure class (a missing parameter)
    crashed recover on the real cluster, the quiet twin just mis-attributes.
    The comma form self-rescues through the fallback's comma split; only
    the space form forks. Tests constructing an ``EffectiveTarget``
    directly are the fallback's honest constituency and live under tests/
    (scan domain: src only).
    """
    sites: list[tuple[str, str, bool]] = []
    for path, source in iter_source_files(root):
        rel = str(path.relative_to(root))
        tree = ast.parse(source)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            else:
                continue
            if callee != "is_vehicle_teardown_delete":
                continue
            threaded = any(kw.arg == "v_args" for kw in node.keywords)
            scope, cur = "<module>", parents.get(node)
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = cur.name
                    break
                if isinstance(cur, ast.ClassDef):
                    scope = f"<class:{cur.name}>"
                    break
                cur = parents.get(cur)
            sites.append((rel, scope, threaded))
    return sites


def iter_source_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        yield path, path.read_text(encoding="utf-8")


def scan_tree(root: Path) -> list[tuple[str, str, tuple[str, ...]]]:
    """Violations across the whole source tree, relative paths."""
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for path, source in iter_source_files(root):
        rel = str(path.relative_to(root))
        out.extend(scan_source(source, rel))
    return out


def whitelist_violations(tree_violations) -> list[tuple[str, str, tuple[str, ...]]]:
    """Violations NOT covered by a whitelist entry."""
    return [
        v
        for v in tree_violations
        if (v[0], v[1]) not in WHITELIST
    ]


def all_defined_functions(root: Path) -> set[tuple[str, str]]:
    """Every (relpath, scope-key) defined in the tree — whitelist liveness.

    Scope keys cover functions/methods, the module top-level
    (``<module>``), and class bodies (``<class:Name>``) so whitelist
    entries registered for C/C2 scopes stay liveness-checked too.
    """
    defined: set[tuple[str, str]] = set()
    for path, source in iter_source_files(root):
        rel = str(path.relative_to(root))
        tree = ast.parse(source)
        defined.add((rel, "<module>"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add((rel, node.name))
            elif isinstance(node, ast.ClassDef):
                defined.add((rel, f"<class:{node.name}>"))
    return defined


def all_defined_names(root: Path) -> set[str]:
    """Every name BOUND anywhere in the src tree — watch-set liveness.

    Function/class definitions bind their names directly; assignments,
    loop targets, and walrus bindings appear as Store-context
    ``ast.Name`` nodes. Import aliases (``ast.alias`` — never a Name)
    and except-handler names (``ExceptHandler.name`` — a plain str) are
    deliberately INVISIBLE: watched entries are repo-defined scan
    primitives, never import-bound re-exports, so a watched name that
    exists ONLY through an import alias is genuinely stale. A
    WATCHED_PRIMITIVES entry matching nothing here is stale — the
    primitive was deleted or renamed and the watch registration must
    follow (mirrors ``all_defined_functions`` for the whitelist).
    """
    names: set[str] = set()
    for _path, source in iter_source_files(root):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return names


# ---------------------------------------------------------------------------
# Teeth
# ---------------------------------------------------------------------------


class TestTeardownVocabularySentinel:
    """The structural invariant: vocabulary consumers carry the exemption."""

    def test_no_unwhitelisted_vocabulary_consumer(self):
        """主牙：全树扫描，白名单外零违规——任何加载裸词汇原语的函数
        必须同时加载豁免标记。新消费点忘装过滤 ⇒ 本牙红（第七扇门
        无法静默诞生）。"""
        violations = whitelist_violations(scan_tree(SRC_ROOT))
        assert not violations, (
            "functions consuming the mutation vocabulary without the "
            "teardown exemption (register them in WHITELIST with a "
            "reason, or apply the filter):\n"
            + "\n".join(f"  {f} :: {fn} -> {hits}" for f, fn, hits in violations)
        )

    def test_whitelist_entries_still_exist(self):
        """白名单活性牙：条目对应的函数必须仍存在——被删/改名后白名单
        条目必须清理，防止白名单腐化为掩盖新函数的死角。"""
        defined = all_defined_functions(SRC_ROOT)
        stale = [k for k in WHITELIST if k not in defined]
        assert not stale, (
            "stale whitelist entries (function gone — remove the entry or "
            "update the name): " + ", ".join(f"{f}::{fn}" for f, fn in stale)
        )

    def test_whitelist_reasons_are_substantive(self):
        """理由完整性牙：每条白名单必须带实质理由（≥40 字符）——登记
        开放门必须说明为什么开、谁负责关（P3 落地时清空本表）。"""
        thin = {
            k: r for k, r in WHITELIST.items() if len(r.strip()) < 40
        }
        assert not thin, (
            "whitelist entries need a substantive reason (what door, why "
            "open, what closes it): " + repr(thin)
        )

    def test_watched_primitives_still_defined(self):
        """监听集活性牙（O-7 退役轮）：每个被监听名必须仍在 src 树内有
        定义锚点——原语被删/改名后监听条目必须同步清理，否则哨兵在
        监听一个不存在的名字（静默空转，白名单活性牙防的同种腐化）。
        本轮红侧实证：scan_kubectl_mutation_attempted 退役时监听条目
        未删即红。"""
        defined = all_defined_names(SRC_ROOT)
        stale = sorted(set(WATCHED_PRIMITIVES) - defined)
        assert not stale, (
            "stale WATCHED_PRIMITIVES entries (name not defined anywhere "
            "in src — remove the entry, or restore/rename the primitive): "
            f"{stale}"
        )

    def test_watched_primitives_cover_every_teardown_armed_primitive(self):
        """完备性牙（R17/G-1）：message_scanning 里每个带 ``is_teardown``
        参数的模块级函数（词汇层自己的敏感性声明——刀1 给它穿参数
        本身就是承认在暴露面内）必须 ∈ WATCHED_PRIMITIVES ∪
        PRIMITIVE_EXCLUSIONS（后者逐条带实质理由）。监听集曾是手工
        枚举：第四个原语 scan_kubectl_mutation_attempted 曾无注释
        遗漏（零消费者所以无行为测试红，未来裸调缺省 None=RAW 证据
        不触哨兵＝第七门「未监听原语」形态）——R17 登记后，O-7 裁决
        其作为死代码整体退役（本牙的真树红侧即用该遗漏实证）。
        本牙从源头推导义务：下一个原语忘登记即红。"""
        src_path = (
            SRC_ROOT / "agent" / "providers" / "message_scanning.py"
        )
        armed = teardown_armed_module_functions(
            src_path.read_text(encoding="utf-8")
        )
        registered = set(WATCHED_PRIMITIVES) | set(PRIMITIVE_EXCLUSIONS)
        unregistered = sorted(armed - registered)
        assert not unregistered, (
            "teardown-armed scan primitives missing from "
            "WATCHED_PRIMITIVES (register the name, or add a reasoned "
            "PRIMITIVE_EXCLUSIONS entry): "
            f"{unregistered}"
        )
        thin = {
            k: r
            for k, r in PRIMITIVE_EXCLUSIONS.items()
            if len(r.strip()) < 40
        }
        assert not thin, (
            "PRIMITIVE_EXCLUSIONS entries need a substantive reason "
            f"(what exposure, why out of scope): {thin}"
        )

    def test_completeness_tooth_catches_fifth_primitive(self):
        """锋利度牙（合成探针，防牙自身空转绿）：合成一个带
        ``is_teardown`` 参数的新 scan 原语——提取器必须只收它（无关
        helper 不收、非模块级不收），且它不在监听/排除集。提取器
        失效 ⇒ armed 恒空 ⇒ 完备性牙永远绿；本牙钉住提取形态与差集
        形态两者（同哨兵合成违规牙的自测纪律）。"""
        synth = (
            "def scan_kubectl_mutation_next(\n"
            "    messages, write_subcommands, *, is_teardown=None,\n"
            "):\n"
            "    return False\n"
            "\n"
            "def unrelated_helper(messages):\n"
            "    return bool(messages)\n"
            "\n"
            "def outer(messages):\n"
            "    def nested(mm, *, is_teardown=None):\n"
            "        return False\n"
            "    return nested(messages)\n"
        )
        armed = teardown_armed_module_functions(synth)
        # 形态正确：只收模块级带参函数；helper 无参不收、嵌套函数
        # 非模块级不收（原语都以模块级名字被 import 消费）。
        assert armed == {"scan_kubectl_mutation_next"}
        # 差集形态：新名未登记 ⇒ 完备性牙的断言输入非空（真树上
        # 等价于红）。
        registered = set(WATCHED_PRIMITIVES) | set(PRIMITIVE_EXCLUSIONS)
        assert armed - registered == {"scan_kubectl_mutation_next"}

    def test_armed_primitive_callsites_thread_the_matcher(self):
        """F-14 主牙（round-32k 宽集压测）：每个 teardown-armed 模块级
        函数（推导集——R17/G-1 教训，非手工枚举）的 src 调用点必须在
        调用点上带 ``is_teardown`` 关键字。作用域共址（契约 D）管不了
        W1（marker 加载了但调用 RAW）与 W4（同 scope 第二个原语 RAW）
        形态——宽集压测实证四形态全静默通过；本牙以调用点粒度钉死
        这个子集。零例外（方法面刻意不进推导域，由 scope 哨兵 +
        family 牙分管，见 ``scan_raw_matcher_callsites`` 的边界声明）。"""
        armed = teardown_armed_tree(SRC_ROOT)
        # 推导活性锚：词汇层核心扫描器必须在推导集内——推导退化成
        # 空集会让主牙空转绿（同哨兵合成违规牙的自测纪律）。
        assert "scan_kubectl_mutation_index" in armed, (
            "teardown_armed_tree derivation went vacuous — the vocabulary "
            "layer's core scanner must stay in the derived armed set"
        )
        raw = scan_raw_matcher_callsites(SRC_ROOT)
        assert not raw, (
            "teardown-armed primitives called RAW (thread is_teardown= at "
            "the call site — scope co-location of a marker is not "
            "threading):\n"
            + "\n".join(
                f"  {f} :: {scope} -> {prim}()" for f, scope, prim in raw
            )
        )

    def test_callsite_tooth_catches_raw_forms(self, tmp_path):
        """F-14 锋利度牙（合成 tmp 树，防牙空转绿）：W1 形态（matcher
        构建了但 scan 调用 RAW——作用域哨兵实证管不了的形态）必须
        抓；W4 形态（同函数两个 armed 调用一穿一 RAW）必须只抓 RAW
        那个（调用点粒度，非作用域粒度）；穿线调用不抓；非 armed
        的同名方法调用不抓（推导域边界——方法不进推导集）。"""
        w1 = (
            "def scan_probe(messages, *, is_teardown=None):\n"
            "    return []\n"
            "\n"
            "def w1_door(messages, artifacts):\n"
            "    from ea import make_teardown_matcher\n"
            "    matcher = make_teardown_matcher(artifacts)\n"
            "    log(matcher)\n"
            "    return scan_probe(messages)\n"
        )
        w4 = (
            "def scan_probe(messages, *, is_teardown=None):\n"
            "    return []\n"
            "\n"
            "def scan_probe_two(messages, *, is_teardown=None):\n"
            "    return []\n"
            "\n"
            "def w4_door(messages, matcher):\n"
            "    a = scan_probe(messages, is_teardown=matcher)\n"
            "    b = scan_probe_two(messages)\n"
            "    return a, b\n"
        )
        threaded = (
            "def scan_probe(messages, *, is_teardown=None):\n"
            "    return []\n"
            "\n"
            "def ok_door(messages, matcher):\n"
            "    return scan_probe(messages, is_teardown=matcher)\n"
        )
        method_shape = (
            "class Detector:\n"
            "    def detect(self, messages):\n"
            "        return []\n"
            "\n"
            "def run(d):\n"
            "    return d.detect([])\n"
        )
        (tmp_path / "w1.py").write_text(w1, encoding="utf-8")
        (tmp_path / "w4.py").write_text(w4, encoding="utf-8")
        (tmp_path / "threaded.py").write_text(threaded, encoding="utf-8")
        (tmp_path / "method_shape.py").write_text(method_shape, encoding="utf-8")
        raw = scan_raw_matcher_callsites(tmp_path)
        assert sorted(raw) == [
            ("w1.py", "w1_door", "scan_probe"),
            ("w4.py", "w4_door", "scan_probe_two"),
        ]

    def test_delete_predicate_callsites_thread_v_args(self):
        """P-9 主牙（round-32p 探针矩阵实证）：teardown 判定的底层谓词
        ``is_vehicle_teardown_delete`` 在 src 的每个调用点必须显式传
        ``v_args=``。不传时 keyword 空串默认值静默兜底——分类器 names
        锚会丢掉空间批的后续成员（``pod rc-a victim`` 只锚 ``rc-a``），
        混合批（注册载体 + 未注册受害者）的删除被误豁免为 teardown，
        teardown≠mutation 边界静默泄漏：无报错、无红测，安静失败
        （与今晨签名漏改的响亮失败恰成互补对——后者当场炸当场修，
        前者什么都不发生只是判定悄悄变错）。"""
        sites = scan_delete_predicate_callsites(SRC_ROOT)
        # 活性锚：谓词必须仍被 src 调用（改名/搬迁/全部改走 getattr
        # 会让本牙空转绿——同 F-14 主牙的推导活性锚纪律）。
        assert sites, (
            "is_vehicle_teardown_delete is no longer CALLED from src — "
            "the P-9 tooth has gone vacuous; re-anchor it to the "
            "predicate's new name or dispatch shape"
        )
        raw = [(f, scope) for f, scope, threaded in sites if not threaded]
        assert not raw, (
            "is_vehicle_teardown_delete called WITHOUT v_args= (the "
            "classifier names anchor drops space-batch members — a mixed "
            "batch then silently exempts an unregistered victim's delete "
            "as teardown):\n"
            + "\n".join(f"  {f} :: {scope}" for f, scope in raw)
        )

    def test_v_args_tooth_catches_raw_forms(self, tmp_path):
        """P-9 锋利度牙（合成 tmp 树，防空转绿）：裸两参调用必抓；
        穿线调用不抓；模块属性形态的 RAW 调用（未来调用者
        ``ea.is_vehicle_teardown_delete(...)`` 忘传）也抓；谓词自身的
        def 行不是 Call 节点不误报；穿线调用被记录为 threaded
        （scanner 全量返回不止 raw——主牙活性锚依赖这个面）。"""
        raw_shape = (
            "def is_vehicle_teardown_delete(effective, artifacts, *, v_args=''):\n"
            "    return False\n"
            "\n"
            "def raw_door(effective, artifacts):\n"
            "    return is_vehicle_teardown_delete(effective, artifacts)\n"
        )
        threaded_shape = (
            "def is_vehicle_teardown_delete(effective, artifacts, *, v_args=''):\n"
            "    return False\n"
            "\n"
            "def ok_door(effective, artifacts, cmd):\n"
            "    return is_vehicle_teardown_delete(\n"
            "        effective, artifacts, v_args=cmd,\n"
            "    )\n"
        )
        attr_shape = (
            "import execution_artifacts as ea\n"
            "\n"
            "def attr_door(effective, artifacts):\n"
            "    return ea.is_vehicle_teardown_delete(effective, artifacts)\n"
        )
        (tmp_path / "raw.py").write_text(raw_shape, encoding="utf-8")
        (tmp_path / "threaded.py").write_text(threaded_shape, encoding="utf-8")
        (tmp_path / "attr_raw.py").write_text(attr_shape, encoding="utf-8")
        sites = scan_delete_predicate_callsites(tmp_path)
        raw = sorted(
            (f, scope) for f, scope, threaded in sites if not threaded
        )
        assert raw == [
            ("attr_raw.py", "attr_door"),
            ("raw.py", "raw_door"),
        ]
        assert ("threaded.py", "ok_door", True) in sites

    def test_sentinel_catches_synthetic_violation(self):
        """哨兵自测牙（锋利度）：合成的违规函数（加载词汇原语、无豁免）
        必须被抓——证明哨兵不是空转绿。"""
        synthetic = (
            "def new_door(messages):\n"
            "    return scan_kubectl_mutation_index(\n"
            "        messages, KUBECTL_WRITE_SUBCOMMANDS\n"
            "    )\n"
        )
        violations = scan_source(synthetic, "synthetic.py")
        assert violations == [
            ("synthetic.py", "new_door", ("KUBECTL_WRITE_SUBCOMMANDS", "scan_kubectl_mutation_index"))
        ]

    def test_sentinel_passes_exempted_and_guarded_forms(self):
        """哨兵自测牙（无误报）：豁免形态（P3 matcher 穿线 / predicate）
        与已装豁免的受控入口（guarded entry）调用方都不报——豁免可经
        入口间接生效，不能逼着调用方重复装。"""
        src_ok = (
            "def matcher_door(messages, artifacts):\n"
            "    from x import make_teardown_matcher\n"
            "    return scan_kubectl_mutation_index(\n"
            "        messages, KUBECTL_WRITE_SUBCOMMANDS,\n"
            "        is_teardown=make_teardown_matcher(artifacts),\n"
            "    )\n"
            "\n"
            "def guarded_entry_caller(state, messages, provider):\n"
            "    from x import _issue_disproven_in_epoch\n"
            "    return _issue_disproven_in_epoch(state, messages, provider)\n"
            "\n"
            "def predicate_door(effective, artifacts):\n"
            "    from x import is_vehicle_teardown_delete\n"
            "    return scan_kubectl_mutation_index(\n"
            "        effective, KUBECTL_WRITE_SUBCOMMANDS,\n"
            "    ) or is_vehicle_teardown_delete(effective, artifacts)\n"
        )
        assert scan_source(src_ok, "ok.py") == []

    def test_alias_import_violation_is_caught(self):
        """别名逃逸牙（R12-1）：``from x import scan_... as _s`` 后函数
        体内只加载别名——原名从未以 Load 形态出现，名称扫描曾静默放行
        （自然编码形态：长名缩写/避免冲突，无恶意也能触发，违背
        「提交即红」承诺）。别名必须反查回原名再判命中；函数内与
        模块级两种 as 导入形态都要覆盖。"""
        func_level = (
            "def aliased_door(messages):\n"
            "    from x import scan_kubectl_mutation_index as _scan\n"
            "    return _scan(messages, frozenset())\n"
        )
        module_level = (
            "from x import KUBECTL_WRITE_SUBCOMMANDS as _verbs\n"
            "\n"
            "def module_alias_door(messages):\n"
            "    return bool(_verbs)\n"
        )
        for src in (func_level, module_level):
            violations = scan_source(src, "alias.py")
            assert len(violations) == 1
            assert violations[0][1] in (
                "aliased_door",
                "module_alias_door",
            )

    def test_alias_import_exemption_is_resolved(self):
        """别名豁免牙（对称性）：``from y import make_teardown_matcher
        as _m`` 的豁免标记也必须经别名反查解析——豁免与监听同用一
        套解析，否则别名形态的合规消费者被误报（豁免解析不能落后
        于监听解析）。"""
        src = (
            "def aliased_exempt_door(messages, artifacts):\n"
            "    from x import scan_kubectl_mutation_index\n"
            "    from y import make_teardown_matcher as _m\n"
            "    return scan_kubectl_mutation_index(\n"
            "        messages, frozenset(),\n"
            "        is_teardown=_m(artifacts),\n"
            "    )\n"
        )
        assert scan_source(src, "alias_ok.py") == []

    def test_alias_collision_across_scopes_is_caught(self):
        """同名冲突牙（R14-1）：两个合法作用域各自用同名别名 ``_s``
        指向不同源（一个受监原语、一个无关函数）——tree 级 dict 后写
        覆盖先写会把受监映射丢弃（MISSED），且写入顺序决定方向（反序
        则无关函数被误报）。别名解析必须作用域感知：每个函数只解析
        自己作用域 + 模块级的别名，跨作用域同名互不污染。"""
        src = (
            "def watched_door(m):\n"
            "    from msg import scan_kubectl_mutation_index as _s\n"
            "    return _s(m, frozenset())\n"
            "\n"
            "def other_door(m):\n"
            "    from util import helper as _s\n"
            "    return _s(m)\n"
        )
        violations = scan_source(src, "collision.py")
        assert [v[1] for v in violations] == ["watched_door"]

    def test_alias_shadowing_module_import(self):
        """遮蔽语义牙（R14-1 对称面）：函数级别名遮蔽同品模块级别名
        （Python 遮蔽语义）——遮蔽函数内的 ``_s`` 实际是无关联的
        helper，不得误报；无函数级别名的方法用模块级 ``_s``（受监
        原语）照常必抓。函数内同名双 import 的后写遮蔽反映真实运行
        语义，非逃逸。"""
        src = (
            "from msg import scan_kubectl_mutation_index as _s\n"
            "\n"
            "def shadowing_door(m):\n"
            "    from util import helper as _s\n"
            "    return _s(m)\n"
            "\n"
            "def module_alias_door(m):\n"
            "    return _s(m, frozenset())\n"
        )
        violations = scan_source(src, "shadow.py")
        assert [v[1] for v in violations] == ["module_alias_door"]

    def test_block_import_alias_at_module_scope_is_caught(self):
        """F-13 牙（round-32h）：模块级 try-import 别名（可选依赖降级的
        自然形态——威胁模型内）+ 函数消费。旧 ``_direct_body_aliases``
        只收 body 顶层 ImportFrom，try 块内的别名在 enclosing-chain 侧
        全丢；函数自身子树地图救不了模块级 → 逃逸。剪枝遍历（含块、
        跳过嵌套作用域）后必须抓。"""
        src = (
            "try:\n"
            "    from msg import scan_kubectl_mutation_index as _s\n"
            "except ImportError:\n"
            "    _s = None\n"
            "\n"
            "def try_door(m):\n"
            "    return _s(m, frozenset())\n"
        )
        violations = scan_source(src, "mod_try.py")
        assert [v[1] for v in violations] == ["try_door"]

    def test_block_import_alias_in_class_body_is_caught(self):
        """F-13 对称牙：类体 If 块内 import 别名 + 方法消费——
        ClassDef unit 的 chain 条目同样曾只看顶层。修复后方法
        解析到块内别名，照常必抓（豁免由标记括得，不由块遮蔽）。"""
        src = (
            "class Door:\n"
            "    if True:\n"
            "        from msg import scan_kubectl_mutation_index as _s\n"
            "    def method(self, m):\n"
            "        return self._s(m, frozenset())\n"
        )
        violations = scan_source(src, "cls_if.py")
        assert [v[1] for v in violations] == ["method"]

    def test_nested_function_sees_outer_block_import_alias(self):
        """F-13 嵌套链牙：嵌套函数消费外层函数 **try 块内** 的 import
        别名——旧实现下 outer 自身被自身子树地图救回，但 inner 的
        enclosing chain 里 outer 条目只看顶层 → inner 逃逸。修复后
        chain 条目含块内，inner 照常必抓（outer 同时报出是既有粒度
        行为：函数 unit 的 load 扫描含嵌套体，errs toward reporting）。"""
        src = (
            "def outer(m):\n"
            "    try:\n"
            "        from msg import scan_kubectl_mutation_index as _s\n"
            "    except ImportError:\n"
            "        return None\n"
            "    def inner(mm):\n"
            "        return _s(mm, frozenset())\n"
            "    return inner(m)\n"
        )
        violations = scan_source(src, "nested.py")
        assert [v[1] for v in violations] == ["outer", "inner"]

    def test_same_name_alias_shadowing_follows_execution_order(self):
        """F-13 第二段牙（round-32i）：函数内同名双 import——块在前无关源
        + 顶层在后 watched 源。Python 执行序下顶层后写遮蔽，``_s`` 实际
        绑定 watched 源，必须报；旧 ``_scope_alias_map`` 的 BFS 收集让
        深层块内后入覆盖浅层顶层（层序≠执行序），解析成无关源 →
        假阴性。前序遍历后同名遮蔽按源码执行序决定胜者。"""
        src = (
            "def mixed_door(m):\n"
            "    if True:\n"
            "        from util import helper as _s\n"
            "    from msg import scan_kubectl_mutation_index as _s\n"
            "    return _s(m, frozenset())\n"
        )
        violations = scan_source(src, "mixed.py")
        assert [v[1] for v in violations] == ["mixed_door"]

    def test_same_name_alias_reversed_order_correctly_silent(self):
        """F-13 第二段对照牙：正序形态（顶层 watched 在前 + 块内无关在
        后）——执行序下无关源后写遮蔽，``_s`` 实际绑定无关源，不报是
        正确语义而非漏报。防前序化把这类合法形态误伤（假阳性对照）。"""
        src = (
            "def mixed_door2(m):\n"
            "    from msg import scan_kubectl_mutation_index as _s\n"
            "    if True:\n"
            "        from util import helper as _s\n"
            "    return _s(m, frozenset())\n"
        )
        assert scan_source(src, "mixed2.py") == []

    def test_step_credit_entry_is_watched_not_guarded(self):
        """F-11 牙（P3 形态）：step-credit 入口的过滤是源内但「有条件」
        ——``is_teardown`` 参数默认 None = RAW credit，未穿 matcher 的
        调用方拿到的正是 O-3 缺陷形态。因此入口继续被 WATCH（guarded
        entry 必须无条件在体内应用豁免，有条件的不算）：裸调用方必须
        红；穿 matcher 的合法调用方绿。F-12 原计划把入口整体搬回
        EXEMPTION_MARKERS——那会重开 F-11 的假阴性窗（忘穿 matcher 的
        第七门静默绿），本牙钉住这个不搬家的裁决。"""
        ghost = (
            "def seventh_door_step_credit(skill_case, messages, method):\n"
            "    from chaos_agent.agent.nodes.execute._injection_detection import (\n"
            "        build_injection_step_selfcheck,\n"
            "    )\n"
            "    return build_injection_step_selfcheck(\n"
            "        skill_case, messages, method\n"
            "    )\n"
        )
        violations = scan_source(ghost, "ghost.py")
        assert violations == [
            (
                "ghost.py",
                "seventh_door_step_credit",
                ("build_injection_step_selfcheck",),
            )
        ]
        legit = (
            "def wired_step_check(state, result):\n"
            "    from x import make_teardown_matcher\n"
            "    from y import build_injection_step_selfcheck\n"
            "    return build_injection_step_selfcheck(\n"
            "        'case',\n"
            "        state.get('messages', []) + result.get('messages', []),\n"
            "        'kubectl_native',\n"
            "        is_teardown=make_teardown_matcher(\n"
            "            state.get('execution_artifacts') or []\n"
            "        ),\n"
            "    )\n"
        )
        assert scan_source(legit, "legit.py") == []

    def test_matcher_threading_is_the_p3_legit_form(self):
        """新 P3 原语牙：豁免的合法形态收敛为「穿 matcher」——同一个
        词汇消费者，穿 ``make_teardown_matcher`` 绿，裸用红；``is_teardown``
        参数引用（穿线位）同样计入豁免标记。钉住哨兵对新合法形态的
        识别，防止未来把 marker 集改窄时默默放开第七门。"""
        bare = (
            "def raw_consumer(messages):\n"
            "    from msg import scan_kubectl_mutation_index\n"
            "    from v import KUBECTL_WRITE_SUBCOMMANDS\n"
            "    return scan_kubectl_mutation_index(\n"
            "        messages, KUBECTL_WRITE_SUBCOMMANDS\n"
            "    )\n"
        )
        assert [v[1] for v in scan_source(bare, "bare.py")] == ["raw_consumer"]

        threaded = (
            "def matcher_consumer(messages, artifacts):\n"
            "    from msg import scan_kubectl_mutation_index\n"
            "    from v import KUBECTL_WRITE_SUBCOMMANDS\n"
            "    from ea import make_teardown_matcher\n"
            "    return scan_kubectl_mutation_index(\n"
            "        messages, KUBECTL_WRITE_SUBCOMMANDS,\n"
            "        is_teardown=make_teardown_matcher(artifacts),\n"
            "    )\n"
        )
        assert scan_source(threaded, "threaded.py") == []

        param_pass = (
            "def passthrough(messages, *, is_teardown=None):\n"
            "    from msg import scan_kubectl_mutation_index\n"
            "    from v import KUBECTL_WRITE_SUBCOMMANDS\n"
            "    return scan_kubectl_mutation_index(\n"
            "        messages, KUBECTL_WRITE_SUBCOMMANDS,\n"
            "        is_teardown=is_teardown,\n"
            "    )\n"
        )
        assert scan_source(param_pass, "passthrough.py") == []

    def test_retired_filter_names_no_longer_exempt(self):
        """退役活性牙：消息级过滤的两个名字（_epoch_scan_window /
        _strip_pure_teardown_messages）已从豁免集退役——依赖退役名
        抵扣豁免的合成消费方必须重新变红。防止豁免集里残留死名字
        （同白名单活性纪律：死条目会掩盖新消费点）。"""
        stale = (
            "def stale_exempt_consumer(messages, state):\n"
            "    from msg import scan_kubectl_mutation_index\n"
            "    from v import KUBECTL_WRITE_SUBCOMMANDS\n"
            "    from loop import _epoch_scan_window\n"
            "    return scan_kubectl_mutation_index(\n"
            "        _epoch_scan_window(messages, state),\n"
            "        KUBECTL_WRITE_SUBCOMMANDS,\n"
            "    )\n"
        )
        assert [v[1] for v in scan_source(stale, "stale.py")] == [
            "stale_exempt_consumer"
        ]

    def test_module_and_class_level_consumption_is_caught(self):
        """C/C2 牙：扫描域覆盖模块顶层与类体（原只扫函数体）——顶层/
        类体消费词汇原语同样必须带豁免或登记。合成两种形态必须转
        红，消费函数本体不因 boot 值间接引用而豁免。"""
        top_level = (
            "from x import (\n"
            "    KUBECTL_WRITE_SUBCOMMANDS, scan_kubectl_mutation_index,\n"
            ")\n"
            "_BOOT_INDEX = scan_kubectl_mutation_index(\n"
            "    [], KUBECTL_WRITE_SUBCOMMANDS\n"
            ")\n"
            "def later(messages):\n"
            "    return _BOOT_INDEX + len(messages)\n"
        )
        violations = scan_source(top_level, "top.py")
        assert violations == [
            (
                "top.py",
                "<module>",
                (
                    "KUBECTL_WRITE_SUBCOMMANDS",
                    "scan_kubectl_mutation_index",
                ),
            )
        ]
        class_body = (
            "from x import (\n"
            "    KUBECTL_WRITE_SUBCOMMANDS, scan_kubectl_mutation_index,\n"
            ")\n"
            "class Registry:\n"
            "    BASE = scan_kubectl_mutation_index(\n"
            "        [], KUBECTL_WRITE_SUBCOMMANDS\n"
            "    )\n"
            "    def method(self, m):\n"
            "        return self.BASE\n"
        )
        violations = scan_source(class_body, "cls.py")
        assert violations == [
            (
                "cls.py",
                "<class:Registry>",
                (
                    "KUBECTL_WRITE_SUBCOMMANDS",
                    "scan_kubectl_mutation_index",
                ),
            )
        ]
