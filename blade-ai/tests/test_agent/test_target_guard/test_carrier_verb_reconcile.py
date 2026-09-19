"""B85 layer-1 code assertion: arming payload write verbs ⊆ registered grant.

The #51/B85 failure form (recovery-carrier.md §2 form-agnostic rule): the
plan pinned PUT, the agent switched the restore form at arm time (PATCH /
DELETE), and the stack's Role still granted ``get,update`` — the timer
fired into 403s and the pod reported recovery without recovering. The doc
legislates the behaviour; these tests pin the CODE-level fail-closed
assertion in ``tool_screener`` plus its grant inputs in
``execution_artifacts``.
"""

from __future__ import annotations

import base64

import pytest
from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes.planning.tool_screener import (
    _NO_STATE_POST_RESOURCES,
    _carrier_restore_write_verbs,
    _screen_carrier_restore_verbs,
    tool_screener,
)
from chaos_agent.agent.target_guard import freeze_approved_target
from chaos_agent.config.settings import settings


def _carrier(rbac_family: list | None = None) -> dict:
    return {
        "artifact_id": "recovery_carrier:prod/drill-rc-b85x",
        "type": "recovery_carrier",
        "status": "active",
        "task_id": "task-b85",
        "name": "drill-rc-b85x",
        "namespace": "prod",
        "operation_family": "recovery_carrier",
        "rbac_family": rbac_family if rbac_family is not None else [{
            "kind": "role", "name": "drill-rc-b85x",
            "namespace": "prod", "verbs": ["get", "update"],
        }],
    }


def _arm_exec_args(payload: str) -> dict:
    return {
        "subcommand": "exec",
        "v_args": f"drill-rc-b85x -n prod -- sh -c '{payload}'",
    }


# The standard's full-form arming template (§4), with the two restore
# curls the form-agnostic rule names in its own example.
_FULL_FORM_PAYLOAD = (
    "( sleep 60; "
    "curl -s -X PATCH --cacert /var/run/secrets/kubernetes.io/"
    "serviceaccount/ca.crt -H \"Authorization: Bearer $(cat /var/run/"
    "secrets/kubernetes.io/serviceaccount/token)\" -H \"Content-Type: "
    "application/merge-patch+json\" -d \"{\\\"spec\\\":{\\\"replicas\\\":1}}\" "
    "https://kubernetes.default.svc/apis/apps/v1/namespaces/prod/"
    "deployments/web/scale; "
    "curl -s -X DELETE --cacert /var/run/secrets/kubernetes.io/"
    "serviceaccount/ca.crt -H \"Authorization: Bearer $(cat /var/run/"
    "secrets/kubernetes.io/serviceaccount/token)\" "
    "https://kubernetes.default.svc/api/v1/namespaces/prod/"
    "resourcequotas/quota-x "
    ") >/tmp/restore.log 2>&1 & echo armed"
)

# The compact loop variant (§4): same verbs, variable-form curl.
_COMPACT_PAYLOAD = (
    "( sleep 60; C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; "
    "T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); "
    "U=https://kubernetes.default.svc/apis/apps/v1/namespaces/prod/"
    "deployments/web; for d in \"{\\\"spec\\\":{\\\"replicas\\\":1}}\"; "
    "do curl -s -X PATCH --cacert $C -H \"Authorization: Bearer $T\" "
    "-H \"Content-Type: application/merge-patch+json\" -d \"$d\" $U; done "
    ") >/tmp/restore.log 2>&1 & echo armed"
)


