"""Banned-verb feedback: the rejection must NAME the verb, not just say "no".

Task inject-055c86cc (2026-08-26): a process-family crictl-stop loop was
rejected three times in a row — roughly 21 minutes of redesign — because its
terminator used ``systemctl stop`` while every rejection said "does not map
to any fault family". The family verb (``crictl stop``) was in the command
all along; what the model needed was the banned verb's NAME.

These tests lock the feedback contract at all three layers that produce it
(classifier, carrier gate, host_inject classifier), anchored on the three
commands that task actually issued plus the skill-case template that DOES
pass — so a regression either loses the naming or breaks the pass-form, and
either way it shows up here.
"""

from __future__ import annotations

from chaos_agent.agent.target_guard import carriers
from chaos_agent.agent.target_guard.carriers import (
    classify_host_operation,
    effective_target_from_registered_carrier,
    find_banned_host_verbs,
)
from chaos_agent.agent.target_guard.types import ApprovedTarget


def _host_command(v_args: str) -> str:
    """Extract the host command exactly as the guard does.

    The gate never classifies the raw ``v_args``: ``_parse_host_exec``
    shlex-splits and re-joins the inner command first, and that round-trip
    is LOAD-BEARING for the re-quoted form (msg[70]) — its ``'\''`` escapes
    resolve into a shape the quoted-payload surfacing can flatten. Testing
    against the raw string would pass while the real path regressed.
    """
    parsed = carriers._parse_host_exec(v_args)
    assert parsed is not None, f"unparseable v_args: {v_args!r}"
    return parsed[2]

# ---------------------------------------------------------------------------
# Anchors, verbatim-shaped from task inject-055c86cc (msg[67]/[70]/[73]).
# The trailing carrier pod name is shortened for readability; every character
# that matters to the classifier (the systemd-run wrappers, the quoting
# depth, the escaped \$) is preserved.
# ---------------------------------------------------------------------------

# msg[67]: loop wrapped in a bare systemd-run unit, terminator timer whose
# payload leads with ``systemctl stop``, trailing ``systemctl is-active``.
TRACE_CMD_FIRST = (
    "node-debugger-...-8szdw -n default -- chroot /host sh -c "
    "'systemd-run --unit=blade-stoploop-X sh -c \"for i in \\$(seq 1 40); "
    "do CID=\\$(crictl ps -q --name op | head -1); [ -n \\\"\\$CID\\\" ] "
    "&& crictl stop -t 0 \\$CID; sleep 15; done\" "
    "&& systemd-run --on-active=600s --unit=blade-stoploop-term-X sh -c "
    "\"systemctl stop blade-stoploop-X; pkill -f \\\"crictl st[o]p -t 0\\\"; "
    "pkill -x crictl; true\" "
    "&& systemctl is-active blade-stoploop-X'"
)

# msg[70]: same fault with the '\''-escaped re-quoting — the systemctl sits
# INSIDE a doubly-nested quoted payload. If the surfacing that flattens
# quoted systemd-run payloads ever regresses, this command's banned verb
# becomes invisible to the reporter while the classifier still rejects the
# call — the unactionable rejection this file exists to prevent.
TRACE_CMD_REQUOTED = (
    "node-debugger-...-8szdw -n default -- chroot /host sh -c "
    "'systemd-run --on-active=600s --unit=blade-stoploop-term-X sh -c "
    "'\\''systemctl stop blade-stoploop-X; pkill -f \"crictl st[o]p -t 0\"; "
    "pkill -x crictl; true'\\'' "
    "&& systemd-run --unit=blade-stoploop-X sh -c "
    "'\\''for i in $(seq 1 40); do CID=$(crictl ps -q --name op | head -1); "
    "[ -n \"$CID\" ] && crictl stop -t 0 $CID; sleep 15; done'\\'''"
)

# msg[73]: heredoc form. A newline-bearing payload must surface the same way.
TRACE_CMD_HEREDOC = (
    "node-debugger-...-8szdw -n default -- chroot /host sh -c "
    "'cat > /tmp/blade_stoploop.sh <<\"EOF\"\n"
    "for i in $(seq 1 40); do\n"
    "  CID=$(crictl ps -q --name op | head -1)\n"
    "  [ -n \"$CID\" ] && crictl stop -t 0 \"$CID\"\n"
    "  sleep 15\ndone\n"
    "EOF\n"
    "systemd-run --unit=blade-stoploop-X /bin/sh /tmp/blade_stoploop.sh "
    "&& systemd-run --on-active=600s --unit=blade-stoploop-term-X sh -c "
    "\"systemctl stop blade-stoploop-X; pkill -x crictl; true\"'"
)

