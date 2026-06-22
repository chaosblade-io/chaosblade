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

from chaos_agent.agent.fault_spec import (
    fault_parts_from_name,
    fault_spec_from_legacy_state,
)
from chaos_agent.agent.skill_identity import read_active_skill_name

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


def _session_messages_to_langchain(messages: list[dict]) -> list:
    """Best-effort conversion of SessionStore message dicts back to messages."""
    if not messages:
        return []

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    tool_call_names: dict[str, str] = {}
    out: list = []

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type") or ""
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        msg_id = msg.get("id") if isinstance(msg.get("id"), str) else None

        if msg_type == "ai":
            tool_calls = msg.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id")
                    tc_name = tc.get("name")
                    if isinstance(tc_id, str) and isinstance(tc_name, str):
                        tool_call_names[tc_id] = tc_name
            kwargs = {"content": content}
            if msg_id:
                kwargs["id"] = msg_id
            if isinstance(tool_calls, list) and tool_calls:
                kwargs["tool_calls"] = tool_calls
            try:
                out.append(AIMessage(**kwargs))
            except Exception:
                out.append(AIMessage(content=content))
            continue

        if msg_type == "tool":
            tool_call_id = msg.get("tool_call_id") or f"session_tool_{idx}"
            name = msg.get("name") or tool_call_names.get(tool_call_id, "")
            kwargs = {"content": content, "tool_call_id": tool_call_id}
            if isinstance(name, str) and name:
                kwargs["name"] = name
            if msg_id:
                kwargs["id"] = msg_id
            try:
                out.append(ToolMessage(**kwargs))
            except Exception:
                continue
            continue

        if msg_type == "tool_execution":
            detail = msg.get("detail") if isinstance(msg.get("detail"), dict) else {}
            source = detail.get("source") if isinstance(detail.get("source"), str) else ""
            stdout = detail.get("stdout_preview") if isinstance(detail.get("stdout_preview"), str) else ""
            text = stdout or content
            kwargs = {
                "content": text,
                "tool_call_id": msg.get("tool_call_id") or f"session_exec_{idx}",
            }
            if source:
                kwargs["name"] = source
            try:
                out.append(ToolMessage(**kwargs))
            except Exception:
                continue
            continue

        try:
            if msg_type == "human":
                out.append(HumanMessage(content=content, id=msg_id))
            elif msg_type == "system":
                out.append(SystemMessage(content=content, id=msg_id))
        except Exception:
            continue

    return out


def _extract_blade_uid_from_session(
    session: dict | None,
    *,
    prefer_messages: bool = False,
) -> str:
    """Recover blade_uid from task file result/message data."""
    if not isinstance(session, dict):
        return ""

    messages = session.get("messages")

    def uid_from_result_summary() -> str:
        result_data = _session_result_data(session)
        blade_uid = result_data.get("blade_uid")
        return blade_uid if isinstance(blade_uid, str) else ""

    def uid_from_messages() -> str:
        if not isinstance(messages, list):
            return ""
        try:
            from chaos_agent.agent.nodes.execute_loop import _extract_blade_uid_from_messages

            uid = _extract_blade_uid_from_messages(_session_messages_to_langchain(messages))
            if uid:
                return uid
        except Exception:
            logger.debug("Failed to extract blade_uid from session messages", exc_info=True)

        from chaos_agent.utils.blade_uid import extract_blade_uid

        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            detail = msg.get("detail") if isinstance(msg.get("detail"), dict) else {}
            command = detail.get("command") if isinstance(detail.get("command"), str) else ""
            if "blade" not in command or "create" not in command:
                continue
            chunks = [
                detail.get("stdout_preview"),
                detail.get("stderr"),
                msg.get("content"),
            ]
            text = "\n".join(c for c in chunks if isinstance(c, str) and c)
            uid = extract_blade_uid(text)
            if uid:
                return uid
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

        return build_inject_context(_session_messages_to_langchain(messages))
    except Exception:
        logger.debug("Failed to build inject_context from session", exc_info=True)
        return ""


