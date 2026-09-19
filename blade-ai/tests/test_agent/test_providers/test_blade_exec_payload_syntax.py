"""Round-15 root fix: the blade-delivery SYNTAX classifier and its gates.

The word-containment gates (``"blade" in v_args and "create" in v_args``)
judged vocabulary, not syntax — a composite decoy payload
(``sh -c 'kubectl get pods -o json; echo blade create done'``) passed every
one of the (eleven+) copy-pasted sites while carrying no blade command, and
the kubectl output it returned laundered K8s resource UIDs into the birth
registry (round-15 H2). These tests pin the replacement invariant: a blade
delivery is a command SEGMENT whose command-position head is ``blade``.
"""

import re

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.providers.chaosblade.cli_python import (
    PY_FAILED_CREATE_UID_RE,
)
from chaos_agent.agent.providers.chaosblade.python_provider import (
    ChaosbladePythonProvider,
)
from chaos_agent.agent.providers.chaosblade.verify import (
    FAILED_CREATE_UID_RE,
    HEX16_UID_SHAPE,
    RAW_FAILED_CREATE_UID_RE,
    _CHAOSBLADE_RESOURCE_RE,
    _UID_SHAPE_ALTERNATION,
    _UID_SHAPE_RE,
    _UUID_RE,
    _parse_uid_from_status_content,
    classify_blade_exec_payload,
    destroy_uid_from_tokens,
    extract_experiment_uid,
    extract_experiment_uid_from_messages,
    inline_blade_create_receipt_uids,
    inline_destroy_uids,
    scan_blade_evidence_index,
    scan_destroyed_proven_uids,
    scan_destroyed_uids,
)
from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.chaosblade.provider import ChaosbladeProvider
from chaos_agent.agent.providers.message_scanning import exec_command_segments

OWN_UID = "a1b2c3d4-1111-2222-3333-444455556666"
POD_UID = "f1e2d3c4-9999-8888-7777-665544332211"
DECOY = (
    "toolpod -n chaosblade -- sh -c 'kubectl get pods -o json; echo blade create done'"
)
PODS_JSON = (
    '{"items": [{"metadata": {"name": "nginx-1", "uid": "%s"}, '
    '"status": {"phase": "Running"}}]}' % POD_UID
)
DEATH_RECEIPT = '{"code": 200, "success": true, "result": "ok"}'


def _exec_pair(v_args: str, receipt: str, tc_id: str = "tc-x") -> list:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "args": {"subcommand": "exec", "v_args": v_args},
                    "id": tc_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=receipt, name="kubectl", tool_call_id=tc_id),
    ]


class TestDecoyVectorsFailClosed:
    """Words in argument position are vocabulary, never a command."""

    def test_composite_decoy_is_not_a_create_delivery(self):
        # Round-15 H2: the decoy passed every word gate while carrying no
        # blade command — the echo argument carries the WORDS only.
        payload = classify_blade_exec_payload(DECOY)
        assert payload.segments == []
        assert payload.has_create is False
        assert payload.has_destroy is False

    def test_echo_destroy_decoy_proves_no_death(self):
        msgs = _exec_pair("echo blade destroy X", DEATH_RECEIPT)
        assert scan_destroyed_uids(msgs) == set()
        assert scan_destroyed_proven_uids(msgs) == set()

    def test_quoted_literal_is_not_a_command(self):
        payload = classify_blade_exec_payload("bash -c 'echo \"blade create\"'")
        assert payload.has_create is False

    def test_get_json_carrier_attests_nothing(self):
        # task-51193464 shape, now judged at the syntax level too.
        assert exec_command_segments("nginx-1 -n demo -o json") == []
        assert classify_blade_exec_payload("kubectl get pods -o json").segments == []

    def test_unlexable_payload_yields_nothing(self):
        assert exec_command_segments("unbalanced 'quote") == []
        assert classify_blade_exec_payload(None).segments == []
        assert classify_blade_exec_payload("").segments == []

    def test_pods_json_receipt_over_decoy_births_nothing(self):
        # End-to-end H2: decoy exec + resource-JSON receipt — no birth, no
        # aggregation, and the destroy provenance gate refuses the UID.
        msgs = _exec_pair(DECOY, PODS_JSON, "tc-h2")
        assert inline_blade_create_receipt_uids(msgs) == set()
        assert FaultProviderRegistry.created_experiment_ids(msgs, {}) == set()

    def test_washed_in_uid_destroy_is_refused_end_to_end(self):
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _screen_destroy_uid_provenance,
        )
        from chaos_agent.agent.providers.chaosblade.provider import (
            classify_inline_blade,
        )

        msgs = _exec_pair(DECOY, PODS_JSON, "tc-h2")
        et = classify_inline_blade(
            ["blade", "destroy", POD_UID],
            "x",
            fallback_ns="chaosblade",
            fallback_pod="toolpod",
        )
        decision = _screen_destroy_uid_provenance(
            et.blade_destroy_uid,
            et,
            msgs,
            {},
        )
        assert not str(decision.verdict).endswith("ALLOW")


class TestLegitimateDeliveriesUnchanged:
    """The real delivery forms the r14 anchors pin keep working."""

    def test_standard_exec_delivery(self):
        payload = classify_blade_exec_payload(
            "toolpod -n chaosblade -- blade create k8s pod-cpu fullload "
            "--names nginx-1 --timeout 300"
        )
        assert payload.has_create is True
        assert payload.has_destroy is False

    def test_bare_composite_script_captures_all_destroys(self):
        # r14-J3: both UIDs of a composite script share the single verdict.
        # Round-16 shape validation: the r15 anchor's placeholder UIDs
        # ("A"/"B") are not experiment-UID shaped — a destroy target is
        # hex16, and a non-shaped token is a form issue, not a ledger UID.
        assert inline_destroy_uids(
            "sh -c 'blade destroy aabbccddeeff0011 && blade destroy 9988776655443322'"
        ) == {"aabbccddeeff0011", "9988776655443322"}

    def test_flag_spelling_resolves_to_value(self):
        # r14-F1/F3 semantics: --uid X registers X, never the literal flag.
        assert inline_destroy_uids(f"toolpod -- blade destroy --uid {OWN_UID}") == {
            OWN_UID
        }
        assert inline_destroy_uids(f"toolpod -- blade destroy --uid={OWN_UID}") == {
            OWN_UID
        }

    def test_revoke_vocabulary_is_not_a_death_verb(self):
        # r14 pinned revoke as a death verb; round-25 K1 — aligning the
        # inline face with the round-24 K3 host-face ruling — corrected
        # it: revoke tears down a PREPARE uid (a precondition), never an
        # experiment. See TestInlineRevokeNotADeathCarrier for the seam
        # level anchors.
        assert inline_destroy_uids(f"toolpod -- blade revoke {OWN_UID}") == set()

    def test_value_absorbing_flag_skips_its_value(self):
        assert inline_destroy_uids(
            f"toolpod -- blade destroy --kubeconfig /root/kc {OWN_UID}"
        ) == {OWN_UID}

    def test_create_command_is_not_a_destroy(self):
        v_args = (
            f"toolpod -n chaosblade -- blade create k8s pod-cpu fullload "
            f"--names nginx-1 --uid {OWN_UID}"
        )
        assert inline_destroy_uids(v_args) == set()

    def test_chroot_and_script_delivery_recognised(self):
        payload = classify_blade_exec_payload(
            "kubectl debug node/x -it --image=ubuntu -- chroot /host bash -c "
            "'blade create k8s node-cpu fullload'"
        )
        assert payload.has_create is True

    def test_full_command_line_form_recognised(self):
        # The session-dict face (detail.command) carries the full line.
        payload = classify_blade_exec_payload(
            "kubectl exec toolpod -n chaosblade -- blade create k8s pod-cpu"
        )
        assert payload.has_create is True
        # Global flags before the subcommand, too.
        payload = classify_blade_exec_payload(
            "kubectl -n ns exec toolpod -- blade create k8s pod-cpu"
        )
        assert payload.has_create is True

    def test_path_form_and_bare_chroot_recognised(self):
        assert (
            classify_blade_exec_payload("/usr/bin/blade destroy Q").has_destroy is True
        )
        assert (
            classify_blade_exec_payload("chroot /host blade destroy Q").has_destroy
            is True
        )

    def test_pod_slot_without_separator_is_fail_closed(self):
        # No `--` and the first positional is not a command head: guessing
        # past the pod would be fail-open (an echo decoy's second word would
        # reach command position). Nothing is extracted instead.
        assert exec_command_segments("toolpod blade destroy X") == []
        assert exec_command_segments("echo blade destroy X") == []


class TestSharedScanInjectionContract:
    """The shared attribution scans take the blade judgement as a parameter."""

    def test_exclusion_active_when_injected(self):
        # A gated blade create delivery is NOT a kubectl-native injection.
        from chaos_agent.agent.providers.message_scanning import (
            KUBECTL_COMMAND_SUBCOMMANDS,
            KUBECTL_WRITE_SUBCOMMANDS,
            exec_inner_command_mutates,
            scan_kubectl_injection_after_blade,
        )
        from chaos_agent.agent.providers.chaosblade.verify import (
            _is_blade_create_delivery,
        )

        msgs = _exec_pair(
            "toolpod -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code": 200, "success": true, "result": "%s"}' % OWN_UID,
            "tc-blade",
        )
        assert (
            scan_kubectl_injection_after_blade(
                msgs,
                KUBECTL_WRITE_SUBCOMMANDS,
                command_subcommands=KUBECTL_COMMAND_SUBCOMMANDS,
                is_mutating_command=exec_inner_command_mutates,
                is_blade_create_delivery=_is_blade_create_delivery,
            )
            is False
        )

    def test_registry_facade_routes_the_judgement(self):
        assert (
            FaultProviderRegistry.is_blade_exec_create_delivery(
                "kubectl exec pod -- blade create k8s x"
            )
            is True
        )
        assert FaultProviderRegistry.is_blade_exec_create_delivery(DECOY) is False

    def test_without_injection_no_exclusion_applies(self):
        # Documented boundary: a caller that does NOT inject the classifier
        # gets no blade exclusion — its exec is judged by the mutating
        # vocabulary alone. Callers intersecting the blade-exec delivery
        # MUST inject (both production callers do).
        from chaos_agent.agent.providers.message_scanning import (
            KUBECTL_COMMAND_SUBCOMMANDS,
            KUBECTL_WRITE_SUBCOMMANDS,
            exec_inner_command_mutates,
            scan_kubectl_injection_after_blade,
        )

        msgs = _exec_pair(
            "toolpod -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code": 200, "success": true, "result": "%s"}' % OWN_UID,
            "tc-blade",
        )
        assert (
            scan_kubectl_injection_after_blade(
                msgs,
                KUBECTL_WRITE_SUBCOMMANDS,
                command_subcommands=KUBECTL_COMMAND_SUBCOMMANDS,
                is_mutating_command=exec_inner_command_mutates,
            )
            is True
        )


class TestFailedCreateDialectAnchor:
    """R1: dialect anchors do not cross output domains."""

    # Round-19 N3 flip: the host-face ``UID:`` anchor composes from the
    # single-source hex16 shape now. The module-level OWN_UID (a dashed
    # UUID) still serves the destroy-extraction anchors below — dashed is
    # destroy-face legacy compatibility — but a birth-side failed-create
    # anchor rules dashed K8s-object vocabulary OUT (round-16's ruling,
    # composed into this regex only in round-19). The wording's producer
    # (cli.py) always re-wraps a lowercase hex16 it mined itself, so the
    # pre-round-19 uppercase/dash/8-char tolerance was unreachable-legitimate
    # shape admit — pure leakage surface.
    HOST_UID = "deadbeef00000001"

    def test_cli_failure_dialect_still_matches(self):
        # The blade CLI failure wording cli.py emits.
        m = FAILED_CREATE_UID_RE.search(
            f'{{"code": 500, "success": false, "error": "UID: {self.HOST_UID} CRD stuck"}}'
        )
        assert m and m.group(1) == self.HOST_UID

    def test_json_uid_key_no_longer_matches(self):
        # The K8s-output vocabulary branch is gone — a pods-JSON receipt
        # cannot wash its metadata.uid in through this anchor any more.
        assert FAILED_CREATE_UID_RE.search(PODS_JSON) is None
        assert re.search(r'"uid"', FAILED_CREATE_UID_RE.pattern) is None

    def test_host_face_failed_create_still_owed_cleanup(self):
        # Host face is structurally closed (blade_create ToolMessages) and
        # keeps its r14-G1 semantics via the surviving dialect anchor.
        from chaos_agent.agent.providers.chaosblade.verify import (
            extract_experiment_uid,
        )

        content = f'{{"code": 500, "success": false, "error": "UID: {self.HOST_UID}"}}'
        uids = {extract_experiment_uid(content)}
        uids.update(m.group(1) for m in FAILED_CREATE_UID_RE.finditer(content))
        assert self.HOST_UID in uids

    def test_dashed_uuid_is_k8s_vocabulary_not_a_host_uid(self):
        # Round-19 N3 flip: dashed used to ride this anchor's dash-tolerant
        # shape; the birth-side legislation (round-16) always ruled it out.
        assert FAILED_CREATE_UID_RE.search(f"UID: {OWN_UID}") is None

    def test_uppercase_hex16_refused(self):
        # Round-19 N3: lowercase-domain parity, finally composed in.
        assert FAILED_CREATE_UID_RE.search("UID: ABCDEF0123456789") is None

    def test_over_length_hex_refused(self):
        # Round-19 N2/N3: the 16-32 bounds apply to every capturing anchor
        # (40-hex sha256-shaped garbage stays out of the owed-cleanup set).
        assert FAILED_CREATE_UID_RE.search("UID: " + "a" * 40) is None