class TestPayloadVerbExtraction:
    """The payload-side extraction (curl -X METHOD → RBAC verb)."""

    def test_full_form_patch_and_delete(self):
        verbs = _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- sh -c '{_FULL_FORM_PAYLOAD}'"
        )
        assert verbs == {"patch", "delete"}

    def test_compact_loop_variant(self):
        verbs = _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- sh -c '{_COMPACT_PAYLOAD}'"
        )
        assert verbs == {"patch"}

    def test_fused_flag_spelling(self):
        verbs = _carrier_restore_write_verbs(
            "drill-rc-b85x -n prod -- sh -c 'curl -sf -XPATCH $U'"
        )
        assert verbs == {"patch"}

    def test_get_probe_reconciles_nothing(self):
        """Read-only probes and log cats carry no write verbs."""
        probe = (
            "TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/"
            "token); curl -s -X GET -H \"Authorization: Bearer $TOKEN\" "
            "https://kubernetes.default.svc/api/v1/namespaces/prod/pods; "
            "cat /tmp/restore.log"
        )
        assert _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- sh -c '{probe}'"
        ) == set()

    def test_curl_proxy_flag_not_a_method(self):
        """curl's lowercase -x is the PROXY flag — must not read as a verb."""
        assert _carrier_restore_write_verbs(
            "drill-rc-b85x -n prod -- sh -c 'curl -x http://p:1 $U'"
        ) == set()


class TestScreenVerdicts:
    """The reconciliation verdict itself (grant ⊇ payload)."""

    def test_b85_form_rejected_with_missing_verb(self):
        """The B85 case verbatim: grant get,update vs payload PATCH —
        the refusal must name the missing verb and the fix path."""
        result = _screen_carrier_restore_verbs(
            _arm_exec_args(_FULL_FORM_PAYLOAD), {}, {
                "execution_artifacts": [_carrier()],
            },
        )
        assert result is not None
        reason, suggestion = result
        assert "patch" in reason and "delete" in reason
        assert "get,update" in reason
        assert "recovery-carrier.md" in suggestion

    def test_reconciled_grant_admits(self):
        carrier = _carrier(rbac_family=[{
            "kind": "role", "name": "drill-rc-b85x",
            "namespace": "prod", "verbs": ["get", "patch", "delete"],
        }])
        assert _screen_carrier_restore_verbs(
            _arm_exec_args(_FULL_FORM_PAYLOAD), {},
            {"execution_artifacts": [carrier]},
        ) is None

    def test_clusterrole_and_role_grants_union(self):
        """Six-object stack: the union of BOTH grant chains counts."""
        carrier = _carrier(rbac_family=[
            {"kind": "role", "name": "drill-rc-b85x",
             "namespace": "prod", "verbs": ["get", "patch"]},
            {"kind": "clusterrole", "name": "drill-rc-b85x",
             "namespace": "", "verbs": ["delete"]},
        ])
        assert _screen_carrier_restore_verbs(
            _arm_exec_args(_FULL_FORM_PAYLOAD), {},
            {"execution_artifacts": [carrier]},
        ) is None

    def test_unregistered_pod_not_screened(self):
        """A pod that is not a registered carrier belongs to other guards."""
        assert _screen_carrier_restore_verbs(
            _arm_exec_args(_FULL_FORM_PAYLOAD), {}, {"execution_artifacts": []},
        ) is None

    def test_no_role_member_fail_open_to_layer2(self):
        """Carrier registered but no Role on record (manifest-built stack,
        resumed pre-verbs artifact): layer-1 reconciles nothing — §3 SSAR
        at arm time owns the check."""
        carrier = _carrier(rbac_family=[
            {"kind": "serviceaccount", "name": "drill-rc-b85x",
             "namespace": "prod"},
        ])
        assert _screen_carrier_restore_verbs(
            _arm_exec_args(_FULL_FORM_PAYLOAD), {},
            {"execution_artifacts": [carrier]},
        ) is None

    def test_readonly_payload_not_screened(self):
        probe = (
            "TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/"
            "token); curl -s -X GET -H \"Authorization: Bearer $TOKEN\" $U"
        )
        assert _screen_carrier_restore_verbs(
            _arm_exec_args(probe), {},
            {"execution_artifacts": [_carrier()]},
        ) is None

    def test_non_exec_subcommand_not_screened(self):
        assert _screen_carrier_restore_verbs(
            {"subcommand": "get", "v_args": "pod drill-rc-b85x -n prod"}, {},
            {"execution_artifacts": [_carrier()]},
        ) is None

    def test_vehicle_cache_artifacts_win_over_state(self):
        """Same-batch registration (run allowed this round, exec in the
        same batch): the cache's fresher artifact list is the read."""
        carrier = _carrier(rbac_family=[{
            "kind": "role", "name": "drill-rc-b85x",
            "namespace": "prod", "verbs": ["get", "patch", "delete"],
        }])
        assert _screen_carrier_restore_verbs(
            _arm_exec_args(_FULL_FORM_PAYLOAD),
            {"execution_artifacts": [carrier]},
            {"execution_artifacts": [_carrier()]},
        ) is None


