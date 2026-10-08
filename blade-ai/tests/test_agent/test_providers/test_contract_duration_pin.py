"""Contract duration pin: the carrier's timer must equal the pinned D+G.

On the blade surfaces ``--timeout`` IS the fault's self-destroy bound; on
the faultdrill assembler the window is the ``duration_seconds`` structured
argument rendered into the carrier's own restore timer. Under the
two-number window contract the registry's single dispatch point computes
the fault's own recovery timer ``D + G`` (``recovery_timer_seconds``) ONCE
and hands it to the claiming provider — so any carrier value other than
that pin either leaves the fault resident past the safety-net window or
ends it before the framework can actively recover. Provider hooks pin
verbatim whatever number the registry computed; the D→D+G arithmetic lives
only at the dispatch point. These tests pin the rewrite seam — the string
helpers, the blade carriers that own the flag, the faultdrill assembler
pin, and the registry dispatch the execute loop consults before dispatch.
"""

from __future__ import annotations

from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.chaosblade.provider import ChaosbladeProvider
from chaos_agent.agent.providers.chaosblade.python_provider import (
    ChaosbladePythonProvider,
)
from chaos_agent.agent.providers.faultdrill.assembler import ASSEMBLER_TOOL_NAME
from chaos_agent.agent.providers.faultdrill.provider import FaultDrillProvider
from chaos_agent.utils.fault_type import (
    read_timeout_flag,
    recovery_timer_seconds,
    set_timeout_flag,
)


class TestTimeoutFlagStringHelpers:
    """``read_timeout_flag`` / ``set_timeout_flag`` on a command string."""

    def test_reads_both_spellings_and_last_wins(self):
        assert read_timeout_flag("--mode ram --timeout 300") == 300
        assert read_timeout_flag("--mode ram --timeout=300") == 300
        assert read_timeout_flag("--timeout 60s") == 60
        assert read_timeout_flag("--timeout 60 --timeout 600") == 600

    def test_reads_absent_and_non_numeric_as_none(self):
        assert read_timeout_flag("--mode ram") is None
        assert read_timeout_flag("") is None
        assert read_timeout_flag("--timeout abc") is None

    def test_never_matches_a_prefixed_flag(self):
        assert read_timeout_flag("--timeout-multiplier 5") is None
        assert set_timeout_flag("--timeout-multiplier 5", 120) == (
            "--timeout-multiplier 5 --timeout 120"
        )

    def test_appends_when_absent(self):
        assert set_timeout_flag("--mode ram", 120) == "--mode ram --timeout 120"
        assert set_timeout_flag("", 120) == "--timeout 120"

    def test_rewrites_first_occurrence_and_drops_later_ones(self):
        assert set_timeout_flag("--timeout 600 --mode ram", 120) == (
            "--timeout 120 --mode ram"
        )
        assert set_timeout_flag("--timeout=600 --timeout 60", 120) == (
            "--timeout 120 "
        )

    def test_leaves_every_other_byte_untouched(self):
        flags = "--process nginx --signal 15 --timeout 60"
        assert set_timeout_flag(flags, 600) == (
            "--process nginx --signal 15 --timeout 600"
        )


class TestChaosbladeCarrierPin:
    """The provider rewrites both surfaces it owns, in place."""

    def setup_method(self):
        self.provider = ChaosbladeProvider()

    def test_blade_create_absent_timeout_is_pinned(self):
        args = {"scope": "node", "flags": "--network-traffic out"}
        note = self.provider.enforce_contract_duration("blade_create", args, 120)
        assert note == "blade --timeout absent"
        assert args["flags"] == "--network-traffic out --timeout 120"

    def test_blade_create_inflated_timeout_is_corrected(self):
        args = {"flags": "--mode ram --timeout 600"}
        assert self.provider.enforce_contract_duration(
            "blade_create", args, 120
        ) == "blade --timeout 600"
        assert args["flags"] == "--mode ram --timeout 120"

    def test_blade_create_short_timeout_is_corrected_too(self):
        args = {"flags": "--process nginx --signal 15 --timeout 60"}
        self.provider.enforce_contract_duration("blade_create", args, 300)
        assert read_timeout_flag(args["flags"]) == 300

    def test_blade_create_already_equal_is_left_alone(self):
        args = {"flags": "--mode ram --timeout 120"}
        before = args["flags"]
        assert self.provider.enforce_contract_duration(
            "blade_create", args, 120
        ) is None
        assert args["flags"] == before

    def test_embedded_kubectl_exec_blade_create_is_pinned(self):
        args = {
            "subcommand": "exec",
            "v_args": (
                "chaosblade-tool-x -n chaosblade -- blade create mem load "
                "--mode ram --timeout 600"
            ),
        }
        assert self.provider.enforce_contract_duration(
            "kubectl", args, 120
        ) == "blade --timeout 600"
        assert args["v_args"].endswith("--mode ram --timeout 120")

    def test_plain_kubectl_exec_is_not_this_carrier(self):
        args = {"subcommand": "exec", "v_args": "pod-x -n ns -- df -h"}
        before = dict(args)
        assert self.provider.enforce_contract_duration("kubectl", args, 120) is None
        assert args == before

    def test_other_tools_are_not_this_carrier(self):
        assert self.provider.enforce_contract_duration(
            "kubectl", {"subcommand": "get", "v_args": "pods"}, 120
        ) is None