class TestExecFaceFailedCreateDialect:
    """Round-16 domain split: the two faces speak two failure dialects.

    The host face (blade_create tool output) carries cli.py's ``UID:
    <uid>`` wrapper wording; the exec channel sees the blade CLI's RAW
    failure JSON — a top-level ``"uid": "<hex16>"`` key (the exact shape
    cli.py itself mines). The r15 cut deleted the JSON branch from BOTH
    faces (exec-face failed-create ingestion went dark — cleanup still
    owed but the hydration destroy was refused) while leaving the
    ``UID:`` wording consuming exec-face content (an ``echo`` companion
    could forge it). Shape alone cannot separate the domains (E3) — the
    pure-create segment gate is the separator.
    """

    HEX16 = "f00dcafe12345678"
    RAW_FAIL = (
        '{"code": 500, "success": false, '
        '"error": "create experiment failed: rpc error: timeout", '
        f'"uid": "{HEX16}"}}'
    )
    CREATE_V = (
        "toolpod -n chaosblade -- blade create k8s pod-cpu fullload "
        "--names nginx-1 --timeout 300"
    )

    def test_raw_json_uid_key_is_ingested_on_pure_create(self):
        # A1: the exec face's real failed-create dialect, ingested again.
        msgs = _exec_pair(self.CREATE_V, self.RAW_FAIL, "tc-a1")
        assert inline_blade_create_receipt_uids(msgs) == {self.HEX16}

    def test_host_face_wording_is_not_exec_dialect(self):
        # F2: the ``UID:`` prefix is the HOST face's wording — a forged
        # ``UID:`` text in an exec receipt licenses nothing.
        msgs = _exec_pair(self.CREATE_V, f"noise UID: {self.HEX16} noise", "tc-f2")
        assert inline_blade_create_receipt_uids(msgs) == set()

    def test_k8s_dashed_uuid_out_of_raw_anchor(self):
        # A4/E4: a dashed UUID is K8s-object vocabulary, never a blade UID.
        assert RAW_FAILED_CREATE_UID_RE.search(PODS_JSON) is None

    def test_shape_alone_cannot_separate_query_output(self):
        # E3: a query output's uid keys are FORM-identical to a failed
        # create's — the pure-create segment gate is the separator.
        query_out = (
            '{"code":200,"success":true,"result":['
            '{"uid":"aabbccddeeff0011"},{"uid":"9988776655443322"}]}'
        )
        assert {m.group(1) for m in RAW_FAILED_CREATE_UID_RE.finditer(query_out)} == {
            "aabbccddeeff0011",
            "9988776655443322",
        }
        # …and end-to-end: a real create + query compound receipt licenses
        # NOTHING (some other command contributed to the receipt).
        msgs = _exec_pair(
            "toolpod -n chaosblade -- sh -c 'blade create k8s pod-cpu "
            "fullload --names nginx-1; blade query k8s create'",
            query_out,
            "tc-e3",
        )
        assert inline_blade_create_receipt_uids(msgs) == set()

    def test_echo_companion_forged_prefix_births_nothing(self):
        # F1: the echo segment can forge ANY shape — the segment
        # composition is the only structural lever.
        msgs = _exec_pair(
            'toolpod -n chaosblade -- sh -c \'echo "UID: deadbeef12345678"; '
            "blade create k8s pod-cpu fullload --names nginx-1'",
            "UID: deadbeef12345678",
            "tc-f1",
        )
        assert inline_blade_create_receipt_uids(msgs) == set()

    def test_hydration_destroy_of_raw_json_failed_create_allows(self):
        # A5 end-to-end: the task's own cleanup of its own failed inline
        # create (UID known via the raw JSON key) passes the gate.
        from chaos_agent.agent.nodes.planning.tool_screener import (
            _screen_destroy_uid_provenance,
        )
        from chaos_agent.agent.providers.chaosblade.provider import (
            classify_inline_blade,
        )

        msgs = _exec_pair(self.CREATE_V, self.RAW_FAIL, "tc-a5")
        et = classify_inline_blade(
            ["blade", "destroy", self.HEX16],
            "x",
            fallback_ns="chaosblade",
            fallback_pod="toolpod",
        )
        decision = _screen_destroy_uid_provenance(
            et.blade_destroy_uid,
            et,
            msgs,
            {},
        )
        assert str(decision.verdict).endswith("ALLOW")


class TestPureCreateSegmentGate:
    """``pure_create``: receipt ingestion trusts ONLY whole-payload creates."""

    def test_plain_create_is_pure(self):
        p = classify_blade_exec_payload(
            "toolpod -n chaosblade -- blade create k8s pod-cpu fullload"
        )
        assert p.pure_create is True

    def test_chroot_script_wrapped_create_is_pure(self):
        p = classify_blade_exec_payload(
            "kubectl debug node/n1 --image=ubuntu -- chroot /host sh -c "
            "'blade create k8s pod-cpu fullload'"
        )
        assert p.pure_create is True

    def test_create_plus_query_is_not_pure(self):
        p = classify_blade_exec_payload(
            "toolpod -n chaosblade -- sh -c 'blade create k8s pod-cpu "
            "fullload; blade query k8s create'"
        )
        assert p.has_create is True  # attribution is still a create delivery
        assert p.pure_create is False

    def test_create_plus_echo_is_not_pure(self):
        p = classify_blade_exec_payload(
            "toolpod -n chaosblade -- sh -c 'echo start; blade create k8s "
            "pod-cpu fullload'"
        )
        assert p.pure_create is False

    def test_create_plus_destroy_is_not_pure(self):
        # The receipt cannot attribute WHICH uid belongs to which verb.
        p = classify_blade_exec_payload(
            "toolpod -n chaosblade -- sh -c 'blade create k8s pod-cpu "
            "fullload; blade destroy aabbccddeeff0011'"
        )
        assert p.pure_create is False

    def test_destroy_only_is_not_pure_create(self):
        p = classify_blade_exec_payload(
            "toolpod -n chaosblade -- blade destroy aabbccddeeff0011"
        )
        assert p.pure_create is False

    def test_empty_and_decoy_payloads_are_not_pure(self):
        assert classify_blade_exec_payload("").pure_create is False
        assert classify_blade_exec_payload(DECOY).pure_create is False


class TestDestroyUidShapeValidation:
    """Round-16 B: a non-shaped destroy target is a form issue, not a UID.

    The r15 H1 residue: a variable reference / command substitution /
    redirection token used to land in the issued ledger as a fake UID and
    persisted into the durable retired ledger.
    """

    def test_variable_reference_token_not_registered(self):
        assert (
            inline_destroy_uids("toolpod -n chaosblade -- blade destroy $EXP_UID")
            == set()
        )

    def test_command_substitution_token_not_registered(self):
        assert (
            inline_destroy_uids(
                "toolpod -n chaosblade -- blade destroy $(cat /tmp/uid)"
            )
            == set()
        )

    def test_bare_redirection_token_not_registered(self):
        assert (
            inline_destroy_uids("toolpod -n chaosblade -- blade destroy 2>&1") == set()
        )

    def test_flag_value_shape_validated(self):
        assert destroy_uid_from_tokens(["--uid", "$EXP_UID"]) == ""
        assert (
            destroy_uid_from_tokens(["--uid", "aabbccddeeff0011"]) == "aabbccddeeff0011"
        )

    def test_legacy_dashed_uid_shape_accepted(self):
        assert inline_destroy_uids(
            "toolpod -n chaosblade -- blade destroy "
            "deadbeef-1234-5678-9abc-def012345678"
        ) == {"deadbeef-1234-5678-9abc-def012345678"}


class TestWrapperPrefixDelivery:
    """Round-16 C: a wrapped verb is still the verb the container runs.

    ``timeout 30 blade destroy X`` / ``nohup ...`` / ``env VAR= ...`` /
    ``xargs ...`` delay the real command behind their own arguments —
    the r15 parser judged the wrapper as the head and the death ledger
    missed the real kill (fail-open toward residual false positives).
    """

    UID = "a1b2c3d4e5f60718"

    def test_timeout_prefix_destroy(self):
        assert inline_destroy_uids(
            f"toolpod -n chaosblade -- timeout 30 blade destroy {self.UID}"
        ) == {self.UID}

    def test_timeout_duration_spellings(self):
        assert inline_destroy_uids(
            f"toolpod -n chaosblade -- timeout 30s blade destroy {self.UID}"
        ) == {self.UID}

    def test_nohup_prefix_destroy(self):
        assert inline_destroy_uids(
            f"toolpod -n chaosblade -- nohup blade destroy {self.UID}"
        ) == {self.UID}

    def test_env_assignment_prefix_destroy(self):
        assert inline_destroy_uids(
            f"toolpod -n chaosblade -- env BLADE_HOME=/x blade destroy {self.UID}"
        ) == {self.UID}

    def test_bare_assignment_prefix_destroy(self):
        assert inline_destroy_uids(
            f"toolpod -n chaosblade -- BLADE_HOME=/x blade destroy {self.UID}"
        ) == {self.UID}

    def test_xargs_pipeline_destroy(self):
        # The stdin-supplied UIDs are statically unknowable, but the
        # literal template UID is a command-position fact.
        assert inline_destroy_uids(
            f"toolpod -n chaosblade -- sh -c 'cat /tmp/uids | "
            f"xargs blade destroy {self.UID}'"
        ) == {self.UID}

    def test_interleaved_redirection_before_verb(self):
        # C4: the redirection sat BETWEEN the head and the verb.
        assert inline_destroy_uids(
            f"toolpod -n chaosblade -- blade 2>&1 destroy {self.UID}"
        ) == {self.UID}

    def test_trailing_redirection_after_uid_still_fine(self):
        assert inline_destroy_uids(
            f"toolpod -n chaosblade -- blade destroy {self.UID} 2>&1 | tee /tmp/log"
        ) == {self.UID}

    def test_wrapper_alone_yields_no_command(self):
        # Fail-closed: a wrapper with nothing behind it is no command.
        assert exec_command_segments("toolpod -- timeout 30") == []

    def test_wrapped_create_keeps_attribution(self):
        p = classify_blade_exec_payload(
            "toolpod -n chaosblade -- timeout 60 blade create k8s "
            "pod-cpu fullload --names nginx-1"
        )
        assert p.has_create is True
        assert p.pure_create is True


