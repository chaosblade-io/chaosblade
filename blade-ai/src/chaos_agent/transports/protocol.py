"""Shared wiz stdout protocol parser.

Wiz protocol:
- wiz exit_code=0: stdout first line is ``exit_code: N``, rest is inner command output
- wiz exit_code!=0: wiz itself failed, stderr has error message

Used by both ``KubewizK8sChannel`` and ``KubewizHostChannel`` since
they share the same wiz task exec output format.
"""

from __future__ import annotations

from dataclasses import replace

from chaos_agent.models.command_result import CommandResult

# Signatures of a TRANSPORT-level anomaly leaking through as a parser error.
#
# task-46317228 #64/#66: the gateway answered with an HTML page instead of JSON.
# ``wiz`` is a Node binary, so V8 reported ``Unexpected token '<', "<!DOCTYPE "
# ... is not valid JSON``; ``blade`` is Go, so it reported ``invalid character
# 'b' after top-level value``. We relayed both verbatim, and the LLM read
# "invalid character" as "blade_create failed" — then invented a
# ``kubectl debug --image=stress-ng`` second injection and left a stray debug
# pod behind, even though blade_status already said Running/Success.
#
# A JSON-parse complaint about the response body is never a statement about the
# command's outcome; saying that plainly is what breaks the misread.
#
# ❗ The text states ONE fact and nothing else. It deliberately does NOT:
#   - guess the cause ("probably an auth failure / a timeout") — speculation
#     presented as explanation ends up quoted in the model's verdict, and any
#     such word can also collide with ``errors.classify_error``'s patterns
#     ("timeout" flips a terminal failure into a retry);
#   - tell the model what to do next — that is policy, and it already lives
#     where it belongs (``blade_create``'s own failure message, the phase
#     prompts). This layer reports facts.
# The raw reply is always kept, never replaced or truncated. The codebase's rule
# is "surface the raw signal without a verdict" (see ``kubectl._kubectl_impl``);
# a one-line statement of WHERE the failure happened is the most this may add.
#
# ❗ Each marker must be SPECIFIC to a JSON decoder. A bare "invalid character"
# is NOT: kubectl / blade / iptables all emit it for bad ARGUMENTS ("invalid
# character in resource name"), and mislabelling a genuine failure as "this may
# not mean the command failed" is the dangerous direction. Go's encoding/json
# always suffixes its position clause, so match that instead.
_NON_JSON_RESPONSE_MARKERS = (
    "<!doctype",
    "<html",
    "unexpected token '<'",
    "is not valid json",
    # Go encoding/json position clauses.
    "after top-level value",
    "looking for beginning of value",
    "looking for beginning of object key string",
)

_NON_JSON_EXPLANATION = (
    "transport error: the reply was not JSON, so the CLI failed while PARSING "
    "the response — the inner command's own outcome is not reported here. "
    "Raw reply follows."
)


def explain_transport_anomaly(text: str) -> str:
    """Return an explanatory prefix when *text* is a response-parsing error.

    Empty string when the text looks like a normal command failure, so callers
    can prepend unconditionally without polluting genuine errors.
    """
    if not text:
        return ""
    lowered = text.lower()
    if any(marker in lowered for marker in _NON_JSON_RESPONSE_MARKERS):
        return _NON_JSON_EXPLANATION
    return ""


def _annotate(text: str) -> str:
    """Prepend the transport-anomaly explanation to *text* when it applies."""
    explanation = explain_transport_anomaly(text)
    return f"{explanation} {text}".strip() if explanation else text


def parse_wiz_output(result: CommandResult) -> CommandResult:
    """Parse wiz stdout protocol and return corrected CommandResult.

    Unlike the old ``_adapt_kubewiz_result``, this function does NOT
    check ``settings.kube_connection_mode`` — it is only called by
    channels that know they need wiz protocol parsing.
    """
    # wiz itself failed — stderr already contains the error. Annotate it when
    # the failure is a response-parsing complaint rather than a command outcome.
    if result.exit_code != 0:
        annotated = _annotate(result.stderr or "")
        if annotated == (result.stderr or ""):
            return result
        # ``replace`` rather than a hand-built CommandResult: reconstructing it
        # field by field would silently reset any field added later.
        return replace(result, stderr=annotated)

    stdout = result.stdout
    lines = stdout.split("\n", 1)

    # Protocol violation: stdout must start with "exit_code:"
    if not lines or not lines[0].startswith("exit_code:"):
        return CommandResult(
            exit_code=1,
            stdout="",
            stderr=_annotate(
                f"wiz protocol error: stdout missing exit_code prefix. raw={stdout[:200]}"
            ),
            duration_ms=result.duration_ms,
        )

    try:
        real_exit_code = int(lines[0].split(":", 1)[1].strip())
    except (ValueError, IndexError):
        real_exit_code = 1

    clean_stdout = lines[1] if len(lines) > 1 else ""

    return CommandResult(
        exit_code=real_exit_code,
        stdout=clean_stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
    )
