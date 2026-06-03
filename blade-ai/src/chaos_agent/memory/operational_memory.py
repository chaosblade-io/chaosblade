"""Operational memory: MEMORY.md long-term experience read/write (Layer 3).

Follows the Claude Code memdir/memoryTypes.ts pattern with 4 memory types
(user_preference, feedback, project, reference) plus save/access/trust guidance.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Memory type taxonomy (aligned with Claude Code's memoryTypes.ts)
# ---------------------------------------------------------------------------

MEMORY_TYPES = {
    "user_preference": {
        "description": "User's preferred settings and patterns for chaos experiments",
        "when_to_save": (
            "When the user explicitly states a preference or when you learn "
            "about their role/responsibilities"
        ),
        "body_structure": "Lead with the preference, then **Why:** and **How to apply:** lines",
        "example": "User prefers confirmation before every injection. Private preference.",
    },
    "feedback": {
        "description": (
            "Guidance from past experiments — both what to avoid and what to keep doing"
        ),
        "when_to_save": (
            "After a failed experiment, unexpected result, OR when user confirms "
            "a non-obvious approach worked"
        ),
        "body_structure": "Lead with the rule, then **Why:** and **How to apply:** lines",
        "example": (
            "pod-kill on deployment with 1 replica causes service outage. "
            "Reason: no replica to absorb traffic."
        ),
    },
    "project": {
        "description": (
            "Project-specific K8s topology, namespaces, and conventions "
            "not derivable from kubectl"
        ),
        "when_to_save": (
            "When discovering important cluster/service topology that isn't "
            "obvious from kubectl get"
        ),
        "body_structure": "Lead with the fact/decision, then **Why:** and **How to apply:** lines",
        "example": (
            "Production namespace: prod, staging namespace: staging. "
            "prod has PDB on all deployments."
        ),
    },
    "reference": {
        "description": "Non-obvious blade command patterns and gotchas",
        "when_to_save": "When finding a non-obvious but correct command pattern",
        "body_structure": "Command pattern + when to use it + gotchas",
        "example": (
            "blade_create for network-delay requires --interface flag on CNI-based clusters"
        ),
    },
}

# Aligned with Claude Code's WHAT_NOT_TO_SAVE_SECTION
MEMORY_SAVE_GUIDANCE = """### What NOT to save in memory
- Transient state (current pod list, temporary errors) — use kubectl instead
- Information already in skill instructions
- K8s topology derivable from kubectl get nodes/pods — only save what's NOT derivable
- Redundant entries — search before saving

### Before recommending from memory
- Verify the entry still applies (cluster may have changed)
- Don't apply stale preferences without confirmation
- A memory that names a specific pod/deployment may no longer exist — verify before acting
"""

# Aligned with Claude Code's WHEN_TO_ACCESS_SECTION + TRUSTING_RECALL_SECTION
MEMORY_ACCESS_GUIDANCE = """### When to access memories
- When memories seem relevant to the current fault injection task
- When the user references a prior experiment or preference
- If the user says to ignore/not use memory: proceed as if memory were empty

### Trusting what you recall
A memory that names a specific resource is a claim that it existed when the memory was written.
It may have been renamed, removed, or never existed. Before recommending:
- If the memory names a namespace: verify with kubectl
- If the memory names a pod/deployment: check it still exists
"The memory says X exists" is not the same as "X exists now."
"""

# Default memory template using the 4-type taxonomy
DEFAULT_MEMORY_CONTENT = """# Operational Memory

## User Preferences
(No preferences recorded yet)

## Feedback
(No feedback recorded yet)

## Project Knowledge
(No project knowledge recorded yet)

## Reference Commands
(No reference commands recorded yet)
"""

# Valid memory type names for validation
VALID_MEMORY_TYPES = set(MEMORY_TYPES.keys())


class OperationalMemory:
    """Read/write MEMORY.md for cross-task operational knowledge.

    Supports typed memory operations aligned with the Claude Code
    memdir/memoryTypes.ts 4-type taxonomy.
    """

    def __init__(self, memory_path: Path):
        self.memory_path = memory_path

    def read(self) -> str:
        """Read operational memory content. Creates default if not exists."""
        if not self.memory_path.exists():
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            self.memory_path.write_text(DEFAULT_MEMORY_CONTENT, encoding="utf-8")
            return DEFAULT_MEMORY_CONTENT

        return self.memory_path.read_text(encoding="utf-8")

    def write(self, content: str) -> None:
        """Overwrite operational memory content."""
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_path.write_text(content, encoding="utf-8")
        logger.info("Operational memory updated")

    def append_section(self, section: str, content: str) -> None:
        """Append content to a specific section in MEMORY.md.

        If the section exists, appends after it. If not, creates it.
        """
        current = self.read()
        section_header = f"## {section}"

        if section_header in current:
            # Find the section and append after its content
            parts = current.split(section_header, 1)
            if len(parts) == 2:
                after_header = parts[1]
                # Find the next ## header or end of file
                next_section = after_header.find("\n## ")
                if next_section == -1:
                    # Last section, append at end
                    new_content = current.rstrip() + "\n" + content + "\n"
                else:
                    # Insert before next section
                    insert_pos = len(parts[0]) + len(section_header) + next_section
                    new_content = current[:insert_pos] + "\n" + content + current[insert_pos:]
                self.write(new_content)
        else:
            # Section doesn't exist, append at end
            new_content = current.rstrip() + f"\n\n{section_header}\n{content}\n"
            self.write(new_content)

    def save_typed_memory(self, memory_type: str, content: str) -> None:
        """Save a typed memory entry to the appropriate section.

        Automatically maps memory_type to the corresponding section header
        in MEMORY.md.

        Args:
            memory_type: One of 'user_preference', 'feedback', 'project', 'reference'.
            content: The memory content to save.

        Raises:
            ValueError: If memory_type is not a valid type.
        """
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"Invalid memory type '{memory_type}'. "
                f"Valid types: {', '.join(sorted(VALID_MEMORY_TYPES))}"
            )

        # Map memory type to section header
        type_to_section = {
            "user_preference": "User Preferences",
            "feedback": "Feedback",
            "project": "Project Knowledge",
            "reference": "Reference Commands",
        }
        section_name = type_to_section[memory_type]
        self.append_section(section_name, f"- {content}")
        logger.info(f"Saved {memory_type} memory to section '{section_name}'")

    def search_memories(self, query: str) -> list[str]:
        """Search memory for entries matching the query.

        Simplified version of Claude Code's findRelevantMemories.ts.
        Uses keyword matching instead of a side-query LLM call.

        Args:
            query: Search query string.

        Returns:
            List of matching memory lines.
        """
        content = self.read()
        if not content or not query:
            return []

        query_lower = query.lower()
        query_terms = query_lower.split()

        results = []
        for line in content.split("\n"):
            line_lower = line.lower()
            # Match if any query term appears in the line
            if any(term in line_lower for term in query_terms) and line.strip():
                # Skip section headers and the document title
                if not line.startswith("# ") and not line.startswith("## "):
                    # Skip empty placeholder lines
                    if not line.strip().startswith("("):
                        results.append(line.strip())

        return results
