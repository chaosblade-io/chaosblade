"""Registry completeness guards — the mechanism behind "extensible".

Every contract below is implicit in the code and would otherwise be enforced
only by whoever remembers it. Adding a transport channel, a fault family, a
provider or a capability phase without its declaration must fail HERE, at test
time, rather than degrade silently at runtime.

That distinction is what task-46317228 was made of: the capability gate looked
complete, but a provider tool bound in a phase its provider did not declare fell
straight through it, and a channel's profile lived in a table parallel to the
channel itself.
"""

import pytest


@pytest.fixture(autouse=True)
def _builtins_only_registry():
    """Assert against the SHIPPED channel set, not whatever a test left behind.

    ``TransportRegistry._channels`` is class-level state; a test double
    registered elsewhere would otherwise show up here as a channel missing every
    declaration, turning these guards into false alarms.
    """
    from chaos_agent.transports.registry import TransportRegistry

    saved = dict(TransportRegistry._channels)
    TransportRegistry._channels = {}
    TransportRegistry._ensure_default()
    yield
    TransportRegistry._channels = saved


class TestChannelDeclarations:
    """A channel must declare everything the framework reads off it."""

    @staticmethod
    def _channels():
        from chaos_agent.transports.registry import TransportRegistry

        TransportRegistry._ensure_default()
        return dict(TransportRegistry._channels)

    def test_every_channel_declares_a_known_profile(self):
        from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

        known = {PROFILE_K8S, PROFILE_HOST}
        for name, channel in self._channels().items():
            profile = getattr(channel, "profile", None)
            assert profile in known, (
                f"channel {name!r} declares profile {profile!r}; must be one of "
                f"{sorted(known)}. An undeclared profile resolves to 'unknown', "
                f"which fails closed but reports only 'unknown channel'."
            )

    def test_every_channel_declares_claims_and_priority(self):
        for name, channel in self._channels().items():
            assert callable(getattr(channel, "claims", None)), (
                f"channel {name!r} has no claims(); it could only ever be "
                f"selected through an explicit channel_override"
            )
            assert isinstance(getattr(channel, "priority", None), int), (
                f"channel {name!r} has no integer priority()"
            )

    def test_every_channel_profile_has_an_environment_profile(self):
        """A profile with no EnvironmentProfile yields NO tools at all."""
        from chaos_agent.agent.environment_profiles import get_environment_profile

        for name, channel in self._channels().items():
            profile = getattr(channel, "profile", "")
            assert get_environment_profile(profile) is not None, (
                f"channel {name!r} declares profile {profile!r} but no "
                f"EnvironmentProfile is registered for it — every tool would be "
                f"gated away on that channel"
            )

    def test_profile_of_is_derived_not_tabulated(self):
        """``profile_of`` must read the channel, not a parallel table."""
        from chaos_agent.transports.registry import channel_profiles, profile_of

        derived = channel_profiles()
        for name, channel in self._channels().items():
            assert profile_of(name) == getattr(channel, "profile", "")
            assert derived[name] == getattr(channel, "profile", "")


class TestFaultFamilyDeclarations:
    def test_every_family_profile_has_an_environment_profile(self):
        from chaos_agent.agent.environment_profiles import get_environment_profile
        from chaos_agent.agent.spec import fault_registry

        for name, family in fault_registry._REGISTRY.items():
            assert get_environment_profile(family.profile) is not None, (
                f"fault family {name!r} declares profile {family.profile!r} with "
                f"no EnvironmentProfile registered — every request in that "
                f"family would resolve to an unsupported environment"
            )

    def test_every_family_scope_resolves_back_to_that_family(self):
        from chaos_agent.agent.spec.fault_registry import _REGISTRY, family_for_scope

        for name, family in _REGISTRY.items():
            for scope in family.scopes:
                resolved = family_for_scope(scope)
                assert resolved is not None, f"scope {scope!r} resolves to nothing"


