"""Tests for the multi-strategy blade UID extractor.

The 10 SAMPLES below come from real-world variants we have observed in
ChaosBlade tool output: clean blade_create JSON, kubectl-exec wrapping,
pretty-printed multi-line JSON, error-prefixed JSON, code-54000 race and
permanent-failure cases, and bare resource-name fallbacks.

The contract under test:
  * Every success/race format yields the correct UID.
  * 54000+success=false (permanent failure) yields None — the caller must
    NOT treat the experiment as live.
  * Empty / non-string / unrelated text yields None.
"""

from __future__ import annotations

import pytest

from chaos_agent.utils.blade_uid import extract_blade_uid

VALID_UID = "abcd1234-ef56-7890-abcd-1234567890ab"
SECOND_UID = "11112222-3333-4444-5555-666677778888"
RESOURCE_NAME = "chaosblade-1234abcd5678"


SAMPLES_SUCCESS: list[tuple[str, str, str]] = [
    # 1. Clean single-line success JSON (canonical blade_create stdout).
    (
        "clean_success_json",
        f'{{"code":200,"success":true,"result":"{VALID_UID}"}}',
        VALID_UID,
    ),
    # 2. Pretty-printed multi-line JSON (some blade builds emit this).
    (
        "pretty_multiline",
        f'''{{
            "code": 200,
            "success": true,
            "result": "{VALID_UID}"
        }}''',
        VALID_UID,
    ),
    # 3. JSON wrapped in error preamble (blade_create returned non-zero,
    # but stdout still carried the success JSON — happens when the parent
    # exit code is poisoned by a downstream `tee`/pipe).
    (
        "error_prefix_wrap",
        f'Error: blade create failed (exit 1): {{"code":200,"success":true,"result":"{VALID_UID}"}}\nlog: see /var/log',
        VALID_UID,
    ),
    # 4. kubectl-exec stdout wrapping — typical when the LLM bypassed the
    # host blade tool and ran `kubectl exec ... -- blade create ...`.
    (
        "kubectl_exec_wrap",
        f'Defaulted container "main" out of: main, sidecar.\n{{"code":200,"success":true,"result":"{VALID_UID}"}}\n',
        VALID_UID,
    ),
    # 5. Two JSON objects in stdout (warmup/probe + actual create) —
    # the FIRST recognized success object wins.
    (
        "multi_json_objects",
        f'{{"code":200,"success":true,"result":"{VALID_UID}"}}\n'
        f'{{"code":200,"success":true,"result":"{SECOND_UID}"}}',
        VALID_UID,
    ),
    # 6. code 54000 race — CRD created, Operator not yet synced.
    # success may be true OR absent; both should yield the uid.
    (
        "code_54000_success_true",
        f'{{"code":54000,"success":true,"result":{{"uid":"{VALID_UID}"}}}}',
        VALID_UID,
    ),
    (
        "code_54000_success_absent",
        f'{{"code":54000,"result":{{"uid":"{VALID_UID}"}}}}',
        VALID_UID,
    ),
    # 7. Loose regex fallback — JSON is malformed/truncated but the uid
    # field is still recognizable (parser bailed, regex catches it).
    (
        "regex_fallback_truncated",
        f'{{"code":200,"success":true,"result":"{VALID_UID}"  ',
        VALID_UID,
    ),
    # 8. ChaosBlade resource-name fallback — blade emitted a resource
    # ref (e.g. from `kubectl get chaosblades`) instead of a UID.
    (
        "chaosblade_resource_fallback",
        f"NAME              AGE\n{RESOURCE_NAME}   2m",
        RESOURCE_NAME,
    ),
    # 9. Tab-separated kubectl output that embeds the success JSON
    # alongside other fields (mirrors `kubectl -o json` post-processing).
    (
        "tabular_output_embedded_json",
        f'pod/foo\tInjected\t{{"code":200,"success":true,"result":"{VALID_UID}"}}',
        VALID_UID,
    ),
]


SAMPLES_REJECT: list[tuple[str, str]] = [
    # 10. code 54000 + success=false — CRD created but injection FAILED
    # (e.g. DaemonSet pod not Running on target node). Must NOT extract
    # the uid; must NOT fall back to looser strategies that would.
    (
        "code_54000_success_false",
        f'{{"code":54000,"success":false,"result":{{"uid":"{VALID_UID}"}},"error":"DaemonSet pod not Running"}}',
    ),
]


@pytest.mark.parametrize("name,text,expected", SAMPLES_SUCCESS, ids=lambda x: x if isinstance(x, str) else "")
def test_extract_blade_uid_success_samples(name: str, text: str, expected: str) -> None:
    assert extract_blade_uid(text) == expected, f"sample={name}"


@pytest.mark.parametrize("name,text", SAMPLES_REJECT, ids=lambda x: x if isinstance(x, str) else "")
def test_extract_blade_uid_rejects_failed_54000(name: str, text: str) -> None:
    # 54000+success=false means the injection failed; we must return None
    # AND must not let regex/resource fallbacks rescue the uid.
    assert extract_blade_uid(text) is None, f"sample={name}"


def test_extract_blade_uid_total_success_rate() -> None:
    """Sanity guard: 9/9 success samples extract correctly (100%)."""
    successes = sum(
        1 for _, text, expected in SAMPLES_SUCCESS
        if extract_blade_uid(text) == expected
    )
    assert successes == len(SAMPLES_SUCCESS)


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "   ",
        "no json here, just narration",
        "{not valid json",
        '{"code":500,"success":false,"error":"boom"}',  # unrelated error
    ],
)
def test_extract_blade_uid_returns_none_for_unusable_inputs(text) -> None:
    assert extract_blade_uid(text) is None


def test_strategy_order_json_wins_over_regex() -> None:
    """When JSON parsing yields a uid, the loose regex never gets a chance.

    Sample where two UIDs are present: one inside a 54000+success=false
    failure response, one inside a regular code=200 success. The success
    wins; the failure's UID is never extracted by regex fallback.
    """
    text = (
        f'{{"code":200,"success":true,"result":"{VALID_UID}"}} '
        f'{{"code":54000,"success":false,"result":{{"uid":"{SECOND_UID}"}}}}'
    )
    assert extract_blade_uid(text) == VALID_UID


def test_strategy_order_failed_54000_blocks_regex_rescue() -> None:
    """A failed 54000 response must not be 'rescued' by the loose regex.

    Even though the regex would happily match `"uid": "<uuid>"` inside
    the failure object, the JSON-aware strategy detected the failure and
    short-circuits the fallbacks. Returning the UID here would cause the
    verifier to wait for a non-existent live experiment.
    """
    text = f'{{"code":54000,"success":false,"result":{{"uid":"{VALID_UID}"}}}}'
    assert extract_blade_uid(text) is None


def test_resource_fallback_only_when_no_uuid_present() -> None:
    """If a UUID-shaped uid IS present alongside a chaosblade-* resource
    name, the UUID wins (resource fallback is last-resort)."""
    text = (
        f"name: {RESOURCE_NAME}\n"
        f'response: {{"code":200,"success":true,"result":"{VALID_UID}"}}'
    )
    assert extract_blade_uid(text) == VALID_UID