class TestScriptInternalRedirection:
    """Round-17 S1: an in-script numeric redirection (``2>&1``, ``3>&1``)
    placed BEFORE the verb must not be torn at the ``&`` — the separator
    belongs to the redirection token, not the command boundary. The
    round-16 C4 anchor pinned only the direct token-stream form; the
    in-script form tore into ``blade 2>`` + ``1 destroy X`` and the real
    kill's segment grew a non-blade head (death-ledger miss)."""

    UID = "a1b2c3d4e5f60718"

    def test_in_script_stderr_dup_before_verb_registers(self):
        assert inline_destroy_uids(
            f"toolpod -- sh -c 'blade 2>&1 destroy {self.UID}'"
        ) == {self.UID}

    def test_in_script_fd3_dup_before_verb_registers(self):
        assert inline_destroy_uids(
            f"toolpod -- sh -c 'blade 3>&1 destroy {self.UID}'"
        ) == {self.UID}

    def test_in_script_stderr_only_after_uid_still_fine(self):
        assert inline_destroy_uids(
            f"toolpod -- sh -c 'blade destroy {self.UID} 1>&2'"
        ) == {self.UID}

    def test_in_script_trailing_pipe_still_splits(self):
        # A redirection AFTER the uid piped to tee: both halves survive
        # the (correct) pipe split — the verb half keeps the uid.
        assert inline_destroy_uids(
            f"toolpod -- sh -c 'blade destroy {self.UID} 2>&1 | tee /tmp/log'"
        ) == {self.UID}

    def test_real_background_separator_still_splits(self):
        # A true ``&`` (background) after a NON-redirection tail must
        # still split — the guard must not swallow real separators.
        segs = exec_command_segments("toolpod -- sh -c 'sleep 10 & echo done'")
        heads = {s[0] for s in segs}
        assert "sleep" in heads and "echo" in heads

    def test_spaced_redirection_then_ampersand_still_splits(self):
        # ``2> file & cmd2`` — the ``&`` follows a FILE token, not the
        # ``>``: it is a real background separator and must split.
        segs = exec_command_segments(
            "toolpod -- sh -c 'blade destroy x 2> file & echo done'"
        )
        heads = [s[0] for s in segs]
        assert heads == ["blade", "echo"]


class TestWrapperValueFlags:
    """Round-17 S2: a wrapper's SEPARATED value flag
    (``timeout --signal KILL 30 blade``) eats its VALUE token — the
    value was mistaken for the command head and the wrapped verb was
    never reached (death-ledger miss). Glued spellings were already
    covered by the plain flag skip."""

    UID = "a1b2c3d4e5f60718"

    def test_timeout_separated_signal_value_registers(self):
        assert inline_destroy_uids(
            f"toolpod -- timeout --signal KILL 30 blade destroy {self.UID}"
        ) == {self.UID}

    def test_timeout_short_signal_flag_registers(self):
        assert inline_destroy_uids(
            f"toolpod -- timeout -s KILL 30 blade destroy {self.UID}"
        ) == {self.UID}

    def test_xargs_separated_placeholder_registers(self):
        assert inline_destroy_uids(
            f"toolpod -- sh -c 'cat /tmp/uids | xargs -I {{}} blade destroy {self.UID}'"
        ) == {self.UID}

    def test_nice_separated_adjustment_registers(self):
        assert inline_destroy_uids(
            f"toolpod -- nice -n 5 blade destroy {self.UID}"
        ) == {self.UID}

    def test_env_separated_unset_registers(self):
        assert inline_destroy_uids(
            f"toolpod -- env -u LEAKED_VAR blade destroy {self.UID}"
        ) == {self.UID}

    def test_xargs_non_flag_companion_head_is_not_blade(self):
        # ``xargs echo blade destroy X`` — ``echo`` is xargs's delivered
        # command head; blade stays an ARGUMENT of echo (no decoy
        # promotion just because a wrapper is present).
        assert (
            inline_destroy_uids(f"toolpod -- xargs echo blade destroy {self.UID}")
            == set()
        )

    def test_glued_value_flag_still_registers(self):
        # Control: glued spelling was already handled (r16 C anchor).
        assert inline_destroy_uids(
            f"toolpod -- timeout --kill-after=10s 30 blade destroy {self.UID}"
        ) == {self.UID}

    def test_optional_value_flag_does_not_eat_command_head(self):
        # ``xargs -i blade destroy X`` — ``-i`` takes an OPTIONAL value;
        # treating it as mandatory would swallow the command head
        # ``blade`` itself. The verb must still be reached.
        assert inline_destroy_uids(f"toolpod -- xargs -i blade destroy {self.UID}") == {
            self.UID
        }


class TestUidShapeBounds:
    """Round-17 S3: the hex16 branch is lowercase-bounded and capped at
    32 hex chars — a 40-hex sha256-shaped token or an all-uppercase hex
    is not a blade experiment UID (every ingestion anchor in the module
    is lowercase; H1's residue in new shapes)."""

    def test_sha256_shaped_token_rejected(self):
        assert inline_destroy_uids("toolpod -- blade destroy " + "a" * 40) == set()

    def test_uppercase_hex16_rejected(self):
        assert inline_destroy_uids("toolpod -- blade destroy DEADBEEF12345678") == set()

    def test_lowercase_hex16_accepted(self):
        assert inline_destroy_uids("toolpod -- blade destroy a1b2c3d4e5f60718") == {
            "a1b2c3d4e5f60718"
        }

    def test_lowercase_hex32_upper_bound_accepted(self):
        # The cap admits a future longer UID without re-opening the
        # sha-shaped lane.
        uid32 = "a" * 32
        assert inline_destroy_uids(f"toolpod -- blade destroy {uid32}") == {uid32}

    def test_dashed_uuid_case_insensitive_branch_kept(self):
        # The dashed branch stays case-insensitive (legacy spellings;
        # no laundering chain rides it — hygiene only).
        assert inline_destroy_uids(
            "toolpod -- blade destroy AABBCCDD-1111-2222-3333-444455556666"
        ) == {"AABBCCDD-1111-2222-3333-444455556666"}


class TestCompositeReceiptLicensesNothing:
    """Rounds 17-18: a composite payload's receipt licenses NOTHING on
    ANY ingestion face.

    Round-17 H2c opened a graded middle lane — composite receipts (real
    create segment + get/echo companions) were allowed the JSON-aware
    success anchors on the theory that a companion does not produce that
    shape "organically". Round-18 F REVERTED it: an ECHO companion forges
    the success JSON verbatim (``echo '{"code":200,...}'`` laundered a
    forged UID right through the strict lane), and round-16 F had already
    ruled the segment composition the ONLY lever. Every ingestion face
    now agrees with the birth registry's fail-closed ruling:
    ``pure_create`` or nothing — legislative parity across the four
    faces (birth registry / single slot / evidence index / session-dict).
    """

    UID = "a1b2c3d4e5f60718"
    POD_UID = "12345678-1234-1234-1234-123456789012"
    FORGED_UID = "deadbeefcafe0123"  # hex16 — a VALID shape, forged by echo

    COMPOSITE_V = (
        "sh -c 'blade create k8s pod-cpu fullload "
        "--labels app=demo --namespace demo --cpu-percent 80; "
        "kubectl get pods -n demo -o json'"
    )

    FORGE_V = (
        "sh -c 'blade create k8s pod-cpu fullload "
        "--labels app=demo --namespace demo --cpu-percent 80; "
        'echo {"code":200,"success":true,"result":"' + FORGED_UID + "\"}'"
    )

    def _composite_msgs(self, content: str, v_args: str | None = None) -> list:
        return [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_gc",
                        "name": "kubectl",
                        "args": {
                            "subcommand": "exec",
                            "v_args": v_args or self.COMPOSITE_V,
                        },
                    }
                ],
            ),
            ToolMessage(content=content, tool_call_id="call_gc", name="kubectl"),
        ]

    FAILED_CREATE_PLUS_GET = (
        "Create experiment failed! The pods not found\n"
        '{"apiVersion":"v1","items":[{"metadata":{"name":"demo-7f9c",'
        '"uid":"12345678-1234-1234-1234-123456789012"}}],"kind":"PodList"}'
    )

    SUCCESS_CREATE_PLUS_GET = (
        '{"code":200,"success":true,"result":"a1b2c3d4e5f60718"}\n'
        '{"apiVersion":"v1","items":[{"metadata":{"name":"demo-7f9c",'
        '"uid":"12345678-1234-1234-1234-123456789012"}}],"kind":"PodList"}'
    )

    FAILED_PLUS_FORGED_SUCCESS = (
        "Create experiment failed! The pods not found\n"
        '{"code":200,"success":true,"result":"' + FORGED_UID + '"}'
    )

    FAILED_PLUS_FORGED_54000 = (
        "Create experiment failed! The pods not found\n"
        '{"code":54000,"success":true,"result":{"uid":"' + FORGED_UID + '"},'
        '"error":"the experiment is initializing, please wait"}'
    )

    def test_composite_failed_create_laundering_blocked_single_slot(self):
        got = extract_experiment_uid_from_messages(
            self._composite_msgs(self.FAILED_CREATE_PLUS_GET)
        )
        assert got is None

    def test_composite_failed_create_laundering_blocked_evidence_index(self):
        idx, method = scan_blade_evidence_index(
            self._composite_msgs(self.FAILED_CREATE_PLUS_GET)
        )
        assert method is None and idx == -1

    def test_composite_successful_create_licenses_nothing(self):
        # Round-18 F flipped this anchor: the round-17 graded lane let a
        # SUCCESSFUL composite create license its real UID through the
        # JSON-aware anchor — but the same lane let an echo companion
        # forge that anchor (see the forged tests below), and a UID the
        # gate cannot tell from a forgery is a UID the gate must refuse.
        # Composite receipts license NOTHING, success or failure.
        got = extract_experiment_uid_from_messages(
            self._composite_msgs(self.SUCCESS_CREATE_PLUS_GET)
        )
        assert got is None

    def test_composite_successful_create_certifies_no_evidence(self):
        # Flipped with the anchor above (round-18 F): evidence
        # certification follows the same pure_create gate.
        idx, method = scan_blade_evidence_index(
            self._composite_msgs(self.SUCCESS_CREATE_PLUS_GET)
        )
        assert method is None and idx == -1

    def test_echo_forged_success_json_refused(self):
        # Round-18 F: the strict anchor is forgeable — an echo companion
        # prints the success JSON verbatim; the single slot and the
        # evidence index must both refuse (the birth registry already
        # did — round-16 F's ruling).
        msgs = self._composite_msgs(
            self.FAILED_PLUS_FORGED_SUCCESS, v_args=self.FORGE_V
        )
        assert extract_experiment_uid_from_messages(msgs) is None
        idx, method = scan_blade_evidence_index(msgs)
        assert method is None and idx == -1

    def test_echo_forged_54000_initializing_refused(self):
        # The 54000-initializing branch of the JSON-aware strategy is
        # inside the same forged lane (round-18 F-d).
        msgs = self._composite_msgs(self.FAILED_PLUS_FORGED_54000, v_args=self.FORGE_V)
        assert extract_experiment_uid_from_messages(msgs) is None

    def test_all_four_ingestion_faces_agree_on_composite(self):
        # Legislative parity (round-18 F-c): the same forged composite
        # messages produce the SAME verdict on every ingestion face —
        # birth registry, single slot, evidence index AND the session-dict
        # fallback (the face the round-17 anchors never covered).
        msgs = self._composite_msgs(
            self.FAILED_PLUS_FORGED_SUCCESS, v_args=self.FORGE_V
        )
        assert inline_blade_create_receipt_uids(msgs) == set()
        assert extract_experiment_uid_from_messages(msgs) is None
        assert scan_blade_evidence_index(msgs) == (-1, None)
        session_dicts = [
            {
                "type": "tool_execution",
                "detail": {
                    "command": self.FORGE_V,
                    "stdout_preview": self.FAILED_PLUS_FORGED_SUCCESS,
                },
            }
        ]
        assert (
            ChaosbladeProvider().extract_experiment_id_from_session_dict(session_dicts)
            == ""
        )

    def test_pure_receipt_keeps_full_chain_loose_anchor(self):
        # A pure-create receipt's output domain is blade's own: the loose
        # fallbacks stay licensed there (host-face dialect words, resource
        # names) — the pure lane is untouched by the round-18 revert.
        # Round-20 Q2 flip: the resource-name fallback STRIPS the
        # ``chaosblade-`` prefix (the suffix IS the experiment UID) and
        # composes the single-source hex16 shape — a legal 16-hex suffix
        # returns the bare UID (the pre-r20 anchor asserted the verbatim
        # PREFIXED string off a 12-hex suffix, both now refused).
        msgs = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_gp",
                        "name": "kubectl",
                        "args": {
                            "subcommand": "exec",
                            "v_args": "blade create k8s pod-cpu fullload "
                            "--labels app=demo --namespace demo",
                        },
                    }
                ],
            ),
            ToolMessage(
                content="chaosblade-a1b2c3d4e5f60718 created",
                tool_call_id="call_gp",
                name="kubectl",
            ),
        ]
        got = extract_experiment_uid_from_messages(msgs)
        assert got == "a1b2c3d4e5f60718"

    def test_session_dict_pure_create_still_ingests(self):
        # Control for the session-dict face: a PURE-create command's
        # receipt (output domain blade's own) keeps the full chain —
        # the composite gate must not over-tighten the pure lane (the
        # face's first positive anchor; round-18).
        session_dicts = [
            {
                "type": "tool_execution",
                "detail": {
                    "command": "blade create k8s pod-cpu fullload --labels app=d",
                    "stdout_preview": (
                        '{"code":200,"success":true,"result":"' + self.UID + '"}'
                    ),
                },
            }
        ]
        assert (
            ChaosbladeProvider().extract_experiment_id_from_session_dict(session_dicts)
            == self.UID
        )

    def test_session_dict_composite_licenses_nothing(self):
        # The session-dict face's composite gate (round-18 F): a create
        # segment plus a get companion licenses nothing — the K8s
        # metadata.uid riding in the companion output must not hydrate a
        # live-experiment UID (round-17 H2c's third face, closed).
        session_dicts = [
            {
                "type": "tool_execution",
                "detail": {
                    "command": self.COMPOSITE_V,
                    "stdout_preview": self.FAILED_CREATE_PLUS_GET,
                },
            }
        ]
        assert (
            ChaosbladeProvider().extract_experiment_id_from_session_dict(session_dicts)
            == ""
        )

    def test_non_uid_result_string_refused(self):
        # Round-18 F-e: the JSON-aware success anchor shape-validates its
        # payload — a success receipt whose ``result`` is not a UID-shaped
        # string is not an experiment UID and must not launder into any
        # ledger.
        assert (
            extract_experiment_uid(
                '{"code":200,"success":true,"result":"ok-not-a-uid"}'
            )
            is None
        )

    def test_non_uid_54000_uid_refused(self):
        # The 54000-initializing return point carries the same shape
        # validation (round-18 F-e2).
        assert (
            extract_experiment_uid(
                '{"code":54000,"success":true,"result":{"uid":"nonsense"},'
                '"error":"the experiment is initializing, please wait"}'
            )
            is None
        )

    def test_shaped_success_result_still_ingests(self):
        # Control: the pure-face lane is untouched — a SHAPED success
        # result still ingests on a proven-pure domain.
        assert (
            extract_experiment_uid(
                f'{{"code":200,"success":true,"result":"{self.UID}"}}'
            )
            == self.UID
        )

    def test_host_evidence_unaffected_by_later_composite_kubectl(self):
        # Round-18 G (closed as a side effect of the F revert — the
        # ``strict`` flag no longer exists): a NEWER composite kubectl
        # message must not suppress an OLDER host ``blade_create``
        # receipt's evidence. The round-17 implementation leaked
        # ``strict=True`` across the reverse scan; the single-gate
        # rewrite cannot leak what it no longer carries.
        host_loose_only = '{"uid": "' + self.POD_UID + '"}'
        msgs = [
            AIMessage(
                content="",
                tool_calls=[{"id": "call_h", "name": "blade_create", "args": {}}],
            ),
            ToolMessage(
                content=host_loose_only,
                tool_call_id="call_h",
                name="blade_create",
            ),
        ] + self._composite_msgs("Create experiment failed!\n{}")
        idx, method = scan_blade_evidence_index(msgs)
        assert idx == 1 and method == "host_blade"


