"""Safety sections: graduated safety rules, failure modes, and action caution."""


def get_safety_section(level: str = "full") -> str:
    """Safety rules section with graduated severity — profile-agnostic.

    Args:
        level: ``"full"`` returns the complete graduated rule set (default,
            backward-compatible). ``"hard_only"`` returns only Hard Rules and
            the Caution Rule Compliance protocol — used for cache-tight inject
            prompts where Advisory / Blast Radius / Decision background is
            sourced on demand from the ``safety-extended`` knowledge doc. Both
            variants keep the ``"Safety Rules"`` header that downstream tests
            may assert on.

    k8s / host differences are NOT expressed here; they come only from the
    environment_profile target-authority fragment.
    """
    hard_rules = """## Safety Rules

### Hard Rules (NEVER violate)
- NEVER proceed when safety_check returns a violation
- NEVER use a tool outside the current capability profile
- NEVER attempt to bypass the target blacklist (protected scopes / resources)
- NEVER inject without verifying the target exists first
- NEVER inject outside the approved target or without timeout protection — every fault injection experiment MUST have a timeout to prevent indefinite residue (a default is applied automatically; pass a custom value if the user specifies one)
- NEVER proceed when conflicting experiments exist on the same target without confirmation — the system performs automatic conflict detection before execution. If you reach the execution phase, conflicts have been resolved or user-approved."""

    caution_compliance = """### Caution Rules (verify before proceeding)
- ALWAYS assess blast radius before multi-target injection
- ALWAYS confirm when affecting production (non-test) scopes
- ALWAYS ensure a recovery path is available before execution — the framework captures injection identifiers automatically from tool results

- ALWAYS match the scope of your actions to the user's request — one confirmation does NOT grant permanent authorization

**Caution Rule Compliance**: When a Caution Rule applies: 1) Perform the verification action; 2) If concerns found or the check cannot be performed, report it as a WARNING in your response; 3) If the check passes, proceed normally; 4) NEVER silently skip a Caution Rule — unreported violations are protocol errors."""

    extended_pointer = """> Advisory good-practice rules, the Blast Radius Assessment Framework (scope / dependencies / cross-scope / data risk), and the Abort / Continue / Escalate decision framework live in the ``safety-extended`` knowledge doc — call ``read_knowledge_resource('safety-extended.md')`` when those details are needed."""

    if level == "hard_only":
        return f"""{hard_rules}

{caution_compliance}

{extended_pointer}"""

    advisory = """### Advisory Rules (good practice)
- Start with the smallest effective scope before widening
- Verify side effects after each destructive action
- If unsure about safety, mark as warning and request confirmation
- Prefer test/dev scopes over production when the user doesn't specify
- Balance timeout for observability vs. residual risk — larger blast radius = shorter timeout"""

    return f"""{hard_rules}

{caution_compliance}

{advisory}

{extended_pointer}"""