def _rebuild_inject_verification_summary(verification: dict | None) -> str:
    """Rebuild inject_verification_summary from the stored verification dict."""
    if not verification or not isinstance(verification, dict):
        return ""
    layer2 = verification.get("layer2")
    if not layer2 or not isinstance(layer2, dict):
        return ""
    details = layer2.get("details", "")
    if not details:
        return ""
    return f"Layer2={layer2.get('status', 'unknown')}, Details={details}"


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
    but recover should still mine .jsonl/.jsonl.compacted for blade_uid and
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
    blade_uid: str = ""
    skill_name: str = ""
    verification: dict | None = None
    inject_context: str = ""
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
        record_target = _coerce_json_dict(record.get("target"))
        session_target = _target_from_result_data(result_data)
        record_params = _coerce_json_dict(record.get("params"))
        session_params = _coerce_json_dict(result_data.get("params"))
        session_blade_uid = _extract_blade_uid_from_session(
            session,
            prefer_messages=has_increment_log,
        )
        record_skill_name = record.get("skill_name") or record.get("fault_type") or ""
        session_skill_name = result_data.get("fault_type") or ""
        session_inject_context = _build_inject_context_from_session(session)

        resolved_tui_session_id = tui_session_id
        if not resolved_tui_session_id and isinstance(session, dict):
            session_tui_session_id = session.get("tui_session_id")
            if isinstance(session_tui_session_id, str):
                resolved_tui_session_id = session_tui_session_id

        if has_increment_log:
            target = session_target or record_target
            params = session_params or record_params
            blade_uid = session_blade_uid or record.get("blade_uid") or ""
            skill_name = session_skill_name or record_skill_name
            verification = result_data.get("verification") or record.get("verification")
            inject_context = session_inject_context or record.get("inject_context") or ""
        else:
            target = record_target or session_target
            params = record_params or session_params
            blade_uid = record.get("blade_uid") or session_blade_uid or ""
            skill_name = record_skill_name or session_skill_name
            verification = record.get("verification") or result_data.get("verification")
            inject_context = record.get("inject_context") or session_inject_context or ""

        return cls(
            task_id=task_id,
            record=record,
            session=session,
            result_data=result_data,
            has_increment_log=has_increment_log,
            target=target,
            params=params,
            blade_uid=blade_uid,
            skill_name=skill_name,
            verification=verification if isinstance(verification, dict) else None,
            inject_context=inject_context,
            tui_session_id=resolved_tui_session_id,
        )

    @property
    def has_recover_context(self) -> bool:
        """Whether this snapshot has enough information to attempt recovery."""
        return bool(self.blade_uid) or (bool(self.skill_name) and bool(self.target))

    def fault_spec(self) -> dict:
        spec = fault_spec_from_legacy_state(
            {
                "target": self.target,
                "params": self.params,
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
            "blade_uid": self.blade_uid,
            "skill_name": self.skill_name,
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
) -> dict | None:
    """Build recover initial_state from a merged TaskSnapshot."""
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

    from chaos_agent.agent.recovery_state import build_recover_initial_from_checkpoint

    inject_verification_summary = _rebuild_inject_verification_summary(snapshot.verification)
    if not inject_verification_summary:
        inject_verification_summary = checkpoint_values.get("inject_verification_summary", "") or ""

    fault_spec = _merge_snapshot_checkpoint_fault_spec(snapshot, checkpoint_values)
    target = snapshot.target or _coerce_json_dict(checkpoint_values.get("target"))
    params = snapshot.params or _coerce_json_dict(checkpoint_values.get("params"))
    inject_context = snapshot.inject_context or checkpoint_values.get("inject_context") or None

    seed = {
        "tui_session_id": snapshot.tui_session_id or checkpoint_values.get("tui_session_id", ""),
        "blade_uid": snapshot.blade_uid or checkpoint_values.get("blade_uid", "") or "",
        "skill_name": skill_name,
        "skill_case_content": skill_case_content,
        "inject_verification_summary": inject_verification_summary,
        "baseline_data": snapshot.record.get("baseline_data") or checkpoint_values.get("baseline_data"),
        "fault_spec": fault_spec,
        "target": target,
        "params": params,
        "params_flags": list(checkpoint_values.get("params_flags") or []),
        "duration_seconds": int(
            checkpoint_values.get("duration_seconds")
            or checkpoint_values.get("duration")
            or fault_spec.get("duration_seconds")
            or 0
        ),
        "blade_scope": checkpoint_values.get("blade_scope", ""),
        "blade_target": checkpoint_values.get("blade_target", ""),
        "blade_action": checkpoint_values.get("blade_action", ""),
        "kubeconfig": (
            kubeconfig_override
            or snapshot.record.get("kubeconfig")
            or checkpoint_values.get("kubeconfig")
            or ""
        ),
        "kube_context": snapshot.record.get("kube_context") or checkpoint_values.get("kube_context", "") or "",
        "kubewiz_cluster_uuid": checkpoint_values.get("kubewiz_cluster_uuid", "") or "",
        "kubewiz_profile": checkpoint_values.get("kubewiz_profile", "") or "",
        "injection_method": snapshot.record.get("injection_method") or checkpoint_values.get("injection_method"),
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
        "messages": list(checkpoint_values.get("messages") or []),
    }
    return build_recover_initial_from_checkpoint(
        seed,
        snapshot.task_id,
        record_task_id=record_task_id,
        inject_context=inject_context,
    )


async def resolve_recover_initial_state(
    inject_task_id: str,
    *,
    record_task_id: str,
    agents: dict | None = None,
    checkpoint_values: dict | None = None,
    tui_session_id: str = "",
    kubeconfig_override: str | None = None,
) -> RecoverInitialResolution | None:
    """Resolve recover graph input from TaskSnapshot plus optional checkpoint.

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
        )
        if initial is not None:
            return RecoverInitialResolution(
                initial_state=initial,
                source_values=_source_values_from_initial(
                    initial,
                    inject_task_id,
                    checkpoint_values=checkpoint_values,
                ),
                snapshot=snapshot,
                checkpoint_values=dict(checkpoint_values),
                source="snapshot",
            )

    if not _checkpoint_has_recover_context(checkpoint_values):
        return None

    from chaos_agent.agent.recovery_state import build_recover_initial_from_checkpoint

    initial = build_recover_initial_from_checkpoint(
        checkpoint_values,
        inject_task_id,
        record_task_id=record_task_id,
        kubeconfig_override=kubeconfig_override,
        tui_session_id_override=tui_session_id or None,
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
        values.get("blade_uid")
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

    scope, blade_target, blade_action = fault_parts_from_name(snapshot.skill_name)
    if snapshot.target:
        merged["namespace"] = snapshot.target.get("namespace", "") or ""
        merged["scope"] = snapshot.target.get("resource_type", "") or scope or merged.get("scope", "")
        merged["names"] = list(snapshot.target.get("names") or [])
        merged["labels"] = dict(snapshot.target.get("labels") or {})
    elif scope and not merged.get("scope"):
        merged["scope"] = scope

    if blade_target:
        merged["blade_target"] = blade_target
    if blade_action:
        merged["blade_action"] = blade_action

    checkpoint_params = _coerce_json_dict(checkpoint_values.get("params"))
    if snapshot.params:
        merged["params"] = dict(snapshot.params or {})
    elif "params" not in merged:
        merged["params"] = dict(checkpoint_params)

    merged.setdefault("params_flags", list(checkpoint_values.get("params_flags") or []))
    merged.setdefault(
        "duration_seconds",
        int(checkpoint_values.get("duration_seconds") or checkpoint_values.get("duration") or 0),
    )
    merged.setdefault("source", "task_snapshot_rebuild")
    merged.setdefault("user_description", "")
    return merged


def _source_values_from_initial(
    initial: dict,
    inject_task_id: str,
    *,
    checkpoint_values: dict | None = None,
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
        "blade_uid": initial.get("blade_uid", "") or "",
        "skill_name": read_active_skill_name(initial),
        "fault_spec": fault_spec,
        "target": target,
        "params": dict(fault_spec.get("params") or {}),
        "inject_context": initial.get("inject_context", "") or "",
        "messages": list(checkpoint_values.get("messages") or []),
    })
    return source_values


__all__ = [
    "RecoverInitialResolution",
    "TaskSnapshot",
    "build_recover_initial_from_task_snapshot",
    "load_task_snapshot",
    "resolve_recover_initial_state",
]