class TestProviderToolOwnership:
    def test_shared_tools_resolve_order_independently(self):
        """A shared tool's verdict must not depend on registration order.

        ``blade_help`` / ``blade_status`` are declared by BOTH ChaosBlade
        providers, and they disagree on reach (one accepts either profile, the
        other only host). Recording a single owner would make the gate answer
        whichever happened to register first, so the index keeps every claimant
        and reachability is the union.
        """
        from chaos_agent.agent.capabilities.context import provider_tool_owners
        from chaos_agent.agent.providers import FaultProviderRegistry

        FaultProviderRegistry.register_builtins()
        owners = provider_tool_owners()
        shared = {n: o for n, o in owners.items() if len(o) > 1}
        assert shared, (
            "expected at least one shared tool (blade_help / blade_status); if "
            "that changed, this guard is no longer covering anything"
        )
        for name, claimants in owners.items():
            assert isinstance(claimants, tuple) and claimants, (
                f"tool {name!r} must map to a non-empty tuple of providers"
            )

    def test_single_profile_provider_tools_declare_a_transport_profile(self):
        """``TOOL_PROFILE`` must cover every SINGLE-profile provider tool.

        The tools layer cannot import providers (circular), so the profile a
        tool passes to ``execute_via_transport`` lives in its own table. This
        asserts the two agree, and that dual-reach tools are absent — listing one
        would refuse a legitimate call (ChaosBlade runs on host targets too).
        """
        from chaos_agent.agent.capabilities.context import provider_tool_owners
        from chaos_agent.tools._tool_profiles import TOOL_PROFILE
        from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

        for tool_name, owners in provider_tool_owners().items():
            k8s_ok = any(o.matches_channel(PROFILE_K8S) for o in owners)
            host_ok = any(o.matches_channel(PROFILE_HOST) for o in owners)
            if k8s_ok and host_ok:
                assert tool_name not in TOOL_PROFILE, (
                    f"tool {tool_name!r} is reachable on both profiles yet "
                    f"declares {TOOL_PROFILE.get(tool_name)!r} — that would "
                    f"refuse a valid call on the other profile"
                )
                continue
            assert tool_name in TOOL_PROFILE, (
                f"provider tool {tool_name!r} is single-profile but not "
                f"registered in tools/_tool_profiles.py — it would dispatch "
                f"with no expect_profile and could run on the wrong machine"
            )
            expected = PROFILE_K8S if k8s_ok else PROFILE_HOST
            assert TOOL_PROFILE[tool_name] == expected, (
                f"tool {tool_name!r} declares {TOOL_PROFILE[tool_name]!r} but "
                f"is only reachable on {expected!r}"
            )


class TestCapabilityPhaseCoverage:
    def test_every_provider_phase_constant_is_mapped(self):
        """A phase providers can serve must be reachable through the gate.

        ``providers.base`` declares the phase vocabulary; the ownership index is
        derived from ``_PHASE_TO_PROVIDER_PHASE``'s value set. If someone adds a
        phase constant to ``base`` and wires ``provider.tools()`` for it but
        forgets the mapping, those tools drop out of the index and stop being
        gated — defect A all over again.
        """
        from chaos_agent.agent.capabilities.context import (
            _ALL_PROVIDER_PHASES,
            _PHASE_TO_PROVIDER_PHASE,
        )
        from chaos_agent.agent.providers import base

        declared = {
            value for name, value in vars(base).items()
            if name.isupper() and not name.startswith("_") and isinstance(value, str)
        }
        mapped = set(_PHASE_TO_PROVIDER_PHASE.values())
        assert declared == mapped, (
            f"provider phase constants {sorted(declared)} do not match the "
            f"gate's mapping values {sorted(mapped)}"
        )
        assert set(_ALL_PROVIDER_PHASES) == mapped

    def test_every_phase_string_used_by_callers_is_registered(self):
        """A phase the gate does not know now fails closed — so it must be known.

        Fail-closed is the safe direction, but an unregistered phase would block
        every tool in that node. Enumerating the callers keeps a typo from
        shipping.
        """
        import re
        from pathlib import Path

        from chaos_agent.agent.capabilities.context import _PHASE_TO_PROVIDER_PHASE

        src = Path(__file__).resolve().parents[2] / "src" / "chaos_agent"
        pattern = re.compile(
            r"(?:build_capability_context|is_tool_name_allowed_for_context)\s*\("
            r"[^)]*?[\"']([a-z_]+)[\"']",
            re.S,
        )
        found: set[str] = set()
        for path in src.rglob("*.py"):
            found |= set(pattern.findall(path.read_text()))

        unknown = found - set(_PHASE_TO_PROVIDER_PHASE)
        assert not unknown, (
            f"these phase strings are passed by callers but not registered in "
            f"_PHASE_TO_PROVIDER_PHASE: {sorted(unknown)} — they would fail "
            f"closed and disable those nodes"
        )


class TestEveryDispatchDeclaresItsProfile:
    """No transport dispatch may leave its required profile unstated.

    ``expect_profile`` is what stops a command shape from travelling a channel
    that cannot address it. The LLM-facing tools were the obvious half; the
    internal probes are the other one, and they are the more dangerous half —
    a baseline, a feasibility check or a verification-evidence probe answered by
    the wrong machine becomes *evidence*, silently, with no failure to notice
    (task-46317228).

    ``""`` is a legitimate value (a dual-profile tool such as ``blade``, whose
    CLI runs on both cluster and bare host). What is NOT legitimate is omitting
    the argument, because then nobody decided.
    """

    def test_no_call_site_omits_expect_profile(self):
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "chaos_agent"
        offenders = []
        for path in src.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name != "execute_via_transport":
                    continue
                if "expect_profile" not in {kw.arg for kw in node.keywords}:
                    offenders.append(f"{path.relative_to(src)}:{node.lineno}")

        assert not offenders, (
            "these execute_via_transport call sites do not declare "
            "expect_profile — pass PROFILE_K8S / PROFILE_HOST, or "
            "profile_for_tool(<name>) when the tool's provider spans both:\n  "
            + "\n  ".join(sorted(offenders))
        )