class TestPythonCarrierPin:
    """``blade_python_create`` carries the same rule on its flags string."""

    def test_flags_pinned_when_absent(self):
        provider = ChaosbladePythonProvider()
        args = {"flags": "--delay --time 3000"}
        note = provider.enforce_contract_duration(
            "blade_python_create", args, 120
        )
        assert note == "blade python --timeout absent"
        assert args["flags"] == "--delay --time 3000 --timeout 120"

    def test_prepare_and_revoke_are_untouched(self):
        provider = ChaosbladePythonProvider()
        args = {"flags": "--timeout 60"}
        assert provider.enforce_contract_duration(
            "blade_python_prepare", args, 120
        ) is None
        assert args["flags"] == "--timeout 60"


class TestFaultdrillCarrierPin:
    """The assembler's window is a structured argument, pinned in place.

    ``faultdrill_assemble_carrier`` carries no ``--timeout`` flag: the fault
    window is its ``duration_seconds`` tool argument, which the assembler
    renders into the carrier's own restore timer. The blade rule applies
    verbatim — any value other than the approved window is rewritten before
    dispatch (the execute loop hands the registry the very dict the ToolNode
    will execute).
    """

    def setup_method(self):
        self.provider = FaultDrillProvider()

    def test_differing_window_is_rewritten(self):
        args = {"duration_seconds": 300, "patches": "[]"}
        note = self.provider.enforce_contract_duration(
            ASSEMBLER_TOOL_NAME, args, 600
        )
        assert note == "faultdrill duration_seconds 300"
        assert args["duration_seconds"] == 600

    def test_equal_window_is_left_alone(self):
        args = {"duration_seconds": 600}
        assert self.provider.enforce_contract_duration(
            ASSEMBLER_TOOL_NAME, args, 600
        ) is None
        assert args["duration_seconds"] == 600

    def test_absent_window_is_filled_from_the_contract(self):
        args = {"patches": "[]"}
        note = self.provider.enforce_contract_duration(
            ASSEMBLER_TOOL_NAME, args, 600
        )
        assert note == "faultdrill duration_seconds absent"
        assert args["duration_seconds"] == 600

    def test_unparseable_window_is_replaced(self):
        args = {"duration_seconds": "soon"}
        self.provider.enforce_contract_duration(ASSEMBLER_TOOL_NAME, args, 600)
        assert args["duration_seconds"] == 600

    def test_other_tools_are_not_this_carrier(self):
        args = {"duration_seconds": 300}
        assert self.provider.enforce_contract_duration(
            "kubectl", args, 600
        ) is None
        assert args["duration_seconds"] == 300

    def test_registry_dispatches_to_the_assembler_pin(self):
        args = {"duration_seconds": 300, "patches": "[]"}
        note = FaultProviderRegistry.enforce_contract_duration(
            ASSEMBLER_TOOL_NAME, args, 600
        )
        assert note is not None
        # 经 registry 单点：载体拿到的是 D+G（安全网窗），不是裸 D。
        assert args["duration_seconds"] == recovery_timer_seconds(600)


class TestRegistryDispatch:
    """The seam the execute loop consults: first provider to claim wins."""

    def test_registry_pins_the_blade_carrier(self):
        args = {"flags": "--network-traffic out"}
        note = FaultProviderRegistry.enforce_contract_duration(
            "blade_create", args, 90
        )
        assert note is not None
        assert read_timeout_flag(args["flags"]) == recovery_timer_seconds(90)

    def test_registry_returns_none_when_nothing_changes(self):
        pinned = recovery_timer_seconds(90)
        args = {"flags": f"--network-traffic out --timeout {pinned}"}
        assert FaultProviderRegistry.enforce_contract_duration(
            "blade_create", args, 90
        ) is None
        assert read_timeout_flag(args["flags"]) == pinned

    def test_registry_rewrites_a_bare_d_timeout_to_d_plus_grace(self):
        """裸 D 的 --timeout（如 LLM 从契约抄来）被改写为 D+G 安全网窗。"""
        args = {"flags": "--network-traffic out --timeout 90"}
        note = FaultProviderRegistry.enforce_contract_duration(
            "blade_create", args, 90
        )
        assert note is not None
        assert read_timeout_flag(args["flags"]) == recovery_timer_seconds(90)

    def test_registry_ignores_unclaimed_tools(self):
        args = {"subcommand": "apply", "v_args": "deploy/x"}
        before = dict(args)
        assert FaultProviderRegistry.enforce_contract_duration(
            "kubectl", args, 90
        ) is None
        assert args == before

    def test_registry_refuses_a_non_positive_contract(self):
        """A non-positive window is not a contract, so nothing may be written.

        Pins the dispatch-level refusal (see
        ``FaultProviderRegistry.enforce_contract_duration``): relying on the
        caller-side ``> 0`` guard would leave every future call site one
        forgotten ``if`` away from rewriting a legal command into
        ``--timeout 0`` — an invalid window that also slips past the duration
        anchor, whose comparison goes silent on a zero side. The carriers
        must therefore never see the value, on any surface.
        """
        surfaces = (
            ("blade_create", {"flags": "--mode ram --timeout 600"}),
            (
                "blade_python_create",
                {"flags": "--delay --time 3000 --timeout 600"},
            ),
            (
                "kubectl",
                {
                    "subcommand": "exec",
                    "v_args": "chaosblade-tool-x -n chaosblade -- blade create "
                    "cpu fullload --timeout 600",
                },
            ),
        )
        for invalid in (0, -5, None):
            for tool, args in surfaces:
                before = dict(args)
                assert FaultProviderRegistry.enforce_contract_duration(
                    tool, args, invalid
                ) is None, (tool, invalid)
                assert args == before, (tool, invalid)