class TestStatusFaceShapeGate:
    """Round-19 N1: the status-face dict-result return point is gated.

    ``_parse_uid_from_status_content`` is the THIRD return point of the
    code=200/success=true extraction family (after the result string and
    the 54000-initializing uid the round-18 F-e ruling gated). Its dict
    branch used to accept ANY non-empty ``result.uid`` string; it now takes
    the same ``_UID_SHAPE_RE`` gate.
    """

    STATUS_UID = "a1b2c3d4e5f60718"

    def test_unit_non_shaped_dict_uid_refused(self):
        assert (
            _parse_uid_from_status_content(
                '{"code":200,"success":true,'
                '"result":{"uid":"not-a-uid-at-all","status":"Running"}}'
            )
            is None
        )

    def test_unit_shaped_dict_uid_ingests(self):
        assert (
            _parse_uid_from_status_content(
                '{"code":200,"success":true,'
                f'"result":{{"uid":"{self.STATUS_UID}","status":"Running"}}}}'
            )
            == self.STATUS_UID
        )

    def _priority3_messages(self, status_uid: str) -> list:
        # The existence gate only needs a create ToolMessage to EXIST (the
        # create-timeout recovery scenario: the create failed/timed out,
        # blade_status later discovers the experiment).
        return [
            ToolMessage(
                content='{"code":500,"success":false,"err":"timed out"}',
                name="blade_create",
                tool_call_id="tc-c",
            ),
            ToolMessage(
                content='{"code":200,"success":true,'
                f'"result":{{"uid":"{status_uid}","status":"Running"}}}}',
                name="blade_status",
                tool_call_id="tc-s",
            ),
        ]

    def test_priority3_non_shaped_status_uid_not_ingested(self):
        # End-to-end: the single slot stays empty when the status output's
        # uid is not experiment-UID shaped (round-19 N1).
        assert (
            extract_experiment_uid_from_messages(
                self._priority3_messages("garbage-token")
            )
            is None
        )

    def test_priority3_shaped_status_uid_ingests(self):
        # Control: the create-timeout recovery path itself is untouched — a
        # shaped uid discovered via blade_status still rides the single slot
        # (Priority 3's whole reason to exist).
        assert (
            extract_experiment_uid_from_messages(
                self._priority3_messages(self.STATUS_UID)
            )
            == self.STATUS_UID
        )


class TestPythonFaceFailedCreateAnchor:
    """Round-19 N2b: the python-application face's failed-create anchor.

    ``PY_FAILED_CREATE_UID_RE`` (module-level since round-19, composed from
    the single source) mines the registered experiment id out of the blade
    CLI's raw failure JSON so the caller can clean up instead of leaking an
    active interception.
    """

    PY_UID = "c1d2e3f4a5b60718"

    def test_shaped_uid_key_mines(self):
        m = PY_FAILED_CREATE_UID_RE.search(
            f'{{"code":500,"success":false,"uid":"{self.PY_UID}"}}'
        )
        assert m and m.group(1) == self.PY_UID

    def test_legacy_result_key_mines(self):
        m = PY_FAILED_CREATE_UID_RE.search(
            f'{{"code":500,"success":false,"result":"{self.PY_UID}"}}'
        )
        assert m and m.group(1) == self.PY_UID

    def test_over_length_hex_refused(self):
        # Round-19 N2b: the open ``{16,}`` bound admitted 40-hex
        # sha256-shaped garbage; the anchor now composes the bounded shape.
        assert PY_FAILED_CREATE_UID_RE.search('{"uid":"' + "b" * 40 + '"}') is None

    def test_uppercase_hex_refused(self):
        assert PY_FAILED_CREATE_UID_RE.search('{"uid":"ABCDEF0123456789"}') is None


class TestHex16ShapeSingleSource:
    """Round-19: the hex16 experiment-UID shape is single-sourced.

    r17-S3 tightened ``_UID_SHAPE_RE`` alone; every sibling capturing anchor
    kept its own (open-bounded / uppercase / dash-tolerant) definition — the
    "enumerate the repair surface" defect in regex form. These anchors pin
    both the structure (every capturing anchor composes HEX16_UID_SHAPE)
    and the behaviour (identical shapes rule identically on every face).
    """

    CAPTURING_ANCHORS = (
        FAILED_CREATE_UID_RE,
        RAW_FAILED_CREATE_UID_RE,
        PY_FAILED_CREATE_UID_RE,
    )

    def test_every_capturing_anchor_composes_the_single_source(self):
        # Structural: the shape authority literally builds each anchor.
        for anchor in self.CAPTURING_ANCHORS:
            assert HEX16_UID_SHAPE in anchor.pattern, anchor.pattern
        assert HEX16_UID_SHAPE in _UID_SHAPE_RE.pattern

    def test_lowercase_bounded_shapes_agree_across_anchors(self):
        for sample in ("a" * 16, "a" * 24, "f" * 32):
            for anchor in self.CAPTURING_ANCHORS:
                assert anchor.search(f"UID: {sample}") is not None or anchor.search(
                    f'"uid": "{sample}"'
                ), (sample, anchor.pattern)
            assert _UID_SHAPE_RE.fullmatch(sample)

    def test_out_of_domain_shapes_refused_by_every_anchor(self):
        for sample in ("a" * 40, "ABCDEF0123456789", "a" * 15):
            for anchor in self.CAPTURING_ANCHORS:
                assert anchor.search(f'"uid": "{sample}"') is None, (
                    sample,
                    anchor.pattern,
                )
            assert not _UID_SHAPE_RE.fullmatch(sample)

    def test_dashed_uuid_split_ruling_pins_the_domain_split(self):
        # dashed is destroy-face legacy compatibility ONLY: the JSON-aware
        # gate (_UID_SHAPE_RE) accepts it, every birth-side capturing anchor
        # refuses it (K8s-object vocabulary, round-16).
        dashed = OWN_UID
        assert _UID_SHAPE_RE.fullmatch(dashed)
        for anchor in self.CAPTURING_ANCHORS:
            assert anchor.search(f'"uid": "{dashed}"') is None, anchor.pattern
            assert anchor.search(f"UID: {dashed}") is None, anchor.pattern


class TestStrategyAnchorShapeSingleSource:
    """Round-20: the extraction strategies' own anchors join the single
    source.

    Round-19's "ONE authority every CAPTURING anchor composes from" list
    enumerated only the three failed-create dialect anchors plus the
    _UID_SHAPE_RE gate — the two anchors INSIDE extract_experiment_uid
    (the malformed-JSON key fallback _UUID_RE and the resource-name
    fallback _CHAOSBLADE_RESOURCE_RE) never entered it, so each kept its
    own dialect (dashed-only / prefixed-and-open-bounded): the "enumerate
    the repair surface" defect's 7th recurrence.
    """

    RES_UID = "a1b2c3d4e5f60718"

    def test_strategy_anchors_compose_the_single_source(self):
        # Structural: the full shape domain literally builds the JSON-aware
        # gate and the malformed-JSON fallback; the hex16 source builds the
        # resource-name capture.
        assert _UID_SHAPE_ALTERNATION in _UUID_RE.pattern
        assert _UID_SHAPE_ALTERNATION in _UID_SHAPE_RE.pattern
        assert HEX16_UID_SHAPE in _CHAOSBLADE_RESOURCE_RE.pattern

    def test_resource_name_strips_prefix(self):
        # The resource-name suffix IS the experiment UID — the capture
        # returns it bare (round-20 Q2: the prefixed string used to ride
        # every ingestor, short-circuiting even the round-19 N1 gate).
        got = extract_experiment_uid(
            "Error: experiment chaosblade-12345678abcdef01 rejected"
        )
        assert got == "12345678abcdef01"

    def test_short_resource_suffix_refused(self):
        # 8 hex < the legislated 16 lower bound — and its refusal lets the
        # N1 dict-branch gate run (the short-circuit cascade repaired).
        assert extract_experiment_uid("stale chaosblade-1234abcd leftover") is None
        got = _parse_uid_from_status_content(
            'chaosblade-1234abcd record: {"code":200,"success":true,'
            '"result":{"uid":"' + self.RES_UID + '","phase":"Running"}}'
        )
        assert got == self.RES_UID

    def test_forty_hex_resource_suffix_refused(self):
        # The word boundary is the right edge: no partial-match truncation
        # of a 40-hex suffix into a 32-char legal-shaped fake UID.
        assert extract_experiment_uid("chaosblade-" + "a" * 40 + " tail") is None

    def test_malformed_json_fallback_domain_parity(self):
        # Strategy 2 is strategy 1's malformed-JSON variant: same input
        # family, so the SAME shape domain (round-20 Q3 — it used to match
        # dashed-ONLY, leaving malformed hex16 receipts with no fallback
        # at all while admitting the K8s metadata.uid spelling verbatim).
        assert (
            extract_experiment_uid('wrapped: "result":"deadbeef00000001" trailing junk')
            == "deadbeef00000001"
        )
        assert (
            extract_experiment_uid(
                'wrapped: "result":"a1b2c3d4-1111-2222-3333-444455556666" t'
            )
            == OWN_UID
        )
        assert extract_experiment_uid('wrapped: "result":"' + "a" * 40 + '" t') is None
        assert extract_experiment_uid('wrapped: "result":"ABCDEF0123456789" t') is None

    def test_malformed_json_hex16_new_capability(self):
        # The dashed-only drift left truncated-stdout hex16 receipts with
        # NO extraction path at all (strategy 1 needs parseable JSON) —
        # domain parity closes the capability gap as a side effect.
        assert (
            extract_experiment_uid('trunc: "uid":"f00dface12345678" tail')
            == "f00dface12345678"
        )


