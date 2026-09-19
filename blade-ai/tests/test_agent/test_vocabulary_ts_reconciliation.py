"""Python/TS vocabulary reconciliation (B76 round-15 D7, jurisdiction
extended to the TUI in round-16 S1).

The web TS layer hand-writes branch words against Python-legislated
closed sets (verdicts, layer statuses, checklist statuses, task_state).
Nothing can stop TS from drifting — Python renames a word and the TS
branch silently dies into its default arm. These tests parse the TS
sources and assert every consumed word is still a member of the Python
legislation, so a rename fails CI on the Python side instead of failing
SILENTLY on the web side.

Round-16 S1 extended the jurisdiction from web/src to tui/src as well:
the TUI boot card's PENDING_STATES was a hand-copy of the SQL active
set that had ALREADY drifted (two words vs three — 'unverified' tasks
were silently hidden while the SQL backends kept returning them as
live-fault rows). The set is now pinned exactly against the
TASK_STATE_ACTIVE_VALUES legislation.

Parsers are deliberately naive (segment-scoped regex over the TSX text):
the extraction shape is pinned by the assertions themselves — if a TS
refactor changes the shape (e.g. words move to a lookup table), the test
fails loudly and the parser gets updated with eyes on the diff.
"""

from __future__ import annotations

import re
from pathlib import Path

