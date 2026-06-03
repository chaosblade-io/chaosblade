"""FaultSpec — single source of truth for fault injection intent.

Replaces the historically scattered fields (state.target / state.fault_intent /
state.blade_scope / state.blade_target / state.blade_action / state.params /
state.params_flags / state.duration) with a single typed dataclass.

Design
------

All input modes converge through one of the constructors:

  - ``FaultSpec.from_cli_structured(kwargs)`` — CLI with ``--scope ... --target-name ...``
  - ``FaultSpec.from_cli_nl(input)``          — CLI with ``--input "natural language"``
  - ``FaultSpec.from_http_request(request)``  — HTTP /inject endpoint (both structured and NL)
  - ``FaultSpec.from_intent_args(args)``      — TUI / any NL flow after ``submit_fault_intent``
  - ``FaultSpec.from_direct_setup(spec, …)``  — direct mode after ``direct_setup``
  - ``FaultSpec.placeholder_nl(...)``         — initial stub at NL entry; later rewritten

All consumers go through ``read_fault_spec(state)`` to get a strongly-typed
instance. No consumer should read ``state["fault_spec"]`` directly — the
helper handles the dict↔instance round-trip and never returns malformed data.

Why frozen
----------

``FaultSpec`` is ``frozen=True`` so:
  - Accidental in-place mutation in a consumer (e.g. ``spec.params["x"] = 1``)
    surfaces immediately at the dict layer rather than silently corrupting
    shared state across nodes.
  - Mutation is explicit through ``.replace(...)`` returning a new instance,
    which is the convention LangGraph reducers expect.

The contained dict fields (``labels`` / ``params``) are not deep-frozen — we
trust callers not to mutate them after construction. The frozen outer
container catches the common bug.

State integration
-----------------

``AgentState.fault_spec`` is declared as ``Optional[dict]`` (not
``Optional[FaultSpec]``) because LangGraph's checkpointer round-trips state
through JSON. We store ``spec.to_dict()`` and rehydrate via
``FaultSpec.from_dict()`` on read.

Extension
---------

To add a new input mode (e.g. webhook, Slack bot), add a ``from_xxx``
constructor here. To add a new fault dimension (e.g. ``gpu_index`` for GPU
chaos), add a field to this dataclass with a sensible default; existing
constructors keep working since the field is optional, and new consumers
read ``spec.gpu_index`` without coordinating with other entry points.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from chaos_agent.utils.coerce import (
    coerce_to_dict,
    coerce_to_int,
    coerce_to_list,
    coerce_to_str,
)

logger = logging.getLogger(__name__)


# Canonical ``source`` values. Centralising them as constants so
# entry-point modules import the name instead of risking typo'd
# string literals. The vocabulary is intentionally extensible — add a
# new constant when wiring a new entry mode (webhook, slack, ...).
SOURCE_CLI_STRUCTURED = "cli_structured"
SOURCE_CLI_NL = "cli_nl"
SOURCE_HTTP_STRUCTURED = "http_structured"
SOURCE_HTTP_NL = "http_nl"
SOURCE_TUI = "tui"
SOURCE_DIRECT = "direct"


@dataclass(frozen=True, eq=True)
class FaultSpec:
    """Single source of truth for 'what fault to inject where'.

    Fields are intentionally flat (no nested ``target`` / ``params``
    sub-dicts) so a consumer reads ``spec.namespace`` instead of
    ``spec.target.namespace``. Consumers that need the old nested
    shape (e.g. legacy renderers) can call ``.to_legacy_target_dict()``
    but we don't write those into state anywhere.

    Immutability story:
      - ``frozen=True`` prevents attribute reassignment.
      - ``__post_init__`` defensively copies dict / list inputs so a
        caller mutating the original after construction doesn't leak
        into the spec.
      - ``__hash__`` is intentionally disabled (``unsafe_hash=False``)
        because labels/params dicts aren't hashable. The dataclass
        would otherwise auto-generate ``__hash__`` and crash at call
        time with TypeError; this gives the clearer "spec not
        hashable" surface (callers should compare via ``==``, not
        use spec as a set/dict key).
    """
    __hash__ = None  # type: ignore[assignment]

    # ---- Identity: WHAT resource is targeted -------------------------------
    namespace: str = ""
    scope: str = ""                              # "pod" | "node" | "container" | ...
    names: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)

    # ---- Fault Type: WHAT subsystem to break ------------------------------
    blade_target: str = ""                       # "cpu" | "mem" | "network" | ...
    blade_action: str = ""                       # "fullload" | "burn" | "drop" | ...

    # ---- Tuning: HOW to break it ------------------------------------------
    params: dict[str, str] = field(default_factory=dict)
    params_flags: tuple[str, ...] = ()
    duration_seconds: int = 0

    # ---- Origin metadata (audit only) -------------------------------------
    source: str = ""                             # "cli_structured" | "cli_nl" | "http_structured" | "http_nl" | "tui" | "direct"
    user_description: str = ""

    # ---- Mutation defense (frozen alone doesn't deep-freeze) --------------

    def __post_init__(self):
        # Defensive copy of mutable inputs so external callers can't
        # mutate the backing dicts/lists after construction.
        # ``object.__setattr__`` is required because ``frozen=True``
        # blocks normal assignment.
        object.__setattr__(self, "labels", dict(self.labels or {}))
        object.__setattr__(self, "params", dict(self.params or {}))
        object.__setattr__(self, "names", tuple(self.names or ()))
        object.__setattr__(self, "params_flags", tuple(self.params_flags or ()))

    # ---- Derived properties -----------------------------------------------

    @property
    def fault_type(self) -> str:
        """Composite label e.g. ``node-cpu-fullload``."""
        return "-".join(p for p in (self.scope, self.blade_target, self.blade_action) if p)

    @property
    def is_namespace_wide(self) -> bool:
        """True when the user approved 'any resource of this scope in this namespace'."""
        return not self.names and not self.labels

    @property
    def is_complete(self) -> bool:
        """True iff the spec is ready to drive a real fault injection.

        Used by intent_confirm to decide whether to show the confirm card
        (complete) or stay in clarification (incomplete).

        Acceptance rules:
          - scope / blade_target / blade_action all non-empty.
          - For non-cluster-scoped scopes (pod / container / deployment /
            ...), namespace must be set; cluster-scoped (node / pv / ...)
            don't carry one.

        Resource selector (names / labels) is NOT required here —
        ``namespace-wide`` is a legitimate intent ("inject any pod in
        ns prod"). The downstream guard treats ``is_namespace_wide``
        as an explicit operator opt-in; confirming such a spec is
        valid. Callers that want stricter intent (must have names or
        labels) should check ``is_namespace_wide`` themselves.
        """
        if not (self.scope and self.blade_target and self.blade_action):
            return False
        if self.scope not in _CLUSTER_SCOPED and not self.namespace:
            return False
        return True

    # ---- Constructors -----------------------------------------------------

    @classmethod
    def placeholder_nl(cls, *, user_description: str, source: str) -> "FaultSpec":
        """Empty stub written at NL entry points before clarification runs.

        The downstream ``intent_clarification`` node will overwrite this
        with a complete spec once the user converges on an intent.
        ``user_description`` is preserved so the LLM can read the user's
        original phrasing throughout the conversation.
        """
        return cls(
            source=source,
            user_description=coerce_to_str(user_description, default=""),
        )

    @classmethod
    def from_cli_structured(cls, kwargs: dict) -> "FaultSpec":
        """CLI structured: ``blade-ai inject --scope ... --target-name ...``.

        Mirrors the field layout in ``cli/runner.py``; the kwargs dict
        is what click passes after option parsing.
        """
        names_raw = kwargs.get("target_name") or ""
        names = tuple(
            n.strip() for n in str(names_raw).split(",") if n.strip()
        )
        return cls(
            namespace=coerce_to_str(kwargs.get("namespace"), default=""),
            scope=coerce_to_str(kwargs.get("scope"), default=""),
            names=names,
            labels=_normalise_labels(kwargs.get("labels")),
            blade_target=coerce_to_str(kwargs.get("target"), default=""),
            blade_action=coerce_to_str(kwargs.get("action"), default=""),
            params=_normalise_params(kwargs.get("params")),
            params_flags=tuple(kwargs.get("params_flags") or ()),
            duration_seconds=coerce_to_int(kwargs.get("duration"), default=0),
            source=SOURCE_CLI_STRUCTURED,
            user_description=coerce_to_str(kwargs.get("input"), default=""),
        )

    @classmethod
    def from_cli_nl(cls, *, input_text: str, kwargs: Optional[dict] = None) -> "FaultSpec":
        """CLI with ``--input "..."``.

        Identity fields (scope/target/action/namespace/names/labels)
        are left empty for ``intent_clarification`` to fill later.
        Tuning fields (``params`` / ``params_flags`` / ``duration``)
        ARE captured from kwargs when provided — CLI accepts
        ``--input "..." --duration 600 --params percent=80`` to seed
        the NL flow with hard-pinned tuning. Without this, the LLM
        would have to re-derive those numbers from natural language,
        risking drift.
        """
        kwargs = kwargs or {}
        return cls(
            params=_normalise_params(kwargs.get("params")),
            params_flags=tuple(kwargs.get("params_flags") or ()),
            duration_seconds=coerce_to_int(kwargs.get("duration"), default=0),
            source=SOURCE_CLI_NL,
            user_description=coerce_to_str(input_text, default=""),
        )

    @classmethod
    def from_http_request(cls, request: Any) -> "FaultSpec":
        """HTTP /inject endpoint. Handles both structured and NL forms.

        Inspects which fields the request carries to pick the right
        ``source`` tag. ``request`` is a pydantic ``InjectRequest`` (or
        ``InjectStreamRequest``) — accessed via getattr for tolerance.
        """
        scope = getattr(request, "scope", "") or ""
        target_name = getattr(request, "target_name", "") or ""
        labels = getattr(request, "labels", None) or {}
        namespace = getattr(request, "namespace", "") or ""
        # ``is_structured`` mirrors the SAME 5-field test that
        # ``InjectRequest.validate_mode`` and the inject*.py route
        # branches use — keeping the three in sync so ``spec.source``
        # never disagrees with which entry-point branch actually ran.
        is_structured = bool(
            scope and getattr(request, "target", "")
            and getattr(request, "action", "")
            and (target_name or labels)
            and namespace
        )
        source = SOURCE_HTTP_STRUCTURED if is_structured else SOURCE_HTTP_NL

        names: tuple[str, ...] = ()
        if target_name:
            names = tuple(n.strip() for n in target_name.split(",") if n.strip())

        return cls(
            namespace=coerce_to_str(getattr(request, "namespace", ""), default=""),
            scope=coerce_to_str(scope, default=""),
            names=names,
            labels=_normalise_labels(labels),
            blade_target=coerce_to_str(getattr(request, "target", ""), default=""),
            blade_action=coerce_to_str(getattr(request, "action", ""), default=""),
            params=_normalise_params(getattr(request, "params", None)),
            params_flags=tuple(getattr(request, "params_flags", None) or ()),
            duration_seconds=coerce_to_int(getattr(request, "duration", 0), default=0),
            source=source,
            user_description=coerce_to_str(getattr(request, "input", ""), default=""),
        )

    @classmethod
    def from_intent_args(
        cls,
        args: dict,
        *,
        existing: Optional["FaultSpec"] = None,
        source: Optional[str] = None,
    ) -> "FaultSpec":
        """From a ``submit_fault_intent`` tool_call's args dict (NL flow).

        The args come from an LLM tool_call so they may arrive in
        unexpected shapes (JSON-stringified lists, comma-strings, etc.).
        We push everything through coerce helpers — same defensive
        posture as intent_clarification's own field extraction.

        Args:
            args: the ``submit_fault_intent`` tool_call args dict.
            existing: the previous spec for this turn (typically the
                placeholder written at NL entry). Used to carry
                forward ``user_description`` if the LLM forgot to
                echo it, and to inherit ``source`` so a CLI NL flow
                doesn't get mislabelled as ``tui``.
            source: explicit override. When None, inherits from
                ``existing.source``, else falls back to ``"tui"``.
        """
        # ChaosBlade params often carry ``timeout`` which is the fault
        # duration in seconds. Hoist it to ``duration_seconds`` so
        # consumers don't have to peer into params for a common field.
        params = _normalise_params(args.get("params"))
        duration = 0
        if "timeout" in params:
            duration = coerce_to_int(params["timeout"], default=0)

        user_desc = coerce_to_str(args.get("user_description"), default="")
        if not user_desc and existing is not None:
            user_desc = existing.user_description

        # source inheritance: explicit > existing.source > SOURCE_TUI fallback
        if source is None:
            source = existing.source if existing else SOURCE_TUI

        return cls(
            namespace=coerce_to_str(args.get("namespace"), default=""),
            scope=coerce_to_str(args.get("scope"), default=""),
            names=_normalise_names(args.get("names")),
            labels=_normalise_labels(args.get("labels")),
            blade_target=coerce_to_str(args.get("target"), default=""),
            blade_action=coerce_to_str(args.get("action"), default=""),
            params=params,
            params_flags=(),  # NL flow doesn't separate flag-style args
            duration_seconds=duration,
            source=source,
            user_description=user_desc,
        )

    @classmethod
    def from_direct_setup(
        cls,
        *,
        base: "FaultSpec",
        skill_meta: Optional[dict] = None,
    ) -> "FaultSpec":
        """direct mode enrichment hook.

        ``direct_setup`` may want to attach skill-derived defaults
        (e.g. action timeout, labels from skill registry). The base
        spec (from CLI structured at entry) is the canonical input;
        skill_meta only fills gaps and never overrides explicit user
        values.
        """
        if not skill_meta:
            return base
        updates: dict[str, Any] = {}
        if not base.duration_seconds and skill_meta.get("default_duration"):
            updates["duration_seconds"] = coerce_to_int(
                skill_meta["default_duration"], default=0,
            )
        return base.replace(**updates) if updates else base

    # ---- Mutation (frozen → returns new instance) -------------------------

    def replace(self, **kwargs) -> "FaultSpec":
        """``dataclasses.replace`` wrapper — preserves immutability while
        producing an updated copy. The standard idiom in LangGraph nodes
        that want to mutate one field of a spec."""
        return dataclasses.replace(self, **kwargs)

    # ---- intent_clarification interop -------------------------------------

    def to_intent_dict(self) -> dict:
        """Convert to the dict shape ``intent_clarification`` uses internally.

        ``intent_clarification`` merges three sources (existing intent /
        regex fallback / submit_fault_intent args) by dict-overlay,
        then constructs a new FaultSpec from the merged dict. This
        helper produces the dict shape its merge code expects so the
        node body doesn't need to be rewritten when state moves from
        fault_intent dict to fault_spec.

        Distinct from ``to_dict()`` (which is the state-persistence
        format). Keep them separate so future evolution of the
        on-the-wire shape doesn't entangle with the LLM-args merge
        convention.
        """
        return {
            "fault_type": self.fault_type,
            "scope": self.scope,
            "target": self.blade_target,
            "action": self.blade_action,
            "namespace": self.namespace,
            "names": list(self.names),
            "labels": dict(self.labels),
            "params": dict(self.params),
            "user_description": self.user_description,
        }

    # ---- Serialisation ----------------------------------------------------

    def to_dict(self) -> dict:
        """Convert to a JSON-serialisable dict for state.fault_spec.

        Tuples become lists (LangGraph checkpointer uses JSON which
        has no tuple type). ``from_dict`` reverses the conversion.
        """
        return {
            "namespace": self.namespace,
            "scope": self.scope,
            "names": list(self.names),
            "labels": dict(self.labels),
            "blade_target": self.blade_target,
            "blade_action": self.blade_action,
            "params": dict(self.params),
            "params_flags": list(self.params_flags),
            "duration_seconds": self.duration_seconds,
            "source": self.source,
            "user_description": self.user_description,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["FaultSpec"]:
        """Hydrate from a state.fault_spec dict. Returns None for
        missing/empty/malformed input so the caller can short-circuit
        rather than constructing a defaulted spec that would silently
        compare equal to zero-valued fields elsewhere."""
        if not d or not isinstance(d, dict):
            return None
        try:
            return cls(
                namespace=coerce_to_str(d.get("namespace"), default=""),
                scope=coerce_to_str(d.get("scope"), default=""),
                names=_normalise_names(d.get("names")),
                labels=_normalise_labels(d.get("labels")),
                blade_target=coerce_to_str(d.get("blade_target"), default=""),
                blade_action=coerce_to_str(d.get("blade_action"), default=""),
                params=_normalise_params(d.get("params")),
                params_flags=tuple(coerce_to_list(d.get("params_flags"))),
                duration_seconds=coerce_to_int(d.get("duration_seconds"), default=0),
                source=coerce_to_str(d.get("source"), default=""),
                user_description=coerce_to_str(d.get("user_description"), default=""),
            )
        except Exception:
            logger.exception("FaultSpec.from_dict failed for %r", d)
            return None


# Cluster-scoped k8s kinds — they don't carry a namespace. Used by
# ``is_complete`` and by callers that need to decide whether ``namespace=""``
# is legitimate or a missing field.
_CLUSTER_SCOPED: frozenset[str] = frozenset({
    "node", "pv", "namespace", "clusterrole", "clusterrolebinding",
    "storageclass",
})


# ---------------------------------------------------------------------------
# Helpers — defensive normalisation against LLM / external schema drift
# ---------------------------------------------------------------------------


def _normalise_names(raw: Any) -> tuple[str, ...]:
    """Names always come out as ``tuple[str, ...]`` regardless of input shape.

    LLMs sometimes JSON-stringify lists (``'["a","b"]'`` instead of
    ``["a", "b"]``); CLI passes comma strings; HTTP carries lists.
    coerce_to_list handles all three; we add the empty-string filter
    and stringification.
    """
    items = coerce_to_list(raw, context="FaultSpec:names")
    return tuple(str(n).strip() for n in items if str(n).strip())


def _normalise_labels(raw: Any) -> dict[str, str]:
    """Labels always come out as ``dict[str, str]``.

    Accepts dict, JSON string ``'{"k":"v"}'``, label-selector string
    ``"k1=v1,k2=v2"``, or a list of ``"k=v"`` strings (via coerce_to_dict).
    """
    parsed = coerce_to_dict(raw, context="FaultSpec:labels")
    return {str(k): str(v) for k, v in parsed.items()}


def _normalise_params(raw: Any) -> dict[str, str]:
    """Params always come out as ``dict[str, str]``.

    blade flags conventionally take string values (kubectl-style
    ``--percent=80`` not ``--percent=int(80)``), so we stringify
    everything for downstream consistency.
    """
    parsed = coerce_to_dict(raw, context="FaultSpec:params")
    return {str(k): "" if v is None else str(v) for k, v in parsed.items()}


# ---------------------------------------------------------------------------
# Read helper — the ONLY entry point consumers should use
# ---------------------------------------------------------------------------


def legacy_target_dict(state_or_values: dict) -> dict:
    """Project ``fault_spec`` to the legacy ``{namespace, names, labels,
    resource_type}`` dict shape that response envelopes and external
    audit tools still consume.

    Returns an empty dict when no spec is on record (rather than None
    or raising), matching the historical "no target yet" sentinel
    that response builders expect.
    """
    spec = read_fault_spec(state_or_values)
    if not spec:
        return {}
    return {
        "namespace": spec.namespace,
        "names": list(spec.names),
        "labels": dict(spec.labels),
        "resource_type": spec.scope,
    }


def legacy_params_dict(state_or_values: dict) -> dict:
    """Project ``fault_spec.params`` to a dict. Empty when no spec."""
    spec = read_fault_spec(state_or_values)
    return dict(spec.params) if spec else {}


def read_fault_spec(state: dict) -> Optional[FaultSpec]:
    """Pull the FaultSpec out of state in normalised form.

    Returns None for missing/malformed state.fault_spec — caller decides
    how to handle (skip, fail, log warning). All consumers should
    standardise on this helper instead of touching state["fault_spec"]
    directly, so the dict↔instance contract lives in one place.

    Defensive legacy-shape projection: if ``state.fault_spec`` is
    missing but the caller still passes the old scattered fields
    (``state.target`` / ``state.blade_scope`` / ``state.blade_target``
    / ``state.blade_action`` / ``state.params`` / ``state.params_flags``
    / ``state.duration``), we construct a spec from those. This lets
    older test fixtures (and any out-of-tree caller that hasn't yet
    migrated) keep working. Production entry points always set
    ``fault_spec`` directly, so this branch is a no-op in real use.
    """
    spec = FaultSpec.from_dict(state.get("fault_spec"))
    if spec is not None:
        return spec
    # Legacy fallback — for older test fixtures and any out-of-tree
    # caller that hasn't yet migrated. Production entry points always
    # set fault_spec, so this branch firing in production is a bug
    # (entry point forgot to construct the spec); log a WARNING so
    # the failure is visible instead of silently papering over it
    # with a reconstructed-from-stale-fields spec.
    legacy_target = state.get("target")
    legacy_scope = state.get("blade_scope")
    legacy_blade_target = state.get("blade_target")
    legacy_action = state.get("blade_action")
    if not any((legacy_target, legacy_scope, legacy_blade_target, legacy_action)):
        return None
    logger.warning(
        "read_fault_spec: state.fault_spec missing — falling back to legacy "
        "scattered fields. Entry point may have forgotten to call "
        "FaultSpec.from_xxx (state keys present: %s).",
        sorted(k for k in state if k in (
            "target", "blade_scope", "blade_target", "blade_action",
            "params", "params_flags", "duration",
        )),
    )
    target = legacy_target or {}
    return FaultSpec(
        namespace=str(target.get("namespace") or ""),
        scope=str(legacy_scope or target.get("resource_type") or ""),
        names=tuple(target.get("names") or ()),
        labels=target.get("labels") or {},
        blade_target=str(legacy_blade_target or ""),
        blade_action=str(legacy_action or ""),
        params=state.get("params") or {},
        params_flags=tuple(state.get("params_flags") or ()),
        duration_seconds=int(state.get("duration") or 0),
    )


__all__ = [
    "FaultSpec",
    "legacy_params_dict",
    "legacy_target_dict",
    "read_fault_spec",
    "SOURCE_CLI_STRUCTURED",
    "SOURCE_CLI_NL",
    "SOURCE_HTTP_STRUCTURED",
    "SOURCE_HTTP_NL",
    "SOURCE_TUI",
    "SOURCE_DIRECT",
]