class TestDurableReadSideShapeGate:
    """Round-20 Q4: the durable sources gate on the UID shape at the
    trust-chain END.

    The single slot and the owned registry trusted the writer chain
    wholesale (``str().strip()`` non-empty = whitelisted) — belt-and-
    suspenders now, so a non-shaped value can never ride the destroy
    whitelist whatever wrote it.
    """

    def test_chaosblade_durable_slot_gate(self):
        provider = ChaosbladeProvider()
        assert (
            provider.created_experiment_ids([], {"experiment_uid": "garbage!!!"})
            == set()
        )
        assert provider.created_experiment_ids(
            [], {"experiment_uid": "deadbeef00000001"}
        ) == {"deadbeef00000001"}

    def test_chaosblade_owned_registry_gate(self):
        provider = ChaosbladeProvider()
        got = provider.created_experiment_ids(
            [], {"owned_experiment_uids": ["junk", "deadbeef00000001", "!!!"]}
        )
        assert got == {"deadbeef00000001"}

    def test_python_provider_durable_gate(self):
        provider = ChaosbladePythonProvider()
        assert (
            provider.created_experiment_ids(
                [], {"experiment_uid": "junk-token", "injection_method": "python_agent"}
            )
            == set()
        )
        got = provider.created_experiment_ids(
            [],
            {
                "experiment_uid": "f00dface12345678",
                "injection_method": "python_agent",
                "owned_experiment_uids": ["junk", "f00dface12345678"],
            },
        )
        assert got == {"f00dface12345678"}

    def test_dashed_legacy_still_whitelisted(self):
        # The dashed legacy spelling passes the fullmatch gate (r14
        # destroy-face compatibility branch) — the read-side gate refuses
        # NON-shaped values, not legacy-shaped ones.
        provider = ChaosbladeProvider()
        assert provider.created_experiment_ids([], {"experiment_uid": OWN_UID}) == {
            OWN_UID
        }


class TestBareCompositeSegmentSplit:
    """Round-25 K2 root fix: a BARE ``&&``/``||``/``;``/``|``/``&`` token is
    a command boundary at the token-stream level — the same boundary the
    ``sh -c`` script face already draws character-level inside quotes.

    Pre-fix, a bare composite stayed ONE segment: the second destroy of
    ``blade destroy A && blade destroy B`` vanished from the death scan
    (the ``--uid`` spelling lost the FIRST instead —
    ``collect_flag_values`` last-wins), while an ``echo`` companion
    riding the same composite penetrated the ``pure_create``
    receipt-trust gate (round-25 K2c).
    """

    UID = "deadbeef00000001"
    UID2 = "1eafbeef00000003"
    PREP = "feedbeef00000009"

    def test_bare_positional_composite_registers_both(self):
        uids = inline_destroy_uids(
            f"tool-pod -n chaosblade -- blade destroy {self.UID} "
            f"&& blade destroy {self.UID2}"
        )
        assert uids == {self.UID, self.UID2}

    def test_bare_flag_composite_registers_both(self):
        # The two spellings used to lose OPPOSITE ends (positional lost
        # the second, flag lost the first); the split makes both whole.
        uids = inline_destroy_uids(
            f"tool-pod -- blade destroy --uid {self.UID} "
            f"&& blade destroy --uid {self.UID2}"
        )
        assert uids == {self.UID, self.UID2}

    def test_bare_semicolon_composite_registers_both(self):
        uids = inline_destroy_uids(
            f"tool-pod -- blade destroy {self.UID} ; blade destroy {self.UID2}"
        )
        assert uids == {self.UID, self.UID2}

    def test_quoted_script_face_unchanged(self):
        # Control: the sh -c quoted form already worked (the script
        # face's own splitter) — it must stay exactly as correct.
        uids = inline_destroy_uids(
            f'tool-pod -- sh -c "blade destroy {self.UID} '
            f'&& blade destroy {self.UID2}"'
        )
        assert uids == {self.UID, self.UID2}

    def test_wrapper_prefix_survives_the_split(self):
        # The wrapper eats into the FIRST side only; both sides still
        # resolve their own heads after the boundary split.
        uids = inline_destroy_uids(
            f"tool-pod -- timeout 30 blade destroy {self.UID} "
            f"&& blade destroy {self.UID2}"
        )
        assert uids == {self.UID, self.UID2}

    def test_pipe_splits_into_independent_segments(self):
        from chaos_agent.agent.providers.message_scanning import (
            exec_command_segments,
        )

        segs = exec_command_segments(
            f"pod -- blade destroy {self.UID} | kubectl get pod"
        )
        assert segs == [
            ["blade", "destroy", self.UID],
            ["kubectl", "get", "pod"],
        ]

    def test_redirection_token_is_not_a_boundary(self):
        # ``2>&1`` is a single token and REDIRECTION syntax, not a
        # boundary — the round-17 S1 judgement (verb behind an
        # interleaved redirection) must survive the split legislation.
        from chaos_agent.agent.providers.message_scanning import (
            exec_command_segments,
        )

        segs = exec_command_segments(f"pod -- blade 2>&1 destroy {self.UID}")
        assert segs == [["blade", "destroy", self.UID]]

    def test_glued_operator_stays_one_token(self):
        # ``x&&echo`` is ONE token — its interior is value space; only
        # the script face (character-level) may judge glued spellings.
        # Fail-closed: fewer segments, never a decoy promotion.
        from chaos_agent.agent.providers.message_scanning import (
            exec_command_segments,
        )

        segs = exec_command_segments("pod -- blade create x&&echo y")
        assert segs == [["blade", "create", "x&&echo", "y"]]

    def test_bare_echo_companion_refuses_pure_create(self):
        # Round-25 K2c: the echo companion riding a bare composite used
        # to hide inside the single create-verb segment — the
        # receipt-trust gate never saw it. The split restores the
        # round-16 A/E/F refusal for the bare dialect.
        payload = classify_blade_exec_payload(
            "tool-pod -- blade create k8s pod-cpu fullload "
            '&& echo {"code":200,"success":true,"result":"%s"}' % self.UID
        )
        assert payload.pure_create is False
        # has_create survives: the create segment is still a blade
        # segment — attribution is not traded for receipt-trust.
        assert payload.has_create is True

    def test_solo_create_keeps_pure_create(self):
        # Control: the uncompounded create payload remains exactly as
        # ingestible as before (the gate must not over-fire).
        payload = classify_blade_exec_payload(
            "tool-pod -- blade create k8s pod-cpu fullload"
        )
        assert payload.pure_create is True

    def test_proven_scan_registers_both_kills_from_one_output(self):
        # Round-26 alignment: a composite's REAL receipt is one JSON
        # object per command — the single-JSON shape the J3 era anchored
        # is an impossible form (blade prints once per invocation). The
        # line-per-segment alignment registers both kills, each proven
        # by its OWN receipt line.
        msgs = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_bc",
                        "name": "kubectl",
                        "args": {
                            "subcommand": "exec",
                            "v_args": (
                                f"tool-pod -- blade destroy {self.UID} "
                                f"&& blade destroy {self.UID2}"
                            ),
                        },
                    }
                ],
            ),
            ToolMessage(
                content=(
                    '{"code":200,"success":true,"result":"success"}\n'
                    '{"code":200,"success":true,"result":"success"}'
                ),
                tool_call_id="call_bc",
                name="kubectl",
            ),
        ]
        assert scan_destroyed_proven_uids(msgs) == {self.UID, self.UID2}
        assert scan_destroyed_uids(msgs) == {self.UID, self.UID2}


class TestInlineRevokeNotADeathCarrier:
    """Round-25 K1: revoke tears down a PREPARE uid — a precondition —
    never an experiment. The inline face's verb enumeration carried the
    r14-era ``('destroy', 'revoke')`` vocabulary while the round-24 K3
    ruling landed on the host face only; this aligns the inline face.
    The mutating/provenance semantics of ``destroy/revoke`` stay where
    they belong (the provider's target-scope judgement).
    """

    PREP = "feedbeef00000009"

    def _revoke_msgs(self):
        return [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_rv",
                        "name": "kubectl",
                        "args": {
                            "subcommand": "exec",
                            "v_args": (
                                f"tool-pod -n chaosblade -- blade revoke {self.PREP}"
                            ),
                        },
                    }
                ],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"success"}',
                tool_call_id="call_rv",
                name="kubectl",
            ),
        ]

    def test_revoke_never_registers_a_death(self):
        msgs = self._revoke_msgs()
        assert scan_destroyed_proven_uids(msgs) == set()
        assert inline_destroy_uids(
            f"tool-pod -- blade revoke {self.PREP}"
        ) == set()

    def test_revoke_never_makes_a_uid_terminal(self):
        # Issued=terminal attribution is experiment semantics: a revoke
        # targeting an experiment uid is a misuse that must NOT remove
        # the uid from the live-fault claim set.
        msgs = self._revoke_msgs()
        assert scan_destroyed_uids(msgs) == set()

    def test_destroy_verb_still_registers(self):
        # Control: the destroy half of the vocabulary is untouched.
        msgs = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_d",
                        "name": "kubectl",
                        "args": {
                            "subcommand": "exec",
                            "v_args": f"tool-pod -- blade destroy {self.PREP}",
                        },
                    }
                ],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"success"}',
                tool_call_id="call_d",
                name="kubectl",
            ),
        ]
        assert scan_destroyed_proven_uids(msgs) == {self.PREP}


class TestExecutionEventAlignment:
    """Round-26 root fix: the receipt-side half of the composite family.

    ``align_execution`` is the single decomposition primitive — one event
    per command segment, each with its OWN receipt slice. The layers are
    decided by STRUCTURE (companion presence, segment count, line count,
    JSON shape), never by receipt wording.
    """

    UID = "deadbeef00000001"
    UID2 = "1eafbeef00000003"
    SUCCESS = '{"code":200,"success":true,"result":"success"}'
    FAIL = '{"code":500,"success":false,"error":"boom"}'

    def _events(self, v_args: str, receipt: str):
        from chaos_agent.agent.providers.chaosblade.verify import align_execution

        return align_execution(v_args, receipt)

    def test_layer1_single_command_owns_whole_receipt(self):
        # A single command keeps the existing verdict lanes byte-for-byte:
        # the whole receipt (preamble, prose, whatever) is its slice.
        evs = self._events(
            f"tool-pod -- blade destroy {self.UID}", self.SUCCESS
        )
        assert len(evs) == 1 and evs[0].provable
        assert evs[0].receipt_slice == self.SUCCESS
        assert evs[0].segment[1] == "destroy"

    def test_layer2_exact_line_alignment_is_provable(self):
        cmd = (
            f"tool-pod -- blade destroy {self.UID} "
            f"&& blade destroy {self.UID2}"
        )
        evs = self._events(cmd, self.SUCCESS + "\n" + self.SUCCESS)
        assert len(evs) == 2
        assert all(e.provable for e in evs)
        assert evs[0].receipt_slice == self.SUCCESS
        assert evs[1].receipt_slice == self.SUCCESS

    def test_layer2_line_count_mismatch_fails_closed(self):
        # && left-failure: right never ran, ONE line for TWO segments.
        cmd = (
            f"tool-pod -- blade destroy {self.UID} "
            f"&& blade destroy {self.UID2}"
        )
        evs = self._events(cmd, self.FAIL)
        assert len(evs) == 2 and not any(e.provable for e in evs)

    def test_layer2_non_json_line_fails_closed(self):
        cmd = (
            f"tool-pod -- blade destroy {self.UID} "
            f"&& blade destroy {self.UID2}"
        )
        evs = self._events(cmd, self.FAIL + "\ndestroyed")
        assert not any(e.provable for e in evs)

    def test_layer3_companion_makes_all_unprovable(self):
        # An echo companion rides the output path — its contribution is
        # forgeable, so NO event's slice can be attributed.
        evs = self._events(
            f"tool-pod -- blade destroy {self.UID} && echo destroyed",
            self.FAIL + "\ndestroyed",
        )
        assert len(evs) == 2 and not any(e.provable for e in evs)

    def test_layer3_pipe_downstream_is_companion_segment(self):
        # The round-25 split draws | as a boundary: wc is a companion
        # whose output TRANSLATES the receipt — unprovable by physics.
        evs = self._events(f"tool-pod -- blade destroy {self.UID} | wc -l", "1")
        assert len(evs) == 2
        assert evs[0].segment[0] == "blade"
        assert evs[1].segment[0] == "wc"
        assert not any(e.provable for e in evs)

    def test_unpaired_receipt_is_empty_text(self):
        # results.get() returns None for an unpaired call — the receipt
        # face must tolerate it (empty text, alignment fails closed).
        evs = self._events(
            f"tool-pod -- blade destroy {self.UID}", None
        )
        assert len(evs) == 1 and evs[0].provable
        assert evs[0].receipt_slice == ""


