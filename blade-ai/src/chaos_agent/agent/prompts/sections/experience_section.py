"""Experience section: EXPERIENCE.md experience accumulation."""


def get_experience_section() -> str:
    """Accumulated experience from past operations (loaded from ~/.blade-ai/EXPERIENCE.md).

    Returns empty string if no EXPERIENCE.md exists, so the section is simply
    omitted from the assembled prompt.
    """
    from chaos_agent.agent.experience import load_agent_experience

    content = load_agent_experience()
    if not content:
        return ""
    return (
        "## Accumulated Experience\n"
        "The following lessons were learned from past operations. Apply them proactively:\n\n"
        f"{content}"
    )
