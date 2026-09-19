"""``kubectl set`` was unclassifiable, and refusals did not say why.

Both defects had the same signature: a call the guard could not parse became
``SCOPE_UNKNOWN``, which ``target_drift_guard`` turns into ``REJECT_UNKNOWN`` and
the screener turns into a fabricated error ToolMessage. The call never ran, and
the advice the model received ("State the target explicitly") was unactionable
because the target WAS explicit. Reissuing the same call is the only move that
advice suggests, which is the loop shape we keep chasing.

  1. ``kubectl set <sub-resource>`` — ``set`` is the only whitelisted write verb
     whose first positional is the FIELD, not the resource. The generic resource
     classifier read ``image`` as the kind, ``_is_known_kind`` rejected it, and
     every ``kubectl set`` call became unclassifiable — while ``set`` sits in
     BOTH ``ToolGuard.KUBECTL_ALLOWED_SUBCOMMANDS`` and
     ``K8sNativeProvider.inject_kubectl_subcommands``, i.e. gate ① runs it and
     the provider calls it an injection carrier. That is the
     "unexecutable-by-construction" combination the whitelist's own docstring
     warns about.

  2. Refusals with an empty ``reject_detail``. ``target_drift_guard`` falls back
     to echoing the raw command, so the model learned THAT parsing failed but
     never WHICH argument was missing. All 16 ``SCOPE_UNKNOWN`` construction
     sites in the classifier now name their own cause.

Cluster-verified before changing anything (kubewiz channel, pre cluster):
  - ``kubectl set image <deploy> <container>=<img> --dry-run=client -o name`` →
    exit 0, target resolved — so kubectl accepts the form the guard refused.
  - ``kubectl set <deploy> --dry-run=client`` (no sub-resource) → exit 1,
    ``error: unknown flag: --dry-run`` — so the bare form is NOT a valid
    operation and UNKNOWN remains the right verdict for it.

Not covered here, on purpose: stacked short flags (``kubectl exec -it <pod>``).
A rule admitting them was written, verified and then reverted — see
``_is_valueless_flag``'s docstring for why. ``-it`` therefore still lands in
UNKNOWN, but now with a reason attached.
"""

import pytest

from chaos_agent.agent.target_guard import (
    SCOPE_BANNED,
    freeze_approved_target,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.classifier import (
    SCOPE_UNKNOWN,
    iter_flag_assignments,
)
from chaos_agent.agent.execution_artifacts import (
    _RECOVERY_CARRIER_CREATE_KINDS,
)
from chaos_agent.agent.target_guard.freeze import approved_from_dict
from chaos_agent.agent.target_guard.guard import target_drift_guard
from chaos_agent.agent.target_guard.types import GuardVerdict
from chaos_agent.config.settings import settings


def classify(subcommand: str, v_args: str):
    return infer_effective_target(
        "kubectl", {"subcommand": subcommand, "v_args": v_args}
    )


# ---------------------------------------------------------------------------
# 1. kubectl set <sub-resource>
# ---------------------------------------------------------------------------


class TestKubectlSet:
    @pytest.mark.parametrize("subresource", [
        "image", "env", "resources", "serviceaccount", "sa", "subject", "selector",
    ])
    def test_subresource_is_stripped_and_target_resolved(self, subresource):
        eff = classify("set", f"{subresource} deployment/demo -n arms-prom app=v")
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)
        assert eff.namespace == "arms-prom"

    def test_the_documented_injection_command(self):
        """``Pod_镜像拉取失败_容器镜像被篡改`` kubectl-native fallback, injection."""
        eff = classify(
            "set", "image deployment/demo -n arms-prom app=nginx:non-existent-tag"
        )
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)

    def test_the_documented_recovery_command(self):
        """Same case, recovery — the reverse of the line above."""
        eff = classify("set", "image deployment/demo -n arms-prom app=nginx:1.21")
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)

    def test_subresource_after_a_flag(self):
        """kubectl accepts the flag first; the sub-resource is not positional-fixed."""
        eff = classify("set", "-n arms-prom image deployment/demo app=nginx:x")
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)
        assert eff.namespace == "arms-prom"

    def test_two_positional_kind_name_form(self):
        eff = classify("set", "image deployment demo -n arms-prom app=nginx:x")
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)

    def test_wrong_target_still_carries_its_identity(self):
        """Drift detection needs the name the call ACTUALLY addresses."""
        eff = classify("set", "image deployment/OTHER -n arms-prom app=nginx:x")
        assert eff.names == ("OTHER",)

    def test_unknown_subresource_is_refused(self):
        eff = classify("set", "bogus deployment/demo -n arms-prom")
        assert eff.scope == SCOPE_UNKNOWN

    def test_bare_set_without_subresource_is_refused(self):
        """Cluster-verified as not a valid operation (exit 1, unknown flag)."""
        eff = classify("set", "deployment/demo -n arms-prom")
        assert eff.scope == SCOPE_UNKNOWN