class TestDeathFacePerEventAttribution:
    """The death face consumes aligned events: each destroy's proof is its
    OWN receipt line — K1 (echo forgery), K2 (sibling laundering), K3
    (pipe translation) all closed; the single-command mainline and the
    host tool face unchanged."""

    UID = "deadbeef00000001"
    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"
    SUCCESS = '{"code":200,"success":true,"result":"success"}'
    FAIL = '{"code":500,"success":false,"error":"boom"}'

    @staticmethod
    def _pair(v_args: str, content: str, tc_id: str) -> list:
        return [
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "exec", "v_args": v_args},
                "id": tc_id,
                "type": "tool_call",
            }]),
            ToolMessage(content=content, name="kubectl", tool_call_id=tc_id),
        ]

    def test_echo_companion_forgery_proves_nothing(self):
        # K1: X's REAL receipt is a code:500 failure; the echo stage
        # appends "destroyed" — pre-fix the wording lane returned SUCCESS
        # and X false-retired into the durable ledger.
        msgs = self._pair(
            f"tool-pod -- blade destroy {self.UID} && echo destroyed",
            self.FAIL + "\ndestroyed",
            "tc-k1",
        )
        assert scan_destroyed_proven_uids(msgs) == set()

    def test_mixed_outcome_laundering_closed(self):
        # K2: A succeeded, B failed — pre-fix one "success" substring
        # retired BOTH; now each destroy reads its own line.
        msgs = self._pair(
            f"tool-pod -- blade destroy {self.UID_A} ; blade destroy {self.UID_B}",
            self.SUCCESS + "\n" + self.FAIL,
            "tc-k2",
        )
        assert scan_destroyed_proven_uids(msgs) == {self.UID_A}

    def test_pipe_translation_proves_nothing(self):
        # K3: X genuinely died but wc TRANSLATED the receipt to a digit —
        # unprovable by physics; the convergence valve owns the doubt.
        msgs = self._pair(
            f"tool-pod -- blade destroy {self.UID} | wc -l", "1", "tc-k3"
        )
        assert scan_destroyed_proven_uids(msgs) == set()

    def test_double_success_registers_both(self):
        # The honest composite receipt: each kill proven by its own line.
        msgs = self._pair(
            f"tool-pod -- blade destroy {self.UID_A} && blade destroy {self.UID_B}",
            self.SUCCESS + "\n" + self.SUCCESS,
            "tc-dbl",
        )
        assert scan_destroyed_proven_uids(msgs) == {self.UID_A, self.UID_B}

    def test_single_command_mainline_unchanged(self):
        # C4: the everyday single destroy keeps the blob verdict lanes
        # (JSON authority, wording fallback, not-found valve).
        msgs = self._pair(
            f"tool-pod -- blade destroy {self.UID}", self.SUCCESS, "tc-c4"
        )
        assert scan_destroyed_proven_uids(msgs) == {self.UID}

    def test_host_tool_face_unchanged(self):
        # The blade_destroy tool call is a single command by protocol —
        # its paired output is judged whole, exactly as before.
        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "blade_destroy",
                "args": {"uid": self.UID},
                "id": "tc-host",
                "type": "tool_call",
            }]),
            ToolMessage(content=self.SUCCESS, name="blade_destroy", tool_call_id="tc-host"),
        ]
        assert scan_destroyed_proven_uids(msgs) == {self.UID}


class TestPluralBirthFace:
    """Round-26 birth face: a composite inline create proves MULTIPLE
    births in one call — the plural face registers every one of them (the
    single-slot scan surfaces only the first, so the second was born an
    orphan: invisible to the ownership ledger, unrecoverable by any
    sweep)."""

    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"
    RECEIPT_A = '{"code":200,"success":true,"result":"%s"}' % UID_A
    RECEIPT_B = '{"code":200,"success":true,"result":"%s"}' % UID_B

    @staticmethod
    def _pair(v_args: str, content: str, tc_id: str) -> list:
        return [
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "exec", "v_args": v_args},
                "id": tc_id,
                "type": "tool_call",
            }]),
            ToolMessage(content=content, name="kubectl", tool_call_id=tc_id),
        ]

    def _plural(self, msgs):
        from chaos_agent.agent.providers.chaosblade.verify import (
            extract_experiment_uids_from_messages,
        )

        return extract_experiment_uids_from_messages(msgs)

    def test_composite_create_registers_both_births(self):
        cmd = (
            f"tool-pod -- blade create k8s pod-cpu fullload --uid {self.UID_A} "
            f"&& blade create k8s mem load --uid {self.UID_B}"
        )
        born = self._plural(
            self._pair(cmd, self.RECEIPT_A + "\n" + self.RECEIPT_B, "tc-b1")
        )
        assert born == {self.UID_A, self.UID_B}

    def test_singular_face_still_returns_one(self):
        # The single-slot seam's contract is unchanged (which ONE is
        # current): the fossil is on the OWNERSHIP side, not the slot.
        from chaos_agent.agent.providers.chaosblade.verify import (
            extract_experiment_uid_from_messages,
        )

        cmd = (
            f"tool-pod -- blade create k8s pod-cpu fullload --uid {self.UID_A} "
            f"&& blade create k8s mem load --uid {self.UID_B}"
        )
        uid = extract_experiment_uid_from_messages(
            self._pair(cmd, self.RECEIPT_A + "\n" + self.RECEIPT_B, "tc-b2")
        )
        assert uid == self.UID_A

    def test_companion_create_licenses_nothing(self):
        # Same composition gate as ever (round-16 A/E/F): an echo riding
        # the output path licenses no birth.
        born = self._plural(
            self._pair(
                f"tool-pod -- blade create k8s pod-cpu fullload "
                f"&& echo {self.RECEIPT_A}",
                self.RECEIPT_A,
                "tc-b3",
            )
        )
        assert born == set()

    def test_mixed_composite_create_registers_success_only(self):
        # A's receipt is success, B's is a failure — B owns no liability.
        fail_b = '{"code":500,"success":false,"error":"boom"}'
        cmd = (
            f"tool-pod -- blade create k8s pod-cpu fullload --uid {self.UID_A} "
            f"&& blade create k8s mem load --uid {self.UID_B}"
        )
        born = self._plural(
            self._pair(cmd, self.RECEIPT_A + "\n" + fail_b, "tc-b4")
        )
        assert born == {self.UID_A}

    def test_multiple_tool_face_calls_register_all(self):
        # Two blade_create tool calls = two births; the singular face
        # returns only the newest, the plural face owns both.
        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "blade_create",
                "args": {"command": "create k8s pod-cpu fullload"},
                "id": "tc-b5a",
                "type": "tool_call",
            }]),
            ToolMessage(
                content=self.RECEIPT_A, name="blade_create", tool_call_id="tc-b5a"
            ),
            AIMessage(content="", tool_calls=[{
                "name": "blade_create",
                "args": {"command": "create k8s mem load"},
                "id": "tc-b5b",
                "type": "tool_call",
            }]),
            ToolMessage(
                content=self.RECEIPT_B, name="blade_create", tool_call_id="tc-b5b"
            ),
        ]
        assert self._plural(msgs) == {self.UID_A, self.UID_B}

    def test_registry_union_seam_plural(self):
        # The registry's ownership seam unions every UID-bearing
        # provider's plural face (falling back to singular-wrapped-in-set
        # for providers that have not pluralised).
        cmd = (
            f"tool-pod -- blade create k8s pod-cpu fullload --uid {self.UID_A} "
            f"&& blade create k8s mem load --uid {self.UID_B}"
        )
        born = FaultProviderRegistry.extract_experiment_uids(
            self._pair(cmd, self.RECEIPT_A + "\n" + self.RECEIPT_B, "tc-b6"),
            is_host=False,
        )
        assert born == {self.UID_A, self.UID_B}


# ---------------------------------------------------------------------------
# Round-27 root fix — the birth family's single licensing primitive
# (receipt_birth_uids): a birth licence is CONTENT-DERIVED (the uid lives
# in the receipt line; the segment argv never carried it), so positional
# alignment stays the DEATH face's own discipline. One primitive behind
# the whitelist inline face, the plural ownership face and the singular
# live face's kubectl lane — the 1:1 fossil's last three carriers.
# ---------------------------------------------------------------------------


class TestReceiptBirthLicensing:
    """The shared primitive itself: per-line, transport-wrapper-tolerant,
    JSON-blind fallback preserved."""

    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"
    OK_A = '{"code":200,"success":true,"result":"%s"}' % UID_A
    OK_B = '{"code":200,"success":true,"result":"%s"}' % UID_B
    FAIL_A = '{"code":500,"success":false,"uid":"%s"}' % UID_A
    FAIL_B = '{"code":500,"success":false,"uid":"%s"}' % UID_B

    def _births(self, content):
        from chaos_agent.agent.providers.chaosblade.verify import receipt_birth_uids

        return receipt_birth_uids(content)

    def test_double_success_licenses_both_in_content_order(self):
        births = self._births(self.OK_A + "\n" + self.OK_B)
        assert births == [self.UID_A, self.UID_B]

    def test_failure_line_licenses_no_birth(self):
        # A failed create owns no liability; its CRD uid is the whitelist's
        # finditer lane, never a live birth.
        assert self._births(self.FAIL_A + "\n" + self.OK_B) == [self.UID_B]

    def test_kubectl_error_wrapper_is_unwrapped(self):
        # The kubectl tool glues ``Error: kubectl exec (exit 1): `` onto
        # the first stdout line when the batch exits non-zero (``create A
        # && create B`` with B failing) — batch metadata, not segment
        # output: A's line still licenses A.
        wrapped = "Error: kubectl exec (exit 1): " + self.OK_A + "\n" + self.FAIL_B
        assert self._births(wrapped) == [self.UID_A]

    def test_short_circuit_failure_licenses_nothing(self):
        # ``create A && create B`` with A failing short-circuits B away:
        # one failure line, no live births (A's CRD rides the finditer
        # lane in the whitelist face).
        assert self._births(self.FAIL_A) == []

    def test_extra_trailer_lines_do_not_block_licensing(self):
        # Direct-mode failure content joins stderr after stdout: the
        # kubectl trailer is an extra non-JSON line — extra lines license
        # nothing themselves and never block the JSON lines that ARE
        # there (no count gate for births: the licence is content-derived).
        content = (
            self.OK_A + "\n" + self.OK_B
            + "\nerror: command terminated with exit code 1"
        )
        assert self._births(content) == [self.UID_A, self.UID_B]

    def test_json_blind_receipt_keeps_whole_content_fallback(self):
        # Layer-1 parity: a receipt with NO parseable JSON line (truncated
        # stdout / transport garbage) still licenses through the extractor
        # chain — single-command receipts always did.
        assert self._births(
            "Error: experiment chaosblade-%s rejected" % self.UID_A
        ) == [self.UID_A]