@pytest.mark.asyncio
class TestScreenerPipeline:
    """The full screener round: a B85 refusal renders as REJECT_BANNED
    with the carrier_verb_reconcile gate label."""

    async def test_arming_exec_refused_end_to_end(self):
        settings.target_guard_enforcing = True
        approved = freeze_approved_target(
            target={"namespace": "prod", "names": ["drill-pvc-target"]},
            params={"scope": "deployment"},
            fault_scope="deployment", fault_target="disk",
            fault_action="fill",
        )
        state = {
            "task_id": "task-b85",
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": _arm_exec_args(_FULL_FORM_PAYLOAD),
                    "id": "tc-arm",
                }],
            )],
            "approved_target": approved,
            "execution_artifacts": [_carrier()],
        }
        delta = await tool_screener(state)  # type: ignore[arg-type]
        assert delta.get("screener_route") != "pass"
        rejects = [
            m for m in delta.get("messages", [])
            if getattr(m, "type", "") == "tool" and "carrier-verb-reconcile" in str(
                getattr(m, "content", "")
            )
        ]
        assert rejects, (
            "the arming exec must be answered with a refusal naming "
            "carrier-verb-reconcile, not drift prose"
        )
        assert "patch" in str(rejects[0].content)


def test_wildcard_verb_grant_admits_any_payload():
    """RBAC ``verbs: ["*"]`` 全通配——集合差会把每个载荷动词都假拒成
    缺失（{"patch"} - {"*"} = {"patch"}），必须按全量授权对账。"""
    carrier = _carrier(rbac_family=[{
        "kind": "role", "name": "drill-rc-b85x",
        "namespace": "prod", "verbs": ["*"],
    }])
    assert _screen_carrier_restore_verbs(
        _arm_exec_args(_FULL_FORM_PAYLOAD), {},
        {"execution_artifacts": [carrier]},
    ) is None


def test_payload_dash_n_does_not_confuse_pod_identity():
    """W1：载荷内 ``-n`` 形态 token（curl 参数/sh 旗标）不得被误读为
    exec 的 namespace 旗——pod 身份只取 ``--`` 之前。"""
    # payload carrying an -n-looking token AND an arming write verb,
    # addressed by bare pod name before the separator.
    args = {
        "subcommand": "exec",
        "v_args": (
            "drill-rc-b85x -- sh -c 'curl -sf -X PATCH -n -H x $U'"
        ),
    }
    carrier = _carrier(rbac_family=[{
        "kind": "role", "name": "drill-rc-b85x",
        "namespace": "prod", "verbs": ["get"],
    }])
    # Must still find the carrier by the OUTER pod name and reject on the
    # missing patch verb (not silently pass on a garbled identity read).
    result = _screen_carrier_restore_verbs(
        args, {}, {"execution_artifacts": [carrier]},
    )
    assert result is not None
    assert "patch" in result[0]