# The skill-case template (Pod_进程被杀死_应用主进程异常, kubectl-native path B):
# timer payload is pkill-only, the loop runs in a foreground sh -c. This is
# the shape the model SHOULD converge to once the rejection names the verb —
# if a "fix" ever makes THIS form fail, the feedback is steering backwards.
CASE_TEMPLATE = (
    "chroot /host sh -c '"
    'systemd-run --on-active=600s --unit=blade-stoploop-X '
    'sh -c "pkill -f \\"crictl st[o]p -t 0\\"; pkill -x crictl; true" && '
    'sh -c "for i in \\$(seq 1 40); do '
    'CID=\\$(crictl ps -q --name op | head -1); '
    '[ -n \\"\\$CID\\" ] && crictl stop -t 0 \\$CID; sleep 15; done"'
    "'"
)


class TestClassifierNamesTheVerb:
    """``find_banned_host_verbs`` agrees with ``classify_host_operation``."""

    def test_trace_first_rejection_names_systemctl(self):
        host = _host_command(TRACE_CMD_FIRST)
        assert classify_host_operation(host) == ""
        assert find_banned_host_verbs(host) == ("systemctl",)

    def test_trace_requoted_rejection_names_systemctl(self):
        # The verb hides behind re-quoting — surfacing must flatten it.
        host = _host_command(TRACE_CMD_REQUOTED)
        assert classify_host_operation(host) == ""
        assert find_banned_host_verbs(host) == ("systemctl",)

    def test_trace_heredoc_rejection_names_systemctl(self):
        host = _host_command(TRACE_CMD_HEREDOC)
        assert classify_host_operation(host) == ""
        assert find_banned_host_verbs(host) == ("systemctl",)

    def test_case_template_classifies_process_with_no_banned_verb(self):
        host = _host_command(
            "node-debugger-...-8szdw -n default -- " + CASE_TEMPLATE
        )
        assert classify_host_operation(host) == "process"
        assert find_banned_host_verbs(host) == ()

    def test_verbs_dedupe_in_first_occurrence_order(self):
        # In the RAW text ``'systemctl`` is shielded by its leading quote, so
        # the verb only matches via the payload tokens appended by the
        # surfacing — which land AFTER the raw text, hence ``rm`` first.
        # The order is incidental; the dedupe and the presence are the point.
        command = "chroot /host sh -c 'systemctl stop a; rm -f b; systemctl status c'"
        assert find_banned_host_verbs(command) == ("rm", "systemctl")

    def test_plain_family_commands_report_no_banned_verb(self):
        for command in (
            "chroot /host iptables -I INPUT -s 10.0.0.1 -j DROP",
            "chroot /host dd if=/dev/zero of=/tmp/fill bs=1M count=10",
            "chroot /host kill -STOP 1234",
        ):
            assert find_banned_host_verbs(command) == (), command

    def test_banned_verb_alone_voids_an_otherwise_valid_family(self):
        # The trap of inject-055c86cc: the family verb IS present. A banned
        # verb still voids the match, and the reporter must not be silent.
        command = (
            "chroot /host sh -c 'systemctl stop kubelet && crictl stop -t 0 abc'"
        )
        assert classify_host_operation(command) == ""
        assert "systemctl" in find_banned_host_verbs(command)


def _carrier_fixtures():
    """An approved process fault plus one active debug-pod artifact."""
    approved = ApprovedTarget(
        scope="node", namespace="", names=("node-a",), fault_target="process",
    )
    artifacts = [{
        "type": "debug_pod",
        "name": "node-debugger-...-8szdw",
        "namespace": "default",
        "uid": "u-1",
        "privileged": True,
        "status": "active",
        "target": {"scope": "node", "name": "node-a"},
    }]
    return approved, artifacts