class TestWhitelistPluralCascade:
    """K1: the destroy-gate whitelist's inline face licenses EVERY birth a
    pure-create receipt proves (the singular blob walk licensed only the
    first, so the second birth was REJECT_UNKNOWN at the gate and leaked
    out of the hydration fallback's ownership rebuild — legacy checkpoints
    / DB-only recovery, the fallback's own reason to exist)."""

    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"
    OK_A = '{"code":200,"success":true,"result":"%s"}' % UID_A
    OK_B = '{"code":200,"success":true,"result":"%s"}' % UID_B
    FAIL_B = '{"code":500,"success":false,"uid":"%s"}' % UID_B
    DESTROY_OK = '{"code":200,"success":true,"result":"success"}'
    CREATE_CMD = (
        "tool-pod -- blade create k8s pod-cpu fullload --cpu-percent 80 "
        "--timeout 60 && blade create k8s pod-mem load --mode ram "
        "--mem-percent 90 --timeout 60"
    )

    def test_double_success_whitelists_both(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            inline_blade_create_receipt_uids,
        )

        msgs = _exec_pair(self.CREATE_CMD, self.OK_A + "\n" + self.OK_B, "tc-w1")
        assert inline_blade_create_receipt_uids(msgs) == {self.UID_A, self.UID_B}

    def test_gate_membership_covers_second_birth(self):
        # tool_screener's destroy gate is a pure membership test over this
        # set (tool_screener.py:423) — in the hydration-fallback posture
        # (no owned registry, no durable slot) the LLM's own
        # ``blade destroy B`` must be ALLOWABLE.
        msgs = _exec_pair(self.CREATE_CMD, self.OK_A + "\n" + self.OK_B, "tc-w2")
        whitelist = FaultProviderRegistry.created_experiment_ids(msgs, {})
        assert whitelist == {self.UID_A, self.UID_B}

    def test_hydration_liability_keeps_surviving_sibling(self):
        # live_liability_uids rebuilding ownership from the provenance scan
        # (owned deliberately absent — legacy checkpoint / DB-only
        # recovery): A's destroy is PROVEN, B stays live — B must stay in
        # the sweep's liability set instead of leaking past every valve.
        from chaos_agent.agent.state import live_liability_uids

        msgs = _exec_pair(self.CREATE_CMD, self.OK_A + "\n" + self.OK_B, "tc-w3")
        msgs += _exec_pair(
            "tool-pod -- blade destroy %s" % self.UID_A,
            self.DESTROY_OK,
            "tc-w3k",
        )
        values = {"messages": msgs, "retired_experiment_uids": []}
        assert live_liability_uids(values) == [self.UID_B]

    def test_error_wrapped_composite_whitelists_both_lanes(self):
        # A licensed as a live birth through the unwrapped line; B's
        # failed-create CRD still owed cleanup through the finditer lane.
        from chaos_agent.agent.providers.chaosblade.verify import (
            inline_blade_create_receipt_uids,
        )

        msgs = _exec_pair(
            self.CREATE_CMD,
            "Error: kubectl exec (exit 1): " + self.OK_A + "\n" + self.FAIL_B,
            "tc-w4",
        )
        assert inline_blade_create_receipt_uids(msgs) == {self.UID_A, self.UID_B}


class TestSingularLiveBirthGranularity:
    """K2: the destroyed filter sits at BIRTH granularity — a composite
    create message whose FIRST birth died must still surface its live
    sibling (the message-granular filter skipped the whole message and
    every singular consumer — replan seam, compactor pin, session
    recovery — reported "no live experiment" while the sibling ran)."""

    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"
    OK_A = '{"code":200,"success":true,"result":"%s"}' % UID_A
    OK_B = '{"code":200,"success":true,"result":"%s"}' % UID_B
    FAIL_B = '{"code":500,"success":false,"uid":"%s"}' % UID_B
    DESTROY_OK = '{"code":200,"success":true,"result":"success"}'
    CREATE_CMD = (
        "tool-pod -- blade create k8s pod-cpu fullload --cpu-percent 80 "
        "--timeout 60 && blade create k8s pod-mem load --mode ram "
        "--mem-percent 90 --timeout 60"
    )

    def test_singular_returns_surviving_sibling(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            extract_experiment_uid_from_messages,
        )

        msgs = _exec_pair(self.CREATE_CMD, self.OK_A + "\n" + self.OK_B, "tc-s1")
        msgs += _exec_pair(
            "tool-pod -- blade destroy %s" % self.UID_A,
            self.DESTROY_OK,
            "tc-s1k",
        )
        assert extract_experiment_uid_from_messages(msgs) == self.UID_B

    def test_singular_keeps_first_when_both_live(self):
        # The round-26 single-slot contract anchor: which ONE is current —
        # the FIRST live birth in content order.
        from chaos_agent.agent.providers.chaosblade.verify import (
            extract_experiment_uid_from_messages,
        )

        msgs = _exec_pair(self.CREATE_CMD, self.OK_A + "\n" + self.OK_B, "tc-s2")
        assert extract_experiment_uid_from_messages(msgs) == self.UID_A

    def test_plural_licenses_through_error_wrapper(self):
        # The r26 plural face's transport blind spot closed: an honest
        # birth behind the kubectl Error: wrapper no longer leaks out of
        # the liability ledger (the wrapper is batch metadata, and birth
        # licensing was never positional).
        from chaos_agent.agent.providers.chaosblade.verify import (
            extract_experiment_uids_from_messages,
        )

        msgs = _exec_pair(
            self.CREATE_CMD,
            "Error: kubectl exec (exit 1): " + self.OK_A + "\n" + self.FAIL_B,
            "tc-s3",
        )
        assert extract_experiment_uids_from_messages(msgs) == {self.UID_A}


class TestAttributionFacePluralLicensing:
    """Round-28 K2 — the attribution face's kubectl lane licenses through
    the birth family's primitive (the FIFTH private 1:1 copy rewired):
    a composite double-create whose FIRST birth was destroyed keeps
    attesting the LIVE sibling instead of skipping the whole message on
    the dead uid (the singular walk returned (-1, None) and detect()'s
    arbitration lost the blade candidate to a later native fallback)."""

    UID_A = "aabbccdd00000001"
    UID_B = "9988776600000001"
    OK_A = '{"code":200,"success":true,"result":"%s"}' % UID_A
    OK_B = '{"code":200,"success":true,"result":"%s"}' % UID_B
    DESTROY_OK = '{"code":200,"success":true,"result":"success"}'
    CREATE_CMD = (
        "tool-pod -- blade create k8s pod-cpu fullload --cpu-percent 80 "
        "--timeout 60 && blade create k8s pod-mem load --mode ram "
        "--mem-percent 90 --timeout 60"
    )

    def _composite_with_first_destroyed(self):
        msgs = _exec_pair(self.CREATE_CMD, self.OK_A + "\n" + self.OK_B, "tc-a1")
        msgs += _exec_pair(
            "tool-pod -- blade destroy %s" % self.UID_A,
            self.DESTROY_OK,
            "tc-a1k",
        )
        return msgs

    def test_first_dead_sibling_live_still_attests(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            scan_blade_evidence_index,
            scan_destroyed_uids,
        )

        msgs = self._composite_with_first_destroyed()
        idx, method = scan_blade_evidence_index(
            msgs, destroyed=scan_destroyed_uids(msgs),
        )
        # ToolMessage sits at index 1 (its owning AIMessage at 0).
        assert idx == 1 and method == "kubectl_exec"

    def test_all_dead_message_is_skipped(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            scan_blade_evidence_index,
            scan_destroyed_uids,
        )

        msgs = self._composite_with_first_destroyed()
        msgs += _exec_pair(
            "tool-pod -- blade destroy %s" % self.UID_B,
            self.DESTROY_OK,
            "tc-a2k",
        )
        idx, method = scan_blade_evidence_index(
            msgs, destroyed=scan_destroyed_uids(msgs),
        )
        assert idx == -1 and method is None

    def test_both_live_keeps_first_birth_recency(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            scan_blade_evidence_index,
        )

        msgs = _exec_pair(self.CREATE_CMD, self.OK_A + "\n" + self.OK_B, "tc-a3")
        idx, method = scan_blade_evidence_index(msgs)
        assert idx == 1 and method == "kubectl_exec"

    def test_detect_method_keeps_blade_candidate(self):
        # The consumer-side cascade: detect()'s arbitration needs the
        # blade candidate to survive the destroyed filter, or a later
        # native injection wins the attribution over a LIVE blade
        # experiment.
        msgs = self._composite_with_first_destroyed()
        assert ChaosbladeProvider().detect(msgs, is_host=False) == "kubectl_exec"


class TestPrimitiveFallbackBlockingParity:
    """Round-28 K1 — the licensing primitive's fallback-blocking
    jurisdiction matches the family's single-source legislation
    (round-22 Q4b): only a STRUCTURED BLADE RECEIPT (a dict carrying
    code/success semantics) blocks the JSON-blind fallback. A
    non-receipt dict line never carried blade semantics — blocking on
    it orphaned a uid the truncated text still carried."""

    UID_A = "aabbccdd00000001"

    def test_non_receipt_dict_does_not_block_fallback(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            receipt_birth_uids,
        )

        text = '{"note":"not a blade receipt"}\n"uid": "%s"' % self.UID_A
        # The extractor chain (round-22 Q4b) licenses through the regex
        # fallback — the primitive must agree, not drift stricter.
        assert receipt_birth_uids(text) == [self.UID_A]

    def test_structured_receipt_still_blocks_fallback(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            receipt_birth_uids,
        )

        # A FAILED blade receipt line + the uid in loose text: the
        # structured refusal holds the blocking jurisdiction — the
        # fallback must not overrule a verdict it cannot read.
        text = (
            '{"code":500,"success":false,"error":"rpc timeout"}\n'
            '"uid": "%s"' % self.UID_A
        )
        assert receipt_birth_uids(text) == []

    def test_blade_receipt_line_still_licenses(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            receipt_birth_uids,
        )

        text = (
            '{"code":200,"success":true,"result":"%s"}\n'
            '{"note":"unrelated dict line"}' % self.UID_A
        )
        # The receipt line licenses its own birth; the non-receipt dict
        # line is transparent to the licensing scan.
        assert receipt_birth_uids(text) == [self.UID_A]


class TestChrootProjectionContract:
    """R26/G-10: the parser's chroot PROJECTION contract, both directions.

    ``exec_command_segments`` gained a ``chroot_delegation`` switch — one
    parser, two projections. The DEFAULT is the delegation view ("what
    actually runs": ``chroot /host blade destroy`` → ``[blade, …]``),
    which every pre-R26 consumer (blade payload classification, the G-9
    fault-binary marker) is built on; ``chroot_delegation=False`` keeps
    the chroot token as head ("how the host was entered"), which the
    escape-primitive check needs. The teeth pin BOTH directions: a
    future default flip would silently re-project every consumer that
    never passes the flag — the mutation probe showed no existing tooth
    bites on the default alone, so the contract gets its own.
    """

    def test_default_delegates_chroot_to_the_real_command(self):
        segs = exec_command_segments(
            "tp -n ns -- chroot /host blade destroy aa11bb22cc33dd44"
        )
        assert segs and segs[0][0] == "blade", (
            "the default projection delegates `chroot NEWROOT CMD` down to "
            "the CMD — the 'what actually runs' view the blade payload "
            "classifier (has_destroy) and the G-9 fault-binary marker ride; "
            "flipping the default breaks both consumers silently"
        )

    def test_chroot_keep_projection_keeps_the_primitive_as_head(self):
        segs = exec_command_segments(
            "tp -n ns -- sh -c 'cat f; chroot /host bash'",
            chroot_delegation=False,
        )
        assert any(
            seg and seg[0].rsplit("/", 1)[-1] == "chroot" for seg in segs
        ), (
            "the chroot-KEEP projection must keep the primitive at command "
            "position — a delegated tail (`[bash]`) erases the very "
            "primitive the escape legislation needs"
        )


