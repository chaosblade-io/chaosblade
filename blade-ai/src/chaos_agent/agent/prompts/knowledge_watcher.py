"""Knowledge watcher: hot-reload knowledge_registry on *.md changes.

Thin facade over ``utils.file_watcher.FileSystemWatcher``. The
``knowledge/`` directory is flat (no subdirs of interest), so the watcher
runs non-recursive. Only ``.md`` changes count — anything else (cache
files, editor swap files, etc.) is ignored.

Reloading just rebuilds the module-level registry cache; the actual
markdown content is still read on demand by ``read_knowledge_resource``,
so the LLM picks up new content on its next tool call without any
prompt-side coordination.
"""
from pathlib import Path

from chaos_agent.agent.prompts.knowledge_registry import (
    KNOWLEDGE_DIR,
    get_knowledge_registry,
    rebuild_registry,
)
from chaos_agent.utils.file_watcher import FileSystemWatcher, WatchSpec


def _is_knowledge_file(path: Path) -> bool:
    return path.suffix == ".md"


class KnowledgeWatcher:
    """Backward-compatible facade over generic FileSystemWatcher."""

    def __init__(self, knowledge_dir: Path | None = None):
        target_dir = knowledge_dir if knowledge_dir is not None else KNOWLEDGE_DIR
        self._impl = FileSystemWatcher(WatchSpec(
            label="Knowledge",
            path=target_dir,
            file_filter=_is_knowledge_file,
            on_change=rebuild_registry,
            counter=lambda: len(get_knowledge_registry()),
            recursive=False,
        ))

    def start(self) -> None:
        self._impl.start()

    def stop(self) -> None:
        self._impl.stop()
