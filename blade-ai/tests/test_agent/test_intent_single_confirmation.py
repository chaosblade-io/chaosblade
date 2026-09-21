"""Injection asks once, on the confirmation card — not twice.

Observed: a drill needed two confirmations. The user answered 确认, the model
restated the spec and asked again, the user answered 确认 a second time, and only
then did ``submit_fault_intent`` fire and raise the card.

The cause was in the prompt, not the graph. ``Inject Flow`` listed
"5. Summarize / 6. User approves / 7. Submit", and two other sections repeated the
same demand — while ``intent_confirm`` already stops the graph with
``interrupt()`` and shows the full spec. So the model's chat round duplicated a
gate the framework enforces, contradicting Priority 3 ("Minimize dialogue rounds.
Ideal path: … user confirms → submit").

Safe to collapse because, verified in the code:

- "User approves" existed only in the prompt (``intent.py``); nothing in the graph
  required a preceding chat approval.
- The card carries the whole spec (``_format_intent_summary``: fault type, scope,
  target, action, namespace, labels, resource names, parameters, description) plus
  confidence and reasoning, so nothing that was in step 5 is lost.
- Declining on the card keeps both the dialogue and the reviewed spec:
  ``intent_confirm`` deliberately does not touch ``messages`` and leaves
  ``fault_spec`` in place, so "改成 500ms" on the next turn refines instead of
  restarting.
- ``is_complete`` still guards submission, so an incomplete spec cannot slip
  through just because the chat round is gone.

Recovery is excluded on purpose: ``recover_task`` has no interrupt card, so its
chat confirmation is the only gate there and must stay.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.prompts import build_system_prompt
from chaos_agent.agent.prompts.modes import PromptMode

_CATALOG = "- host-chaos-skills: 主机级\n- k8s-chaos-skills: K8s 集群\n"


def _prompt(profile: str = "k8s") -> str:
    return build_system_prompt(
        PromptMode.INTENT,
        fault_spec=None,
        skill_catalog=_CATALOG,
        semantic_only=True,
        profile=profile,
    )


# ---------------------------------------------------------------------------
# the duplicate gate is gone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", ["k8s", "host", None])
@pytest.mark.parametrize("phrase", [
    "User approves",
    "Explicit approval before calling submit",
    "Show complete spec summary to user",
    "Show the complete semantic summary",
    "Never submit without user's explicit approval",
])
def test_no_section_demands_a_chat_approval_before_submitting(profile, phrase):
    assert phrase not in _prompt(profile), (
        f"{phrase!r} duplicates the confirmation card and costs the user an "
        "extra round"
    )


@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_inject_flow_summarizes_and_submits_in_one_turn(profile):
    """The summary stays; only the second approval round goes.

    A first attempt removed step 5 ("Summarize") along with step 6, which lost the
    part the user actually judges by: the observed chat summary carried 预期现象,
    恢复方案 and 注意事项, while the card's ``_format_intent_summary`` prints only
    English field names (fault type / scope / target / action / parameters). The
    card cannot replace that, so summarising is folded into the submit step.
    """
    prompt = _prompt(profile)
    flat = " ".join(prompt.split())  # normalise hard-wrapping (file convention)
    assert "Summarize & Submit" in flat
    assert "call submit_fault_intent immediately" in flat
    assert "Do not stop" in flat and "for injection approval in chat" in flat
    # and it explains why, so the next reader does not "restore" the round
    assert "raises a confirmation card" in flat


@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_summary_must_carry_what_the_card_omits(profile):
    """Expected symptoms, how it reverts, blast radius — none are on the card."""
    prompt = _prompt(profile)
    # normalise wrapping: the prompt hard-wraps, so match on words not phrases
    flat = " ".join(prompt.split())
    assert "expected symptoms" in flat
    assert "how it gets reverted" in flat
    assert "blast radius" in flat


@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_prompt_states_the_card_is_the_gate(profile):
    """The model must know submission is not silent, or it may hesitate.

    Re-anchored in the 2026-09-20 skeleton/weight cleanup: the wording
    lived in the REMEMBER mirror ("Injection is never silent…"); the
    mirror was removed and the Inject Flow body now carries the same
    contract ("Do not stop for injection approval in chat: submitting
    raises a confirmation card that collects the decision").
    """
    flat = " ".join(_prompt(profile).split())
    assert "Do not stop for injection approval in chat" in flat
    assert "submitting raises a confirmation card" in flat
    assert "collects the decision" in flat
    assert "asks the same question twice" in flat


@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_declining_on_the_card_is_described_as_recoverable(profile):
    """Otherwise the model may keep the chat round "just in case"."""
    flat = " ".join(_prompt(profile).split())
    assert "declines on the card" in flat
    assert "refines them instead of restarting" in flat


# ---------------------------------------------------------------------------
# recovery keeps its chat confirmation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", ["k8s", "host"])
def test_recovery_still_confirms_in_chat(profile):
    """``recover_task`` has no interrupt card — chat is its only gate.

    This also carries the injection/recovery asymmetry that the deleted
    REMEMBER item used to state: the Recover Flow body says recovery has
    no confirmation card, so its chat confirmation must not be dropped
    along with injection's.
    """
    flat = " ".join(_prompt(profile).split())
    assert "wait for the user's explicit approval" in flat
    assert "let the user confirm first" in flat
    assert "Recovery has no confirmation card" in flat
    assert "NEVER call" in flat and "recover_task" in flat


# ---------------------------------------------------------------------------
# the host prompt must stay free of cluster vocabulary
# ---------------------------------------------------------------------------

def test_the_new_wording_leaks_no_k8s_vocabulary_into_host():
    """A first draft enumerated the card's fields and leaked "namespace",
    breaking ``test_host_prompt_builders_do_not_leak_k8s_capabilities``."""
    lowered = _prompt("host").lower()
    for token in ("namespace", "kubectl", "pod"):
        assert token not in lowered, f"{token!r} leaked into the host prompt"
