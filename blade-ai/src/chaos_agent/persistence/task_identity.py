"""Single source of truth for chaos task identity.

Only the **inject** and **recover** pipelines own the concept of a
"task".  Intent clarification, chat, and capability Q&A do not — they
are conversation turns, not tasks (see
``agent.nodes.planning.intent_clarification._allocate_operation_task_id``
for the original statement of this contract).

Consequently a real task identity is *minted* in exactly one format —
``task-<uuid4>`` — and everything else (LangGraph thread ids such as
``chaos-<session>``, per-turn ids such as ``turn-<hex>``, placeholder
strings such as ``"unknown"``, or an absent value) means **"there is no
task"**.

Why this module exists
----------------------
The persistence layer used to guard its writes with a *blacklist*
(``task_id.startswith("turn-")``).  A blacklist only rejects the dirty
ids it happens to know about, so any new caller passing a non-task
string silently created a bogus ``tasks`` row — the platform's
``chaos-<session>`` thread id did exactly that, producing "ghost"
experiments that surfaced in the recover flow with no injection state
to roll back.

The guards therefore use a **whitelist** built on :func:`is_real_task_id`,
and this module is its only home: callers must not re-implement the
``task-`` prefix check locally (the fix for the ``namespace`` validator
was needed twice precisely because that judgement had been hardcoded in
several places).
"""

import uuid

# The one and only prefix that denotes a real, persistable task.
TASK_ID_PREFIX = "task-"

__all__ = ["TASK_ID_PREFIX", "is_real_task_id", "new_task_id"]


def is_real_task_id(task_id: object) -> bool:
    """Return ``True`` only for a real, persistable task identity.

    A real task id is a non-empty ``str`` starting with
    :data:`TASK_ID_PREFIX`.  Everything else — ``None``, non-``str``
    values, ``""``, conversation thread ids (``chaos-…``), per-turn ids
    (``turn-…``) and placeholders (``"unknown"``) — means *no task*, and
    must never reach the ``tasks`` / ``task_details`` / ``task_spans``
    tables.
    """
    return isinstance(task_id, str) and task_id.startswith(TASK_ID_PREFIX)


def new_task_id() -> str:
    """Mint a fresh real task identity.

    Called at the moment a dialogue transitions into the inject or
    recover pipeline, so the task identity is born inside the pipeline
    that owns it.
    """
    return f"{TASK_ID_PREFIX}{uuid.uuid4()}"