class TestChannelProviderMatrix:
    def test_matrix_snapshot(self):
        """Pin which provider is usable on which channel.

        Adding a channel or a provider changes this matrix; the diff makes the
        new combination an explicit decision instead of an accident.
        """
        from chaos_agent.agent.providers import FaultProviderRegistry
        from chaos_agent.transports.registry import TransportRegistry, profile_of

        TransportRegistry._ensure_default()
        FaultProviderRegistry.register_builtins()

        matrix = {
            channel: sorted(
                type(p).__name__
                for p in FaultProviderRegistry.all_providers()
                if p.matches_channel(profile_of(channel))
            )
            for channel in sorted(TransportRegistry._channels)
        }

        assert matrix == {
            "kubeconfig": ["ChaosbladeProvider", "K8sNativeProvider"],
            # ChaosBlade is intentionally on host channels too — the same
            # ``blade`` CLI drives bare-host faults (``blade create cpu load``).
            "kubewiz_host": [
                "ChaosbladeProvider", "ChaosbladePythonProvider", "HostShellProvider",
            ],
            "kubewiz_k8s": ["ChaosbladeProvider", "K8sNativeProvider"],
            "ssh": [
                "ChaosbladeProvider", "ChaosbladePythonProvider", "HostShellProvider",
            ],
        }, f"channel x provider matrix changed: {matrix}"


