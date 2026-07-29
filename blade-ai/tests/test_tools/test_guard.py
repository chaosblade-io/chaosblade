"""Tests for ToolGuard command execution security."""

import json
import logging

import pytest

from chaos_agent.tools.guard import CommandResult, ToolGuard
from chaos_agent.tools.guard_feedback import ViolatedConstraint


class TestToolGuardCheck:
    """Test ToolGuard.check() command validation."""

    def setup_method(self):
        self.guard = ToolGuard()

    # ── Allowed commands ──────────────────────────────────────────────

    @pytest.mark.parametrize("cmd_first", ["blade", "df", "ping", "sleep"])
    def test_allowed_commands_pass(self, cmd_first):
        allowed, reason = self.guard.check([cmd_first, "arg1"])
        assert allowed is True
        assert reason == "OK"

    def test_kubectl_allowed_with_valid_subcommand(self):
        allowed, reason = self.guard.check(["kubectl", "get", "pods"])
        assert allowed is True
        assert reason == "OK"

    def test_blade_create_allowed(self):
        allowed, _ = self.guard.check(["blade", "create", "pod", "network", "delay"])
        assert allowed is True

    def test_kubectl_get_allowed(self):
        allowed, _ = self.guard.check(["kubectl", "get", "pods", "-n", "default"])
        assert allowed is True

    # ── Forbidden commands ─────────────────────────────────────────────
    #
    # ``nc`` / ``fuser`` / ``strace`` left this list when their drill forms were
    # admitted (a fallback plan is the only path on a cluster without
    # ChaosBlade). Their DANGEROUS forms are still refused, by a per-binary guard
    # instead of the whitelist — see test_guard_single_resource_binaries.py.

    @pytest.mark.parametrize(
        "cmd_first",
        ["rm", "curl", "python", "python3", "bash", "sh", "perl",
         "wget", "pkill", "killall", "socat", "bpftrace"],
    )
    def test_forbidden_commands_rejected(self, cmd_first):
        allowed, reason = self.guard.check([cmd_first, "arg1"])
        assert allowed is False
        assert "not allowed" in reason

    def test_empty_command_rejected(self):
        allowed, reason = self.guard.check([])
        assert allowed is False
        assert "Empty" in reason

    # ── kubectl subcommand whitelist ───────────────────────────────────

    @pytest.mark.parametrize("subcmd", ["get", "describe", "delete", "exec", "logs", "top", "patch", "scale", "debug", "wait", "cordon", "uncordon", "taint"])
    def test_kubectl_allowed_subcommands(self, subcmd):
        allowed, _ = self.guard.check(["kubectl", subcmd, "pods"])
        assert allowed is True

    @pytest.mark.parametrize("subcmd", ["edit", "replace"])
    def test_kubectl_forbidden_subcommands(self, subcmd):
        allowed, reason = self.guard.check(["kubectl", subcmd, "something"])
        assert allowed is False
        assert "subcommand not allowed" in reason

    @pytest.mark.parametrize("cmd", [
        ["kubectl", "label", "node", "n1", "chaos-target=ksm", "--overwrite"],
        ["kubectl", "label", "node", "n1", "chaos-target-"],
        ["kubectl", "annotate", "node", "n1", "k=v", "--overwrite"],
    ])
    def test_kubectl_metadata_write_verbs_allowed(self, cmd):
        """``label``/``annotate`` are strict subsets of ``patch``.

        Refusing them while admitting ``patch -p
        '{"metadata":{"labels":...}}'`` narrowed nothing — it only forced the
        model into a rejected call plus a rewrite. Worse, the provider declares
        ``label`` an injection carrier and the step self-check expects to
        observe it, so a drill step spelled ``kubectl label node ...`` could
        never be satisfied. See test_kubectl_verb_consistency.
        """
        allowed, reason = self.guard.check(cmd)
        assert allowed is True, reason

    @pytest.mark.parametrize("cmd", [
        ["kubectl", "drain", "n1", "--ignore-daemonsets", "--grace-period=30"],
        # The exact command from the Node_维护_节点排空Drain skill case.
        ["kubectl", "drain", "n1", "--ignore-daemonsets",
         "--delete-emptydir-data", "--grace-period=30", "--timeout=120s"],
    ])
    def test_kubectl_drain_allowed(self, cmd):
        allowed, reason = self.guard.check(cmd)
        assert allowed is True, reason

    @pytest.mark.parametrize("flag", [
        "--delete-emptydir-data",
        "--delete-emptydir-data=true",
        "--delete-local-data",     # deprecated alias of the same behaviour
    ])
    def test_kubectl_drain_emptydir_flag_allowed(self, flag):
        """An emptyDir dies WITH its pod — losing it is not extra destruction.

        An early version of this guard banned these, which broke the
        ``Node_维护_节点排空Drain`` skill case outright: enough pods mount an
        emptyDir that drain refuses to evict without the flag, so banning it
        made drain unusable on any real cluster. It also protected nothing —
        ``kubectl delete pod`` has always been whitelisted and discards exactly
        the same data with no flag at all.
        """
        allowed, reason = self.guard.check(["kubectl", "drain", "n1", flag])
        assert allowed is True, reason

    @pytest.mark.parametrize("flag", [
        "--disable-eviction",
        "--force",
        "--force=true",
    ])
    def test_kubectl_drain_unrecoverable_flags_rejected(self, flag):
        """Draining is recoverable; these flags make it not.

        - ``--disable-eviction`` deletes pods around the eviction API,
          overriding PodDisruptionBudgets — a drill that disables the
          availability guarantee it exists to exercise proves nothing.
        - ``--force`` deletes pods with NO owning controller, which nothing
          recreates: ``uncordon`` cannot bring them back, so the POD ITSELF is
          gone (not just its data). It is also a BATCH implicit delete the
          target guard cannot see into (it only observes ``node`` scope), unlike
          an explicit ``delete pod <name>``. Without it kubectl fails atomically
          and evicts nothing, which is the safe outcome.
        """
        allowed, reason = self.guard.check(["kubectl", "drain", "n1", flag])
        assert allowed is False
        assert "drain" in reason
        assert flag.split("=")[0] in reason

    def test_drain_rejection_names_the_flag_that_fired(self):
        """Each flag gets ITS OWN cause, in the field the contract reserves.

        A single OR-worded reason covering both flags ("deletes unmanaged pods
        OR bypasses eviction") leaves the model guessing which rule it hit, and
        folding the workaround into ``reason`` bypasses ``compliant_form``.
        Mentioning a flag the call did NOT use is pure noise against
        ``render_for_llm``'s compactness contract.
        """
        force = self.guard.evaluate(["kubectl", "drain", "n1", "--force"])
        evict = self.guard.evaluate(["kubectl", "drain", "n1", "--disable-eviction"])

        assert force.offending == "--force"
        assert evict.offending == "--disable-eviction"
        # Each cause is specific to its own flag, not shared boilerplate.
        assert "no owning controller" in force.reason.lower()
        assert "poddisruptionbudget" in evict.reason.lower()
        assert "poddisruptionbudget" not in force.reason.lower()
        assert "owning controller" not in evict.reason.lower()
        # The workaround lives in compliant_form, not in reason.
        for fb in (force, evict):
            assert fb.compliant_form
            assert "drop the flag" in fb.compliant_form.lower()
            assert "drop the flag" not in fb.reason.lower()
            # An allowed flag must never be discussed in a rejection.
            assert "emptydir" not in fb.reason.lower()

    def test_drain_flag_value_form_reports_the_bare_flag(self):
        fb = self.guard.evaluate(["kubectl", "drain", "n1", "--force=true"])
        assert fb.offending == "--force"

    @pytest.mark.parametrize("cmd", [
        # ``delete --force`` skips graceful termination on ONE explicitly named
        # pod — a different meaning from drain's "delete unmanaged pods", and
        # the target guard sees the exact pod. The knowledge base recommends it.
        ["kubectl", "delete", "pod", "p1", "-n", "ns", "--force", "--grace-period=0"],
        ["kubectl", "delete", "pod", "p1", "-n", "ns", "--delete-emptydir-data"],
    ])
    def test_drain_flag_ban_is_scoped_to_drain(self, cmd):
        """The drain flag ban must not leak onto other subcommands."""
        allowed, reason = self.guard.check(cmd)
        assert allowed is True, reason

    def test_kubectl_no_subcommand(self):
        """kubectl with no subcommand (just 'kubectl') is allowed since len(cmd)<=1."""
        allowed, _ = self.guard.check(["kubectl"])
        assert allowed is True

    # ── rejection completeness: the guard must not withhold what it knows ──

    def test_kubectl_subcommand_rejection_lists_allowed(self):
        """A rejection must state the allow-list the guard itself owns.

        task-c758cdbd is the cost of not doing so: the model met
        ``kubectl subcommand not allowed: label``, went to the kubectl tool
        docstring to work out what WAS permitted, and read a list that was
        itself wrong (it advertised edit/replace, which the guard refuses, and
        omitted label). Withholding a set the guard holds authoritatively turns
        a one-message correction into a guessing loop.
        """
        fb = self.guard.evaluate(["kubectl", "edit", "deployment", "x"])
        assert fb.offending == "edit"
        for sub in self.guard.kubectl_subcommands:
            assert sub in fb.compliant_form, sub
        # The alternative belongs in compliant_form, not smuggled into reason.
        assert "Allowed subcommands" not in fb.reason

    def test_custom_kubectl_whitelist_is_reported_truthfully(self):
        """The list is read from the LIVE instance, never a hardcoded copy."""
        guard = ToolGuard(kubectl_subcommands={"get", "describe"})
        fb = guard.evaluate(["kubectl", "label", "node", "n1", "k=v"])
        assert "get" in fb.compliant_form
        assert "describe" in fb.compliant_form
        assert "patch" not in fb.compliant_form  # not the class default

    def test_binary_rejection_lists_allowed_and_points_at_host_read(self):
        fb = self.guard.evaluate(["curl", "http://x"])
        assert fb.offending == "curl"
        for binary in ("kubectl", "blade", "iptables"):
            assert binary in fb.compliant_form
        # Read-only diagnostics are deliberately absent from the BINARY
        # whitelist; the rejection must say where they DO live, otherwise the
        # model reads "curl not allowed" as "no HTTP probe is possible at all".
        assert "host_read" in fb.compliant_form

    def test_systemctl_rejection_lists_allowed_verbs(self):
        fb = self.guard.evaluate(["systemctl", "reboot"])
        assert fb.offending == "reboot"
        for verb in self.guard.systemctl_subcommands:
            assert verb in fb.compliant_form, verb

    @pytest.mark.parametrize("cmd,expected", [
        (["kill", "-9", "-123", "456"], "-123"),
        (["kill", "-9", "nginx"], "nginx"),
        (["kill", "-9", "1"], "1"),
        (["kill", "-1"], "-1"),
        (["systemctl", "reboot"], "reboot"),
        (["chmod", "-R", "777", "/etc"], "-R"),
        (["kubectl", "edit", "x"], "edit"),
        (["curl", "http://x"], "curl"),
        (["dd", "if=/dev/zero", "of=/dev/sda"], "of=/dev/sda"),
    ])
    def test_rejection_echoes_the_offending_token(self, cmd, expected):
        """``offending`` is the structured copy of what tripped the rule.

        ``render_for_llm`` does not read it (the prose already names the token)
        and nothing in ``src`` consumes it today — the ``GuardFeedback`` contract
        lists it as load-bearing so a machine reader (audit / event detail) has
        a field to key on instead of parsing English, and so tests can assert
        WHICH argument fired rather than substring-matching the message. Two
        branches, systemctl and kill, used to drop it entirely, leaving the
        cause available only as prose.
        """
        fb = self.guard.evaluate(cmd)
        assert fb.allowed is False
        assert fb.offending == expected

    def test_hard_floor_offers_no_false_way_forward(self):
        """A destructive floor must not carry a "reshape and retry" hint.

        Suggesting an alternative for a path that has none tells the model to
        keep trying. The magnitude cap below genuinely does have one, so the
        two must not share wording.
        """
        floor = self.guard.evaluate(["dd", "if=/dev/zero", "of=/dev/sda"])
        assert floor.is_hard_floor is True
        assert floor.compliant_form == ""

        cap = self.guard.evaluate(
            ["dd", "if=/dev/zero", "of=/tmp/f", "count=99999999"]
        )
        assert cap.is_hard_floor is False
        assert "retry" in cap.compliant_form.lower()

    # Every rejection shape this guard can produce. Kept as one list so the
    # invariants below hold across ALL paths — a per-case spot check is what let
    # the kill branch ship with a hard_floor flag contradicting its own
    # compliant_form.
    _EVERY_REJECTION = [
        ["curl", "http://x"],                                  # unknown binary
        ["kubectl", "edit", "x"],                              # kubectl sub
        ["kubectl", "config", "use-context", "other"],          # kubectl config
        ["kubectl", "drain", "n1", "--force"],                  # drain flag
        ["systemctl", "reboot"],                               # systemctl verb
        ["systemctl", "--version"],                            # systemctl noverb
        ["kill", "-1"],                                        # kill broadcast
        ["kill", "-9", "1"],                                   # kill init
        ["kill", "-9", "-123", "456"],                         # kill pgid
        ["kill", "-9", "nginx"],                               # kill non-numeric
        ["kill", "-9"],                                        # kill no target
        ["chmod", "-R", "777", "/etc"],                        # chmod recursive
        ["kubectl", "get", "pods", "|", "wc"],                 # benign pipe
        ["blade", "create", ";", "rm", "file"],                # solo metachar
        ["dd", "if=/dev/zero", "of=/dev/sda"],                 # destructive floor
        ["dd", "if=/dev/zero", "of=/tmp/f", "count=99999999"],  # magnitude cap
        [],                                                     # empty command
    ]

    @pytest.mark.parametrize("cmd", _EVERY_REJECTION)
    def test_hard_floor_never_implies_retrying_the_same_command(self, cmd):
        """A dead-end verdict must not read as "tweak it and retry".

        ``is_hard_floor`` describes the BOUNDARY, not the intent: the screener
        renders it as "this is a boundary the guard will not relax; operate
        within the approved target or abort". So a hard floor MAY still point at
        a different route — ``curl`` is refused as a binary while the HTTP probe
        it wanted lives in ``host_read`` — but it must never suggest that
        reshaping the SAME command would pass. Conversely, anything fixable by
        editing the command (drop a flag, name another PID) is a form issue and
        must NOT be flagged a floor: that is the harmful direction, because it
        makes the model abandon a viable path.
        """
        fb = self.guard.evaluate(cmd)
        assert fb.allowed is False, cmd
        if fb.is_hard_floor:
            lowered = fb.compliant_form.lower()
            for retry_word in ("retry", "reshape", "drop the flag", "instead of forcing"):
                assert retry_word not in lowered, (
                    f"{cmd} is flagged a boundary that will not relax, yet its "
                    f"compliant_form implies editing the same command works: "
                    f"{fb.compliant_form!r}"
                )

    @pytest.mark.parametrize("cmd", _EVERY_REJECTION)
    def test_every_rejection_names_a_specific_cause(self, cmd):
        """No rejection may fall back to a generic label.

        The contract's whole point: the model must learn WHICH rule fired, not
        that "something was dangerous".
        """
        fb = self.guard.evaluate(cmd)
        assert fb.allowed is False, cmd
        assert fb.reason and fb.reason != "OK", cmd
        assert fb.constraint is not ViolatedConstraint.NONE, cmd
        # A reshapeable rejection must say what the compliant form is; only a
        # true dead-end is allowed to stay silent about alternatives. (The empty
        # command has no form to suggest.)
        if not fb.is_hard_floor and cmd:
            assert fb.compliant_form, f"{cmd} is reshapeable but offers no form"

    def test_kubectl_with_kubeconfig_flag_passes(self):
        allowed, _ = self.guard.check([
            "kubectl", "--kubeconfig", "/my/kubeconfig", "get", "pods", "-n", "default",
        ])
        assert allowed is True

    def test_kubectl_with_context_flag_passes(self):
        allowed, _ = self.guard.check([
            "kubectl", "--context", "my-ctx", "get", "nodes",
        ])
        assert allowed is True

    def test_kubectl_with_kubeconfig_forbidden_subcommand(self):
        """Even with --kubeconfig, forbidden subcommands are still rejected."""
        allowed, reason = self.guard.check([
            "kubectl", "--kubeconfig", "/my/kubeconfig", "edit", "deployment", "my-app",
        ])
        assert allowed is False
        assert "subcommand not allowed" in reason

    def test_kubectl_with_only_flags_no_subcommand(self):
        """kubectl with only global flags and no subcommand should be allowed."""
        allowed, _ = self.guard.check(["kubectl", "--kubeconfig", "/my/kubeconfig"])
        assert allowed is True

    def test_kubectl_config_view_is_allowed(self):
        allowed, _ = self.guard.check([
            "kubectl", "config", "view", "--minify", "-o", "jsonpath={..namespace}",
        ])
        assert allowed is True

    @pytest.mark.parametrize("action", ["set", "use-context", "set-context", "delete-context"])
    def test_kubectl_config_mutations_are_rejected(self, action):
        allowed, reason = self.guard.check(["kubectl", "config", action, "value"])
        assert allowed is False
        assert "read-only" in reason

    def test_wiz_wrapped_kubectl_config_view_is_allowed(self):
        allowed, _ = self.guard.check([
            "wiz", "task", "exec",
            "--command", "kubectl config view --minify -o 'jsonpath={..namespace}'",
            "--cluster-uuid", "cluster", "--profile", "profile",
        ])
        assert allowed is True

    def test_wiz_wrapped_kubectl_config_write_allowed_by_guard(self):
        """Guard no longer unwraps wiz --command for subcommand whitelist.

        In the transport-layer architecture, ``execute_via_transport``
        checks the RAW semantic command (e.g. ``kubectl config
        use-context``) BEFORE wrapping it into ``wiz task exec``.
        The guard therefore never needs to inspect the inner command
        of a wiz-wrapped command for subcommand whitelist — the raw
        command is already rejected at step 1.

        Defense-in-depth: ``_parse_wiz`` in guard_parser still
        unwraps the inner command for dangerous-pattern checks
        (pipe, semicolon, etc.), but the kubectl subcommand
        whitelist is intentionally NOT re-applied to avoid
        redundancy with the pre-wrap check.
        """
        allowed, _ = self.guard.check([
            "wiz", "task", "exec",
            "--command", "kubectl config use-context other",
            "--cluster-uuid", "cluster", "--profile", "profile",
        ])
        assert allowed is True

    # ── Parameter blacklist patterns ───────────────────────────────────

    def test_rm_rf_blocked(self):
        allowed, reason = self.guard.check(["blade", "create", "rm -rf /"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_pipe_bash_blocked(self):
        allowed, reason = self.guard.check(["kubectl", "get", "pods", "| bash"])
        assert allowed is False

    def test_pipe_sh_blocked(self):
        allowed, reason = self.guard.check(["kubectl", "logs", "pod", "| sh"])
        assert allowed is False

    def test_redirect_dev_blocked(self):
        allowed, reason = self.guard.check(["blade", "create", ">", "/dev/null"])
        assert allowed is False

    def test_command_substitution_dollar_blocked(self):
        allowed, reason = self.guard.check(["blade", "$(", "whoami", ")"])
        assert allowed is False

    def test_backtick_blocked(self):
        allowed, reason = self.guard.check(["blade", "`whoami`"])
        assert allowed is False

    def test_semicolon_rm_blocked(self):
        allowed, reason = self.guard.check(["blade", "create", ";", "rm", "file"])
        assert allowed is False

    def test_dd_write_raw_block_device_blocked(self):
        """dd writing to raw block devices (sd) must be blocked."""
        allowed, reason = self.guard.check([
            "dd", "if=/dev/zero", "of=/dev/sda", "bs=1M", "count=100",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_dd_write_nvme_block_device_blocked(self):
        """dd writing to NVMe block devices must be blocked."""
        allowed, reason = self.guard.check([
            "dd", "of=/dev/nvme0n1", "bs=1M",
        ])
        assert allowed is False

    def test_fio_write_raw_block_device_blocked(self):
        """fio writing to raw block devices must be blocked."""
        allowed, reason = self.guard.check([
            "fio", "--filename=/dev/sda", "--rw=write",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    @pytest.mark.parametrize("device", [
        "/dev/mapper/vg--root",  # LVM logical volume (common server root)
        "/dev/dm-0",             # device-mapper node
        "/dev/md0",              # software RAID
        "/dev/mmcblk0",          # eMMC / embedded flash
        "/dev/loop0",            # loopback device
        "/dev/dasda",            # mainframe DASD
        "/dev/nbd0",             # network block device
        "/dev/rbd0",             # Ceph RBD — common as a k8s PV backend
    ])
    def test_dd_write_managed_block_device_blocked(self, device):
        """dd writing to LVM/RAID/eMMC/loop/DASD must be blocked too.

        Regression guard for the disk-destruction bypass: the old regex only
        matched bare disks (sd/nvme/...), leaving /dev/mapper (LVM) exposed.
        """
        allowed, reason = self.guard.check([
            "dd", "if=/dev/zero", f"of={device}", "bs=1M", "count=100",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    @pytest.mark.parametrize("device", [
        "/dev/mapper/vg--root",
        "/dev/dm-0",
        "/dev/md0",
        "/dev/mmcblk0",
        "/dev/nbd0",
        "/dev/rbd0",
    ])
    def test_fio_write_managed_block_device_blocked(self, device):
        """fio writing to LVM/RAID/eMMC must be blocked too."""
        allowed, reason = self.guard.check([
            "fio", f"--filename={device}", "--rw=write",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_dd_write_normal_file_allowed(self):
        """dd writing to regular files is allowed (legitimate disk-fill use)."""
        allowed, _ = self.guard.check([
            "dd", "if=/dev/zero", "of=/tmp/fill", "bs=1M", "count=100",
        ])
        assert allowed is True

    def test_dd_read_from_block_device_allowed(self):
        """dd reading from block devices (if=) is allowed — only writes are blocked."""
        allowed, _ = self.guard.check([
            "dd", "if=/dev/sda", "of=/tmp/backup", "bs=1M", "count=10",
        ])
        assert allowed is True

    def test_dd_excessive_count_blocked(self):
        """dd with unreasonably large count (7+ digits) must be blocked."""
        allowed, reason = self.guard.check([
            "dd", "if=/dev/zero", "of=/tmp/fill", "bs=1M", "count=9999999",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_dd_normal_count_allowed(self):
        """dd with reasonable count (6 digits) is allowed."""
        allowed, _ = self.guard.check([
            "dd", "if=/dev/zero", "of=/tmp/fill", "bs=1M", "count=100000",
        ])
        assert allowed is True

    def test_fio_excessive_runtime_blocked(self):
        """fio with unreasonably large --runtime (7+ digits) must be blocked."""
        allowed, reason = self.guard.check([
            "fio", "--filename=/tmp/test", "--runtime=9999999",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    # ── Normal commands not triggering blacklist ───────────────────────

    def test_normal_blade_create_passes(self):
        allowed, _ = self.guard.check([
            "blade", "create", "pod", "network", "delay",
            "--time", "3000", "--interface", "eth0",
            "--names", "my-pod", "--namespace", "default",
        ])
        assert allowed is True

    def test_normal_kubectl_get_passes(self):
        allowed, _ = self.guard.check([
            "kubectl", "get", "pods", "-n", "default", "-o", "json",
        ])
        assert allowed is True

    # ── kubectl patch -p payload exclusion ─────────────────────────────

    def test_kubectl_patch_json_payload_with_dollar_paren_allowed(self):
        """kubectl patch -p value contains $( but it's JSON data, not shell injection."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "pvc", "my-pvc", "-n", "default",
            "-p", '{"spec":{"storageClassName":"$(whoami)"}}',
        ])
        assert allowed is True

    def test_kubectl_patch_json_payload_with_backticks_allowed(self):
        """kubectl patch -p value contains backticks but it's JSON data."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "deployment", "my-deploy", "-n", "default",
            "-p", '{"spec":{"template":{"`unused`":"value"}}}',
        ])
        assert allowed is True

    def test_kubectl_patch_equals_syntax_payload_excluded(self):
        """kubectl patch -p=VALUE syntax: payload value is excluded from check."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "pvc", "my-pvc", "-n", "default",
            "-p={'spec':{'storageClassName':'$(dangerous)'}}",
        ])
        assert allowed is True

    def test_kubectl_patch_long_flag_payload_excluded(self):
        """kubectl patch --patch=VALUE syntax: payload value is excluded."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "pvc", "my-pvc", "-n", "default",
            "--patch={'spec':{'storageClassName':'$(dangerous)'}}",
        ])
        assert allowed is True

    def test_kubectl_patch_dangerous_in_host_part_still_blocked(self):
        """Dangerous patterns outside -p value (in host part) are still blocked."""
        allowed, reason = self.guard.check([
            "kubectl", "patch", "pvc", "my-pvc", "-n", "default",
            "-p", '{"spec":{}}', "| bash",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_kubectl_patch_normal_payload_passes(self):
        """Normal kubectl patch with safe JSON payload passes."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "deployment", "my-deploy", "-n", "default",
            "-p", '{"spec":{"replicas":0}}',
        ])
        assert allowed is True

    # ── kubectl exec -- container command exclusion ─────────────────────

    def test_kubectl_exec_dangerous_in_container_allowed(self):
        """Dangerous patterns after -- (container command) are allowed."""
        allowed, _ = self.guard.check([
            "kubectl", "exec", "my-pod", "-n", "default", "--",
            "blade", "create", "k8s", "pod-cpu", "fullload",
        ])
        assert allowed is True

    def test_kubectl_exec_dangerous_before_separator_blocked(self):
        """Dangerous patterns before -- (host part) are still blocked."""
        allowed, reason = self.guard.check([
            "kubectl", "exec", "| bash", "--", "echo", "hi",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_kubectl_exec_solo_pipe_in_container_allowed(self):
        """Regression: solo ``|`` after ``--`` for exec must be allowed.

        Real LLM output (task-f8320b6ff844, msg #85): wanted to verify
        the chaosblade child process inside the chaosblade-tool DaemonSet
        with ``kubectl exec ... -- ps aux | grep mem``. Pre-fix the
        bare ``|`` token triggered SUSPICIOUS_SOLO_TOKENS and blocked the
        verification path. Post-fix the ``|`` lives in container_command
        which is exempt from the solo-token check.

        Note: under exec-form (shell=False) the ``|`` is forwarded as a
        literal argv to the container's ``ps``, not a host pipeline —
        no injection surface on the host. Real pipe semantics require
        ``-- sh -c "ps aux | grep mem"`` (already worked: the ``|``
        sits inside a single quoted token).
        """
        allowed, _ = self.guard.check([
            "kubectl", "exec", "chaosblade-tool-xxxx", "-n", "chaosblade",
            "--", "ps", "aux", "|", "grep", "mem",
        ])
        assert allowed is True

    @pytest.mark.parametrize("solo", [";", "|", "&", "||", "&&", ">", "<"])
    def test_kubectl_exec_all_solo_metachars_in_container_allowed(self, solo):
        """All SUSPICIOUS_SOLO_TOKENS are exempt inside container_command.

        Companion to the regression above — locks the rule "solo
        metachars after ``--`` are container-side, not host-side" for
        every token in the set so a future tightening that re-checks
        cmd-wide surfaces here, not just for ``|``.
        """
        allowed, _ = self.guard.check([
            "kubectl", "exec", "pod", "--", "sh", "-c", "true", solo, "echo", "x",
        ])
        assert allowed is True

    def test_kubectl_solo_pipe_outside_exec_still_blocked(self):
        """Solo ``|`` in host part (no ``--`` / non-exec subcommand)
        must still be rejected — the relaxation is scoped to
        container_command only."""
        allowed, reason = self.guard.check(["kubectl", "get", "pods", "|"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_blade_solo_pipe_still_blocked(self):
        """blade has no ``--`` separator → all tokens are host-side →
        solo ``|`` must still be rejected."""
        allowed, reason = self.guard.check(["blade", "create", "|"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_kubectl_pipe_benign_filter_gives_actionable_feedback(self):
        """``kubectl get ... | wc`` stays BLOCKED, but the reason is a single
        command-agnostic heuristic (fetch output, post-process yourself) rather
        than an opaque 'dangerous' verdict — no per-tool advice map. The model
        reasons out the native alternative itself, for ANY ``| filter``."""
        allowed, reason = self.guard.check(
            ["kubectl", "get", "pods", "|", "wc", "-l"]
        )
        assert allowed is False
        assert "Dangerous pattern" not in reason
        assert "yourself" in reason  # command-agnostic heuristic core

    def test_nonkubectl_pipe_benign_filter_gets_command_agnostic_heuristic(self):
        """The heuristic is NOT kubectl-only: any read-only command piped into a
        text filter (``df | grep``) gets the exact same 'post-process yourself'
        feedback."""
        allowed, reason = self.guard.check(["df", "-h", "|", "grep", "data"])
        assert allowed is False
        assert "Dangerous pattern" not in reason
        assert "yourself" in reason

    def test_kubectl_pipe_to_shell_keeps_hard_verdict(self):
        """A pipe into a shell (not a benign text filter) must NOT get the soft
        hint — it keeps the hard 'dangerous' verdict."""
        allowed, reason = self.guard.check(["kubectl", "get", "pods", "|", "bash"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    # ── E11 — AST-level parser edge cases ───────────────────────────────

    def test_kubectl_field_selector_with_special_chars(self):
        """E11: --field-selector value is a payload, not a shell command.
        Old host_part regex would have joined and could mis-detect; new
        parser puts it in data_payload_values so it's skipped."""
        allowed, _ = self.guard.check([
            "kubectl", "get", "pods",
            "--field-selector", "status.phase=Running",
        ])
        assert allowed is True

    def test_blade_subcommand_parsed_correctly(self):
        """E11: blade AST parser identifies subcommand + value flags
        without consuming positional args."""
        allowed, _ = self.guard.check([
            "blade", "create", "pod", "network", "delay",
            "--time", "3000", "--interface", "eth0",
            "--names", "my-pod", "--namespace", "default",
        ])
        assert allowed is True

    def test_unknown_kubectl_flag_treated_as_value_taking(self):
        """E11: unknown flag consumes next token (conservative
        fallback). Subcommand + remaining positional still parsed."""
        allowed, _ = self.guard.check([
            "kubectl", "get", "--made-up-future-flag", "value", "pods",
        ])
        # Should still pass: 'get' is allowed, no dangerous patterns
        assert allowed is True

    def test_blade_boolean_flag_h_does_not_consume_next_token(self):
        """E11 Gap A regression: blade -h is boolean, must not eat the
        next positional. If it did, parser would mis-locate 'pod' as
        the -h value and the subcommand check would still work, but
        a future check that depends on positional_args being correct
        would silently break."""
        from chaos_agent.tools.guard_parser import parse_command
        p = parse_command(["blade", "create", "-h", "pod"])
        assert p.subcommand == "create"
        assert "pod" in p.positional_args
        assert ("-h", None) in p.flags

    def test_kubectl_get_with_double_dash_treated_as_positional(self):
        """E11 Gap B regression: `--` outside exec/run/attach/debug
        MUST NOT split container_command. Otherwise a misplaced `--`
        would become a bypass channel for shell-pattern checks on
        anything that follows."""
        from chaos_agent.tools.guard_parser import parse_command
        p = parse_command(["kubectl", "get", "--", "pod"])
        assert p.subcommand == "get"
        assert p.container_command == ()
        # 'pod' must end up somewhere that host_relevant_tokens covers
        assert "pod" in p.host_relevant_tokens()

    def test_kubectl_global_boolean_flag_does_not_misidentify_subcommand(self):
        """E11 first-principles regression: the OLD inline parser
        (pre-E11) skipped any `-` token + the NEXT token together
        (assumed every flag was value-taking). That silently
        misidentified the subcommand whenever a global boolean flag
        appeared before it.

        Example: ``kubectl --insecure-skip-tls-verify get pods``
          - OLD parser: skip --insecure-skip-tls-verify + skip 'get'
            → subcommand="pods" → "pods" not in whitelist → REJECTED
            (false positive — get is a legal subcommand)
          - NEW parser: --insecure-skip-tls-verify is in
            KUBECTL_BOOLEAN_FLAGS → no consume → subcommand="get"
            → ALLOWED ✓

        This test was absent from the original 28 — none of them
        exercised a boolean global flag before the subcommand. Add
        it so a future revert of the AST parser would surface here
        instead of silently regressing real LLM-generated commands.
        """
        allowed, reason = self.guard.check([
            "kubectl", "--insecure-skip-tls-verify", "get", "pods",
        ])
        assert allowed is True, f"expected allow, got: {reason}"

    @pytest.mark.parametrize("boolean_flag", [
        "--insecure-skip-tls-verify",
        "--help",
        "-h",
    ])
    def test_kubectl_boolean_flag_before_subcommand(self, boolean_flag):
        """Parameterized companion to the regression above — every
        kubectl global boolean flag must allow subcommand to be
        identified correctly when placed before it."""
        cmd = ["kubectl", boolean_flag, "get", "pods"]
        allowed, _ = self.guard.check(cmd)
        assert allowed is True

    def test_container_command_with_dangerous_single_token_allowed(self):
        """E11 mutation-testing regression: the existing
        ``test_kubectl_exec_dangerous_in_container_allowed`` uses
        ``[blade, create, k8s, pod-cpu, fullload]`` as the container
        command — each token is harmless individually, so the test
        cannot distinguish between

          (a) host_relevant_tokens() correctly EXCLUDES container_command
          (b) host_relevant_tokens() includes container_command BUT
              the test cmd happens to have no matching token

        Both produce ALLOW. This test closes that gap by using a
        container command whose SINGLE token ``"rm -rf /"`` does match
        the ``rm\\s+-rf`` regex. If a future change accidentally
        promotes container_command into host_relevant_tokens, this
        test flips to FAIL.
        """
        allowed, _ = self.guard.check([
            "kubectl", "exec", "pod", "--",
            "sh", "-c", "rm -rf /",  # single token "rm -rf /" matches rm\s+-rf
        ])
        assert allowed is True

    def test_data_payload_with_dangerous_single_token_allowed(self):
        """E11 mutation-testing regression: similar gap for
        data_payload_values. The existing -p JSON payload tests use
        ``{"spec":{...}}`` which doesn't match any blacklist pattern
        in single-token form. This one uses a payload that DOES match
        the regex, so a future change that leaks payload values into
        host_relevant_tokens would surface here.
        """
        allowed, _ = self.guard.check([
            "kubectl", "patch", "pvc", "x", "-n", "default",
            "-p", '{"spec":{"x":"rm -rf /"}}',  # single token contains rm -rf
        ])
        assert allowed is True


class TestToolGuardDifferentiatedFeedback:
    """Refactor: the merged ``"Dangerous pattern detected"`` verdict (one string
    for 9+ unrelated causes) is split into differentiated feedback.

    First-principles goal: restore the model's perception of what actually
    happened. Each rejection must name the specific cause, echo the offending
    token, and flag hard-floor vs reshapeable — so two different causes never
    come back with the same opaque sentence.
    """

    def setup_method(self):
        self.guard = ToolGuard()

    def test_distinct_causes_get_distinct_reasons(self):
        """Pipe / redirect / rm -rf / block-device write must NOT share one
        sentence — the whole point of the refactor."""
        reasons = {
            "pipe": self.guard.check(["kubectl", "get", "pods", "|", "wc", "-l"])[1],
            "metachar": self.guard.check(["blade", "create", ";", "reboot"])[1],
            "rm_rf": self.guard.check(["blade", "create", "rm -rf /"])[1],
            "block_dev": self.guard.check(["dd", "if=/dev/zero", "of=/dev/sda"])[1],
            "count": self.guard.check(["dd", "of=/tmp/x", "count=9999999"])[1],
        }
        # every cause yields a unique message
        assert len(set(reasons.values())) == len(reasons)
        # and each names its own cause
        assert "rm -rf" in reasons["rm_rf"]
        assert "block-device" in reasons["block_dev"]
        assert "magnitude" in reasons["count"]

    def test_evaluate_echoes_offending_token(self):
        """The concrete triggering token is echoed back so the model sees
        exactly what to change."""
        fb = self.guard.evaluate(["dd", "if=/dev/zero", "of=/dev/sda", "bs=1M"])
        assert fb.allowed is False
        assert fb.offending == "of=/dev/sda"

    def test_hard_floor_vs_reshapeable_is_flagged(self):
        """Destructive floors are marked hard (dead-end); a magnitude cap is
        reshapeable (retry with a smaller value)."""
        from chaos_agent.tools.guard_feedback import ViolatedConstraint

        floor = self.guard.evaluate(["blade", "create", "rm -rf /"])
        assert floor.is_hard_floor is True
        assert floor.constraint == ViolatedConstraint.DESTRUCTIVE_FLOOR

        magnitude = self.guard.evaluate(["dd", "of=/tmp/x", "count=9999999"])
        assert magnitude.is_hard_floor is False
        assert magnitude.constraint == ViolatedConstraint.UNSUPPORTED_FORM

    def test_unknown_binary_is_hard_floor_with_offender(self):
        fb = self.guard.evaluate(["curl", "http://x"])
        assert fb.allowed is False
        assert fb.offending == "curl"
        assert fb.is_hard_floor is True

    def test_check_is_pure_adapter_over_evaluate(self):
        """``check`` must stay a faithful (bool, reason) view of ``evaluate``."""
        cmd = ["kubectl", "edit", "deploy", "x"]
        allowed, reason = self.guard.check(cmd)
        fb = self.guard.evaluate(cmd)
        assert (allowed, reason) == (fb.allowed, fb.render_for_llm())


class TestToolGuardHostWhitelist:
    """Tier-1 / Tier-2 host binary whitelist + per-binary guards.

    Two independent gates protect host injection:
      Gate ①  binary whitelist (ALLOWED_COMMANDS)
      Gate ②  exec-form + SUSPICIOUS_SOLO_TOKENS / PARAM_BLACKLIST
              (shell loops, ``&`` background, pipes, redirects, ``$(``)

    These tests cover the Tier-1 admissions (low-risk single commands),
    the Tier-2 admissions gated by per-binary guards (systemctl verb,
    kill PID, chmod recursion), the still-forbidden binaries, and a
    regression suite proving Gate ② is NOT weakened by the relaxation.
    """

    def setup_method(self):
        self.guard = ToolGuard()

    # ── Tier 1 — low-risk, single-command, admitted directly ───────────

    @pytest.mark.parametrize("cmd", [
        ["truncate", "-s", "0", "/tmp/x"],
        ["chmod", "000", "/tmp/x"],
        ["cp", "/tmp/a", "/tmp/b"],
        ["ntpdate", "pool.ntp.org"],
        ["chronyc", "makestep"],
    ])
    def test_tier1_commands_allowed(self, cmd):
        allowed, reason = self.guard.check(cmd)
        assert allowed is True, reason
        assert reason == "OK"

    # ── Tier 2 — systemctl verb whitelist ──────────────────────────────

    @pytest.mark.parametrize("verb", [
        "start", "stop", "restart", "mask", "unmask",
        "status", "is-active", "is-enabled",
    ])
    def test_systemctl_allowed_verbs(self, verb):
        allowed, reason = self.guard.check(["systemctl", verb, "nginx"])
        assert allowed is True, reason

    def test_systemctl_flag_before_verb_allowed(self):
        """A leading flag (e.g. ``--now``) must not hide the verb."""
        allowed, reason = self.guard.check(["systemctl", "--now", "stop", "nginx"])
        assert allowed is True, reason

    @pytest.mark.parametrize("verb", [
        "poweroff", "reboot", "halt", "kexec", "isolate",
        "disable", "enable", "daemon-reload", "suspend", "hibernate",
    ])
    def test_systemctl_boot_level_verbs_rejected(self, verb):
        allowed, reason = self.guard.check(["systemctl", verb, "target"])
        assert allowed is False
        assert "systemctl subcommand not allowed" in reason

    def test_systemctl_without_verb_rejected(self):
        allowed, reason = self.guard.check(["systemctl"])
        assert allowed is False
        assert "requires a subcommand" in reason

    def test_systemctl_only_flags_rejected(self):
        allowed, reason = self.guard.check(["systemctl", "--version"])
        assert allowed is False
        assert "requires a subcommand" in reason

    # ── Tier 2 — kill PID safety ────────────────────────────────────────

    @pytest.mark.parametrize("cmd", [
        ["kill", "-9", "1234"],
        ["kill", "-STOP", "1234"],
        ["kill", "-CONT", "1234"],
        ["kill", "-s", "SIGKILL", "1234"],
        ["kill", "1234"],
    ])
    def test_kill_valid_pid_allowed(self, cmd):
        allowed, reason = self.guard.check(cmd)
        assert allowed is True, reason

    @pytest.mark.parametrize("cmd", [
        ["kill", "-9", "1"],     # init
        ["kill", "1"],           # init
        ["kill", "-1"],          # broadcast to every process
        ["kill", "0"],           # whole process group
        ["kill", "-9", "0"],     # process group 0
        ["kill", "-9"],          # no PID target at all
        ["kill", "-9", "self"],  # non-numeric target
    ])
    def test_kill_dangerous_target_rejected(self, cmd):
        allowed, reason = self.guard.check(cmd)
        assert allowed is False

    @pytest.mark.parametrize("cmd", [
        ["kill", "-9", "-123", "456"],           # signal, then a pgid
        ["kill", "-s", "SIGKILL", "-99", "456"],  # named signal, then a pgid
        ["kill", "-SIGTERM", "-99", "456"],
        ["kill", "-9", "456", "-123"],            # pgid after a valid PID
    ])
    def test_kill_process_group_target_rejected(self, cmd):
        """A negative PID is the POSIX process-group form.

        ``kill -9 -123 456`` signals every process in group 123 — the exact
        opposite of the "single, explicitly named process" the guard exists to
        enforce. Every dash-token was previously waved through as a signal
        flag, so only the FIRST numeric one is a signal spec now.
        """
        allowed, reason = self.guard.check(cmd)
        assert allowed is False
        assert "process-group" in reason

    @pytest.mark.parametrize("cmd", [
        ["kill", "-9", "456", "789"],  # two explicit PIDs is still fine
        ["kill", "-s", "SIGKILL", "456"],
    ])
    def test_kill_multiple_pids_still_allowed(self, cmd):
        allowed, reason = self.guard.check(cmd)
        assert allowed is True, reason

    # ── Tier 2 — chmod recursion disabled ───────────────────────────────

    @pytest.mark.parametrize("flag", ["-R", "--recursive"])
    def test_chmod_recursive_rejected(self, flag):
        allowed, reason = self.guard.check(["chmod", flag, "000", "/"])
        assert allowed is False
        assert "recursive" in reason

    def test_chmod_non_recursive_allowed(self):
        allowed, reason = self.guard.check(["chmod", "644", "/tmp/x"])
        assert allowed is True, reason

    # ── Still forbidden binaries (Tier 3 / interpreters / shell) ─────────
    #
    # ``nc`` / ``fuser`` / ``strace`` used to be here. They were admitted so the
    # ``降级方案`` sections of three host cases can actually run — a cluster
    # without ChaosBlade has no other path. They are NOT unconditionally allowed:
    # each is narrowed to its drill form by a per-binary guard (nc listen-only,
    # fuser port-spec-only, strace attach-only), and the dangerous siblings
    # (``nc -e``, ``fuser -k /``, ``strace <cmd>``) are still refused — by that
    # guard rather than by the whitelist, so the reason no longer reads
    # "not allowed". See test_guard_single_resource_binaries.py for both halves.

    @pytest.mark.parametrize("cmd", [
        ["rm", "-f", "/tmp/x"],
        ["pkill", "-9", "nginx"],
        ["killall", "nginx"],
        ["socat", "-", "TCP:host:80"],
        ["python3", "-c", "print(1)"],
        ["bpftrace", "-e", "tracepoint:syscalls:sys_enter_open{}"],
    ])
    def test_still_forbidden_binaries(self, cmd):
        allowed, reason = self.guard.check(cmd)
        assert allowed is False
        assert "not allowed" in reason

    # ── Gate ② regression: relaxation must NOT weaken shell defence ──────

    def test_kill_with_command_substitution_still_blocked(self):
        """``kill -STOP $(pidof x)`` must never slip through. The kill PID
        guard rejects the non-numeric ``$(pidof`` target before Gate ② is
        even reached — an even stricter outcome. Runtime must first resolve
        the PID via host_read, then inject a single ``kill -STOP <pid>``."""
        allowed, reason = self.guard.check(["kill", "-STOP", "$(pidof", "x)"])
        assert allowed is False
        assert "numeric PID" in reason

    def test_command_substitution_dollar_paren_still_blocked(self):
        """Gate ② regression: ``$(`` in a non-guarded Tier-1 binary's args
        is still caught by the PARAM_BLACKLIST — the whitelist relaxation
        does not open a command-substitution channel."""
        allowed, reason = self.guard.check(["truncate", "-s", "$(cat /x)", "/tmp/y"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_shell_loop_solo_tokens_still_blocked(self):
        """``while true; do ...; done`` relies on ``;`` solo tokens that
        Gate ② rejects regardless of the leading binary."""
        allowed, reason = self.guard.check(
            ["systemctl", "stop", "nginx", ";", "reboot"],
        )
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_append_redirect_still_blocked(self):
        """``echo x >> /etc/hosts`` — the ``>`` solo token is rejected by
        Gate ② (and echo/append has no single-command whitelist form)."""
        allowed, reason = self.guard.check(["cp", "/tmp/a", ">", "/etc/hosts"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_background_ampersand_still_blocked(self):
        """Background ``&`` solo token is rejected by Gate ② on any
        admitted binary (using truncate so the kill guard is not in play)."""
        allowed, reason = self.guard.check(["truncate", "-s", "0", "/tmp/x", "&"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    # ── custom systemctl subcommand override ────────────────────────────

    def test_custom_systemctl_subcommands_override(self):
        guard = ToolGuard(systemctl_subcommands={"start"})
        assert guard.check(["systemctl", "start", "x"])[0] is True
        assert guard.check(["systemctl", "stop", "x"])[0] is False


class TestToolGuardWizUnwrap:
    """kubewiz mode: ``wiz task exec --command "<kubectl ...>"`` unwrap.

    Regression for task-aacb0828: in kubewiz mode ``build_kubectl_cmd``
    wraps the ENTIRE kubectl command into wiz's ``--command`` string. Before
    the ``_parse_wiz`` fix, ToolGuard treated that string as one opaque host
    token, losing the inner ``--`` container_command exemption, so a legal
    kubectl-native fallback like ``kubectl debug node/... -- chroot /host sh
    -c '... >/dev/null 2>&1'`` was mis-flagged (``>/dev/null`` hit the
    blacklist). Post-fix kubewiz behaves IDENTICALLY to kubeconfig mode.
    """

    def setup_method(self):
        self.guard = ToolGuard()

    def _wiz(self, inner: str) -> list[str]:
        return [
            "wiz", "task", "exec", "--command", inner,
            "--cluster-uuid", "uuid-1", "--profile", "default",
        ]

    # ── core fix ──────────────────────────────────────

    def test_wiz_wrapped_kubectl_debug_redirect_allowed(self):
        """The exact failing command: inner container command has
        ``>/dev/null`` after ``--`` → must be allowed."""
        inner = (
            "kubectl debug node/n1 -it --image=ubuntu -- chroot /host "
            "sh -c 'iptables -I INPUT -j DROP >/dev/null 2>&1'"
        )
        allowed, reason = self.guard.check(self._wiz(inner))
        assert allowed is True, reason

    def test_wiz_and_kubeconfig_equivalent(self):
        """Same logical command in both modes yields the same verdict."""
        kubeconfig_cmd = [
            "kubectl", "debug", "node/n1", "-it", "--image=ubuntu", "--",
            "chroot", "/host", "sh", "-c",
            "iptables -I INPUT -j DROP >/dev/null 2>&1",
        ]
        inner = (
            "kubectl debug node/n1 -it --image=ubuntu -- chroot /host "
            "sh -c 'iptables -I INPUT -j DROP >/dev/null 2>&1'"
        )
        kc_allowed, _ = self.guard.check(kubeconfig_cmd)
        wiz_allowed, _ = self.guard.check(self._wiz(inner))
        assert kc_allowed is True
        assert wiz_allowed == kc_allowed

    # ── safety preserved: inner host segment still checked ───────────

    def test_wiz_wrapped_pipe_bash_in_host_segment_blocked(self):
        """A pipe BEFORE ``--`` (inner host segment) must still be blocked."""
        inner = "kubectl debug node/n1 | bash -- echo hi"
        allowed, reason = self.guard.check(self._wiz(inner))
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_wiz_wrapped_semicolon_rm_in_host_segment_blocked(self):
        inner = "kubectl get pods ; rm -rf /"
        allowed, reason = self.guard.check(self._wiz(inner))
        assert allowed is False
        assert "Dangerous pattern" in reason

    @pytest.mark.parametrize("inner", [
        "kubectl get pods; rm -rf /",          # ';' glued to the token before
        "kubectl get pods;rm -rf /",           # glued on both sides
        "kubectl get pods -n default; dd if=/dev/zero of=/dev/sda",
    ])
    def test_wiz_wrapped_glued_semicolon_blocked(self, inner):
        """A semicolon with no surrounding space must not slip the lift.

        The blacklist patterns are matched PER TOKEN
        (``;\\s*rm`` / ``rm\\s+-rf``), so after ``shlex.split`` the chaining is
        invisible: ``pods;`` is one token and ``rm``, ``-rf``, ``/`` are three
        more, none of which matches on its own. The lift therefore has to
        detect the glued ``;`` and fall back to whole-command checks on the raw
        ``--command`` string, where the patterns match again — restoring parity
        with the space-separated form above.
        """
        allowed, reason = self.guard.check(self._wiz(inner))
        assert allowed is False, reason

    def test_wiz_wrapped_glued_semicolon_falls_back_to_generic(self):
        """Parser-level: the glued form must NOT be lifted."""
        from chaos_agent.tools.guard_parser import parse_command
        p = parse_command(self._wiz("kubectl get pods; rm -rf /"))
        # _parse_generic keeps the raw --command string as ONE checked token.
        assert any("; rm -rf /" in t for t in p.host_relevant_tokens())

    def test_glued_control_operator_gap_is_not_wiz_specific(self):
        """Residual limitation, pinned so the guarantee is not overstated.

        The fallback restores PATTERN VISIBILITY; it does not make ``;`` itself
        fatal. A chained command that matches no blacklist pattern still passes
        — and it passes identically on the RAW path, so this is a general
        property of the guard (specific dangerous patterns + solo control-op
        tokens), not a wiz weakness. Closing it means treating a glued control
        operator as fatal everywhere, which is a separate decision.
        """
        wiz_allowed, _ = self.guard.check(self._wiz("kubectl get pods;reboot"))
        raw_allowed, _ = self.guard.check(["kubectl", "get", "pods;reboot"])
        assert wiz_allowed == raw_allowed is True

    def test_wiz_wrapped_forbidden_subcommand_allowed_by_guard(self):
        """Guard no longer applies kubectl subcommand whitelist to wiz-wrapped commands.

        Same rationale as ``test_wiz_wrapped_kubectl_config_write_allowed_by_guard``:
        ``execute_via_transport`` checks the raw ``kubectl edit ...``
        command BEFORE wrapping, so the subcommand whitelist is already
        enforced at the pre-wrap stage.  The guard does not re-apply it
        to the inner command of a wiz-wrapped command.
        """
        inner = "kubectl edit deployment my-app"
        allowed, _ = self.guard.check(self._wiz(inner))
        assert allowed is True

    # ── fallback: non-wrapped wiz unchanged ────────────────────

    def test_wiz_without_command_falls_back_to_generic(self):
        allowed, reason = self.guard.check(["wiz", "task", "list"])
        assert allowed is True, reason

    # ── parser-level direct assertions ──────────────────────

    def test_wiz_unwrap_parser_exempts_inner_container_command(self):
        from chaos_agent.tools.guard_parser import parse_command
        inner = "kubectl exec pod -n default -- sh -c 'echo hi >/dev/null'"
        p = parse_command(self._wiz(inner))
        assert p.binary == "wiz"
        # inner "--" tail lifted into container_command (exempt)
        assert p.container_command
        assert any(">/dev/null" in t for t in p.container_command)
        # and therefore NOT present in the checked host tokens
        assert not any(">/dev/null" in t for t in p.host_relevant_tokens())

    # ── end-to-end: build_kubectl_cmd + guard reproduces the bug ───────

    def test_build_kubectl_cmd_kubewiz_debug_passes_guard(self):
        """build_kubectl_cmd always returns a raw kubectl command.

        After the transport-layer migration, ``build_kubectl_cmd`` no
        longer wraps with wiz — it returns ``[kubectl, debug, ...]``
        regardless of connection mode.  The transport layer (wiz/ssh)
        wrapping happens later in ``execute_via_transport``.

        This test verifies the raw command passes the guard.
        """
        from types import SimpleNamespace

        from chaos_agent.tools.kubectl import build_kubectl_cmd

        s = SimpleNamespace(
            kubectl_path="kubectl",
            kubeconfig_path="",
            kube_context="",
        )
        args = [
            "node/n1", "-it", "--image=ubuntu", "--",
            "chroot", "/host", "sh", "-c",
            "iptables -I INPUT -j DROP >/dev/null 2>&1",
        ]
        cmd = build_kubectl_cmd("debug", args, settings=s)
        allowed, reason = self.guard.check(cmd)
        assert allowed is True, reason


class TestToolGuardCustom:
    """Test ToolGuard with custom configuration."""

    def test_custom_allowed_commands(self):
        guard = ToolGuard(allowed_commands={"my-tool"})
        allowed, _ = guard.check(["my-tool", "arg"])
        assert allowed is True

    def test_custom_allowed_commands_override_default(self):
        guard = ToolGuard(allowed_commands={"my-tool"})
        allowed, reason = guard.check(["blade", "create"])
        assert allowed is False

    def test_custom_kubectl_subcommands(self):
        guard = ToolGuard(kubectl_subcommands={"get", "custom"})
        allowed, _ = guard.check(["kubectl", "custom", "arg"])
        assert allowed is True

    def test_custom_param_blacklist(self):
        guard = ToolGuard(param_blacklist=[r"DANGEROUS"])
        allowed, reason = guard.check(["blade", "DANGEROUS"])
        assert allowed is False


class TestToolGuardAuditLog:
    """Test ToolGuard.audit_log() output."""

    def test_audit_log_format(self, caplog):
        guard = ToolGuard()
        result = CommandResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=123.4,
        )
        with caplog.at_level(logging.INFO):
            guard.audit_log(["blade", "create"], result, task_id="task-123")

        assert len(caplog.records) == 1
        log_data = json.loads(caplog.records[0].message)
        assert log_data["task_id"] == "task-123"
        assert log_data["command"] == ["blade", "create"]
        assert log_data["exit_code"] == 0
        assert log_data["duration_ms"] == 123.4
        assert "timestamp" in log_data


class TestCommandResult:
    """Test CommandResult dataclass."""

    def test_default_duration(self):
        r = CommandResult(exit_code=0, stdout="ok", stderr="")
        assert r.duration_ms == 0.0

    def test_fields(self):
        r = CommandResult(exit_code=1, stdout="out", stderr="err", duration_ms=50.0)
        assert r.exit_code == 1
        assert r.stdout == "out"
        assert r.stderr == "err"
        assert r.duration_ms == 50.0


class TestGuardProviderAggregation:
    """B4.1 — the Gate-① whitelist is assembled from provider declarations.

    Proves the knowledge-ownership refactor (host injection binaries declared on
    the providers, aggregated by the guard) introduced ZERO change to the
    effective whitelist, and that the aggregation is a safe UNION that fails
    CLOSED when no providers are registered.
    """

    @pytest.fixture(autouse=True)
    def _restore_registry(self):
        # These tests mutate the class-level provider registry; snapshot and
        # restore so ordering with other tests is irrelevant.
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        yield
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()

    def test_default_equals_static_reference(self):
        """Aggregated default whitelist == the pre-refactor static set (anchor)."""
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()
        assert ToolGuard._default_allowed_commands() == ToolGuard.ALLOWED_COMMANDS
        assert ToolGuard().allowed_commands == ToolGuard.ALLOWED_COMMANDS

    def test_default_is_union_of_base_and_provider_binaries(self):
        """The default is exactly BASE_COMMANDS ∪ each provider injection set."""
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()
        expected = set(ToolGuard.BASE_COMMANDS)
        for provider in FaultProviderRegistry.all_providers():
            expected |= set(provider.injection_binaries)
        assert ToolGuard._default_allowed_commands() == expected

    def test_provider_binaries_partition_the_reference(self):
        """Every non-base command in the reference is owned by exactly one
        provider's ``injection_binaries`` — no orphan and no overlap between the
        guard base set and the provider contributions."""
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()
        provider_union: set[str] = set()
        for provider in FaultProviderRegistry.all_providers():
            provider_union |= set(provider.injection_binaries)
        # base and provider contributions are disjoint
        assert ToolGuard.BASE_COMMANDS & provider_union == set()
        # together they reconstruct the full reference
        assert ToolGuard.BASE_COMMANDS | provider_union == ToolGuard.ALLOWED_COMMANDS

    def test_degrades_closed_to_base_when_registry_empty(self):
        """No providers registered → whitelist collapses to BASE_COMMANDS (fails
        CLOSED, never open). Injection binaries like ``blade`` / ``iptables`` are
        rejected; only the guard-owned diagnostics remain."""
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        FaultProviderRegistry.clear()
        try:
            assert ToolGuard._default_allowed_commands() == ToolGuard.BASE_COMMANDS
            guard = ToolGuard()
            assert guard.check(["df", "-h"])[0] is True
            # provider-contributed binaries are no longer admitted
            for binary in ("blade", "kubectl", "iptables", "systemctl"):
                allowed, reason = guard.check([binary, "x"])
                assert allowed is False
                assert "not allowed" in reason
        finally:
            FaultProviderRegistry.register_builtins()

    def test_explicit_allowed_commands_bypasses_aggregation(self):
        """An explicit ``allowed_commands`` arg still wins (test-injection seam);
        aggregation only assembles the DEFAULT set."""
        guard = ToolGuard(allowed_commands={"df"})
        assert guard.allowed_commands == {"df"}
        assert guard.check(["df", "-h"])[0] is True
        assert guard.check(["blade", "create"])[0] is False

    def test_no_interpreter_or_shell_in_any_provider_binaries(self):
        """SECURITY invariant: interpreters / shells and the guardrail binaries
        are in NO provider's ``injection_binaries`` — the aggregation can never
        admit them regardless of registration."""
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()
        forbidden = {"sh", "bash", "zsh", "python", "python3", "perl", "ruby",
                     "node", "env", "eval", "exec"}
        for provider in FaultProviderRegistry.all_providers():
            assert set(provider.injection_binaries) & forbidden == set()
        assert ToolGuard._default_allowed_commands() & forbidden == set()