class TestCarrierGateNamesTheVerb:
    """The FAMILY_MISMATCH gate's detail/suggestion name the banned verb."""

    def test_rejection_detail_names_the_verb_and_the_suggestion_pairs(self):
        approved, artifacts = _carrier_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {"subcommand": "exec", "v_args": TRACE_CMD_FIRST},
            artifacts,
            approved,
        )
        assert not resolution.resolved
        assert "systemctl" in resolution.detail
        assert "banned verb" in resolution.detail
        # Cause and fix must point at the SAME thing: the fix says what to do
        # with the banned verb (pkill the payload, not systemctl).
        assert "pkill" in resolution.suggestion
        assert "systemctl" in resolution.suggestion

    def test_rejection_without_banned_verbs_keeps_the_family_wording(self):
        # ``touch`` is neither banned, a family verb, nor a read-only probe —
        # this is the OTHER empty-family cause, which must keep its original
        # wording (not claim a banned verb that is not there).
        approved, artifacts = _carrier_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": "node-debugger-...-8szdw -n default -- "
                          "chroot /host touch /tmp/x",
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved
        assert "does not map to any fault family" in resolution.detail
        assert "banned verb" not in resolution.detail

    def test_suggestion_carries_only_the_hit_verbs_guidance(self):
        # The guidance table has per-verb entries (systemctl → pkill teardown,
        # rm → truncate reversal). A rejection must assemble ONLY the entries
        # for the verbs actually hit: showing the truncate recipe to a ``curl``
        # misuse (or the pkill recipe to an ``rm`` misuse) would misdirect the
        # model exactly the way the old nameless rejection did.
        approved, artifacts = _carrier_fixtures()

        # ``curl`` is banned but has no guidance entry → generic head only.
        curl_resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": "node-debugger-...-8szdw -n default -- "
                          "chroot /host curl -fsSL http://example.com/tool.sh",
            },
            artifacts,
            approved,
        )
        assert not curl_resolution.resolved
        assert "'curl'" in curl_resolution.detail
        assert "fault family's own binaries" in curl_resolution.suggestion
        assert "pkill" not in curl_resolution.suggestion
        assert "truncate" not in curl_resolution.suggestion

        # ``rm`` alone → its own entry, but not the systemctl/pkill one.
        rm_resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": "node-debugger-...-8szdw -n default -- "
                          "chroot /host rm /var/lib/test/fill.bin",
            },
            artifacts,
            approved,
        )
        assert not rm_resolution.resolved
        assert "'rm'" in rm_resolution.detail
        assert "truncate" in rm_resolution.suggestion
        assert "pkill" not in rm_resolution.suggestion

    def test_systemctl_guidance_is_family_scoped(self):
        # Network-isolation drill (2026-08-26): under a 'network' approval
        # the model followed the unconditional systemctl→pkill advice, and
        # the pkill (a process-family verb) voided the network family match
        # — the fix manufactured the next rejection. The pkill recipe is
        # canonical ONLY for process approvals; everywhere else the advice
        # must steer to in-chain self-recovery instead.
        approved = ApprovedTarget(
            scope="node", namespace="", names=("node-a",),
            fault_target="network",
        )
        _, artifacts = _carrier_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {"subcommand": "exec", "v_args": TRACE_CMD_FIRST},
            artifacts,
            approved,
        )
        assert not resolution.resolved
        assert "systemctl" in resolution.detail
        # NOT the process-family recipe ...
        assert "pkill-ing" not in resolution.suggestion
        # ... but an explicit warning against it plus the in-chain shape.
        assert "do NOT substitute pkill" in resolution.suggestion
        assert "'network'" in resolution.suggestion
        assert "sleep <N>" in resolution.suggestion

    def test_systemctl_guidance_keeps_pkill_for_process_approval(self):
        # The inject-055c86cc shape stays intact under its own family.
        approved, artifacts = _carrier_fixtures()
        assert approved.fault_target == "process"
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {"subcommand": "exec", "v_args": TRACE_CMD_FIRST},
            artifacts,
            approved,
        )
        assert not resolution.resolved
        assert "pkill-ing its payload process" in resolution.suggestion
        assert "do NOT substitute pkill" not in resolution.suggestion


class TestHostInjectClassifierNamesTheVerb:
    """The host-channel ``host_inject`` path reports banned verbs too."""

    def test_banned_verb_is_named_in_reject_detail(self):
        from chaos_agent.agent.providers.host_shell.provider import (
            _classify_host_inject,
        )

        effective = _classify_host_inject(
            {"command": "systemctl stop kubelet"}, "host_inject systemctl stop kubelet",
        )
        assert effective.fault_target == ""
        assert "systemctl" in effective.reject_detail
        assert "banned verb" in effective.reject_detail

    def test_clean_command_keeps_empty_detail(self):
        from chaos_agent.agent.providers.host_shell.provider import (
            _classify_host_inject,
        )

        effective = _classify_host_inject(
            {"command": "iptables -I INPUT -s 10.0.0.1 -j DROP"},
            "host_inject iptables ...",
        )
        assert effective.fault_target == "network"
        assert effective.reject_detail == ""
