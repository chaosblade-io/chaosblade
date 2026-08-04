"""The intent prompt must lead with the bound environment, and say why.

Grounded in a measured failure, not a style preference. A real session on a
Kubernetes channel had all the environment facts in its system prompt — the
profile statement, the SKILL "适用环境" preconditions, the Inject Flow rule — and
the model still offered the user a menu of Kubernetes / host / python-agent
faults, then submitted a host fault that the tool gate refused.

10-sample A/B runs against that session's prompt isolated two causes:

    variant                                    offtopic  states env  empty reply
    A  as shipped (statement 13th, ~68% in)      2-5/10      0-3/10       1/10
    G  hoisted, Priority 2 waiver intact           3/10        5/10       1/10
       (skill selection 0/10 — worse than A)
    F  hoisted + consequence, no next step          0/10        1/10       7/10
    C  hoisted + "Do NOT ask the user"              0/10      1-5/10       0/10
    H  hoisted + consequence + next step            0/10        8/10       1/10

H shipped. Re-measured on the real ``build_system_prompt`` output afterwards:
offtopic 0/10, k8s skill 10/10, host skill 0/10, empty 0/10.

So three properties are load-bearing and each is asserted below:

1. Position — the section leads the prompt instead of sitting after the output
   contract. A alone shows the same sentence late in the prompt does not work.
2. Consequence + next step — F shows that stating the fact without a next move
   collapses 7/10 replies into an empty proposal envelope.
3. Priority 2 must not waive channel constraints — G shows that with "never which
   fault families you know" left in, hoisting achieves nothing (0/10 skill
   selection).

Prohibitions are deliberately absent: C scored no better than H and conflicts
with the project rule that prompts inform while the submit gate enforces.
"""

from __future__ import annotations

import re

import pytest

from chaos_agent.agent.environment_profiles import get_environment_profile
from chaos_agent.agent.prompts import build_system_prompt
from chaos_agent.agent.prompts.modes import PromptMode

_CATALOG = "- host-chaos-skills: 主机级\n- k8s-chaos-skills: K8s 集群\n"


def _intent_prompt(profile: str = "k8s") -> str:
    return build_system_prompt(
        PromptMode.INTENT,
        fault_spec=None,
        skill_catalog=_CATALOG,
        semantic_only=True,
        profile=profile,
    )


# --------------------------------------------------------------------------
# 1. position
# --------------------------------------------------------------------------

@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_environment_section_leads_the_prompt(profile):
    """It must precede the sections that drive behaviour, not trail them."""
    prompt = _intent_prompt(profile)
    env = prompt.index("# Bound Environment")

    for later in ("# Three Priorities", "# Dialogue Routing", "# Inject Flow",
                  "# Response Contract", "## Skill Index"):
        assert later in prompt
        assert env < prompt.index(later), (
            f"environment section must come before {later!r}; at 68% of the "
            "prompt the same wording measured 2-5/10 off-topic families"
        )

    # Only the role preamble may precede it.
    heads = [m.group(0) for m in re.finditer(r"^# .+$", prompt, re.M)]
    assert heads[:2] == ["# Role", "# Bound Environment"], heads[:3]


# --------------------------------------------------------------------------
# 2. consequence + next step
# --------------------------------------------------------------------------

@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_environment_section_states_consequence_and_next_step(profile):
    prompt = _intent_prompt(profile)
    start = prompt.index("# Bound Environment")
    block = prompt[start:start + 900]

    # settled fact, not a parameter to collect
    assert "rather than a parameter to collect" in block
    # consequence: the gate refuses a mismatch, so proposing one wastes a round
    assert "refused when the intent is submitted" in block
    assert "wasted round" in block
    # the next move — F proved this cannot be omitted
    assert "the useful opening move is" in block


@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_environment_section_informs_without_prohibiting(profile):
    """No imperatives: C scored no better and the gate already enforces."""
    prompt = _intent_prompt(profile)
    start = prompt.index("# Bound Environment")
    block = prompt[start:start + 900]

    for banned in ("Do NOT", "do not offer", "never ask", "禁止", "MUST NOT"):
        assert banned not in block, f"{banned!r} reintroduces a prohibition"


# --------------------------------------------------------------------------
# 3. Priority 2 must not waive the channel constraint
# --------------------------------------------------------------------------

def test_priority_two_no_longer_waives_channel_constraints():
    prompt = _intent_prompt("k8s")
    assert "never which fault families you know" not in prompt, (
        "this waiver made every other change ineffective (0/10 skill selection)"
    )
    assert "equally which fault families" in prompt
    assert "cannot be injected" in prompt


# --------------------------------------------------------------------------
# other phases are untouched
# --------------------------------------------------------------------------

@pytest.mark.parametrize("profile", ["k8s", "host"])
@pytest.mark.parametrize("phase", ["recover", "verify", "default", "execute"])
def test_other_phases_keep_the_original_fragment(profile, phase):
    """Only ``intent`` was measured and only ``intent`` changed."""
    env = get_environment_profile(profile)
    fragment = env.prompt_fragment(phase)
    assert fragment.startswith("## Capability Profile")
    assert "the useful opening move is" not in fragment
    assert "wasted round" not in fragment


@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_intent_phase_has_its_own_fragment(profile):
    env = get_environment_profile(profile)
    intent = env.prompt_fragment("intent")
    default = env.prompt_fragment("default")

    assert intent.startswith("# Bound Environment")
    assert intent != default
    # same underlying statement, extended — not a divergent rewrite
    body = default.split("\n", 1)[1]
    assert body in intent


def test_unknown_profile_still_omits_the_section():
    """An unresolvable channel must not claim an environment."""
    prompt = build_system_prompt(
        PromptMode.INTENT,
        fault_spec=None,
        skill_catalog=_CATALOG,
        semantic_only=True,
        profile=None,
    )
    assert "# Bound Environment" not in prompt