class TestNameLessSelectionFailsClosed:
    """``--all`` / ``-l`` forms must be REFUSED, never allowed with a wrong target.

    A known limitation of the shared resource classifier, inherited rather than
    introduced: with no explicit resource name, ``positionals[1]`` falls on the
    trailing ``key=value`` argument and becomes the "name". Measured on the
    pre-existing verbs too — ``kubectl label deployments --all -n ns k=v`` yields
    ``names=('k=v',)`` — so this is not specific to ``set``.

    It is not fixed here because it predates this change and touches verbs outside
    its scope. What matters is the DIRECTION of the error: a bogus name mismatches
    the approved one, so the drift guard refuses. These tests pin that direction.
    Refusing ``--all`` is also the right answer on its own terms — its blast radius
    is unbounded, which is precisely what an approved target is supposed to bound.

    If someone later "fixes" the name parsing, this class must keep passing: the
    verdict may become a clearer rejection, but it must never become ALLOW.
    """

    APPROVED = {
        "scope": "deployment", "namespace": "ns",
        "names": ["demo"], "fault_target": "pod",
    }

    def _verdict(self, subcommand: str, v_args: str):
        from chaos_agent.agent.target_guard.freeze import approved_from_dict
        from chaos_agent.agent.target_guard.guard import target_drift_guard

        effective = classify(subcommand, v_args)
        return target_drift_guard(effective, approved_from_dict(self.APPROVED)).verdict

    @pytest.mark.parametrize(("subcommand", "v_args"), [
        ("set", "image deployments --all -n ns app=nginx:x"),
        ("set", "image deployment -l app=demo -n ns app=nginx:x"),
        ("set", "env deployments --all -n ns KEY=v"),
        # Pre-existing verbs with the same shape — the limitation is shared.
        ("label", "deployments --all -n ns k=v"),
        ("annotate", "deployments --all -n ns k=v"),
    ])
    def test_nameless_selection_is_never_allowed(self, subcommand, v_args):
        assert self._verdict(subcommand, v_args) is not GuardVerdict.ALLOW

    def test_the_explicit_form_is_still_allowed(self):
        """Control: the limitation must not make legitimate calls unusable."""
        assert self._verdict(
            "set", "image deployment/demo -n ns app=nginx:x"
        ) is GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# 2. Every refusal names its own cause
# ---------------------------------------------------------------------------


class TestUnknownVerdictsCarryAReason:
    """``REJECT_UNKNOWN`` with an empty detail is a dead end with no lead.

    ``target_drift_guard`` falls back to echoing the raw command, so the model
    was told THAT parsing failed but never WHICH argument was missing — and the
    generic suggestion asks it to state a target that was already stated. These
    assert a cause exists and names the missing piece, not its exact wording.
    """

    @pytest.mark.parametrize(("subcommand", "v_args", "needle"), [
        ("set", "bogus deployment/demo -n arms-prom", "sub-resource"),
        ("set", "-n arms-prom", "sub-resource"),
        ("debug", "--image=x -n arms-prom", "node/"),
        ("cordon", "", "node"),
        ("patch", "--type=merge -p {}", "kind"),
        ("taint", "nodes", "node"),
        ("exec", "-n arms-prom -- ls /", "pod"),
        ("frobnicate", "whatever", "subcommand"),
    ])
    def test_detail_is_present_and_points_at_the_gap(
        self, subcommand, v_args, needle,
    ):
        eff = classify(subcommand, v_args)
        assert eff.scope == SCOPE_UNKNOWN
        detail = (eff.reject_detail or "").strip()
        assert detail, f"kubectl {subcommand} {v_args} refused with no reason"
        assert needle in detail, f"detail does not mention {needle!r}: {detail!r}"

    def test_unknown_set_subresource_lists_the_valid_ones(self):
        """A refusal the model can act on names the accepted values."""
        eff = classify("set", "bogus deployment/demo -n arms-prom")
        detail = eff.reject_detail or ""
        assert "image" in detail
        assert "env" in detail


# ---------------------------------------------------------------------------
# v_args flag-normalization family (review rounds 6.9-6.11)
# ---------------------------------------------------------------------------


class TestVArgsFlagNormalizationFamily:
    """Every v_args consumer now reads ONE pflag-normalised expansion.

    Until this family landed, each consumer (``parse_namespace``,
    ``parse_labels``, ``_uses_file_input``, the widening gate) hand-rolled
    its own token matching, and every hand-rolled copy had the same blind
    spots — pflag combined shorthands. The probes were not cosmetic:

      - G2b: ``pod mypod -nprod`` (approved ns=default) classified against
        "default" and PASSED while kubectl executes ns=prod;
      - G6: ``-n prod pod mypod -nother`` classified against the FIRST -n
        and passed while kubectl's pflag lets the LAST one win;
      - G8: ``pods -lapp=evil`` selector invisible to the guard;
      - P8: ``-f /tmp/evil.yaml`` + a compliant stdin manifest classified
        the DECOY stdin as the call's content while kubectl reads the file;
      - G4: ``-f-`` (= ``-f -``, probe-verified) refused as "no -f flag".

    All five closed by routing every consumer through
    ``iter_flag_assignments`` plus a namespace-consistency gate and a
    stdin-filename discriminator. The widening gate (6.9/6.10) was
    refactored onto the same expansion with verdicts unchanged.
    """

    _IMAGE = "registry.example.com/probe:v1"
    _MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: drill-t
  namespace: prod
spec:
  replicas: 1
  selector:
    matchLabels:
      app: drill-t
  template:
    metadata:
      labels:
        app: drill-t
    spec:
      containers:
      - name: target
        image: registry.example.com/probe:v1
        command: ["sleep", "7200"]
