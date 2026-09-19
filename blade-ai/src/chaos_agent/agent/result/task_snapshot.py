"""Recover-oriented task snapshot reconstruction.

Task recovery may need data from two persistent sources:

* TaskStore rows hold structured fields such as target, params and skill_name.
* memory/tasks/<task_id>.json plus optional .jsonl increments hold the
  append-only conversation/tool log.  When .jsonl exists, it is part of the
  source of truth because the final JSON/TaskStore snapshot may lag behind.

This module centralizes that merge so CLI, TUI and server recover paths do not
each invent their own fallback policy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from chaos_agent.agent.spec.fault_spec import (
    fault_parts_from_name,
    fault_spec_from_legacy_state,
)
from chaos_agent.agent.spec.skill_identity import read_active_skill_name

logger = logging.getLogger(__name__)


def _coerce_json_dict(value) -> dict:
    """Return a dict for values stored as JSON text or already-decoded dicts."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _coerce_json_list(value) -> list:
    """Return a list for JSON text or an already-decoded list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []
    return []


def _recover_marker_value(checkpoint_value, record_value) -> bool | None:
    """Combine the combo marker's two carriers into a typed tri-state.

    The recover-side consumers read ``bool(state.get(...))`` or test for
    None — a raw JSON string would poison them (the string "false" is
    truthy), so both carriers are decoded to real bools before they
    reach the seed.

    Precedence mirrors the DB latch's monotonic semantics (round-32b
    F-2): TRUE on EITHER carrier sticks. The checkpoint (the fresher
    word) can hold a stale False from before a combo upgrade whose
    store sync landed but whose next superstep checkpoint save did not
    — a crash inside that window would otherwise hydrate the seed with
    False, route that round's recovery deterministic-only, and leak
    the native mutation (exactly what the marker exists to prevent)
    while the DB row keeps the True via the latch: the row stays
    recoverable, only the routing was wrong. Below True, the
    checkpoint's word (fresher) wins over the record's; neither known
    → None.
    """
    def _as_bool(value) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip():
            try:
                loaded = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return None
            return loaded if isinstance(loaded, bool) else None
        return None

    checkpoint_marker = _as_bool(checkpoint_value)
    record_marker = _as_bool(record_value)
    if checkpoint_marker is True or record_marker is True:
        # Sticky: a combo fact on EITHER carrier — mirrors the upsert
        # latch's True-sticks discipline at the hydration seam.
        return True
    if checkpoint_marker is not None:
        return checkpoint_marker
    return record_marker


def _session_result_data(session: dict | None) -> dict:
    """Extract JSONEnvelope.data from the task file result_summary."""
    if not isinstance(session, dict):
        return {}
    summary = session.get("result_summary")
    if isinstance(summary, str) and summary.strip():
        try:
            summary = json.loads(summary)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(summary, dict):
        return {}
    data = summary.get("data")
    return data if isinstance(data, dict) else {}


def _target_from_result_data(data: dict) -> dict:
    """Normalize result payload target/targets into the TaskStore target shape."""
    target = _coerce_json_dict(data.get("target"))
    if target:
        return target

    targets = data.get("targets")
    if not isinstance(targets, list):
        return {}

    names: list[str] = []
    namespace = ""
    for item in targets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
        if not namespace and isinstance(item.get("namespace"), str):
            namespace = item.get("namespace") or ""
    if not names and not namespace:
        return {}
    return {
        "namespace": namespace,
        "names": names,
        "labels": {},
        "resource_type": data.get("scope", "") or "",
    }


def _target_from_fault_spec(spec: dict) -> dict:
    if not spec:
        return {}
    raw_names = spec.get("names") or []
    if isinstance(raw_names, str):
        names = [raw_names] if raw_names else []
    elif isinstance(raw_names, (list, tuple)):
        names = [str(name) for name in raw_names if name]
    else:
        names = []
    labels = spec.get("labels")
    labels = dict(labels) if isinstance(labels, dict) else {}
    namespace = spec.get("namespace", "") or ""
    scope = spec.get("scope", "") or ""
    if not (namespace or names or labels or scope):
        return {}
    return {
        "namespace": namespace,
        "names": names,
        "labels": labels,
        "resource_type": scope,
    }


def _params_from_fault_spec(spec: dict) -> dict:
    params = spec.get("params") if spec else None
    return dict(params) if isinstance(params, dict) else {}


def _fault_type_from_fault_spec(spec: dict) -> str:
    if not spec:
        return ""
    return "-".join(
        str(part)
        for part in (
            spec.get("scope", ""),
            spec.get("fault_target", ""),
            spec.get("fault_action", ""),
        )
        if part
    )


def _extract_experiment_uid_from_session(
    session: dict | None,
    *,
    prefer_messages: bool = False,
) -> str:
    """Recover the experiment UID from task file result/message data."""
    if not isinstance(session, dict):
        return ""

    messages = session.get("messages")

    def uid_from_result_summary() -> str:
        result_data = _session_result_data(session)
        experiment_uid = result_data.get("experiment_uid")
        return experiment_uid if isinstance(experiment_uid, str) else ""

    def uid_from_messages() -> str:
        if not isinstance(messages, list):
            return ""
        try:
            # Phase-13 D2: registry seam — the per-provider claim over the
            # best-effort converted history plus the carrier-owned dict
            # fallback are both orchestrated (and individually contained)
            # inside the seam; a failure falls through the layers, never
            # aborts the snapshot rebuild.
            from chaos_agent.agent.providers.registry import FaultProviderRegistry

            return FaultProviderRegistry.recover_experiment_uid_from_session(
                messages
            )
        except Exception:
            logger.debug("Failed to extract experiment uid from session messages", exc_info=True)
            return ""

    if prefer_messages:
        return uid_from_messages() or uid_from_result_summary()
    return uid_from_result_summary() or uid_from_messages()


def _build_inject_context_from_session(session: dict | None) -> str:
    """Build inject_context from session messages, including live .jsonl data."""
    if not isinstance(session, dict):
        return ""
    messages = session.get("messages")
    if not isinstance(messages, list):
        return ""
    try:
        from chaos_agent.utils.inject_context import build_inject_context
        # Phase-13 D2: the conversion moved to the registry home (it is
        # session-recovery orchestration, not snapshot-private logic).
        from chaos_agent.agent.providers.registry import (
            _session_messages_to_langchain,
        )

        return build_inject_context(_session_messages_to_langchain(messages))
    except Exception:
        logger.debug("Failed to build inject_context from session", exc_info=True)
        return ""


def _rebuild_inject_verification_summary(verification: dict | None) -> str:
    """Rebuild inject_verification_summary from the stored verification dict.

    Side-effect warnings recorded at injection time are structural facts
    (not reusable raw observations), so they carry no causal-chain-illusion
    risk and MUST survive into the recover context — the recover verifier
    needs them to reconcile collateral impact beyond the primary target.
    """
    if not verification or not isinstance(verification, dict):
        return ""
    layer2 = verification.get("layer2")
    if not layer2 or not isinstance(layer2, dict):
        return ""
    details = layer2.get("details", "")
    if not details:
        return ""
    summary = f"Layer2={layer2.get('status', 'unknown')}, Details={details}"
    warnings = [
        str(w).strip()
        for w in (verification.get("warnings") or [])
        if str(w).strip()
    ]
    if warnings:
        numbered = " ".join(f"({i}) {w}" for i, w in enumerate(warnings, start=1))
        summary += f"\nRecorded side-effect warnings at injection: {numbered}"
    return summary


def _read_task_session(task_id: str) -> tuple[dict | None, bool]:
    """Read memory/tasks/<task_id>, returning merged data and live-log presence."""
    try:
        from chaos_agent.memory.session_store import (
            SessionStore,
            get_global_session_store,
        )

        store = get_global_session_store()
        if store is None:
            from chaos_agent.config.settings import settings

            store = SessionStore(settings.resolved_memory_dir / "tasks")
        task_dir = getattr(store, "task_dir", None)
        has_increment_log = False
        if task_dir is not None:
            has_increment_log = (
                (task_dir / f"{task_id}.jsonl").exists()
                or (task_dir / f"{task_id}.jsonl.compacted").exists()
            )
        session = store.read_session(task_id)
        if session is not None or not has_increment_log or task_dir is None:
            return session, has_increment_log
        return _read_jsonl_only_session(task_id, task_dir), has_increment_log
    except Exception:
        logger.debug("Failed to read session store for task %s", task_id, exc_info=True)
        return None, False


def _read_jsonl_only_session(task_id: str, task_dir) -> dict | None:
    """Best-effort task session reconstruction when .json is missing/corrupt.

    This is intentionally scoped to TaskSnapshot recovery.  The general
    SessionStore.read_session() contract treats .json as the required snapshot,
    but recover should still mine .jsonl/.jsonl.compacted for the experiment uid and
    inject_context when those logs survived a partial write.
    """
    jsonl_path = task_dir / f"{task_id}.jsonl"
    compacted_path = task_dir / f"{task_id}.jsonl.compacted"
    messages: list[dict] = []
    if compacted_path.exists():
        messages.extend(_replay_jsonl_file(compacted_path, task_id))
    if jsonl_path.exists():
        messages.extend(_replay_jsonl_file(jsonl_path, task_id))
    messages = _dedupe_messages(messages)
    if not messages:
        return None
    return {
        "taskId": task_id,
        "operation": "inject",
        "messages": messages,
        "result_summary": None,
        "status": "active",
    }


def _replay_jsonl_file(path, task_id: str) -> list[dict]:
    """Read valid JSON lines from a task jsonl file without mutating it."""
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Corrupt JSONL line in %s for task %s, skipping",
                        path.name,
                        task_id,
                    )
                    continue
                if isinstance(entry, dict):
                    out.append(entry)
    except OSError as e:
        logger.warning("Failed to read %s for task %s: %s", path.name, task_id, e)
    return out


def _dedupe_messages(messages: list[dict]) -> list[dict]:
    from chaos_agent.memory.session_store import _message_dedup_key

    seen: set[str] = set()
    out: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        key = _message_dedup_key(msg)
        if key in seen:
            continue
        seen.add(key)
        out.append(msg)
    return out


@dataclass(frozen=True)
class TaskSnapshot:
    """Merged task snapshot consumed by recover setup."""

    task_id: str
    record: dict = field(default_factory=dict)
    session: dict | None = None
    result_data: dict = field(default_factory=dict)
    has_increment_log: bool = False
    target: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    stored_fault_spec: dict = field(default_factory=dict)
    experiment_uid: str = ""
    injection_method: str = ""
    fault_handle: dict = field(default_factory=dict)
    skill_name: str = ""
    fault_type: str = ""
    verification: dict | None = None
    execution_artifacts: list[dict] = field(default_factory=list)
    inject_context: str = ""
    blast_radius_detail: str = ""
    side_effects: dict = field(default_factory=dict)
    tui_session_id: str = ""

    @classmethod
    def from_sources(
        cls,
        *,
        task_id: str,
        record: dict | None,
        session: dict | None,
        has_increment_log: bool,
        tui_session_id: str = "",
    ) -> "TaskSnapshot | None":
        """Merge TaskStore + task session data into one recover snapshot."""
        result_data = _session_result_data(session)
        if not record and not result_data and not session:
            return None

        record = record or {}
        record_fault_spec = _coerce_json_dict(record.get("fault_spec"))
        session_fault_spec = _coerce_json_dict(result_data.get("fault_spec"))
        record_target = (
            _target_from_fault_spec(record_fault_spec)
            or _coerce_json_dict(record.get("target"))
        )
        session_target = (
            _target_from_fault_spec(session_fault_spec)
            or _target_from_result_data(result_data)
        )
        record_params = (
            _params_from_fault_spec(record_fault_spec)
            or _coerce_json_dict(record.get("params"))
        )
        session_params = (
            _params_from_fault_spec(session_fault_spec)
            or _coerce_json_dict(result_data.get("params"))
        )
        session_experiment_uid = _extract_experiment_uid_from_session(
            session,
            prefer_messages=has_increment_log,
        )
        record_skill_name = str(record.get("skill_name") or "")
        record_fault_type = (
            _fault_type_from_fault_spec(record_fault_spec)
            or record.get("fault_type")
            or record_skill_name
            or ""
        )
        session_fault_type = (
            _fault_type_from_fault_spec(session_fault_spec)
            or result_data.get("fault_type")
            or ""
        )
        session_skill_name = str(result_data.get("skill_name") or "")
        session_inject_context = _build_inject_context_from_session(session)
        # Durable-first: the inject finalize persists a pre-built inject_context
        # into result_summary.data; message-scan reconstruction is only the
        # fallback for records written before that field existed.
        persisted_inject_context = str(result_data.get("inject_context") or "")
        session_blast_radius = str(result_data.get("blast_radius_detail") or "")
        session_side_effects = _coerce_json_dict(result_data.get("side_effects"))
        record_artifacts = _coerce_json_list(record.get("execution_artifacts"))
        session_artifacts = _coerce_json_list(result_data.get("execution_artifacts"))
        # Attribution facts for recover hydration (R4): the finalize-persisted
        # result_summary is authoritative when present; the TaskStore record
        # column is the fallback for tasks finalized before R4.
        record_injection_method = str(record.get("injection_method") or "")
        session_injection_method = str(result_data.get("injection_method") or "")
        record_fault_handle = _coerce_json_dict(record.get("fault_handle"))
        session_fault_handle = _coerce_json_dict(result_data.get("fault_handle"))

        resolved_tui_session_id = tui_session_id
        if not resolved_tui_session_id and isinstance(session, dict):
            session_tui_session_id = session.get("tui_session_id")
            if isinstance(session_tui_session_id, str):
                resolved_tui_session_id = session_tui_session_id

        if has_increment_log:
            target = session_target or record_target
            params = session_params or record_params
            experiment_uid = session_experiment_uid or record.get("experiment_uid") or ""
            skill_name = session_skill_name or record_skill_name
            fault_type = session_fault_type or record_fault_type
            verification = result_data.get("verification") or record.get("verification")
            # Finalize-persisted value is authoritative; otherwise the LIVE
            # session jsonl is fresher than the record's mid-flight field
            # (record syncs lag the running session).
            inject_context = (
                persisted_inject_context
                or session_inject_context
                or record.get("inject_context")
                or ""
            )
            blast_radius_detail = (
                session_blast_radius or str(record.get("blast_radius_detail") or "")
            )
            side_effects = session_side_effects or _coerce_json_dict(
                record.get("side_effects")
            )
            stored_fault_spec = session_fault_spec or record_fault_spec
            execution_artifacts = session_artifacts or record_artifacts
            injection_method = session_injection_method or record_injection_method
            fault_handle = session_fault_handle or record_fault_handle
        else:
            target = record_target or session_target
            params = record_params or session_params
            experiment_uid = record.get("experiment_uid") or session_experiment_uid or ""
            skill_name = record_skill_name or session_skill_name
            fault_type = record_fault_type or session_fault_type
            verification = record.get("verification") or result_data.get("verification")
            inject_context = (
                persisted_inject_context
                or record.get("inject_context")
                or session_inject_context
                or ""
            )
            blast_radius_detail = (
                str(record.get("blast_radius_detail") or "") or session_blast_radius
            )
            side_effects = _coerce_json_dict(
                record.get("side_effects")
            ) or session_side_effects
            stored_fault_spec = record_fault_spec or session_fault_spec
            execution_artifacts = record_artifacts or session_artifacts
            injection_method = record_injection_method or session_injection_method
            fault_handle = record_fault_handle or session_fault_handle

        return cls(
            task_id=task_id,
            record=record,
            session=session,
            result_data=result_data,
            has_increment_log=has_increment_log,
            target=target,
            params=params,
            stored_fault_spec=stored_fault_spec,
            experiment_uid=experiment_uid,
            injection_method=injection_method,
            fault_handle=fault_handle,
            skill_name=skill_name,
            fault_type=fault_type,
            verification=verification if isinstance(verification, dict) else None,
            execution_artifacts=[
                item for item in execution_artifacts if isinstance(item, dict)
            ],
            inject_context=inject_context,
            blast_radius_detail=blast_radius_detail,
            side_effects=side_effects,
            tui_session_id=resolved_tui_session_id,
        )

    @property
    def has_recover_context(self) -> bool:
        """Whether this snapshot has enough information to attempt recovery."""
        return bool(self.experiment_uid) or (
            bool(self.fault_type or self.skill_name) and bool(self.target)
        )

    def fault_spec(self) -> dict:
        if self.stored_fault_spec:
            merged = dict(self.stored_fault_spec)
            scope, fault_target, fault_action = fault_parts_from_name(self.fault_type)
            if self.target:
                merged["namespace"] = self.target.get("namespace", "") or ""
                merged["scope"] = (
                    self.target.get("resource_type", "")
                    or scope
                    or merged.get("scope", "")
                )
                merged["names"] = list(self.target.get("names") or [])
                merged["labels"] = dict(self.target.get("labels") or {})
            elif scope and not merged.get("scope"):
                merged["scope"] = scope
            if fault_target:
                merged["fault_target"] = fault_target
            if fault_action:
                merged["fault_action"] = fault_action
            if self.params:
                merged["params"] = dict(self.params or {})
            else:
                merged.setdefault("params", {})
            merged["params_flags"] = list(merged.get("params_flags") or [])
            try:
                merged["duration_seconds"] = int(merged.get("duration_seconds") or 0)
            except (TypeError, ValueError):
                merged["duration_seconds"] = 0
            merged.setdefault("source", "task_snapshot_rebuild")
            merged.setdefault("user_description", "")
            return merged

        spec = fault_spec_from_legacy_state(
            {
                "target": self.target,
                "params": self.params,
                "fault_type": self.fault_type,
                "skill_name": self.skill_name,
            },
            source="task_snapshot_rebuild",
        )
        return spec.to_dict() if spec else {}

    def legacy_state_values(self) -> dict:
        """Return the small legacy shape still used by CLI recover formatting."""
        return {
            "messages": [],
            "params": dict(self.params or {}),
            "target": dict(self.target or {}),
            "experiment_uid": self.experiment_uid,
            "skill_name": self.skill_name,
            "fault_type": self.fault_type,
        }


@dataclass(frozen=True)
class RecoverInitialResolution:
    """Resolved recover graph input plus merged inject facts.

    ``initial_state`` is the state passed to the recover graph.  ``source_values``
    is the recover-result/session-facing inject view: dynamic fields match the
    initial state, while checkpoint messages are retained as baseline messages
    when a live checkpoint was available.
    """

    initial_state: dict
    source_values: dict
    snapshot: TaskSnapshot | None = None
    checkpoint_values: dict = field(default_factory=dict)
    source: str = ""


async def load_task_snapshot(
    task_id: str,
    *,
    tui_session_id: str = "",
) -> TaskSnapshot | None:
    """Load and merge TaskStore + memory/tasks data for ``task_id``."""
    from chaos_agent.persistence.task_store import get_task_store

    store = await get_task_store()
    record = await store.get(task_id)
    session, has_increment_log = _read_task_session(task_id)
    return TaskSnapshot.from_sources(
        task_id=task_id,
        record=record,
        session=session,
        has_increment_log=has_increment_log,
        tui_session_id=tui_session_id,
    )


async def build_recover_initial_from_task_snapshot(
    snapshot: TaskSnapshot,
    *,
    record_task_id: str,
    agents: dict | None = None,
    kubeconfig_override: str | None = None,
    checkpoint_values: dict | None = None,
    connection_override: dict | None = None,
) -> dict | None:
    """Build recover initial_state from a merged TaskSnapshot.

    ``connection_override`` (caller-carried runtime connection, e.g. the
    L4 payload snapshot of the session-bound environment) outranks every
    seed value below — see ``build_recover_initial_from_checkpoint``.
    """
    checkpoint_values = checkpoint_values or {}
    if not snapshot.has_recover_context and not _checkpoint_has_recover_context(checkpoint_values):
        return None

    skill_name = snapshot.skill_name or read_active_skill_name(checkpoint_values)
    skill_case_content = checkpoint_values.get("skill_case_content", "") or ""
    if skill_name and agents:
        try:
            registry = agents.get("skill_registry")
            if registry:
                skill_case_content = registry.activate(skill_name)
        except Exception:
            logger.debug("Failed to activate skill %s", skill_name, exc_info=True)

    from chaos_agent.agent.state_mgmt.recovery_state import build_recover_initial_from_checkpoint

    inject_verification_summary = _rebuild_inject_verification_summary(snapshot.verification)
    if not inject_verification_summary:
        inject_verification_summary = checkpoint_values.get("inject_verification_summary", "") or ""

    fault_spec = _merge_snapshot_checkpoint_fault_spec(snapshot, checkpoint_values)
    target = snapshot.target or _coerce_json_dict(checkpoint_values.get("target"))
    params = snapshot.params or _coerce_json_dict(checkpoint_values.get("params"))
    inject_context = snapshot.inject_context or checkpoint_values.get("inject_context") or None

    seed = {
        "tui_session_id": snapshot.tui_session_id or checkpoint_values.get("tui_session_id", ""),
        "experiment_uid": (
            snapshot.experiment_uid
            or checkpoint_values.get("experiment_uid", "")
            or ""
        ),
        # Liability axis (B76 review G): birth + death registries for the
        # recover finale's residual sweep. Round-32 made both wings DB
        # columns, so the record fills sessions whose checkpoint predates
        # them — the live checkpoint (fresher) wins when it carries the
        # wings at all. The combo discriminator (round-32b) rides the same
        # precedence: checkpoint first, record for DB-only recovery.
        "owned_experiment_uids": (
            list(checkpoint_values.get("owned_experiment_uids") or [])
            or _coerce_json_list(snapshot.record.get("owned_experiment_uids"))
        ),
        "retired_experiment_uids": (
            list(checkpoint_values.get("retired_experiment_uids") or [])
            or _coerce_json_list(snapshot.record.get("retired_experiment_uids"))
        ),
        "combo_native_issued": _recover_marker_value(
            checkpoint_values.get("combo_native_issued"),
            snapshot.record.get("combo_native_issued"),
        ),
        "skill_name": skill_name,
        "fault_type": snapshot.fault_type or checkpoint_values.get("fault_type", ""),
        "skill_case_content": skill_case_content,
        "inject_verification_summary": inject_verification_summary,
        # Structured inject facts the recover prompt needs to reconcile the
        # full blast radius — not just the primary target.  Record-persisted
        # values win; the live checkpoint fills fields older records lack.
        "blast_radius_detail": (
            snapshot.blast_radius_detail
            or str(checkpoint_values.get("blast_radius_detail") or "")
        ),
        "side_effects": (
            dict(snapshot.side_effects)
            or dict(checkpoint_values.get("side_effects") or {})
        ),
        "baseline_data": snapshot.record.get("baseline_data") or checkpoint_values.get("baseline_data"),
        "fault_spec": fault_spec,
        "target": target,
        "params": params,
        "params_flags": list(checkpoint_values.get("params_flags") or []),
        # Retired old-key fallback (l4-contract-faithfulness): checkpoint
        # values carry ``duration_seconds``; the legacy ``duration`` key
        # is no longer hydrated.
        "duration_seconds": int(
            checkpoint_values.get("duration_seconds")
            or fault_spec.get("duration_seconds")
            or 0
        ),
        "fault_scope": checkpoint_values.get("fault_scope", ""),
        "fault_target": checkpoint_values.get("fault_target", ""),
        "fault_action": checkpoint_values.get("fault_action", ""),
        "kubeconfig": (
            kubeconfig_override
            or snapshot.record.get("kubeconfig")
            or checkpoint_values.get("kubeconfig")
            or ""
        ),
        "kube_context": snapshot.record.get("kube_context") or checkpoint_values.get("kube_context", "") or "",
        "kubewiz_cluster_uuid": checkpoint_values.get("kubewiz_cluster_uuid", "") or "",
        "kubewiz_profile": checkpoint_values.get("kubewiz_profile", "") or "",
        # Injection-time channel, carried for the builder's cross-channel
        # guard only — the recover graph's own channel comes from the
        # carried connection override or the caller's settings, never this
        # frozen value.
        "kube_connection_mode": checkpoint_values.get("kube_connection_mode", "") or "",
        "injection_method": (
            snapshot.injection_method or checkpoint_values.get("injection_method")
        ),
        # Durable fault identity (R4): the finalize-persisted handle wins so a
        # native fault (no UID) survives into the recover graph; the registry
        # hydration in build_recover_initial_from_checkpoint fills older tasks.
        "fault_handle": (
            dict(snapshot.fault_handle)
            or checkpoint_values.get("fault_handle")
            or None
        ),
        "execution_artifacts": (
            list(snapshot.execution_artifacts)
            or list(checkpoint_values.get("execution_artifacts") or [])
        ),
        "kubectl_exec_pod_name": (
            snapshot.record.get("kubectl_exec_pod_name")
            or checkpoint_values.get("kubectl_exec_pod_name")
        ),
        "created_at": str(
            snapshot.record.get("gmt_create")
            or checkpoint_values.get("created_at")
            or checkpoint_values.get("gmt_create")
            or ""
        ),
        # Identity axes are birth-constant facts (the wing-field
        # precedent, round-32): the live checkpoint wins when it carries
        # them, and the DB record leg fills sessions whose checkpoint is
        # gone — a checkpoint-only read would silently re-scope the recover
        # task's own rows to unfiltered on the DB-only recovery path
        # (round-32b F-6; same seam as the wings and the combo marker).
        "tenant_id": (
            checkpoint_values.get("tenant_id", "")
            or str(snapshot.record.get("tenant_id") or "")
        ),
        # Durable ownership fact (see recovery_state builder): rides the
        # same checkpoint carrier as tenant_id, never the connection
        # override — a recovered task keeps its home workspace. Same
        # checkpoint-first, record-fallback shape as tenant_id above
        # (mirrors its two-carrier discipline deliberately — a workspace
        # copy of tenant's original single-carrier line was exactly the
        # F-6 defect this shape replaces).
        "workspace_id": (
            checkpoint_values.get("workspace_id", "")
            or str(snapshot.record.get("workspace_id") or "")
        ),
        "messages": list(checkpoint_values.get("messages") or []),
    }
    return build_recover_initial_from_checkpoint(
        seed,
        snapshot.task_id,
        record_task_id=record_task_id,
        inject_context=inject_context,
        connection_override=connection_override,
    )


async def resolve_recover_initial_state(
    inject_task_id: str,
    *,
    record_task_id: str,
    agents: dict | None = None,
    checkpoint_values: dict | None = None,
    tui_session_id: str = "",
    kubeconfig_override: str | None = None,
    connection_override: dict | None = None,
) -> RecoverInitialResolution | None:
    """Resolve recover graph input from TaskSnapshot plus optional checkpoint.

    ``connection_override`` carries the recovering caller's runtime
    connection (channel + credentials).  It outranks both the persisted
    snapshot record and the checkpoint-frozen inject values, so a
    cross-user / cross-time recover runs with the caller's identity
    instead of the injector's (see ``recovery_state`` module docstring).
    Entries that carry no connection pass ``None`` and behave exactly as
    before — frozen values remain the fallback.

    Persistent task data is always attempted first so ``.jsonl`` increments are
    considered even when a LangGraph checkpoint is still available.  The
    checkpoint is then used only to fill missing live-only fields and to retain
    baseline messages for session persistence.
    """
    checkpoint_values = checkpoint_values or {}
    snapshot = None
    try:
        snapshot = await load_task_snapshot(inject_task_id, tui_session_id=tui_session_id)
    except Exception:
        logger.debug("Failed to load TaskSnapshot for recover task %s", inject_task_id, exc_info=True)

    if snapshot is not None:
        initial = await build_recover_initial_from_task_snapshot(
            snapshot,
            record_task_id=record_task_id,
            agents=agents,
            kubeconfig_override=kubeconfig_override,
            checkpoint_values=checkpoint_values,
            connection_override=connection_override,
        )
        if initial is not None:
            return RecoverInitialResolution(
                initial_state=initial,
                source_values=_source_values_from_initial(
                    initial,
                    inject_task_id,
                    checkpoint_values=checkpoint_values,
                    snapshot=snapshot,
                ),
                snapshot=snapshot,
                checkpoint_values=dict(checkpoint_values),
                source="snapshot",
            )

    if not _checkpoint_has_recover_context(checkpoint_values):
        return None

    from chaos_agent.agent.state_mgmt.recovery_state import build_recover_initial_from_checkpoint

    initial = build_recover_initial_from_checkpoint(
        checkpoint_values,
        inject_task_id,
        record_task_id=record_task_id,
        kubeconfig_override=kubeconfig_override,
        tui_session_id_override=tui_session_id or None,
        connection_override=connection_override,
    )
    return RecoverInitialResolution(
        initial_state=initial,
        source_values=_source_values_from_initial(
            initial,
            inject_task_id,
            checkpoint_values=checkpoint_values,
        ),
        snapshot=None,
        checkpoint_values=dict(checkpoint_values),
        source="checkpoint",
    )


def _checkpoint_has_recover_context(values: dict | None) -> bool:
    values = values or {}
    return bool(
        values.get("experiment_uid")
        or read_active_skill_name(values)
        or values.get("fault_spec")
        or values.get("target")
    )


def _merge_snapshot_checkpoint_fault_spec(
    snapshot: TaskSnapshot,
    checkpoint_values: dict,
) -> dict:
    checkpoint_spec = _coerce_json_dict(checkpoint_values.get("fault_spec"))
    merged = dict(checkpoint_spec)
    if snapshot.stored_fault_spec:
        merged.update(dict(snapshot.stored_fault_spec))

    scope, fault_target, fault_action = fault_parts_from_name(snapshot.fault_type)
    if snapshot.target:
        merged["namespace"] = snapshot.target.get("namespace", "") or ""
        merged["scope"] = snapshot.target.get("resource_type", "") or scope or merged.get("scope", "")
        merged["names"] = list(snapshot.target.get("names") or [])
        merged["labels"] = dict(snapshot.target.get("labels") or {})
    elif scope and not merged.get("scope"):
        merged["scope"] = scope

    if fault_target:
        merged["fault_target"] = fault_target
    if fault_action:
        merged["fault_action"] = fault_action

    checkpoint_params = _coerce_json_dict(checkpoint_values.get("params"))
    if snapshot.params:
        merged["params"] = dict(snapshot.params or {})
    elif "params" not in merged:
        merged["params"] = dict(checkpoint_params)

    merged.setdefault("params_flags", list(checkpoint_values.get("params_flags") or []))
    merged.setdefault(
        "duration_seconds",
        int(checkpoint_values.get("duration_seconds") or 0),
    )
    merged.setdefault("source", "task_snapshot_rebuild")
    merged.setdefault("user_description", "")
    return merged


def _source_values_from_initial(
    initial: dict,
    inject_task_id: str,
    *,
    checkpoint_values: dict | None = None,
    snapshot: TaskSnapshot | None = None,
) -> dict:
    checkpoint_values = checkpoint_values or {}
    source_values = dict(checkpoint_values)
    fault_spec = _coerce_json_dict(initial.get("fault_spec"))
    target = {
        "namespace": fault_spec.get("namespace", ""),
        "names": list(fault_spec.get("names") or []),
        "labels": dict(fault_spec.get("labels") or {}),
        "resource_type": fault_spec.get("scope", ""),
    }
    source_values.update({
        "task_id": inject_task_id,
        "tui_session_id": initial.get("tui_session_id", "") or "",
        "experiment_uid": initial.get("experiment_uid", ""),
        "skill_name": read_active_skill_name(initial),
        "fault_type": (
            _fault_type_from_fault_spec(fault_spec)
            or checkpoint_values.get("fault_type", "")
        ),
        "fault_spec": fault_spec,
        "target": target,
        "params": dict(fault_spec.get("params") or {}),
        "inject_context": initial.get("inject_context", "") or "",
        "inject_verification_summary": initial.get("inject_verification_summary", "") or "",
        "baseline_data": initial.get("baseline_data"),
        "kubeconfig": initial.get("kubeconfig", "") or "",
        "kube_context": initial.get("kube_context", "") or "",
        "injection_method": initial.get("injection_method"),
        "execution_artifacts": list(initial.get("execution_artifacts") or []),
        "kubectl_exec_pod_name": initial.get("kubectl_exec_pod_name"),
        "created_at": initial.get("created_at", "") or "",
        "messages": list(checkpoint_values.get("messages") or []),
    })
    if snapshot is not None and isinstance(snapshot.verification, dict):
        source_values["verification"] = dict(snapshot.verification)
    return source_values


__all__ = [
    "RecoverInitialResolution",
    "TaskSnapshot",
    "build_recover_initial_from_task_snapshot",
    "load_task_snapshot",
    "resolve_recover_initial_state",
]