class TestLocalEncodeTwoStepReadThrough:
    """本地编码 base64 两步法（recovery-carrier.md 铁律 2 合法档位）：
    载荷只有编码串，层 1 必须解码提取动词——否则失明放行重开 #51 面。"""

    def test_encoded_blob_verbs_reconciled(self):
        # restore script: one PATCH one DELETE, grant admits neither
        blob = base64.b64encode(
            b"sleep 60; curl -sf -X PATCH -H x $U; curl -sf -X DELETE $V"
        ).decode()
        args = {
            "subcommand": "exec",
            "v_args": (
                f"drill-rc-b85x -n prod -- "
                f"sh -c 'echo {blob} | base64 -d >/tmp/r.sh; sh /tmp/r.sh'"
            ),
        }
        carrier = _carrier(rbac_family=[{
            "kind": "role", "name": "drill-rc-b85x",
            "namespace": "prod", "verbs": ["get"],
        }])
        result = _screen_carrier_restore_verbs(
            args, {}, {"execution_artifacts": [carrier]},
        )
        assert result is not None
        assert "patch" in result[0]
        assert "delete" in result[0]

    def test_encoded_blob_granted_verbs_pass(self):
        blob = base64.b64encode(
            b"sleep 60; curl -sf -X PATCH $U"
        ).decode()
        args = {
            "subcommand": "exec",
            "v_args": (
                f"drill-rc-b85x -n prod -- "
                f"sh -c 'echo {blob} | base64 -d >/tmp/r.sh; sh /tmp/r.sh'"
            ),
        }
        carrier = _carrier(rbac_family=[{
            "kind": "role", "name": "drill-rc-b85x",
            "namespace": "prod", "verbs": ["patch"],
        }])
        assert _screen_carrier_restore_verbs(
            args, {}, {"execution_artifacts": [carrier]},
        ) is None

    def test_plain_payload_idempotent_under_b64_probe(self):
        """普通载荷（无编码串）经 b64 run 探针零行为变化——明文动词
        照常提取、无 run 可解码即无增量。"""
        carrier = _carrier(rbac_family=[{
            "kind": "role", "name": "drill-rc-b85x",
            "namespace": "prod", "verbs": ["get"],
        }])
        result = _screen_carrier_restore_verbs(
            _arm_exec_args(_FULL_FORM_PAYLOAD), {},
            {"execution_artifacts": [carrier]},
        )
        assert result is not None
        # same missing-verb complaint as before the probe existed
        assert "patch" in result[0]

    def test_jwt_like_blob_reconciles_to_nothing(self):
        """JWT/CA 类长 base64 串解码为非 curl 字节——动词提取为空，
        不得误报也不得干扰同载荷明文动词。"""
        jwt = (
            "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
        )
        args = {
            "subcommand": "exec",
            "v_args": (
                f"drill-rc-b85x -n prod -- sh -c 'curl -sf -X PATCH {jwt}'"
            ),
        }
        carrier = _carrier(rbac_family=[{
            "kind": "role", "name": "drill-rc-b85x",
            "namespace": "prod", "verbs": ["patch"],
        }])
        # PATCH is granted; the JWT run decodes to nothing additive — pass.
        assert _screen_carrier_restore_verbs(
            args, {}, {"execution_artifacts": [carrier]},
        ) is None


# The #51-R live form: the §3 SSAR arm-time probe (curl -X POST …/
# selfsubjectaccessreviews -d '{"resourceAttributes":{...}}') rides the
# same carrier exec as the restore curls — layer 1 must NOT read its POST
# as restore write verb ``create`` (it fired exactly that rejection live).
_SSAR_PROBE = (
    "T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); "
    "C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; "
    "curl -s --cacert $C -H \"Authorization: Bearer $T\" -X POST "
    "https://kubernetes.default.svc/apis/authorization.k8s.io/v1/"
    "selfsubjectaccessreviews "
    "-d '{\"apiVersion\":\"authorization.k8s.io/v1\",\"kind\":"
    "\"SelfSubjectAccessReview\",\"spec\":{\"resourceAttributes\":"
    "{\"namespace\":\"prod\",\"verb\":\"update\",\"group\":\"apps\","
    "\"resource\":\"deployments\"}}}}'"
)