"""

    @pytest.fixture(autouse=True)
    def _image_allowlist(self):
        orig = settings.recovery_carrier_allowed_images
        settings.recovery_carrier_allowed_images = self._IMAGE
        yield
        settings.recovery_carrier_allowed_images = orig

    @staticmethod
    def _approved(namespace, names=None, labels=None):
        target = {"namespace": namespace}
        if names is not None:
            target["names"] = names
        if labels is not None:
            target["labels"] = labels
        return approved_from_dict(freeze_approved_target(
            target=target,
            params={"scope": "pod"},
            fault_scope="pod",
            fault_target="pod",
            fault_action="fill",
        ))

    @staticmethod
    def _verdict(v_args, approved, subcommand="delete"):
        eff = infer_effective_target(
            "kubectl", {"subcommand": subcommand, "v_args": v_args}
        )
        return target_drift_guard(eff, approved).verdict

    # -- expansion unit matrix ------------------------------------------

    @pytest.mark.parametrize(("tokens", "expected"), [
        ("-n prod", [("--namespace", "prod")]),
        ("--namespace prod", [("--namespace", "prod")]),
        ("--namespace=prod", [("--namespace", "prod")]),
        ("-n=prod", [("--namespace", "prod")]),
        ("-nprod", [("--namespace", "prod")]),
        ("-nw", [("--namespace", "w")]),
        ("-nApp", [("--namespace", "App")]),
        ("-An prod", [("--all-namespaces", None), ("--namespace", "prod")]),
        ("-Anw", [("--all-namespaces", None), ("--namespace", "w")]),
        ("-A=false", [("--all-namespaces", "false")]),
        ("-f -", [("--filename", "-")]),
        ("-f-", [("--filename", "-")]),
        ("--filename=-", [("--filename", "-")]),
        ("-fdir/x.yaml", [("--filename", "dir/x.yaml")]),
        ("-Rf x.yaml", [("-R", None), ("--filename", "x.yaml")]),
        ("--prune-allowlist core/v1.X", [("--prune-allowlist", "core/v1.X")]),
        ("-lapp=evil", [("--selector", "app=evil")]),
        # pflag consumes the next arg UNCONDITIONALLY as a value
        # shorthand's value — even flag-shaped or the ``--`` separator
        # (probe-verified: ``get pods -n -A`` → ns "-A").
        ("-n -A", [("--namespace", "-A")]),
        ("-f -A", [("--filename", "-A")]),
    ])
    def test_expansion_matrix(self, tokens, expected):
        got = iter_flag_assignments(tokens.split())
        assert [(n, v) for n, v, _origin in got] == expected

    def test_expansion_stops_at_separator(self):
        """``--`` is the exec boundary: inner flags must not leak out."""
        assert iter_flag_assignments(["exec", "pod", "--", "-n", "inner"]) == []

    def test_expansion_positionals_are_skipped(self):
        assert iter_flag_assignments(["pod", "mypod"]) == []

    # -- G2/G2b: glued namespace becomes anchorable ---------------------

    def test_glued_namespace_matches_approved_namespace(self):
        approved = self._approved("prod", names=["mypod"])
        verdict = self._verdict("pod mypod -nprod", approved)
        assert verdict == GuardVerdict.ALLOW

    def test_glued_namespace_drifts_from_default(self):
        approved = self._approved("default", names=["mypod"])
        verdict = self._verdict("pod mypod -nprod", approved)
        assert verdict == GuardVerdict.REJECT_DRIFT

    # -- G6: conflicting namespaces are a form issue ---------------------

    def test_conflicting_namespaces_is_form_issue(self):
        """kubectl's pflag lets the LAST -n win (probe-verified) — the guard
        must refuse to anchor on the first while kubectl runs the last."""
        eff = infer_effective_target(
            "kubectl",
            {"subcommand": "delete", "v_args": "-n prod pod mypod -nother"},
        )
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is False
        assert "conflicting --namespace" in (eff.reject_detail or "")
        assert "prod" in (eff.reject_detail or "")
        assert "other" in (eff.reject_detail or "")

    def test_duplicate_same_namespace_is_not_a_conflict(self):
        approved = self._approved("prod", names=["mypod"])
        verdict = self._verdict("-n prod pod mypod -n prod", approved)
        assert verdict == GuardVerdict.ALLOW

    # -- G8: glued selector is judged like the separated one --------------

    @pytest.mark.parametrize("v_args", ["pods -lapp=evil -n prod", "pods -l app=evil -n prod"])
    def test_glued_selector_drifts_from_approved_labels(self, v_args):
        approved = self._approved("prod", labels={"app": "x"})
        assert self._verdict(v_args, approved) == GuardVerdict.REJECT_DRIFT

    @pytest.mark.parametrize("v_args", ["pods -lapp=evil -n prod", "pods -l app=evil -n prod"])
    def test_glued_selector_matches_approved_labels(self, v_args):
        approved = self._approved("prod", labels={"app": "evil"})
        assert self._verdict(v_args, approved) == GuardVerdict.ALLOW

    # -- P8/G4: the -f value decides stdin vs decoy ----------------------

    def test_file_flag_with_stdin_data_is_a_decoy(self):
        """-f <file> plus stdin_data: kubectl reads the FILE — the stdin
        manifest must never be classified as the call's content."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f /tmp/evil.yaml",
            "stdin_data": self._MANIFEST,
        })
        assert eff.scope == SCOPE_BANNED
        assert "stdin_data" in (eff.reject_detail or "")
        assert "-f -" in (eff.reject_suggestion or "")

    @pytest.mark.parametrize("v_args", ["-f -", "-f-", "--filename -", "--filename=-"])
    def test_stdin_filename_spellings_reach_the_manifest_channel(self, v_args):
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": v_args,
            "stdin_data": self._MANIFEST,
        })
        assert eff.is_drill_target_manifest is True

    def test_prune_allowlist_absorbs_a_following_dash_f(self):
        """--prune-allowlist -f - is a DIFFERENT command: pflag absorbs
        -f as the allowlist value and kubectl itself fails with
        'Unexpected args: [-]' (probe-verified). UNKNOWN is the accurate
        verdict — the call has no usable filename flag left."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "--prune-allowlist -f -",
            "stdin_data": self._MANIFEST,
        })
        assert eff.scope == SCOPE_UNKNOWN
        assert "no '-f -' flag" in (eff.reject_detail or "")

    # -- absorbed-value re-scan (adversarial round, probe H1/H1c) -----

    def test_value_flag_absorbing_flag_shaped_token_has_no_phantom_flag(self):
        """-n -A is namespace "-A" — the consumed token must not be
        re-scanned as a flag. Re-scanning conjured a phantom
        --all-namespaces and false-rejected a compliant stdin call
        (probe: kubectl itself runs it — ``apply -n -A -f -`` reads
        stdin, "no objects passed to apply")."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-n -A -f -",
            "stdin_data": self._MANIFEST,
        })
        assert eff.is_drill_target_manifest is True

    def test_absorbed_separator_does_not_hide_later_flags(self):
        """-l -- absorbs "--" as the selector VALUE; scanning must
        continue past it, not stop as if it were the exec boundary —
        otherwise every flag after it is invisible to the guard."""
        got = iter_flag_assignments(["pods", "-l", "--", "--all", "-n", "prod"])
        assert [(name, value) for name, value, _origin in got] == [
            ("--selector", "--"),
            ("--all", None),
            ("--namespace", "prod"),
        ]

    # -- repeatable -f / typed stdin_data (adversarial round, probe I1/I2) --

    def test_repeated_filename_mixing_stdin_and_file_is_a_decoy(self):
        """--filename is a REPEATABLE pflag (StringArray): ``-f - -f
        x.yaml`` applies BOTH sources (probe: dry-run created the stdin
        object AND the file object, in either flag order). Judging only
        the first value let the mix through — the guard audited the
        compliant stdin while kubectl also executed the file."""
        for v_args in ("-f - -f /tmp/evil.yaml", "-f /tmp/evil.yaml -f -"):
            eff = infer_effective_target("kubectl", {
                "subcommand": "apply",
                "v_args": v_args,
                "stdin_data": self._MANIFEST,
            })
            assert eff.scope == SCOPE_BANNED, v_args
            assert "stdin_data" in (eff.reject_detail or ""), v_args
            assert eff.is_drill_target_manifest is False

    def test_repeated_filename_all_stdin_still_reaches_manifest_channel(self):
        """Every -f value "-" is a legal stdin-only call (kubectl
        concatenates them; stdin is read once)."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f - -f -",
            "stdin_data": self._MANIFEST,
        })
        assert eff.is_drill_target_manifest is True

    def test_glued_repeated_filename_mix_is_a_decoy(self):
        """Glued spellings of the repetition decoy must classify the
        same as their separated forms."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f- -f/tmp/x.yaml",
            "stdin_data": self._MANIFEST,
        })
        assert eff.scope == SCOPE_BANNED

    @pytest.mark.parametrize("bad", [
        {"apiVersion": "apps/v1", "kind": "Deployment"},
        [{"apiVersion": "v1", "kind": "ConfigMap"}],
        None,
        12345,
    ])
    def test_structured_stdin_data_fails_closed_without_crashing(self, bad):
        """The tool schema declares stdin_data: str, but the model emits
        tool_call args as JSON — a manifest can arrive structured. The
        classifier must fail closed (probe I2: a dict raised a
        TypeError straight out of re.findall instead)."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f -",
            "stdin_data": bad,
        })
        assert eff.scope == SCOPE_UNKNOWN
        assert "YAML text string" in (eff.reject_detail or "")

    # -- regex-vs-parser divergence (adversarial round, probe J family) --

    def test_quoted_kind_key_is_not_invisible_to_the_guard(self):
        """A quoted key (``"kind": ClusterRole``) is legal YAML kubectl
        fully executes (probe: dry-run created the ConfigMap AND the
        ClusterRole), but the old ``^kind:`` regex could not see it —
        the guard whitelisted the ConfigMap and let the ClusterRole
        ride along. Extraction must be structural (safe_load_all),
        the same layer kubectl consults."""
        quoted = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: ok-cm\n"
            '---\n"apiVersion": "rbac.authorization.k8s.io/v1"\n'
            '"kind": ClusterRole\nmetadata:\n  name: evil-crb\n'
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-n default -f -",
            "stdin_data": quoted,
        })
        assert eff.scope == SCOPE_BANNED
        assert "non-whitelisted resource kind" in (eff.reject_detail or "")

    def test_json_document_in_a_multi_doc_manifest_is_seen(self):
        """JSON is a YAML subset: a whole JSON document in a multi-doc
        stdin bypassed the text regex the same way a quoted key did
        (probe: dry-run created configmap/ok AND the ClusterRole while
        the guard returned scope=configmap)."""
        mixed = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: ok\n"
            '---\n{"apiVersion": "rbac.authorization.k8s.io/v1", '
            '"kind": "ClusterRole", "metadata": {"name": "evil-json"}}\n'
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-n default -f -",
            "stdin_data": mixed,
        })
        assert eff.scope == SCOPE_BANNED

    def test_pure_json_manifest_classifies_instead_of_false_rejecting(self):
        """A pure-JSON manifest is legal kubectl input (probe: dry-run
        created the object); the text regex saw no ``kind:`` line and
        false-rejected it as 'no recognizable kind'. Structural
        extraction classifies it like the YAML spelling."""
        json_manifest = (
            '{"apiVersion": "v1", "kind": "ConfigMap", '
            '"metadata": {"name": "j2-cm", "namespace": "default"}}'
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-n default -f -",
            "stdin_data": json_manifest,
        })
        assert eff.scope == "configmap"
        assert eff.names == ("j2-cm",)
        assert eff.namespace == "default"

    def test_name_anchor_is_metadata_name_not_any_indented_name_key(self):
        """The old ``^\\s+name:`` regex matched the FIRST indented
        ``name:`` line, so a ConfigMap ``data.name`` key placed before
        ``metadata`` (legal field order in YAML) stole the resource
        name anchor while kubectl created the real ``metadata.name``
        (probe J5b)."""
        swapped = (
            "apiVersion: v1\nkind: ConfigMap\ndata:\n  name: fake-data-key\n"
            "metadata:\n  name: real-cm-name\n  namespace: default\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f -",
            "stdin_data": swapped,
        })
        assert eff.scope == "configmap"
        assert eff.names == ("real-cm-name",)

    # -- multi-doc anchor coverage (cascade round, probe K family) --

    def test_multi_doc_manifest_anchors_every_document_name(self):
        """kubectl creates EVERY document (probe: dry-run created both
        ConfigMaps), so the guard must anchor every document's name —
        the old first-doc-only anchor let doc 2 ride along with no
        name anchor at all while the guard ALLOWed on doc 1 alone."""
        two_docs = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: ok-cm\n  namespace: default\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: evil-cm\n  namespace: default\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f -",
            "stdin_data": two_docs,
        })
        assert eff.names == ("ok-cm", "evil-cm")

    def test_multi_doc_manifest_with_mixed_namespaces_is_a_form_issue(self):
        """kubectl resolves each doc's namespace individually (probe:
        ok-cm@default AND evil-cm@kube-system both created), and no
        single namespace value can anchor a mixed-namespace apply —
        the same legislation as the --namespace consistency gate."""
        mixed_ns = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: ok-cm\n  namespace: default\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: evil-cm\n  namespace: kube-system\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f -",
            "stdin_data": mixed_ns,
        })
        assert eff.scope == SCOPE_BANNED
        assert "mixes namespaces" in (eff.reject_detail or "")

    def test_multi_doc_explicit_implicit_ns_mix_is_a_form_issue(self):
        """Probe NS-MIX fail-open closure: a doc WITHOUT a namespace
        lands in "default" while its explicit sibling lands in its own
        — the anchor over-claimed the implicit doc into the explicit
        namespace, and the per-name reconciliation passed an
        in-contract check for an out-of-contract (default) write."""
        explicit_plus_implicit = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-a\n  namespace: prod\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-b\ndata:\n  k: v\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f -",
            "stdin_data": explicit_plus_implicit,
        })
        assert eff.scope == SCOPE_BANNED
        assert "namespace-less documents in \"default\"" in (eff.reject_detail or "")
        assert "--namespace" in (eff.reject_suggestion or "")

    def test_multi_doc_explicit_implicit_mix_passes_with_explicit_namespace(self):
        """The same manifest WITH ``-n prod`` is legal and correctly
        anchored: kubectl lets the namespace-less doc inherit the flag
        (only a CONFLICTING explicit doc namespace is rejected), so one
        namespace value still anchors the whole apply."""
        explicit_plus_implicit = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-a\n  namespace: prod\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-b\ndata:\n  k: v\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-n prod -f -",
            "stdin_data": explicit_plus_implicit,
        })
        assert eff.scope == "configmap"
        assert eff.namespace == "prod"
        assert eff.names == ("cm-a", "cm-b")

    def test_multi_doc_explicit_default_ns_with_implicit_doc_allows(self):
        """An explicit ``namespace: default`` doc beside an implicit one
        is NOT a mix: the implicit doc lands in "default" too, so the
        single anchored value is accurate and the apply stays legal."""
        explicit_default_plus_implicit = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-a\n  namespace: default\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-b\ndata:\n  k: v\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f -",
            "stdin_data": explicit_default_plus_implicit,
        })
        assert eff.scope == "configmap"
        assert eff.namespace == "default"
        assert eff.names == ("cm-a", "cm-b")

    def test_multi_doc_manifest_all_anchored_names_still_allows(self):
        """A multi-doc apply whose every document is inside the approved
        name set stays legal — the fix anchors more, it does not ban
        multi-doc whitelisted applies wholesale."""
        approved = approved_from_dict(freeze_approved_target(
            target={"namespace": "default", "names": ["ok-cm", "ok2-cm"]},
            params={"scope": "configmap"},
            fault_scope="pod", fault_target="pod", fault_action="fill",
        ))
        two_ok = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: ok-cm\n  namespace: default\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: ok2-cm\n  namespace: default\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f -",
            "stdin_data": two_ok,
        })
        assert target_drift_guard(eff, approved).verdict is GuardVerdict.ALLOW

    def test_multi_doc_manifest_unapproved_name_drifts(self):
        """End-to-end: doc 2's name outside the approved set must reject
        — before the fix the guard anchored doc 1 only and ALLOWed the
        whole apply, creating an unanchored doc-2 resource."""
        approved = approved_from_dict(freeze_approved_target(
            target={"namespace": "default", "names": ["ok-cm"]},
            params={"scope": "configmap"},
            fault_scope="pod", fault_target="pod", fault_action="fill",
        ))
        one_ok_one_evil = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: ok-cm\n  namespace: default\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: evil-cm\n  namespace: default\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-f -",
            "stdin_data": one_ok_one_evil,
        })
        assert target_drift_guard(eff, approved).verdict is GuardVerdict.REJECT_DRIFT

    # -- kustomize input channel (probe KUSTO, kubectl v1.34.1 live) ----

    @pytest.mark.parametrize(
        "sub, v_args",
        [
            ("apply", "-k /tmp/kust"),
            ("delete", "-k /tmp/kust"),
            ("replace", "-k /tmp/kust"),
            ("create", "-k /tmp/kust"),
            ("apply", "-Rk /tmp/kust"),
            ("apply", "--kustomize /tmp/kust"),
            ("apply", "-k=/tmp/kust"),
            ("apply", "-k dir -f -"),
        ],
        ids=[
            "apply-k", "delete-k", "replace-k", "create-k",
            "bundle-Rk", "long-kustomize", "glued-k=", "k-plus-f-combo",
        ],
    )
    def test_kustomize_channel_banned_invisible_content(self, sub, v_args):
        """kubectl builds the manifests from a DIRECTORY the guard
        cannot see — live probe: apply/delete/replace/create all execute
        the built objects. The same invisibility class as ``-f <file>``,
        one legislation for both: the classified identity would describe
        content kubectl does not execute."""
        eff = infer_effective_target("kubectl", {
            "subcommand": sub, "v_args": v_args,
        })
        assert eff.scope == SCOPE_BANNED
        assert "kustomization DIRECTORY" in (eff.reject_detail or "")
        assert "stdin_data" in (eff.reject_suggestion or "")

    def test_kustomize_ban_beats_the_stdin_manifest_branch(self):
        """A COMPLIANT stdin manifest beside ``-k`` must not reach the
        stdin-manifest classifier: kubectl itself rejects the -k + -f
        combo today, but the ban ordering makes the verdict independent
        of that client-side check ever changing."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply",
            "v_args": "-k dir -f -",
            "stdin_data": (
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
                "  name: cm-t\n  namespace: prod\ndata:\n  k: v\n"
            ),
        })
        assert eff.scope == SCOPE_BANNED
        assert "kustomization DIRECTORY" in (eff.reject_detail or "")

    def test_readonly_kustomize_stays_classified(self):
        """``get -k`` executes nothing — the read-only face keeps its
        normal classification (no invisible-content issue: nothing is
        built or written)."""
        eff = infer_effective_target("kubectl", {
            "subcommand": "get", "v_args": "-k /tmp/kust",
        })
        assert eff.scope != SCOPE_BANNED

    # -- mixed-kind multi-doc manifests (probe M3, live-verified) --------

    def test_multi_doc_mixed_kinds_is_a_form_issue(self):
        """The approval's scope anchors ONE kind while kubectl creates
        every document as its own kind (live dry-run: configmap/m3-cm +
        secret/m3-secret both created, exit 0) — a mixed-kind apply has
        no single scope anchor, same legislation as the mixed-namespace
        gate."""
        mixed_kinds = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-a\n  namespace: default\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: Secret\nmetadata:\n"
            "  name: evil-secret\n  namespace: default\ntype: Opaque\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": mixed_kinds,
        })
        assert eff.scope == SCOPE_BANNED
        assert "manifest mixes kinds (ConfigMap, Secret)" in (eff.reject_detail or "")
        assert "one kind per apply" in (eff.reject_suggestion or "")

    def test_multi_doc_mixed_kinds_rejects_even_namespace_wide(self):
        """End-to-end: the probe shape — a namespace-wide configmap
        approval (names and labels empty) ALLOWed the ConfigMap+Secret
        apply before the fix because the namespace-wide path skips the
        names/labels checks and the scope anchor only described doc 1."""
        approved = approved_from_dict(freeze_approved_target(
            target={"namespace": "default"},
            params={"scope": "configmap"},
            fault_scope="pod", fault_target="pod", fault_action="fill",
        ))
        mixed_kinds = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-a\n  namespace: default\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: Secret\nmetadata:\n"
            "  name: evil-secret\n  namespace: default\ntype: Opaque\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": mixed_kinds,
        })
        assert target_drift_guard(eff, approved).verdict is GuardVerdict.REJECT_BANNED

    def test_multi_doc_same_kind_namespace_wide_still_allows(self):
        """The fix bans MIXED kinds, not multi-doc: same-kind documents
        under a namespace-wide approval stay legal."""
        approved = approved_from_dict(freeze_approved_target(
            target={"namespace": "default"},
            params={"scope": "configmap"},
            fault_scope="pod", fault_target="pod", fault_action="fill",
        ))
        same_kind = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-x\n  namespace: default\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-y\n  namespace: default\ndata:\n  k: v\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": same_kind,
        })
        assert eff.scope == "configmap"
        assert eff.names == ("cm-x", "cm-y")
        assert target_drift_guard(eff, approved).verdict is GuardVerdict.ALLOW

    def test_kind_gate_compares_lowercase_identity(self):
        """Kind identity is lower-cased before the mixed-kind gate: a
        ``CONFIGMAP`` spelling counts as the SAME kind as ``ConfigMap``.
        kubectl itself refuses the non-canonical spelling (probe M5:
        "no matches for kind" — executor-side fail-closed), so the
        case-merge on the guard side never admits anything kubectl
        would actually create."""
        case_mix = (
            "apiVersion: v1\nkind: CONFIGMAP\nmetadata:\n"
            "  name: cm-a\n  namespace: default\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-b\n  namespace: default\ndata:\n  k: v\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": case_mix,
        })
        assert eff.scope == "configmap"
        assert eff.names == ("cm-a", "cm-b")

    # -- labels anchor for the whitelist branch (probe M4) -------------

    def test_label_only_approval_whitelist_apply_anchors_labels(self):
        """The whitelist branch never extracted manifest labels, so a
        label-only approval rejected a fully-compliant whitelisted apply
        while the SAME shape passed on the deployment contract branch
        (which extracts labels) — two branches of one channel, two
        verdicts. Single doc: the intersection is that doc's labels."""
        approved = approved_from_dict(freeze_approved_target(
            target={"namespace": "default", "labels": {"app": "myapp"}},
            params={"scope": "configmap"},
            fault_scope="pod", fault_target="pod", fault_action="fill",
        ))
        labelled = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-l\n  namespace: default\n  labels:\n"
            "    app: myapp\ndata:\n  k: v\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": labelled,
        })
        assert eff.labels == {"app": "myapp"}
        assert target_drift_guard(eff, approved).verdict is GuardVerdict.ALLOW

    def test_label_only_approval_unlabelled_sibling_doc_rejects(self):
        """Multi-doc labels anchor is the INTERSECTION: a document
        without the approved labels empties it, so one labelled document
        cannot vouch for an unlabelled sibling (fail-closed on the doc
        coverage, mirroring the K1 names anchoring)."""
        approved = approved_from_dict(freeze_approved_target(
            target={"namespace": "default", "labels": {"app": "myapp"}},
            params={"scope": "configmap"},
            fault_scope="pod", fault_target="pod", fault_action="fill",
        ))
        one_labelled_one_naked = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-ok\n  namespace: default\n  labels:\n"
            "    app: myapp\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-naked\n  namespace: default\ndata:\n  k: v\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": one_labelled_one_naked,
        })
        assert eff.labels == {}
        assert target_drift_guard(eff, approved).verdict is GuardVerdict.REJECT_DRIFT

    def test_names_approval_unaffected_by_labels_dimension(self):
        """A names-based approval does not require the labels dimension:
        the guard rejects only when BOTH the names and the labels checks
        fail, so a fully name-anchored apply with an empty label
        intersection stays legal."""
        approved = approved_from_dict(freeze_approved_target(
            target={"namespace": "default", "names": ["cm-ok", "cm-naked"]},
            params={"scope": "configmap"},
            fault_scope="pod", fault_target="pod", fault_action="fill",
        ))
        one_labelled_one_naked = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-ok\n  namespace: default\n  labels:\n"
            "    app: myapp\ndata:\n  k: v\n"
            "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            "  name: cm-naked\n  namespace: default\ndata:\n  k: v\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": one_labelled_one_naked,
        })
        assert target_drift_guard(eff, approved).verdict is GuardVerdict.ALLOW

    # -- carrier RBAC reshape distinction (inject-cc2d5080) -----------

    def test_carrier_rbac_manifest_is_form_rejection_not_mechanism_ban(self):
        """A manifest whose every kind rides the imperative create
        channel (the recovery-carrier RBAC family) is a FORM rejection:
        the same objects pass as separate ``kubectl create sa <name>``
        calls, so the rejection must NOT carry mechanism_banned (whose
        renderer says "no reshape of this call will pass") and must
        TEACH the reshape. inject-cc2d5080: the mislabel steered the
        model off its approved carrier-stacking path."""
        rbac_stack = (
            "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
            "  name: drill-rc-x\n  namespace: default\n"
            "---\napiVersion: rbac.authorization.k8s.io/v1\n"
            "kind: ClusterRole\nmetadata:\n  name: drill-rc-x\n"
            "---\napiVersion: rbac.authorization.k8s.io/v1\n"
            "kind: ClusterRoleBinding\nmetadata:\n  name: drill-rc-x\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": rbac_stack,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is False
        assert "kubectl create sa" in (eff.reject_suggestion or "")
        assert "IMPERATIVE" in (eff.reject_suggestion or "")

    def test_carrier_rbac_kind_coverage_is_single_sourced(self):
        """The reshape branch's kind set is
        :data:`_RECOVERY_CARRIER_CREATE_KINDS` itself — a hand-written
        copy would drift the first time the carrier standard adds a
        member (7cd32cf2: vocabulary single-sourcing)."""
        # sa / serviceaccount spellings both covered
        assert "sa" in _RECOVERY_CARRIER_CREATE_KINDS
        assert "serviceaccount" in _RECOVERY_CARRIER_CREATE_KINDS
        sa_manifest = (
            "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
            "  name: drill-rc-y\n  namespace: default\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": sa_manifest,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is False
        # The manifest kinds canonicalise into the map's keys: the
        # branch's membership test is spelled on the CANONICAL side.
        from chaos_agent.agent.target_guard.classifier import canonicalise_kind

        assert canonicalise_kind("ServiceAccount") in _RECOVERY_CARRIER_CREATE_KINDS

    def test_rbac_plus_workload_kind_keeps_mechanism_ban(self):
        """A manifest mixing an RBAC object with a WORKLOAD kind keeps
        the mechanism ban — the workload kind has no compliant form on
        ANY channel, and the RBAC member must not dilute that verdict."""
        rbac_plus_workload = (
            "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
            "  name: drill-rc-z\n  namespace: default\n"
            "---\napiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n"
            "  name: evil-ds\n  namespace: default\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": rbac_plus_workload,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is True

    def test_non_carrier_non_workload_kind_keeps_mechanism_ban(self):
        """A kind that is neither carrier family nor workload nor
        whitelist (e.g. Ingress) keeps the standing mechanism ban —
        the reshape branch admits ONLY the imperative-channel family."""
        ingress = (
            "apiVersion: networking.k8s.io/v1\nkind: Ingress\n"
            "metadata:\n  name: evil-ing\n  namespace: default\n"
        )
        eff = infer_effective_target("kubectl", {
            "subcommand": "apply", "v_args": "-f -",
            "stdin_data": ingress,
        })
        assert eff.scope == SCOPE_BANNED
        assert eff.mechanism_banned is True
        assert "non-whitelisted resource kind" in (eff.reject_detail or "")


# ---------------------------------------------------------------------------
# W-55-6: ``kubectl create <kind> <subtype> <name>`` — the imperative-create
# subtype grammar. ``create service clusterip NAME`` / ``create secret tls
# NAME`` carry a SUBTYPE positional between the kind and the name. The generic
# reader only models ``KIND NAME``, so without stripping the subtype it read the
# subtype AS the name and dropped the real one — the #55 v2 injection stalled
# because the guard named the wrong target. The subtype is grammar; the create
# branch now strips it by index before the generic reader runs.
# ---------------------------------------------------------------------------


class TestKubectlCreateSubtypeGrammar:
    @pytest.mark.parametrize("subtype", [
        "clusterip", "nodeport", "loadbalancer", "externalname",
    ])
    def test_service_subtype_is_stripped_and_name_resolved(self, subtype):
        eff = classify("create", f"service {subtype} drill-svc --tcp=80:80 -n default")
        assert eff.scope == "service"
        assert eff.names == ("drill-svc",)
        assert eff.namespace == "default"

    @pytest.mark.parametrize("subtype", ["generic", "docker-registry", "tls"])
    def test_secret_subtype_is_stripped_and_name_resolved(self, subtype):
        eff = classify("create", f"secret {subtype} drill-secret -n default")
        assert eff.scope == "secret"
        assert eff.names == ("drill-secret",)
        assert eff.namespace == "default"

    def test_service_without_subtype_reads_name_directly(self):
        """Negative anchor: ``create service NAME`` (no subtype) must NOT lose
        its name — only a token in the subtype word-set is stripped."""
        eff = classify("create", "service my-svc --clusterip=None -n default")
        assert eff.scope == "service"
        assert eff.names == ("my-svc",)

    def test_kind_without_a_subtype_grammar_is_untouched(self):
        """configmap has no imperative subtype; ``generic`` here is the NAME,
        not grammar — it must survive as the resolved target."""
        eff = classify("create", "configmap my-cm --from-file=x -n default")
        assert eff.scope == "configmap"
        assert eff.names == ("my-cm",)

    def test_subtype_stripped_by_index_not_by_value(self):
        """The removal is index-based: a flag VALUE that equals the subtype
        (``-n clusterip``) must survive as the namespace while the positional
        subtype is stripped. A value-based ``list.remove`` would have eaten the
        namespace token instead."""
        eff = classify("create", "service -n clusterip clusterip real-svc --tcp=80")
        assert eff.scope == "service"
        assert eff.names == ("real-svc",)
        assert eff.namespace == "clusterip"

    def test_raw_command_preserves_the_subtype(self):
        """Audit fidelity: stripping the subtype is a reading aid only — the
        recorded raw command still carries every token the model issued."""
        eff = classify("create", "service clusterip drill-svc --tcp=80 -n default")
        assert "clusterip" in eff.raw_command
        assert eff.names == ("drill-svc",)