class TestCommandSubstitutionSegments:
    r"""R33/G-12: command substitution is a SEGMENT boundary, not a word.

    The splitter's vocabulary was the six separators (``;`` ``&&`` ``||``
    ``|`` ``&`` newline) — but shell runs commands from four more
    structures: ``$()`` command substitution, backticks, ``( )`` subshells
    and ``{ }`` command groups. A primitive riding one of those
    structures (``cat f; $(nsenter -t 1 -m sh)`` — structure-head,
    argument-position, or double-quoted) never produced a segment whose
    head was the primitive, so every segment-based consumer (escape
    check, fault-binary marker, blade payload classification, death
    ledger) was blind to a command the shell EXECUTES — the G-series
    "traversal domain ≠ legislated domain" defect recurring inside the
    G-9/G-10 fix itself. The teeth pin: structures split, expansions
    (``${VAR}``/``$((...))``/``\$(...)``/single-quoted literals) do not,
    and the legitimate skill corpus stays unattributed.
    """

    @staticmethod
    def _heads(v_args: str) -> list[str]:
        return [
            seg[0].rsplit("/", 1)[-1]
            for seg in exec_command_segments(v_args, chroot_delegation=False)
            if seg
        ]

    def test_structure_head_riding_primitive_is_a_segment(self):
        # The seven hidden forms from the R33 probe matrix — each must
        # yield a segment headed by the primitive (currently: all seven
        # classified scope=pod, route=pass end-to-end).
        forms = [
            "tp -n ns -- sh -c 'cat f; $(nsenter -t 1 -m sh)'",
            "tp -n ns -- sh -c 'cat f; `nsenter -t 1 -m sh`'",
            "tp -n ns -- sh -c 'cat f; (nsenter -t 1 -m sh)'",
            "tp -n ns -- sh -c 'cat f; { nsenter -t 1 -m sh; }'",
            "tp -n ns -- sh -c 'cat f; \"$(nsenter -t 1 -m sh)\"'",
            "tp -n ns -- sh -c 'echo done `chroot /host bash`'",
            "tp -n ns -- sh -c 'echo done (unshare -m sh)'",
        ]
        for v in forms:
            heads = self._heads(v)
            assert "nsenter" in heads or "chroot" in heads or "unshare" in heads, (
                f"a primitive riding a shell structure must head its own "
                f"segment — the shell executes it: {v!r} → {heads}"
            )

    def test_argument_position_cmdsub_splits_the_inner_command(self):
        # `cat $(nsenter …)` — the substitution runs BEFORE cat; the
        # inner command is its own segment.
        heads = self._heads("tp -n ns -- sh -c 'cat $(nsenter -t 1 -m cat /etc/shadow)'")
        assert "nsenter" in heads, (
            f"argument-position command substitution executes the inner "
            f"command — it must be a visible segment: {heads}"
        )

    def test_nested_cmdsub_innermost_primitive_is_visible(self):
        heads = self._heads("tp -n ns -- sh -c 'cat f; $(echo $(nsenter -t 1 -m sh))'")
        assert "nsenter" in heads, heads

    def test_case_branch_command_heads_its_own_segment(self):
        # `case x in a) nsenter …;; esac` — the pattern terminator `)`
        # OPENS the branch command list (shell executes it); the branch
        # command must be its own segment. Pre-fix: `)` with an empty
        # suspend stack fell into the buffer and nsenter stayed an
        # argument of `case` (probe: scope=pod, route=pass end-to-end).
        heads = self._heads(
            "tp -n ns -- sh -c 'case x in a) nsenter -t 1 -m sh;; esac'"
        )
        assert "nsenter" in heads, (
            f"a case-branch command executes like any other — it must "
            f"head its own segment: {heads}"
        )

    def test_expansions_and_literals_do_not_split(self):
        # ${VAR} expands a VARIABLE, $((...)) is arithmetic, \$(...) is
        # escaped (the systemd-run corpus's deferred-expansion spelling),
        # and a single-quoted literal is a literal — none runs a command.
        singles = [
            "tp -n ns -- sh -c 'cat ${HOSTNAME} '",
            "tp -n ns -- sh -c 'echo $((1+1))'",
            "tp -n ns -- sh -c 'echo \"\\$(seq 1 3)\"'",
            "tp -n ns -- sh -c 'echo '\\''$(nsenter)'\\'''",
            "tp -n ns -- awk '{print $10}' /proc/diskstats",
        ]
        for v in singles:
            heads = self._heads(v)
            assert not any(h in ("nsenter", "chroot", "unshare") for h in heads), (
                f"non-command expansions must not invent segments: {v!r} → {heads}"
            )

    def test_brace_expansion_in_argument_position_stays_literal(self):
        # `cat {a,b}` is brace EXPANSION (two words to cat), not a command
        # group — the brace sits in argument position (buf non-empty).
        heads = self._heads("tp -n ns -- sh -c 'cat {a,b} '")
        assert heads == ["cat"], heads

    def test_legitimate_cmdsub_corpus_stays_unattributed(self):
        # Real skill-corpus shapes: the debug-pod kubelet cmdline probe
        # (argument-position $(pgrep …)) and the Pod_Pending deferred
        # restore payload (subshell + $(cat token) inside double quotes).
        # Splitting them must not manufacture an escape/fbm/blade head.
        corpus = [
            "tp -n ns -- sh -c 'tr \"\\0\" \"\\n\" < /proc/$(pgrep -x kubelet | head -1)/cmdline | grep -E eviction'",
            (
                "tp -n ns -- sh -c '( sleep 30; curl -s -X PATCH --cacert $C "
                "-H \"Authorization: Bearer $(cat /var/run/tok)\" "
                "https://k8s.default.svc ) >/tmp/r.log 2>&1 & echo armed'"
            ),
        ]
        banned = ("nsenter", "chroot", "unshare", "blade", "stress-ng")
        for v in corpus:
            heads = self._heads(v)
            assert not any(h in banned for h in heads), (
                f"legitimate corpus shapes must not grow attributed heads: "
                f"{v!r} → {heads}"
            )

    def test_argument_tail_after_closer_is_not_a_command(self):
        # R35/G-13 — the closer's PARAMETER TAIL. `echo done $(date)
        # stress-ng -c 1`: shell semantics make stress-ng echo's
        # argument TEXT (sh probe: it is printed, never executed), but
        # the G-12 closer split promoted it to a segment HEAD — four
        # consumers misjudged (escape scope=__escape__ rejected the
        # legal form; fbm withheld the demolition exemption;
        # has_destroy=True; align ledgered 3 ghost destroy events).
        # The round-15 parser contract says an argument-position word
        # never produces a segment of its own: the tail must merge back
        # into the HOST command's segment.
        heads = self._heads(
            "tp -n ns -- sh -c 'echo done $(date) stress-ng -c 1'"
        )
        assert heads == ["echo", "date"], (
            f"an argument tail after ')' is the host's parameter text, "
            f"not a command — it must not head a segment: {heads}"
        )

    def test_nested_and_consecutive_tails_bind_to_their_host(self):
        # The inner substitution's tail belongs to the INNER host
        # (`cat`), the outer tail to the outer host (`echo`); across
        # CONSECUTIVE argument-position substitutions the tail still
        # belongs to the one host command (the substitution opened in
        # argument position, so the tail stays its argument).
        heads = self._heads(
            "tp -n ns -- sh -c 'echo $(cat $(date) stress-ng) done'"
        )
        assert "stress-ng" not in heads, heads
        assert {"echo", "cat", "date"} <= set(heads), heads
        heads = self._heads(
            "tp -n ns -- sh -c 'echo done $(date) foo $(pgrep -x kubelet) stress-ng'"
        )
        assert "stress-ng" not in heads and "foo" not in heads, (
            f"tails around consecutive argument-position substitutions "
            f"are all host parameters: {heads}"
        )

    def test_argument_tail_context_releases_at_a_boundary(self):
        # The tail is the host's parameter only up to the next command
        # boundary: after `;`/`&&` the word is a NEW command again, and
        # after a `VAR=` host it is the post-assignment command — both
        # genuinely execute and must stay visible (the pre-fix parser
        # already kept them; the merge must not overreach).
        heads = self._heads(
            "tp -n ns -- sh -c 'echo done $(date); stress-ng -c 1'"
        )
        assert "stress-ng" in heads, heads
        heads = self._heads(
            "tp -n ns -- sh -c 'echo done $(date) && stress-ng -c 1'"
        )
        assert "stress-ng" in heads, heads
        heads = self._heads(
            "tp -n ns -- sh -c 'VAR=$(date) stress-ng -c 1'"
        )
        assert "stress-ng" in heads, (
            f"a command after a VAR= assignment host executes — it must "
            f"head its own segment: {heads}"
        )

    def test_classify_sees_no_destroy_in_argument_tail(self):
        # Downstream proof (G-13 probe): `echo done $(date) blade
        # destroy UID` — destroy is echo's parameter text; the payload
        # classifier must not read a death verb. Pre-fix: has_destroy
        # flipped True while the SAME payload without the $(date)
        # stayed False — one shell semantics, two verdicts.
        p = classify_blade_exec_payload(
            "chaosblade-tool-abc -n chaosblade -- sh -c "
            "'echo done $(date) blade destroy aa11bb22cc33dd44'"
        )
        assert not p.has_destroy, (
            "a destroy verb in an argument tail is echo's parameter "
            "text — the payload classifier must not read a death verb"
        )


class TestHeredocBodyIsText:
    """R36/G-14: a heredoc's body is STDIN TEXT, not a command stream.

    Shell-proofed: `cat > /tmp/x <<EOF` feeds the following lines to
    cat's stdin — they land IN THE FILE, nothing executes. The parser
    split them as commands and headed every line (the live probe headed
    `['cat', 'nsenter', 'EOF']`), so a restore script WRITTEN VIA the
    carrier-blessed quoted-heredoc form got killed by the escape guard
    whenever its text mentioned a banned primitive.
    """

    @staticmethod
    def _heads(v_args: str) -> list[str]:
        return [
            seg[0].rsplit("/", 1)[-1]
            for seg in exec_command_segments(v_args, chroot_delegation=False)
            if seg
        ]

    def test_body_lines_never_head_a_segment(self):
        heads = self._heads(
            "tp -n ns -- sh -c 'cat > /tmp/restore.sh <<EOF\n"
            "nsenter -t 1 -m sh\n"
            "EOF\necho done'"
        )
        assert heads == ["cat", "echo"], (
            f"heredoc body is stdin text — its lines must not head "
            f"segments: {heads}"
        )

    def test_quoted_delimiter_and_tab_strip_forms(self):
        # The carrier standard blesses `<<"EOF"` (recovery-carrier.md);
        # `<<-EOF` strips leading tabs from terminator lines.
        heads = self._heads(
            "tp -n ns -- sh -c 'cat > /tmp/x <<\"EOF\"\n"
            "stress-ng --cpu 4\n"
            "EOF\necho done'"
        )
        assert heads == ["cat", "echo"], heads
        heads = self._heads(
            "tp -n ns -- sh -c 'cat > /tmp/x <<-EOF\n"
            "stress-ng --cpu 4\n"
            "\tEOF\necho done'"
        )
        assert heads == ["cat", "echo"], heads

    def test_command_after_the_terminator_heads_its_segment(self):
        # Everything AFTER the terminator line is a command stream again.
        heads = self._heads(
            "tp -n ns -- sh -c 'cat > /tmp/x <<EOF\n"
            "body text\n"
            "EOF\nstress-ng -c 1'"
        )
        assert "stress-ng" in heads, (
            f"a command after the heredoc terminator executes — it must "
            f"head its own segment: {heads}"
        )

    def test_same_line_after_delimiter_is_still_a_command(self):
        # `cat <<EOF; cmd` — the shell parses the delimiter word, then
        # the `;` separates the NEXT command on the SAME line. Only the
        # BODY (from the next line) is text.
        heads = self._heads(
            "tp -n ns -- sh -c 'cat <<EOF; nsenter -t 1 -m sh\n"
            "body\n"
            "EOF'"
        )
        assert "nsenter" in heads, (
            f"a command after `<<EOF;` on the same line executes — it "
            f"must stay visible: {heads}"
        )

    def test_word_internal_quote_delimiter(self):
        # POSIX: quotes ANYWHERE in the delimiter word make that part
        # literal — `<<E"O"F` means the terminator is EOF. Pre-fix probe
        # (live): the parser kept the raw word 'E"O"F' as the delimiter,
        # the terminator line never matched, the body ran to EOF and the
        # command AFTER the terminator was swallowed as body text — a
        # MISS direction (a real post-heredoc command vanished from
        # every segment consumer). Shell probe: dash ran the heredoc and
        # the trailing echo fine.
        heads = self._heads(
            "tp -n ns -- sh -c 'cat > /tmp/x <<E\"O\"F\n"
            "nsenter -t 1 -m sh\n"
            "EOF\nstress-ng -c 1'"
        )
        assert heads == ["cat", "stress-ng"], (
            f"a word-internal quoted delimiter must strip to its bare "
            f"word — the command after the terminator must stay "
            f"visible: {heads}"
        )

    def test_quoted_delimiter_holds_syntax_chars(self):
        # A quoted delimiter can hold operator chars/spaces inside —
        # the quote-aware scan must not end the word at them.
        heads = self._heads(
            "tp -n ns -- sh -c 'cat > /tmp/x <<\"E OF\"\n"
            "stress-ng --cpu 4\n"
            "E OF\necho done'"
        )
        assert heads == ["cat", "echo"], (
            f"a quoted delimiter with a space must still terminate the "
            f"body: {heads}"
        )

    def test_edges_stay_failclosed(self):
        # Unterminated heredoc: everything to EOF is stdin text
        # (the shell itself refuses to run the script).
        heads = self._heads(
            "tp -n ns -- sh -c 'cat > /tmp/x <<EOF\n"
            "nsenter -t 1 -m sh'"
        )
        assert heads == ["cat"], heads
        # `<<<` herestring: the word is an argument, NOT a line-oriented
        # body — must not trigger the heredoc skip (zero regression).
        heads = self._heads(
            "tp -n ns -- sh -c 'cat <<< stress-ng --cpu 4'"
        )
        assert heads == ["cat"], (
            f"a herestring word is the command's argument — the segment "
            f"head stays cat: {heads}"
        )

