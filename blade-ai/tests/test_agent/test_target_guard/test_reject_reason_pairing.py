"""Every rejection carries a fix for ITS OWN cause — enforced structurally.

Two layers must agree for a rejection to be actionable:

  * ``classifier`` records WHY (``reject_detail``) and WHAT TO DO
    (``reject_suggestion``) at the point it observes the problem;
  * ``guard`` forwards both verbatim, falling back to a generic template only
    when neither was recorded.

A cause without its own fix is the dangerous shape: the guard then pairs it with
a template written for a DIFFERENT cause, and the two halves contradict each
other. task-866648cc is the cost — a rejection whose reason blamed the debug pod
while its suggestion said the command form was already fine. The model believed
the wrong half and burned nine minutes.

The meta-test below is the load-bearing one: it walks the classifier's AST so a
NEW reject site that forgets its fix fails immediately, instead of silently
inheriting someone else's advice. The scenario tests then confirm the pairing
survives the whole classifier → guard path.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from chaos_agent.agent.target_guard import (
    ApprovedTarget,
    infer_effective_target,
    target_drift_guard,
)
from chaos_agent.agent.target_guard import classifier as classifier_mod

# Bans with no drill form keep an EMPTY suggestion ON PURPOSE: guard_gateway
# reads that emptiness as "this is a boundary, not a reshapeable call" and
# reports DESTRUCTIVE_FLOOR. Filling it would silently downgrade a hard floor,
# so these are exempt from the completeness rule rather than fixed.
_INTENTIONALLY_NO_FIX = {
    "is explicitly banned",  # BANNED_KUBECTL_SUBS — currently only `certificate`
}


def _reject_sites() -> list[tuple[int, str, str, bool]]:
    """(lineno, scope, detail_source, has_suggestion) for every reject site."""
    tree = ast.parse(inspect.getsource(classifier_mod))
    sites = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "EffectiveTarget"
        ):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        scope = kw.get("scope")
        scope_name = scope.id if isinstance(scope, ast.Name) else ""
        if scope_name not in ("SCOPE_BANNED", "SCOPE_UNKNOWN", "SCOPE_ESCAPE"):
            continue
        detail = kw.get("reject_detail")
        sites.append((
            node.lineno,
            scope_name,
            ast.unparse(detail) if detail is not None else "",
            "reject_suggestion" in kw,
        ))
    return sites


class TestEveryCauseCarriesItsOwnFix:
    def test_reject_sites_are_discovered(self):
        # Guards the AST walk itself: if the classifier is refactored such that
        # this finds nothing, the completeness test below would pass vacuously.
        assert len(_reject_sites()) >= 20

    def test_every_reject_site_records_a_detail(self):
        missing = [
            (line, scope) for line, scope, detail, _ in _reject_sites() if not detail
        ]
        assert not missing, (
            f"reject sites with no reject_detail: {missing} — the guard would "
            "report only the verdict, leaving the model to guess the cause"
        )

    def test_every_reject_site_records_a_fix(self):
        missing = [
            (line, scope, detail[:70])
            for line, scope, detail, has_fix in _reject_sites()
            if not has_fix
            and not any(marker in detail for marker in _INTENTIONALLY_NO_FIX)
        ]
        assert not missing, (
            "reject sites with a cause but no fix:\n  "
            + "\n  ".join(f"L{line} {scope}: {detail}" for line, scope, detail in missing)
            + "\nEach must pair reject_detail with a reject_suggestion for THAT "
            "cause, or be listed in _INTENTIONALLY_NO_FIX with a reason."
        )


class TestPairingSurvivesToTheModel:
    """The fix that reaches the model must address the cause that fired."""

    @staticmethod
    def _approved() -> ApprovedTarget:
        return ApprovedTarget(
            scope="pod", namespace="ns", names=("p1",), blade_target="network",
        )

    def _verdict(self, tool: str, args: dict) -> tuple[str, str]:
        eff = infer_effective_target(tool, args)
        d = target_drift_guard(eff, self._approved())
        return d.reason, d.suggestion

    def test_unknown_tool_is_told_to_change_the_tool(self):
        # The failure mode being prevented: "state the target explicitly" sends
        # the model back to re-issue the same non-existent tool with more args.
        reason, fix = self._verdict("kubectl_apply_yaml", {"x": 1})
        assert "unrecognized tool" in reason
        assert "not a tool that exists" in fix
        assert "State the target explicitly" not in fix

    def test_unknown_subcommand_is_told_to_change_the_subcommand(self):
        reason, fix = self._verdict(
            "kubectl", {"subcommand": "annotate_all", "v_args": "pods"},
        )
        assert "unknown kubectl subcommand" in reason
        # The fix must point at the NAME, and must not hand over a hand-written
        # list of "supported" subcommands: the real set is ~37 entries and some
        # (apply, proxy) are banned outright or only under certain arguments, so
        # any copy here would either omit valid options or advertise rejected
        # ones. It points at the live `--help` instead, which cannot drift.
        assert "subcommand NAME" in fix
        assert "--help" in fix
        assert "no argument will help" in fix

    def test_missing_target_is_told_to_add_the_argument(self):
        reason, fix = self._verdict(
            "kubectl", {"subcommand": "exec", "v_args": "-n ns -- ls"},
        )
        assert "names no pod" in reason
        assert "Add the missing positional argument" in fix
        # Explicitly a form issue, so the model does not read it as a dead end.
        assert "not a blocked target" in fix

    def test_ambiguous_kind_is_told_to_qualify_it(self):
        reason, fix = self._verdict(
            "kubectl", {"subcommand": "patch", "v_args": "myapp -n ns -p {}"},
        )
        assert "neither a resource kind nor a name" in reason
        assert "<kind>/<name>" in fix

    def test_missing_subcommand_is_told_where_to_put_it(self):
        reason, fix = self._verdict("kubectl", {"subcommand": "", "v_args": "-n ns"})
        assert "subcommand" in reason
        assert "subcommand first, before its flags" in fix

    @pytest.mark.parametrize("tool,args", [
        ("kubectl_apply_yaml", {"x": 1}),
        ("kubectl", {"subcommand": "annotate_all", "v_args": "pods"}),
        ("kubectl", {"subcommand": "exec", "v_args": "-n ns -- ls"}),
        ("kubectl", {"subcommand": "", "v_args": "-n ns"}),
    ])
    def test_no_unknown_rejection_falls_back_to_the_generic_template(self, tool, args):
        # The fallback exists for causes nobody recorded. Reaching it on a cause
        # the classifier DID record means the pairing broke somewhere between the
        # two layers.
        _, fix = self._verdict(tool, args)
        assert "State the target explicitly so it can be checked" not in fix


class TestSuggestedFormsActuallyPass:
    """Anything a suggestion tells the model to write must SURVIVE the guard.

    This is the sharpest failure mode in the whole rejection path, and it is not
    hypothetical — it was found here. Two forms recommended by the recoverability
    hint were rejected by the very check that produced the hint:

      * ``systemd-run --on-active=Ns <inverse>`` on its own — the assessment
        needs a forward mutation AND its inverse, so a lone timer fails;
      * ``rm`` as a disk reclaim — the rule is explicitly "truncate/fallocate,
        never rm", and ``rm`` additionally disqualifies the whole command from
        being classified into a fault family at all.

    A model that copies such an example is rejected a second time while believing
    it complied. That is worse than a vague hint: it burns a turn AND destroys
    the model's trust in the feedback. So the examples are asserted executable,
    and the per-family wording is forwarded from ``recoverability.assess``
    instead of being restated by hand anywhere upstream.
    """

    @pytest.mark.parametrize("family,command", [
        # Paired mutation + inverse behind a sleep — the primary recommended form.
        ("network",
         "chroot /host sh -c 'iptables -I OUTPUT -j DROP && sleep 60 && "
         "iptables -D OUTPUT -j DROP'"),
        ("network",
         "chroot /host sh -c 'tc qdisc add dev eth0 root netem loss 100% && "
         "sleep 60 && tc qdisc del dev eth0 root'"),
        # The systemd-run variant, in the shape the suggestion now specifies:
        # forward mutation FIRST, timer carrying the inverse.
        ("network",
         "chroot /host sh -c 'iptables -I OUTPUT -j DROP && "
         "systemd-run --on-active=60s iptables -D OUTPUT -j DROP'"),
        # Disk reclaim as the hint words it (truncate, never rm).
        ("disk",
         "chroot /host sh -c 'fallocate -l 1G /tmp/fill && sleep 60 && "
         "truncate -s 0 /tmp/fill'"),
        # Process suspend/resume pairing.
        ("process",
         "chroot /host sh -c 'kill -STOP 4242 && sleep 60 && kill -CONT 4242'"),
    ])
    def test_recommended_form_is_accepted(self, family, command):
        from chaos_agent.agent.target_guard.recoverability import assess

        result = assess(command, family)
        assert result.recoverable, (
            f"a form the guard recommends is rejected by the guard: {command}\n"
            f"missing: {result.missing}"
        )

    @pytest.mark.parametrize("family,command", [
        # The two forms that were wrongly recommended — kept as tests so the
        # wording cannot drift back to them.
        ("network", "chroot /host systemd-run --on-active=60s iptables -D OUTPUT -j DROP"),
        ("disk",
         "chroot /host sh -c 'fallocate -l 1G /tmp/fill && sleep 60 && "
         "rm -f /tmp/fill'"),
    ])
    def test_previously_miswritten_forms_are_still_rejected(self, family, command):
        from chaos_agent.agent.target_guard.recoverability import assess

        assert not assess(command, family).recoverable

    @staticmethod
    def _llm_facing_texts() -> dict[str, str]:
        """Every constant whose text is forwarded to the model, by qualified name.

        Scoped to the ``_FIX_*`` / ``_SUGGEST_*`` constants ON PURPOSE. An
        earlier version of these checks scanned whole source files and tripped
        on a COMMENT that documents this very bug — comments must stay free to
        describe a rejected form, while the strings the model reads must not
        recommend one.
        """
        import inspect

        from chaos_agent.agent.target_guard import carriers, classifier, guard

        texts: dict[str, str] = {}
        for module in (carriers, classifier, guard):
            for name, value in vars(module).items():
                if not isinstance(value, str):
                    continue
                if name.startswith(("_FIX_", "_SUGGEST_")):
                    texts[f"{module.__name__}.{name}"] = value
        # guard.py builds its ESCAPE fallback inline rather than as a constant,
        # so pull the real rendered text through the guard itself.
        from chaos_agent.agent.target_guard import (
            ApprovedTarget,
            ConfidenceLevel,
            EffectiveTarget,
            target_drift_guard,
        )
        from chaos_agent.agent.target_guard.classifier import SCOPE_ESCAPE

        decision = target_drift_guard(
            EffectiveTarget(
                scope=SCOPE_ESCAPE, namespace="",
                confidence=ConfidenceLevel.UNKNOWN,
                raw_command="chroot /host iptables -I OUTPUT -j DROP",
            ),
            ApprovedTarget(scope="node", namespace="", names=("node-a",)),
        )
        texts["guard.escape_fallback_suggestion"] = decision.suggestion
        assert len(texts) >= 8, "constant discovery found suspiciously few texts"
        return texts

    def test_no_llm_facing_text_recommends_a_lone_timer(self):
        """No example may START with the timer — the forward mutation comes first.

        The distinction is the whole bug: ``<mutation> && systemd-run
        --on-active=Ns <inverse>`` passes, while a snippet that OPENS with
        ``systemd-run`` has no forward mutation and fails.
        """
        for where, text in self._llm_facing_texts().items():
            assert "`systemd-run" not in text, (
                f"{where} shows an example opening with systemd-run; "
                "recoverability.assess rejects a timer with no forward mutation"
            )

    def test_no_llm_facing_text_recommends_rm_as_a_reclaim(self):
        for where, text in self._llm_facing_texts().items():
            assert "rm -f" not in text and "&& rm " not in text, (
                f"{where} recommends rm as a reclaim; the disk rule is "
                "truncate -s 0 / fallocate -d, and rm also disqualifies the "
                "command from any fault family"
            )

    def test_no_llm_facing_text_hardcodes_a_supported_subcommand_list(self):
        """A hand-written "supported subcommands" list cannot stay true.

        The real set is the union of READONLY_KUBECTL_SUBS and
        DESTRUCTIVE_KUBECTL_SUBS (~37 entries), and membership is not the same
        as reachability: ``proxy`` is banned outright and ``apply`` is banned
        once it carries ``-f``. So a copied list either omits valid options
        (the model then avoids ``logs`` / ``top`` / ``events``, which do work)
        or advertises ones that are always refused. Either way it drifts the
        moment the sets change, silently.
        """
        from chaos_agent.agent.target_guard.classifier import (
            DESTRUCTIVE_KUBECTL_SUBS,
            READONLY_KUBECTL_SUBS,
        )

        real = READONLY_KUBECTL_SUBS | DESTRUCTIVE_KUBECTL_SUBS
        for where, text in self._llm_facing_texts().items():
            named = {sub for sub in real if f" {sub} " in f" {text} "}
            # A couple of names double as prose ("get", "set", "run"), so the
            # signal is a LIST of them, not any single mention.
            assert len(named) < 5, (
                f"{where} enumerates {len(named)} kubectl subcommands "
                f"({sorted(named)}). Point at the live `--help` instead — the "
                "authoritative set is ~37 entries and partly argument-dependent."
            )


class TestCapabilityRefusalNamesTheProfiles:
    """The capability gate must name the profiles, not restate its own verdict.

    Its old wording — "tool is unavailable for the current environment
    capability profile" / "Use only tools bound for the current environment" —
    was the SAME sentence for every tool in every profile. It named neither the
    profile in force, nor the profile the tool belongs to, nor what to use
    instead, so "only tools bound" is unactionable: the model cannot see the
    binding. This was the last pure template left in the guard stack.
    """

    _OLD_REASON = "tool is unavailable for the current environment capability profile"
    _OLD_FIX = "Use only tools bound for the current environment."

    def test_cross_profile_tool_names_both_profiles(self):
        from chaos_agent.agent.capabilities import explain_tool_refusal

        # host_inject belongs to the host profile; a kubeconfig puts us on k8s.
        reason, fix = explain_tool_refusal(
            "host_inject", {"kubeconfig": "/tmp/kc"}, "execute",
        )
        assert "host_inject" in reason
        assert "host" in reason and "k8s" in reason
        assert reason != self._OLD_REASON
        # And the fix must enumerate what IS reachable, so the model has a move.
        assert "k8s" in fix
        assert "kubectl" in fix
        assert fix != self._OLD_FIX

    def test_fix_states_that_arguments_cannot_help(self):
        from chaos_agent.agent.capabilities import explain_tool_refusal

        _, fix = explain_tool_refusal(
            "host_inject", {"kubeconfig": "/tmp/kc"}, "execute",
        )
        # Without this the model retries the same tool with more arguments —
        # the retry loop the guard is meant to break.
        assert "cannot make it reachable" in fix

    def test_unregistered_phase_is_reported_as_a_wiring_bug(self):
        from chaos_agent.agent.capabilities import explain_tool_refusal

        reason, fix = explain_tool_refusal(
            "host_inject", {"kubeconfig": "/tmp/kc"}, "no-such-phase",
        )
        assert "unregistered phase" in reason
        # The model cannot repair a screener wiring error; saying "pick another
        # tool" would send it chasing a fix that does not exist.
        assert "internal wiring error" in fix

    def test_unknown_tool_degrades_to_the_generic_pair(self):
        from chaos_agent.agent.capabilities import explain_tool_refusal

        # Not provider-owned → the capability gate would have ALLOWED it, so a
        # refusal came from elsewhere and this layer must not invent a cause.
        reason, fix = explain_tool_refusal(
            "some_graph_control_tool", {"kubeconfig": "/tmp/kc"}, "execute",
        )
        assert reason == self._OLD_REASON
        assert fix == self._OLD_FIX

    @pytest.mark.asyncio
    async def test_screener_surfaces_the_named_profiles(self):
        # End-to-end through the node the model actually receives from.
        import asyncio  # noqa: F401  (marker needs an async test)

        from langchain_core.messages import AIMessage

        from chaos_agent.agent.nodes.planning.tool_screener import tool_screener
        from chaos_agent.agent.target_guard import freeze_approved_target
        from chaos_agent.config.settings import settings

        orig = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            state = {
                "messages": [AIMessage(content="", tool_calls=[{
                    "name": "host_inject",
                    "args": {"command": "iptables -A OUTPUT -j DROP"},
                    "id": "c1",
                }])],
                "approved_target": freeze_approved_target(
                    target={"namespace": "", "names": ["node-a"]},
                    params={"scope": "node"},
                    blade_scope="node", blade_target="network", blade_action="drop",
                ),
                "execution_artifacts": [],
                "task_id": "t1",
                "kubeconfig": "/tmp/kc",
            }
            delta = await tool_screener(state)
        finally:
            settings.target_guard_enforcing = orig

        msg = str(delta["messages"][0].content)
        assert "is provided for the host profile" in msg
        assert "environment in force is 'k8s'" in msg
        assert self._OLD_REASON not in msg


class TestLedgerFactsAreNotStatedAsObservedFacts:
    """A record ABOUT the carrier must not read as the carrier's live state.

    ``execution_artifacts`` keeps two different things on a debug-pod artifact:

      * ``status`` — this Agent's own lifecycle ledger: ``active`` / ``cleaned``
        / ``failed`` / ``recovery_armed``. None of these are Kubernetes words.
      * ``phase``  — the pod's real Kubernetes phase (``Running``, ``Pending``).

    ``privileged`` / ``target`` are likewise snapshots taken when the pod was
    created, not live reads.

    Wording that presents the ledger as the pod's current property ("pod X is in
    status recovery_armed", "pod X is not a privileged container") sends the
    model to ``kubectl get pod X`` / ``-o jsonpath={.spec...securityContext}``,
    where it sees ``Running`` and ``privileged: true`` and concludes the guard is
    wrong about its own pod. task-866648cc spent its final turns on exactly those
    two lookups. So a rejection built from the ledger has to say it is reading a
    record — the guard is not entitled to assert a live fact it never read.
    """

    NODE = "node-a"
    POD = "node-debugger-node-a-abc12"

    @staticmethod
    def _approved():
        from chaos_agent.agent.target_guard import ApprovedTarget

        return ApprovedTarget(
            scope="node", namespace="", names=("node-a",), blade_target="network",
        )

    def _artifact(self, **overrides):
        artifact = {
            "artifact_id": "uid-1", "type": "debug_pod", "status": "active",
            "task_id": "task-1", "name": self.POD, "namespace": "kubewiz",
            "uid": "uid-1", "target": {"scope": "node", "name": self.NODE},
            "operation_family": "network", "privileged": True,
            # The real K8s phase says the pod is perfectly healthy — which is
            # what makes a ledger-as-live-state message misleading.
            "phase": "Running",
        }
        artifact.update(overrides)
        return artifact

    def _reject(self, artifact, host_command="chroot /host iptables -I OUTPUT -j DROP"):
        from chaos_agent.agent.target_guard.carriers import (
            effective_target_from_registered_carrier,
        )

        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {"subcommand": "exec", "v_args": f"{self.POD} -n kubewiz -- {host_command}"},
            [artifact],
            self._approved(),
        )
        assert not resolution.resolved, "expected a rejection for this artifact"
        return resolution.detail

    def test_recovery_armed_says_it_is_reading_a_record(self):
        detail = self._reject(
            self._artifact(status="recovery_armed", recovery_deadline_epoch=9e12),
        )
        assert "this task's record" in detail
        # And it must disclaim the Kubernetes reading explicitly, because
        # ``recovery_armed`` looks like it could be one.
        assert "not the pod's Kubernetes phase" in detail
        # The old phrasing presented the ledger as the pod's own state.
        assert "pod 'node-debugger-node-a-abc12' is in status" not in detail

    def test_unprivileged_says_when_the_fact_was_recorded(self):
        detail = self._reject(self._artifact(privileged=False))
        assert "recorded" in detail
        assert "when it was created" in detail
        # Not an unqualified present-tense claim about the live container.
        assert "is not a privileged container, so it" not in detail

    def test_missing_node_binding_says_it_is_the_record_that_lacks_it(self):
        detail = self._reject(
            self._artifact(target={"scope": "pod", "name": self.NODE}),
        )
        assert "this task's record" in detail
        assert "recorded scope=" in detail

    def test_ledger_words_never_appear_unqualified(self):
        """Any rejection naming a ledger value must also name it as a record.

        Parameterising over the ledger states keeps a future state (say
        ``expired``) from being added with the old unqualified phrasing.
        """
        for status in ("cleaned", "failed", "recovery_armed"):
            artifact = self._artifact(status=status)
            if status == "recovery_armed":
                artifact["recovery_deadline_epoch"] = 9e12
            detail = self._reject(artifact)
            if status in detail:
                assert "record" in detail, (
                    f"detail names the ledger state {status!r} without saying it "
                    f"is a record: {detail}"
                )


class TestFixesDoNotSetUpASecondRejection:
    """A true reason that hides the NEXT gate still costs a round trip.

    Short-circuiting is correct — the checks are ordered and later ones depend
    on earlier results — but it means only the first violated gate is reported.
    For CARRIER gates that is fine: privileged / node-binding / lifecycle all
    resolve the same way (rebuild the carrier), so naming one is naming them all.

    For COMMAND gates it is not. Family and self-recovery describe two
    independent properties of the SAME string, and a model that fixes only the
    family is rejected again immediately. Measured here: ``chroot /host
    fallocate`` (a disk mutation, wrong family) → family_mismatch → the model
    switches to ``iptables -I OUTPUT -j DROP`` exactly as advised →
    no_bounded_recovery. Two turns for one edit, and every extra turn on a
    stuck drill is what task-866648cc was about. (A read-only probe such as
    ``crictl ps`` never reaches this gate — the screener admits it via the
    read-only fast path; only a MUTATION of the wrong family collides here.)

    So the family fix has to disclose the second requirement, without inventing
    the concrete form (the per-family wording belongs to
    ``recoverability.assess`` and is emitted by that gate when it fires).
    """

    @staticmethod
    def _approved():
        from chaos_agent.agent.target_guard import ApprovedTarget

        return ApprovedTarget(
            scope="node", namespace="", names=("node-a",), blade_target="network",
        )

    @staticmethod
    def _artifact():
        return {
            "artifact_id": "u1", "type": "debug_pod", "status": "active",
            "task_id": "t1", "name": "dbg", "namespace": "kubewiz", "uid": "u1",
            "target": {"scope": "node", "name": "node-a"},
            "operation_family": "network", "privileged": True, "phase": "Running",
        }

    def _resolve(self, host_command):
        from chaos_agent.agent.target_guard.carriers import (
            effective_target_from_registered_carrier,
        )

        return effective_target_from_registered_carrier(
            "kubectl",
            {"subcommand": "exec", "v_args": f"dbg -n kubewiz -- {host_command}"},
            [self._artifact()],
            self._approved(),
        )

    def test_family_fix_discloses_the_self_recovery_requirement(self):
        from chaos_agent.agent.target_guard.carriers import CarrierRejectReason

        first = self._resolve("chroot /host fallocate -l 1G /tmp/fill")
        assert first.reason is CarrierRejectReason.FAMILY_MISMATCH
        # Following this advice literally leads straight into the next gate, so
        # the advice must say so up front.
        assert "SECOND requirement" in first.suggestion
        assert "self-recover" in first.suggestion

    def test_the_second_collision_is_real(self):
        """Documents the collision the disclosure above exists to prevent."""
        from chaos_agent.agent.target_guard.carriers import CarrierRejectReason

        # Exactly what the family fix tells the model to switch to.
        second = self._resolve("chroot /host iptables -I OUTPUT -j DROP")
        assert second.reason is CarrierRejectReason.NO_BOUNDED_RECOVERY

    def test_doing_both_at_once_passes(self):
        """And the disclosed pair, applied together, clears in ONE turn."""
        resolution = self._resolve(
            "chroot /host sh -c 'iptables -I OUTPUT -j DROP && sleep 60 && "
            "iptables -D OUTPUT -j DROP'"
        )
        assert resolution.resolved, resolution.detail

    def test_no_fix_promises_that_the_call_will_be_allowed(self):
        """A fix may describe the next CHECK, never guarantee its OUTCOME.

        ``_FIX_NAME_THE_TARGET`` used to end "the same command with its target
        named will be evaluated normally". Literally true — it does get
        evaluated — but it reads as "and then it works", while naming a target
        outside the approval lands on REJECT_DRIFT. The guard may promise a
        comparison; it may not promise the verdict.
        """
        import re

        from chaos_agent.agent.target_guard import carriers, classifier, guard

        promise = re.compile(
            r"will be (accepted|allowed|permitted|evaluated normally)"
            r"|will pass|then it (works|passes)|guarantee",
            re.I,
        )
        for module in (carriers, classifier, guard):
            for name, value in vars(module).items():
                if not isinstance(value, str):
                    continue
                if not name.startswith(("_FIX_", "_SUGGEST_")):
                    continue
                found = promise.search(value)
                assert not found, (
                    f"{module.__name__}.{name} promises an outcome "
                    f"({found.group()!r}); state the next check instead"
                )