class TestExecutionLocationVisible:
    """Every channel must show WHERE a command ran, not just what ran.

    task-46317228: the tool NAME exposed that a host tool was running on a k8s
    session (that is how it was spotted), but nothing showed that the answer came
    from ``kubewiz-executor-…`` rather than the target node — and that second
    fact is what the verifier reasoned from for the rest of the run.
    """

    def test_every_channel_appends_its_name(self):
        from chaos_agent.transports.base import TransportTarget
        from chaos_agent.transports.registry import TransportRegistry

        targets = {
            "kubeconfig": TransportTarget(scope="k8s", channel_override="kubeconfig"),
            "kubewiz_k8s": TransportTarget(
                scope="k8s", channel_override="kubewiz_k8s",
                kubewiz_cluster_uuid="uuid-abcdef", kubewiz_profile="p",
            ),
            "kubewiz_host": TransportTarget(
                scope="host", channel_override="kubewiz_host",
                host_name="node-a", kubewiz_profile="p",
            ),
            "ssh": TransportTarget(
                scope="host", channel_override="ssh",
                ssh_host="10.0.0.7", ssh_user="root",
            ),
        }
        for name, target in targets.items():
            channel = TransportRegistry.get(name)
            shown = channel.display_command(["uptime"], target)
            assert "uptime" in shown, f"{name} lost the semantic command"
            assert name in shown, (
                f"{name} does not show the execution location; a stripped "
                f"display is what hid the wrong-machine read"
            )

    def test_kubewiz_k8s_names_the_cluster_it_targets(self):
        from chaos_agent.transports.base import TransportTarget
        from chaos_agent.transports.registry import TransportRegistry

        target = TransportTarget(
            scope="k8s", channel_override="kubewiz_k8s",
            kubewiz_cluster_uuid="c62735cce1d61445995c0f1d9e4a1bded",
            kubewiz_profile="526255",
        )
        shown = TransportRegistry.get("kubewiz_k8s").display_command(["uptime"], target)
        # Cluster addressing is the reason the platform executor answers.
        assert "cluster" in shown
        # Truncated so a uuid cannot flood the line.
        assert "c62735cce1d61445995c0f1d9e4a1bded" not in shown

    @pytest.mark.asyncio
    async def test_executor_passes_channel_down_to_the_status_layer(self):
        """``source`` may be a semantic label; the facts must travel beside it.

        The executor knows the resolved channel; ``run_command`` is what emits
        the status event. Asserting the hand-off keeps the destination from
        being reduced to whatever label the caller chose ("host-read").
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from chaos_agent.models.command_result import CommandResult
        from chaos_agent.tools.guard_feedback import GuardFeedback
        from chaos_agent.transports.base import TransportTarget
        from chaos_agent.transports.executor import execute_via_transport

        guard = MagicMock()
        guard.evaluate.return_value = GuardFeedback(allowed=True)
        run = AsyncMock(return_value=CommandResult(0, "exit_code: 0\nok", ""))
        target = TransportTarget(
            scope="k8s", channel_override="kubewiz_k8s",
            kubewiz_cluster_uuid="u", kubewiz_profile="p",
        )

        with patch("chaos_agent.tools.shell.get_tool_guard", return_value=guard), \
             patch("chaos_agent.tools.shell.run_command", run):
            await execute_via_transport(
                ["uptime"], target, task_id="task-obs",
                skip_guard=True, source="host-read",
            )

        kwargs = run.await_args.kwargs
        assert kwargs["channel"] == "kubewiz_k8s"
        # The semantic label is preserved rather than replaced.
        assert kwargs["source"] == "host-read"
        # And the command handed down is the WRAPPED one, so ``run_command``
        # can report ``wiz`` as the binary that actually executed.
        assert run.await_args.args[0][0].endswith("wiz")

    def test_status_detail_records_executed_binary_and_channel(self):
        import asyncio
        from unittest.mock import MagicMock, patch

        from chaos_agent.tools import shell

        events = []

        class _Tracker:
            task_id = "task-obs"

            def emit(self, event):
                events.append(event)

        guard = MagicMock()
        guard.evaluate.return_value = MagicMock(allowed=True)

        with patch.object(shell, "get_tracker", return_value=_Tracker()), \
             patch.object(shell, "get_tool_guard", return_value=guard):
            try:
                asyncio.run(shell.run_command(
                    ["/opt/bin/wiz", "task", "exec"], task_id="task-obs",
                    skip_guard=True, source="host-read", channel="kubewiz_k8s",
                ))
            except Exception:
                pass  # the subprocess itself is irrelevant here

        started = [e for e in events if e.detail.get("command")]
        assert started, "no start event emitted"
        detail = started[0].detail
        assert detail["channel"] == "kubewiz_k8s"
        assert detail["executed_binary"] == "wiz"
        # The caller's semantic label survives for the TUI.
        assert started[0].source == "host-read"


class TestOneBrokenChannelCannotBreakResolution:
    """``resolve`` polls EVERY channel on EVERY command.

    ``claims`` is consulted in a loop, so a channel that raises there would take
    transport down for channels that have nothing to do with it. It is treated as
    "claims nothing" — still reachable by explicit override, never auto-selected.
    """

    def test_raising_claims_is_skipped_not_propagated(self, caplog):
        from chaos_agent.transports.base import TransportTarget
        from chaos_agent.transports.registry import TransportRegistry, _CLAIMS_WARNED

        class _Broken:
            name = "broken"
            profile = "k8s"
            priority = 99  # would win if it claimed

            def claims(self, target):
                raise RuntimeError("third-party channel bug")

            def wrap_command(self, cmd, target, timeout=None):
                return cmd

            def adapt_result(self, result, target):
                return result

            def preflight(self, target):
                return []

            def display_command(self, cmd, target=None):
                return " ".join(cmd)

        _CLAIMS_WARNED.discard("broken")
        TransportRegistry.register(_Broken())
        try:
            resolved = TransportRegistry.resolve(TransportTarget(scope="k8s"))
        finally:
            TransportRegistry._channels.pop("broken", None)
            _CLAIMS_WARNED.discard("broken")

        assert resolved.name == "kubeconfig", (
            "the healthy catch-all must still win; a broken channel must not "
            "hijack resolution nor abort it"
        )
        assert "third-party channel bug" in caplog.text


class TestLocationSuffixKeepsTheDistinguishingPart:
    """Truncation must not erase WHICH machine.

    The accident's nodes were ``cn-shanghai-cloudspe.25.209.68.1`` and
    ``cn-shanghai-cloudspe.172.100.3.116``. Head-only truncation renders both as
    ``cn-shanghai-…``, so the suffix would be present and still useless — the
    exact gap it exists to close. What distinguishes a node is its tail.
    """

    def test_same_prefix_hosts_render_differently(self):
        from chaos_agent.transports.base import TransportTarget
        from chaos_agent.transports.registry import TransportRegistry

        channel = TransportRegistry.get("kubewiz_host")
        shown = [
            channel.display_command(["uptime"], TransportTarget(
                scope="host", host_name=host, kubewiz_profile="p",
                channel_override="kubewiz_host",
            ))
            for host in (
                "cn-shanghai-cloudspe.25.209.68.1",
                "cn-shanghai-cloudspe.172.100.3.116",
            )
        ]
        assert shown[0] != shown[1], (
            f"two different hosts render identically: {shown[0]!r}"
        )
        assert "25.209.68.1" in shown[0] and "100.3.116" in shown[1]
        # Still bounded, so the suffix cannot flood the line: "  [" + channel
        # name + " -> " + at most 25 chars of identity + "]".
        assert all(len(s) - len("uptime") <= 50 for s in shown), shown