from chaos_agent.agent.result.verdict import (
    CHECKLIST_STATUS_VALUES,
    INJECT_VERDICT_VALUES,
    LAYER1_STATUS_VALUES,
    LAYER2_STATUS_VALUES,
    RECOVER_VERDICT_VALUES,
)
from chaos_agent.agent.state import (
    TASK_STATE_ACTIVE_VALUES,
    TASK_STATE_CLEARED_VALUES,
    TASK_STATE_COLUMN_VALUES,
    TASK_STATE_OVERLAY_VALUES,
    TASK_STATE_VALUES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "web" / "src"
TRACE_PAGE = WEB_ROOT / "app" / "TracePage.tsx"
INFO_CARDS = WEB_ROOT / "components" / "chat" / "InfoCards.tsx"
TUI_ROOT = REPO_ROOT / "tui" / "src"
BOOT_CARDS = TUI_ROOT / "components" / "boot" / "bootCards.ts"
PENDING_CARD = TUI_ROOT / "components" / "boot" / "PendingTasksCard.tsx"
CORE_I18N = REPO_ROOT / "core" / "src" / "i18n"
SQLITE_STORE = REPO_ROOT / "src" / "chaos_agent" / "persistence" / "task_store_sqlite.py"
PG_STORE = REPO_ROOT / "src" / "chaos_agent" / "persistence" / "task_store_postgresql.py"
TASK_STORE = REPO_ROOT / "src" / "chaos_agent" / "persistence" / "task_store.py"


def _segment(text: str, start_marker: str) -> str:
    """Text from ``start_marker`` to the next top-level ``function``/``}``
    boundary — a bounded window so word extraction stays local."""
    seg = text.split(start_marker, 1)[1]
    nxt = re.search(r"\nfunction |\nconst STATE_FALLBACK", seg)
    return seg[: nxt.start()] if nxt else seg


def _case_words(segment: str) -> set[str]:
    return set(re.findall(r'case "([a-z_]+)":', segment))


def _eq_words(segment: str) -> set[str]:
    """Words on either side of a ``===`` comparison in a TS segment."""
    return {
        w
        for pair in re.findall(r'"([a-z_]+)"\s*===|===\s*"([a-z_]+)"', segment)
        for w in pair
        if w
    }


class TestTracePageReconciliation:
    def test_holistic_status_inputs_are_legislated_verdict_words(self):
        text = TRACE_PAGE.read_text(encoding="utf-8")
        words = _eq_words(_segment(text, "function holisticStatus"))
        assert words, "holisticStatus parser found no comparison words — shape drifted"
        assert words <= (INJECT_VERDICT_VALUES | RECOVER_VERDICT_VALUES), (
            f"TS holisticStatus consumes non-legislated verdict words: "
            f"{sorted(words - (INJECT_VERDICT_VALUES | RECOVER_VERDICT_VALUES))}"
        )

    def test_layer_status_color_cases_are_legislated_layer_words(self):
        text = TRACE_PAGE.read_text(encoding="utf-8")
        seg = _segment(text, "function layerStatusColor")
        words = _case_words(seg)
        assert words <= (LAYER1_STATUS_VALUES | LAYER2_STATUS_VALUES), (
            f"TS layerStatusColor cases outside layer legislation: "
            f"{sorted(words - (LAYER1_STATUS_VALUES | LAYER2_STATUS_VALUES))}"
        )

    def test_checklist_glyph_cases_are_legislated_checklist_words(self):
        text = TRACE_PAGE.read_text(encoding="utf-8")
        seg = _segment(text, "function checklistGlyph")
        words = _case_words(seg)
        assert words <= CHECKLIST_STATUS_VALUES, (
            f"TS checklistGlyph cases outside checklist legislation: "
            f"{sorted(words - CHECKLIST_STATUS_VALUES)}"
        )


class TestInfoCardsReconciliation:
    # STATE_VISUALS mixes THREE domains: task_state lifecycle words
    # (TaskState legislation), persistence-layer overlay words
    # (TaskStateOverlay — produced by TaskStore._infer_fields, NOT by
    # the TUI; round-17 reclassified from "TUI session words"), and
    # legacy display-only aliases (running / interrupted /
    # pending_confirmation — display-layer words with no Python
    # producer today, retained for legacy rows). Every key must belong
    # to one of the three; an unknown key means a new word arrived and
    # needs classification.
    TUI_SESSION_STATE_WORDS = frozenset({
        "pending_confirmation", "running", "interrupted",
    })

    # The task_state words the map carries a dedicated visual for.
    # 'unverified' joined round-17 C1 (previously STATE_FALLBACK — a
    # conscious round-15 decision reversed: the fault is suspected
    # LIVE, so it wears the warn family, not the gray neutral dot).
    # Pinned so a rename on EITHER side trips the test.
    PINNED_TASK_STATE_KEYS = frozenset({
        "injecting", "recovering", "injected", "partial_recovered",
        "cancelled", "recovered", "completed", "failed", "rejected",
        "unverified",
    })

    @classmethod
    def _state_visual_keys(cls) -> set[str]:
        text = INFO_CARDS.read_text(encoding="utf-8")
        seg = text.split("const STATE_VISUALS", 1)[1]
        seg = seg.split("const STATE_FALLBACK", 1)[0]
        return set(re.findall(r'^\s{2}([a-z_]+):', seg, re.MULTILINE))

    def test_state_visual_keys_are_classified_words(self):
        keys = self._state_visual_keys()
        unclassified = (
            keys
            - TASK_STATE_VALUES
            - TASK_STATE_OVERLAY_VALUES
            - self.TUI_SESSION_STATE_WORDS
        )
        assert not unclassified, (
            f"STATE_VISUALS keys matching no known domain: "
            f"{sorted(unclassified)} — classify (task_state lifecycle / "
            "persistence overlay / display alias) or fix the word"
        )

    def test_state_visual_task_state_keys_pinned(self):
        keys = self._state_visual_keys()
        task_state_keys = keys & TASK_STATE_VALUES
        assert task_state_keys == self.PINNED_TASK_STATE_KEYS, (
            "STATE_VISUALS task_state coverage drifted — if intentional, "
            "update PINNED_TASK_STATE_KEYS with the reason (e.g. a new "
            "dedicated visual for 'unverified')"
        )


class TestBootCardsReconciliation:
    """Round-16 S1 pinned the boot card's PENDING_STATES word copy against
    TASK_STATE_ACTIVE_VALUES after it had ALREADY drifted. Round-32 retired
    the word-guessing form entirely: the pending list now filters on the
    materialised liability verdict (``liability_live`` — the same column
    select_active_tasks keys on, exposed on every list row by
    get_all_metrics). These pins keep the new evidence-backed form
    structural: the TS filter key, the SQL predicate, and the server
    serialization must all keep speaking the verdict column — a revert to
    word-set filtering on ANY of the three faces re-opens the
    silent-hide-of-live-faults failure mode (K1/K2 class)."""

    def test_boot_card_filters_on_liability_verdict(self):
        text = BOOT_CARDS.read_text(encoding="utf-8")
        # The evidence-backed filter face.
        assert 'tt["liability_live"] === true' in text, (
            "boot card pending filter no longer keys on liability_live — "
            "word-set filtering re-introduces the round-16 S1 / round-32 "
            "silent-hide family"
        )
        # The retired word-copy face: no task_state Set literal may come
        # back as a pending filter.
        assert not re.search(
            r"new Set\(\[\s*\"injecting\"", text
        ), (
            "a task_state word-set filter returned to bootCards.ts — the "
            "pending list must key on the liability_live verdict column, "
            "not on lifecycle words"
        )

    @staticmethod
    def _select_active_body(source_path: Path) -> str:
        text = source_path.read_text(encoding="utf-8")
        seg = text.split("async def select_active_tasks", 1)[1]
        return seg.split("async def ", 1)[0]

    def test_sql_backends_key_on_liability_column(self):
        """Both SQL backends' select_active_tasks must key on the
        materialised verdict column (the round-32 successor of the
        round-16 S1 single-source pin: the IN-set render was itself a
        word-guess, retired with the predicate it served)."""
        for source_path in (SQLITE_STORE, PG_STORE):
            body = self._select_active_body(source_path)
            assert "liability_live" in body, (
                f"{source_path.name}: select_active_tasks no longer keys on "
                "the liability_live verdict column"
            )
            assert not re.search(
                r"task_state IN \(?'[a-z_]+'", body
            ), (
                f"{source_path.name}: select_active_tasks contains a "
                "hand-copied task_state IN literal — the recoverable set "
                "must not regress to word-guessing"
            )

    def test_server_list_serialization_exposes_liability_verdict(self):
        """get_all_metrics is the server's list-row serializer — the boot
        card can only filter on what this face exposes."""
        text = TASK_STORE.read_text(encoding="utf-8")
        seg = text.split("async def get_all_metrics", 1)[1]
        seg = seg.split("async def ", 1)[0]
        assert '"liability_live"' in seg, (
            "get_all_metrics no longer exposes liability_live on list "
            "rows — the TUI pending filter would read undefined and the "
            "card would silently go empty (the display-side twin of the "
            "SQL predicate retiring)"
        )


class TestPendingTasksCardReconciliation:
    """Round-17 C2: the TUI PendingTasksCard STATE_VISUALS map (13→14
    keys) was the SECOND unreconciled form inside the round-16 extended
    jurisdiction — round-16 pinned exactly one form in one file
    (bootCards PENDING_STATES) while this map, whose web twin claims to
    mirror it, had zero tests. These pins make the mirror claim a
    structural guarantee: key-set equality + per-domain classification
    + every active-set word carrying a dedicated visual (no silent
    gray-hole for live-fault-suspect rows — the round-17 C1 defect)."""

    # Display-only aliases in the maps with no Python producer today
    # (legacy rows / server lifecycle overlays): same classification
    # family as the web side's TUI_SESSION_STATE_WORDS.
    DISPLAY_ALIAS_WORDS = frozenset({
        "pending_confirmation", "running", "interrupted",
    })

    @staticmethod
    def _tui_visual_keys() -> set[str]:
        text = PENDING_CARD.read_text(encoding="utf-8")
        seg = text.split("const STATE_VISUALS", 1)[1].split("const FALLBACK", 1)[0]
        return set(re.findall(r"^\s{2}([a-z_]+):", seg, re.MULTILINE))

    @staticmethod
    def _web_visual_keys() -> set[str]:
        text = INFO_CARDS.read_text(encoding="utf-8")
        seg = text.split("const STATE_VISUALS", 1)[1].split("const STATE_FALLBACK", 1)[0]
        return set(re.findall(r"^\s{2}([a-z_]+):", seg, re.MULTILINE))

    def test_tui_visual_keys_are_classified_words(self):
        keys = self._tui_visual_keys()
        unclassified = (
            keys
            - TASK_STATE_VALUES
            - TASK_STATE_OVERLAY_VALUES
            - self.DISPLAY_ALIAS_WORDS
        )
        assert not unclassified, (
            f"tui STATE_VISUALS keys matching no known domain: "
            f"{sorted(unclassified)} — classify (task_state lifecycle / "
            "persistence overlay / display alias) or fix the word"
        )

    def test_tui_and_web_visual_key_sets_identical(self):
        """The web map's own comment claims "mirrored from the TUI's" —
        pin the claim: both maps carry exactly the same key set, so a
        word added on one side fails CI instead of silently drifting
        (round-16 S1 caught exactly this pre-drift state between the
        SQL set and the TUI filter)."""
        tui_keys = self._tui_visual_keys()
        web_keys = self._web_visual_keys()
        assert tui_keys == web_keys, (
            f"STATE_VISUALS key sets drifted — tui-only: "
            f"{sorted(tui_keys - web_keys)}, web-only: "
            f"{sorted(web_keys - tui_keys)}; update BOTH maps together "
            "(they are pinned key-identical)"
        )

    def test_card_reachable_words_carry_dedicated_visuals(self):
        """Round-32 widened the card's reachable word set: rows surface via
        the liability_live verdict, and a verdict-live row may carry ANY
        non-cleared lifecycle word — 'recovering' (K1), 'failed'-with-
        experiment (K2), 'partial_recovered' included. Every such word
        must have a dedicated visual; falling through FALLBACK is the
        gray-hole defect (round-17 C1's shape, on the widened set)."""
        keys = self._tui_visual_keys()
        reachable = TASK_STATE_VALUES - TASK_STATE_CLEARED_VALUES
        missing = reachable - keys
        assert not missing, (
            f"card-reachable words (non-cleared lifecycle words — any of "
            f"them can ride a liability_live row onto the card) without a "
            f"dedicated visual: {sorted(missing)} — these rows would render "
            "through the gray FALLBACK"
        )

    def test_cleared_archival_set_still_legislated(self):
        """The ACTIVE word set stays a legislated archive constant even
        after its runtime retirement — and the CLEARED mirror must stay a
        strict subset of TERMINAL (its members are verdict-terminal AND
        liability-proven; the four verdict-terminal-but-not-clearing
        words failed/partial_recovered/unverified/recovering are NOT
        members — that distinction IS the round-32 root-cause fix)."""
        assert TASK_STATE_ACTIVE_VALUES == frozenset({
            "injecting", "injected", "unverified",
        }), (
            "TASK_STATE_ACTIVE_VALUES drifted from its archival shape — "
            "it is retired from runtime duty; changing it needs the "
            "archival note updated too"
        )
        from chaos_agent.agent.state import TASK_STATE_TERMINAL_VALUES

        assert TASK_STATE_CLEARED_VALUES <= TASK_STATE_TERMINAL_VALUES, (
            "TASK_STATE_CLEARED_VALUES leaked a non-terminal word — a "
            "cleared word must carry a final verdict"
        )
        assert not (TASK_STATE_CLEARED_VALUES & {"failed", "partial_recovered", "unverified"}), (
            "verdict-terminal-but-not-clearing words entered the CLEARED "
            "set — that re-opens the round-32 K1/K2 blindness at the "
            "legacy-fallback branch"
        )

    def test_overlay_words_present_in_both_maps(self):
        """The persistence overlay words (waiting_input et al.) are
        REAL task_state column values (round-17 D4 legislation) — both
        maps must keep carrying them (removing one breaks the TUI
        crash-recovery display and the web mirror simultaneously)."""
        tui_keys = self._tui_visual_keys()
        missing_overlay = TASK_STATE_OVERLAY_VALUES - tui_keys
        assert not missing_overlay, (
            f"persistence overlay words missing from tui STATE_VISUALS: "
            f"{sorted(missing_overlay)} — they are legislated column "
            "values (TaskStateOverlay), not optional display words"
        )


class TestLiabilityGroupReconciliation:
    """Round-32b P3 — the boot card's three-group split, reconciled.

    The split (in_flight / needs_recovery / uncleared) is legislated
    Python-side (``liability_group_for`` off the CLEARED / TERMINAL
    word tables) and reaches the TUI as a serialized field
    (``liability_group`` on list rows → ``group`` on card rows). The
    TS layer must stay a PASSTHROUGH: bucket by the arrived field,
    never re-derive membership from task_state words — a TS-side
    word→group map would be the PENDING_STATES drift family reborn
    (round-16 S1 / round-32 BC). These pins keep the three faces
    structural: the serializer exposes the field, the fetcher passes
    it through, and the renderer buckets on it."""

    GROUP_WORDS = frozenset({"in_flight", "needs_recovery", "uncleared"})

    def test_serializer_exposes_the_group_field(self):
        """get_all_metrics is the only face the TUI can see — without
        ``liability_group`` on the row the card reads undefined on
        every live row and the split silently vanishes (flat card)."""
        text = TASK_STORE.read_text(encoding="utf-8")
        seg = text.split("async def get_all_metrics", 1)[1]
        seg = seg.split("async def ", 1)[0]
        assert '"liability_group"' in seg, (
            "get_all_metrics no longer exposes liability_group on list "
            "rows — the TUI three-group split would silently flatten"
        )

    def test_fetcher_passes_the_field_through(self):
        """bootCards.ts maps ``liability_group`` verbatim — any word
        consumption here (re-deriving the group from task_state) would
        be a TS word-copy of the Python legislation."""
        text = BOOT_CARDS.read_text(encoding="utf-8")
        assert 'tt["liability_group"]' in text, (
            "bootCards.ts no longer passes liability_group through — the "
            "group must arrive from the server, never be derived TS-side"
        )
        # Call-shape only: doc comments may legitimately NAME the
        # Python legislator; the violation is invoking it TS-side.
        assert "liability_group_for(" not in text, (
            "a Python-side group derivation leaked into the TS layer — "
            "membership is server-legislated"
        )

    def test_renderer_buckets_on_the_arrived_field(self):
        """PendingTasksCard buckets strictly by ``row.group`` — the
        no-word-copy invariant, pinned on the bucketing expression
        itself."""
        text = PENDING_CARD.read_text(encoding="utf-8")
        assert "buckets[row.group]" in text, (
            "PendingTasksCard no longer buckets by row.group — group "
            "membership must arrive on the row, not be re-derived"
        )
        # Call-shape only: doc comments may legitimately NAME the
        # Python legislator; the violation is invoking it TS-side.
        assert "liability_group_for(" not in text

    def test_ts_group_vocabulary_matches_python_legislation(self):
        """The TS GROUP_ORDER literal and the Python ``liability_group_for``
        return domain must be the same three words — pinned on BOTH
        the extracted literal and the full-word-domain behaviour."""
        from chaos_agent.agent.state import (
            TASK_STATE_COLUMN_VALUES,
            liability_group_for,
        )

        text = PENDING_CARD.read_text(encoding="utf-8")
        seg = text.split("const GROUP_ORDER", 1)[1].split("]", 1)[0]
        ts_words = set(re.findall(r'"([a-z_]+)"', seg))
        assert ts_words == self.GROUP_WORDS, (
            f"TS GROUP_ORDER drifted: {sorted(ts_words)} vs "
            f"{sorted(self.GROUP_WORDS)} — update BOTH sides together"
        )

        # The Python legislation's whole reachable domain (lifecycle
        # words + overlay words + legacy/unknown + None) must collapse
        # to exactly the three groups — a fourth return value would
        # strand rows in no bucket TS-side.
        domain = TASK_STATE_COLUMN_VALUES | {"running", "interrupted", ""}
        produced = {liability_group_for(w) for w in domain}
        produced.add(liability_group_for(None))
        assert produced == self.GROUP_WORDS, (
            f"liability_group_for produces outside the legislated "
            f"three groups: {sorted(produced - self.GROUP_WORDS)}"
        )

    def test_i18n_carries_the_group_headers_in_both_languages(self):
        """GROUP_META's three header keys must exist in BOTH dictionaries
        — a missing key renders the raw key text as the header (the
        visible-untranslated failure the parity tests exist for)."""
        for dict_name in ("en.ts", "zh.ts"):
            text = (CORE_I18N / dict_name).read_text(encoding="utf-8")
            found = set(re.findall(r'"(boot\.pending\.group_[a-z_]+)"', text))
            expected = {
                "boot.pending.group_in_flight",
                "boot.pending.group_needs_recovery",
                "boot.pending.group_uncleared",
            }
            assert found >= expected, (
                f"{dict_name} missing group header keys: "
                f"{sorted(expected - found)}"
            )


class TestDdlDefaultReconciliation:
    """Round-17 C3: the sixth shape's DDL residue — both backends'
    CREATE TABLE DEFAULT literals (6 columns) were hand-copied in
    parallel with zero pinning; task_state's DEFAULT being a closed-set
    member was a coincidence, not a guarantee. These pins make the
    coincidence structural: cross-backend verbatim equality +
    membership in the legislated column domain."""

    DDL_DEFAULT_RE = re.compile(
        r"^(\s+)(\w+)\s+TEXT NOT NULL DEFAULT '([a-z_]+)'", re.MULTILINE
    )

    @classmethod
    def _ddl_defaults(cls, source_path: Path) -> dict[str, str]:
        text = source_path.read_text(encoding="utf-8")
        return {m.group(2): m.group(3) for m in cls.DDL_DEFAULT_RE.finditer(text)}

    def test_ddl_defaults_identical_across_backends(self):
        sqlite_ddl = self._ddl_defaults(SQLITE_STORE)
        pg_ddl = self._ddl_defaults(PG_STORE)
        shared = set(sqlite_ddl) & set(pg_ddl)
        assert shared >= {"task_state", "stage", "phase", "operation"}, (
            "expected the core DEFAULT'd columns in both DDLs — parser "
            "shape drifted?"
        )
        assert sqlite_ddl == pg_ddl, (
            f"DDL DEFAULT vocabularies drifted — sqlite: {sqlite_ddl}, "
            f"pg: {pg_ddl}; a fresh DB on one backend would birth rows "
            "the other backend's assumptions contradict"
        )

    def test_task_state_ddl_default_is_legislated(self):
        """The column's birth value must be a member of the column's
        legislated value domain — coincidence (pre-round-17) upgraded to
        a structural guarantee."""
        sqlite_ddl = self._ddl_defaults(SQLITE_STORE)
        pg_ddl = self._ddl_defaults(PG_STORE)
        for name, ddl in (("sqlite", sqlite_ddl), ("postgresql", pg_ddl)):
            assert ddl["task_state"] in TASK_STATE_COLUMN_VALUES, (
                f"{name}: tasks.task_state DEFAULT {ddl['task_state']!r} is "
                "outside the legislated column value domain"
            )
