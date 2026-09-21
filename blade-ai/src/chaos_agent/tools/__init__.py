"""Tool definitions aggregated for binding into LangGraph nodes.

Carrier-specific CLI wrappers (ChaosBlade) live in their provider package
(``agent/providers/chaosblade/cli.py``) and are intentionally NOT re-exported
here — the generic layer must not reach a carrier tool implementation via
this package's import surface (phase-11 carrier-import-boundary).

Namespace invariant: a submodule filename must never equal an exported
symbol name in this package — ``from <pkg>.<sub> import <sub>`` rebinds the
package attribute from the module to the symbol, so attribute-walking and
the import machinery would then disagree about one name (the retired
``tools.kubectl`` / ``tools.web_search`` collision; guarded by
``tests/test_tools/test_package_namespace_invariant.py``).
"""

from chaos_agent.tools.file_reader import safe_read_file
from chaos_agent.tools.file_search import safe_search_files
from chaos_agent.tools.file_writer import safe_write_file
from chaos_agent.tools.host_cmd import host_inject, host_read
from chaos_agent.tools.knowledge_reader import read_knowledge_resource
from chaos_agent.tools.kubectl_cli import kubectl, kubectl_read
from chaos_agent.tools.web_search_tool import web_search

__all__ = [
    "host_inject",
    "host_read",
    "kubectl",
    "kubectl_read",
    "safe_read_file",
    "safe_write_file",
    "safe_search_files",
    "read_knowledge_resource",
    "web_search",
]