class TestNoStatePostReviewExemption:
    """K8s review 族查询型 POST（§3 SSAR 探针本体）：无集群状态变更，
    不携带恢复写动词——层 1 豁免其 POST，不得拒层 2 自己的立法。"""

    def test_ssar_probe_mixed_with_restore_verbs(self):
        """#51-R 原始形态：SSAR 探针与恢复 PUT/DELETE 同载荷——只提取
        {update, delete}，create 不入对账面（否则被迫补授无意义动词）。"""
        payload = (
            _SSAR_PROBE
            + " && curl -sf -X PUT --cacert $C -H \"Authorization: "
            "Bearer $T\" -d @/tmp/baseline.json https://kubernetes.default.svc"
            "/apis/apps/v1/namespaces/prod/deployments/web"
            + "; curl -sf -X DELETE --cacert $C https://kubernetes.default.svc"
            "/api/v1/namespaces/prod/secrets/cred-x"
        )
        carrier = _carrier(rbac_family=[{
            "kind": "role", "name": "drill-rc-b85x",
            "namespace": "prod", "verbs": ["get", "update", "delete"],
        }])
        assert _screen_carrier_restore_verbs(
            _arm_exec_args(payload), {}, {"execution_artifacts": [carrier]},
        ) is None

    def test_pure_ssar_probe_reconciles_nothing(self):
        """纯 SSAR 验权探针载荷——零恢复动词，层 1 无事可对账（放行）。"""
        assert _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- sh -c '{_SSAR_PROBE}'"
        ) == set()

    @pytest.mark.parametrize("resource", sorted(_NO_STATE_POST_RESOURCES))
    def test_review_family_all_exempt(self, resource):
        """全族豁免（authorization + authentication review），非单资源点补丁。"""
        probe = (
            f"curl -s -X POST https://kubernetes.default.svc/apis/x/v1/{resource} "
            "-d '{\"q\":1}'"
        )
        assert _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- sh -c '{probe}'"
        ) == set()

    def test_real_create_not_exempt(self):
        """真写 POST（无 review URL）照常提取 create——豁免不得过宽。"""
        create = (
            "curl -s -X POST -H \"Authorization: Bearer $T\" "
            "https://kubernetes.default.svc/api/v1/namespaces/prod/pods "
            "-d @/tmp/pod.json"
        )
        assert _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- sh -c '{create}'"
        ) == {"create"}

    def test_segment_boundary_holds(self):
        """豁免不越段：同载荷分号隔开的「真 create; SSAR 探针」两个 curl
        各归各段——真 create 仍入对账面。"""
        both = (
            "curl -s -X POST https://kubernetes.default.svc/api/v1/"
            "namespaces/prod/pods -d @/tmp/pod.json"
            "; " + _SSAR_PROBE
        )
        assert _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- sh -c '{both}'"
        ) == {"create"}

    def test_encoded_ssar_probe_exempt_too(self):
        """base64 编码内的 SSAR 探针同样豁免——解码路径共用同一提取器。"""
        blob = base64.b64encode(
            (_SSAR_PROBE + "; curl -sf -X PATCH $U").encode()
        ).decode()
        assert _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- "
            f"sh -c 'echo {blob} | base64 -d >/tmp/r.sh; sh /tmp/r.sh'"
        ) == {"patch"}

    def test_review_url_alone_does_not_exempt_other_writes(self):
        """载荷文本里出现 review URL 不影响同段其它方法动词的提取
        （PUT/PATCH/DELETE 不在豁免逻辑内，只有 create 受段落豁免）。"""
        payload = (
            _SSAR_PROBE
            + "; curl -sf -X PUT $C $U; curl -sf -X PATCH $C $U"
        )
        assert _carrier_restore_write_verbs(
            f"drill-rc-b85x -n prod -- sh -c '{payload}'"
        ) == {"update", "patch"}
