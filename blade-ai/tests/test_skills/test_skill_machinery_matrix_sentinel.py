"""Sentinel: skill machinery command forms and the judgement matrix must stay in lockstep.

Root cause this sentinel pins (R23/G-7): four consecutive inspection rounds
(R20 name granularity → R21 ns topology → R22 verb space → R23 channel
domain) each found the SAME structural hole — a skill case taught a new
machinery command form while the attribution matrix had not legislated the
matching face, so recovery machinery was counted as fault injection
(mis-attribution, combo mis-marks, ghost takeover). The engine behind the
recurrence is an asymmetry: adding a skill case costs nothing, legislating
an exemption face costs a review round — so the corpus drifts ahead of the
matrix silently.

The sentinel converts that silent drift into loud drift, face by face:

- HOST face: every ``systemd-run`` command line in the host catalogue
  (comment forms unwrapped, backslash continuations joined) is judged by
  the matrix entry point. A command whose text carries the timer
  semantics (``--on-active``) must be exempted; one without it (the
  killloop fault-carrier form) must stay attributed — both directions
  pinned, both live in the corpus today.
- CHANNEL face: every ``kubectl exec drill-rc-…`` command (fenced-block
  line starts AND inline backtick spans, continuations joined) is
  materialized (placeholders → a registered carrier) and judged:
  mutating payloads (arm / verify / timer re-arm) must be exempted,
  readonly forensics (``cat /tmp/restore.log``) stay outside the
  attribution domain by their own readonly shape.
- DEMOLITION face: every ``kubectl delete <kind> drill-rc-…`` command is
  materialized against the full registered carrier stack (four-piece
  namespaced family + six-object cluster extensions) and must be
  exempted.
- VERB domain: the kubectl verbs co-occurring with ``drill-rc-`` are
  pinned to the classified baseline; a NEW verb appearing in a future
  case fails here BEFORE it can become the eighth door — forcing the
  author to either route the case at a known form or legislate a new
  matrix face first.

Known boundaries, declared (same family as the R22 carrier-payload blind
spot — statically unanalysable shapes are declared, never silently
excluded):

- the §6 prefix sweep ``kubectl delete $(kubectl get … | grep drill-rc-)``
  is command-substitution shaped: no static target name, matched by
  neither face by construction (the delete extraction anchor requires
  ``<kind> drill-rc-`` in direct succession); a low-frequency hygiene op
  per the standard, kept out deliberately.
- the two-step stack build (``kubectl patch clusterrole/role
  drill-rc-…``) runs in the BUILD window — before registration, before
  issue; the patch verb is deliberately outside the matrix's
  delete/exec domain. A post-issue patch of a registered carrier would
  stay attributed (the conservative direction); re-examine if a case
  ever teaches that sequence.
- the killloop non-timer form (Host_进程异常_进程被杀死.md 形态A) is
  the corpus's one attributed systemd-run — pinned below AND colliding
  with ToolGuard's admission law (``systemd-run`` admitted ONLY as a
  timer): a live skill↔guard drift, kept loud until adjudicated.
"""

import pathlib
import re

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_HOST_CATALOGUE = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"
_K8S_REFERENCES = _SKILLS_DIR / "k8s-chaos-skills" / "references"

#: Materialized carrier identity: the placeholder values the templates
#: carry (<hash> / <名> → x, the ns placeholders → ns) are replaced with a
#: single registered identity, so the extraction judges FORM (the matrix's
#: faces), never the runtime hash.
_CARRIER = "drill-rc-x"
_NS = "ns"

#: The carriers a 自恢复 claim may name — the HOST face's legislated
#: domain, two families (baseline re-measured R23: the stress-ng case
#: taught the second one): ``systemd-run`` transient timers (the armed
#: form, matrix-legislated) and the fault binary's OWN ``timeout``
#: (self-expiring faults — stress-ng ``--timeout`` exits on its own, fd
#: released, nothing to arm). A FUTURE carrier family (--on-calendar,
#: at, nohup …) must fail the claim tooth FIRST, then either extend
#: ``is_systemd_run_timer``'s domain or reshape the case.
_HOST_CARRIER_CLAIMS = ("systemd-run", "timeout")


# ---------------------------------------------------------------------------
# Extractors (host side)
# ---------------------------------------------------------------------------


def _host_systemd_run_commands() -> list[tuple[str, str]]:
    """(relpath, command) — every systemd-run command line in the host
    catalogue.

    Comment forms are unwrapped (``# systemd-run …`` teaches the same
    form); backslash continuations are joined (a template's payload lives
    on the next line); prose mentions never START a line with the binary,
    so the line-start anchor is the command/fence discriminator.
    """
    out: list[tuple[str, str]] = []
    for path in sorted(_HOST_CATALOGUE.rglob("*.md")):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        i = 0
        while i < len(lines):
            stripped = lines[i].lstrip()
            body = stripped[1:].lstrip() if stripped.startswith("#") else stripped
            # "systemd-run -" anchors COMMAND lines only: a prose line
            # starting with the binary ("systemd-run transient service
            # 承载…") carries a flag-less tail and is not a command.
            if not body.startswith("systemd-run -"):
                i += 1
                continue
            parts = [body]
            while parts[-1].endswith("\\") and i + 1 < len(lines):
                parts[-1] = parts[-1].rstrip("\\").rstrip()
                i += 1
                parts.append(lines[i].strip())
            out.append((str(path.relative_to(_SKILLS_DIR)), " ".join(parts)))
            i += 1
    return out


def _host_self_recovery_files() -> set[str]:
    """Files whose text carries a 自恢复 claim line."""
    out: set[str] = set()
    for path in sorted(_HOST_CATALOGUE.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if any("自恢复基于" in line for line in text.splitlines()):
            out.add(str(path.relative_to(_SKILLS_DIR)))
    return out


# ---------------------------------------------------------------------------
# Extractors (k8s side)
# ---------------------------------------------------------------------------


def _k8s_texts() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted(_K8S_REFERENCES.rglob("*.md")):
        out.append(
            (str(path.relative_to(_SKILLS_DIR)),
             path.read_text(encoding="utf-8", errors="replace"))
        )
    return out


def _k8s_carrier_commands(verb: str) -> list[tuple[str, str]]:
    """(relpath, command) — every ``kubectl <verb> … drill-rc-…`` command.

    two corpus shapes, both consumed: fenced-block line starts
    (continuations joined) and inline backtick spans (prose commands —
    the pkill re-arm-stop and the restore.log forensics travel inline).
    The delete anchor additionally requires ``<kind> drill-rc-`` in
    DIRECT succession after the verb, which keeps the §6
    command-substitution prefix sweep (``kubectl delete $(kubectl get
    …)`` — no static target name) outside by construction.
    """
    direct = re.compile(rf"^kubectl {verb} ")
    if verb == "delete":
        # "drill-rc" WITHOUT the trailing dash: a FUTURE carrier naming
        # pattern (drill-rc2-…) must be EXTRACTED and then fail the
        # registered-name match — the sharpness probe proved the
        # dash-terminated anchor silently swallowed it (the sentinel's
        # own recurrence-engine shape, fixed in the same round).
        direct = re.compile(r"^kubectl delete [a-z]+ drill-rc")
    out: list[tuple[str, str]] = []
    for rel, text in _k8s_texts():
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            for span in re.findall(r"`([^`\n]+)`", line):
                if direct.match(span) and "drill-rc" in span:
                    out.append((rel, span))
            stripped = line.strip()
            if direct.match(stripped) and "drill-rc" in stripped:
                parts = [stripped]
                while parts[-1].endswith("\\") and i + 1 < len(lines):
                    parts[-1] = parts[-1].rstrip("\\").rstrip()
                    i += 1
                    parts.append(lines[i].strip())
                out.append((rel, " ".join(parts)))
            i += 1
    return out


def _materialize(cmd: str) -> str:
    """Replace the template placeholders with one registered identity."""
    for name_ph in ("drill-rc-<hash>", "drill-rc-<名>"):
        cmd = cmd.replace(name_ph, _CARRIER)
    for ns_ph in ("<namespace>", "<ns>", "<靶ns>", "<恢复ns>"):
        cmd = cmd.replace(f"-n {ns_ph}", f"-n {_NS}")
    return cmd


def _carrier_stack_registry(namespace: str = _NS) -> list[dict]:
    """The registered carrier stack, namespaced members under
    ``namespace`` (the stack is registered WHERE it was built — the
    CoreDNS case builds in kube-system, the literal ns survives
    materialization and drives this parameter) plus the six-object
    cluster extensions (clusterrolebinding + clusterrole, cluster-scoped)
    — §6's four-way sweep and the #36 six-object variant both resolve
    against it."""
    return [{
        "type": "recovery_carrier", "kind": "pod",
        "name": _CARRIER, "namespace": namespace, "status": "active",
        "rbac_family": [
            {"kind": k, "name": _CARRIER, "namespace": namespace}
            for k in ("rolebinding", "role", "serviceaccount")
        ] + [
            {"kind": k, "name": _CARRIER, "namespace": ""}
            for k in ("clusterrolebinding", "clusterrole")
        ],
    }]


def _call_ns(v_args: str) -> str:
    """The ``-n`` value of a materialized command (registration ns)."""
    m = re.search(r"(?:^|\s)-n (\S+)", v_args)
    return m.group(1) if m else _NS


def _as_kubectl_call(cmd: str) -> dict:
    verb = cmd.split()[1]
    return {
        "subcommand": verb,
        "v_args": cmd[len(f"kubectl {verb} "):],
    }


#: R25/G-8 — the exec-carried demolition prescriptions: full ``kubectl
#: exec … -- blade destroy/revoke …`` command lines. The kubelet-stall
#: case's PREFERRED recovery path (operator/CRD link stays reachable in
#: the stall window) and the re-arm protocol's inline form both travel
#: this shape; the target is the CLUSTER's tool pod, never a task asset.
_EXEC_DESTROY_VERB_RE = re.compile(r"blade (?:destroy|revoke)\b")


def _k8s_exec_blade_destroy_commands() -> list[tuple[str, str]]:
    """(relpath, v_args) — every prescribed exec-carried destroy command,
    placeholders materialized (tool-pod/ns/UID → one identity).

    Same two corpus shapes as the carrier extractor (inline backtick
    spans and fenced line starts); anchored on the ``kubectl exec`` head
    plus a ``blade destroy/revoke`` payload — the verb×domain signature
    the BLADE-DEMOLITION face legislates.
    """
    out: list[tuple[str, str]] = []
    for rel, text in _k8s_texts():
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            for span in re.findall(r"`([^`\n]+)`", line):
                if span.startswith("kubectl exec ") and _EXEC_DESTROY_VERB_RE.search(span):
                    out.append((rel, _materialize_destroy(span)))
            stripped = line.strip()
            if stripped.startswith("kubectl exec ") and _EXEC_DESTROY_VERB_RE.search(stripped):
                parts = [stripped]
                while parts[-1].endswith("\\") and i + 1 < len(lines):
                    parts[-1] = parts[-1].rstrip("\\").rstrip()
                    i += 1
                    parts.append(lines[i].strip())
                out.append((rel, _materialize_destroy(" ".join(parts))))
            i += 1
    return out


def _materialize_destroy(cmd: str) -> str:
    """Strip the kubectl head + replace the destroy template's
    placeholders (tool pod / ns / UID spellings) with one identity."""
    v_args = cmd[len("kubectl exec "):]
    for pod_ph in (
        "<chaosblade-tool-pod>", "<chaosblade-tool-pod-name>", "<tool-pod>",
    ):
        v_args = v_args.replace(pod_ph, "chaosblade-tool-x")
    for ns_ph in ("<tool-namespace>", "<namespace>", "<ns>"):
        v_args = v_args.replace(f"-n {ns_ph}", "-n chaosblade")
    for uid_ph in (
        "<experiment_uid>", "<UID>", "<uid>", "<实验UID>", "<实验uid>",
    ):
        v_args = v_args.replace(uid_ph, "aa11bb22cc33dd44")
    return v_args


# ---------------------------------------------------------------------------
# HOST face teeth
# ---------------------------------------------------------------------------


def test_host_systemd_run_forms_match_the_single_source() -> None:
    """HOST 面对账主牙：语料中每条 systemd-run 命令的 timer 语义特征
    （--on-active 在场）必须与矩阵豁免判定一致——双向。timer 形态
    （machinery）必须豁免；killloop 形态（fault carrier）必须保持
    归因。文本特征与判定分叉 = 新形态漂移（payload-carried flag、
    未立法的新 timer 变体都会在这里先红）。"""
    from chaos_agent.agent.execution_artifacts import (
        issue_call_is_registered_teardown,
    )

    drift: list[str] = []
    for rel, cmd in _host_systemd_run_commands():
        verdict = issue_call_is_registered_teardown(
            "host_inject", {"command": cmd}, [],
        )
        if verdict != ("--on-active" in cmd):
            drift.append(
                f"{rel}: timer-text={'--on-active' in cmd} "
                f"verdict={verdict}: {cmd[:90]}"
            )
    assert not drift, (
        "A host systemd-run command's timer semantics and the matrix's "
        "HOST-face verdict disagree — the corpus taught a form the "
        "attribution matrix has not legislated (or a machinery form that "
        "no longer exempts). Either legislate the face or reshape the "
        f"case BEFORE the next G-round finds it in production: {drift}"
    )


def test_host_fault_carrier_systemd_run_form_is_pinned() -> None:
    """钉住牙：语料中非 timer 的 systemd-run（killloop 故障载体，形态A）
    必须保持归因（谓词 False）——它实例化 HOST 面的钉住方向（豁免由
    timer 形态挣得，不由二进制名挣得）。它同时与 ToolGuard 准入立法
    冲突（systemd-run 只放行 timer 形态——形态A 命令过不了 guard）：
    一处活着的 skill↔guard 执行面漂移，本牙让它保持响亮直到裁决。"""
    from chaos_agent.agent.execution_artifacts import (
        issue_call_is_registered_teardown,
    )

    faults = [
        (rel, cmd) for rel, cmd in _host_systemd_run_commands()
        if "--on-active" not in cmd
    ]
    assert faults, (
        "the killloop fault-carrier form vanished from the corpus — "
        "re-baseline deliberately, not silently"
    )
    for rel, cmd in faults:
        assert "--unit=blade-killloop-" in cmd, (
            f"a NEW non-timer systemd-run shape appeared ({rel}): "
            f"{cmd[:90]} — the HOST face's pinned direction needs a "
            "deliberate ruling, not a silent corpus drift"
        )
        assert not issue_call_is_registered_teardown(
            "host_inject", {"command": cmd}, [],
        )


def test_host_self_recovery_claims_name_the_legislated_carrier() -> None:
    """载体声明对账牙：每条「自恢复基于…」声明必须指名已立法载体
    清单之一——systemd-run transient timer（矩阵 HOST 面立法的武装
    形态）或故障工具自带 timeout（self-expiring，无需武装——R23 基线
    实测补立法：文件句柄耗尽 case 的 stress-ng --timeout 到期自退）。
    未来新载体（--on-calendar、at、nohup 形态）先红这里：要么扩
    is_systemd_run_timer 的立法域，要么改 skill 形态。"""
    drift: list[str] = []
    for path in sorted(_HOST_CATALOGUE.rglob("*.md")):
        for line in path.read_text(
            encoding="utf-8", errors="replace",
        ).splitlines():
            if "自恢复基于" in line and not any(
                claim in line for claim in _HOST_CARRIER_CLAIMS
            ):
                drift.append(
                    f"{path.relative_to(_SKILLS_DIR)}: {line[:80]}"
                )
    assert not drift, (
        "A host case claims self-recovery WITHOUT naming a legislated "
        "carrier (systemd-run transient timer / the fault binary's own "
        "timeout) — a new self-recovery carrier form is entering the "
        "corpus; legislate the matrix face first (is_systemd_run_timer "
        f"or a parallel HOST face): {drift}"
    )


def test_host_self_recovery_claims_have_timer_commands() -> None:
    """声明-命令共存牙：声明 systemd-run timer 自恢复的文件必须真的
    给出 --on-active 武装命令——只声明不给命令 = 文档漂移（声称的
    自恢复没有可执行形态）。timeout-only 自恢复（工具自带到期）
    无需武装命令，不在此约束内。"""
    claimed = {
        rel for rel in _host_self_recovery_files()
        if any(
            "systemd-run" in line
            for line in (pathlib.Path(_SKILLS_DIR / rel))
            .read_text(encoding="utf-8", errors="replace").splitlines()
            if "自恢复基于" in line
        )
    }
    armed = {
        rel for rel, cmd in _host_systemd_run_commands()
        if "--on-active" in cmd
    }
    missing = claimed - armed
    assert not missing, (
        "Cases claim 自恢复 but carry no --on-active arm command — the "
        "claimed recovery has no executable form in the case text "
        f"(doc drift): {sorted(missing)}"
    )


def test_host_scan_is_not_vacuous() -> None:
    """活性牙：提取器必须持续扫到非平凡语料——签名/目录结构漂移时
    重定基线，不许静默空转。"""
    cmds = _host_systemd_run_commands()
    files = {rel for rel, _ in cmds}
    timers = [cmd for _, cmd in cmds if "--on-active" in cmd]
    assert len(files) >= 12, (
        f"systemd-run files dropped below the known baseline (12): {len(files)}"
    )
    assert len(timers) >= 17, (
        f"timer arm commands dropped below the known baseline (17): "
        f"{len(timers)}"
    )


# ---------------------------------------------------------------------------
# CHANNEL face teeth
# ---------------------------------------------------------------------------


def test_k8s_carrier_exec_forms_pass_the_channel_face() -> None:
    """CHANNEL 面对账主牙：语料中每条载体 exec 命令（武装/验权/停表/
    取证）物化为注册载体后过矩阵——mutating 载荷必须豁免（豁免由
    recovery_carrier 注册挣得）；readonly 取证载荷在自己的 readonly
    形态里天然在归因域外。新 exec 形态（新载体名模式、新载荷结构）
    落不进豁免 = 先红这里。"""
    from chaos_agent.agent.execution_artifacts import (
        issue_call_is_registered_teardown,
    )
    from chaos_agent.agent.providers.message_scanning import (
        exec_inner_command_mutates,
    )

    drift: list[str] = []
    for rel, cmd in _k8s_carrier_commands("exec"):
        call = _as_kubectl_call(_materialize(cmd))
        if not exec_inner_command_mutates(call["v_args"]):
            continue  # readonly forensics — outside the attribution domain
        if not issue_call_is_registered_teardown(
            "kubectl", call,
            _carrier_stack_registry(_call_ns(call["v_args"])),
        ):
            drift.append(f"{rel}: {cmd[:90]}")
    assert not drift, (
        "A mutating carrier exec (arm/verify/re-arm shape) is NOT exempt "
        "by the CHANNEL face after materialization — the corpus taught an "
        "exec form the matrix does not legislate; it would be attributed "
        "as kubectl_native injection evidence. Legislate the face or "
        f"reshape the case first: {drift}"
    )


def test_k8s_scan_is_not_vacuous() -> None:
    """活性牙（k8s 侧）：exec/delete 两面的提取语料都不许静默缩水。"""
    execs = _k8s_carrier_commands("exec")
    deletes = _k8s_carrier_commands("delete")
    assert len(execs) >= 20, (
        f"carrier exec commands dropped below the known baseline (20): "
        f"{len(execs)}"
    )
    assert len({rel for rel, _ in execs}) >= 8, (
        "carrier exec files dropped below the known baseline (8)"
    )
    assert len(deletes) >= 20, (
        f"carrier delete commands dropped below the known baseline (20): "
        f"{len(deletes)}"
    )
    assert len({rel for rel, _ in deletes}) >= 5, (
        "carrier delete files dropped below the known baseline (5)"
    )


# ---------------------------------------------------------------------------
# DEMOLITION face teeth
# ---------------------------------------------------------------------------


def test_k8s_carrier_delete_forms_pass_the_demolition_face() -> None:
    """DEMOLITION 面对账主牙：语料中每条载体删除命令（四连删除、
    六对象变体、跨 ns 变体）物化为注册载体栈后必须豁免。新 kind
    （configmap 等未注册类型）或新删除形态落不进注册族 = 先红这里。"""
    from chaos_agent.agent.execution_artifacts import (
        issue_call_is_registered_teardown,
    )

    drift: list[str] = []
    for rel, cmd in _k8s_carrier_commands("delete"):
        call = _as_kubectl_call(_materialize(cmd))
        if not issue_call_is_registered_teardown(
            "kubectl", call,
            _carrier_stack_registry(_call_ns(call["v_args"])),
        ):
            drift.append(f"{rel}: {cmd[:90]}")
    assert not drift, (
        "A carrier delete command is NOT exempt by the DEMOLITION face "
        "after materialization — the corpus taught a delete form (kind "
        "or shape) the registered stack does not cover; it would be "
        "attributed as kubectl_native injection evidence. Extend the "
        f"registration family or reshape the case first: {drift}"
    )


# ---------------------------------------------------------------------------
# BLADE-DEMOLITION face teeth (R25/G-8 — exec-carried blade destroy)
# ---------------------------------------------------------------------------


def test_k8s_exec_blade_destroy_prescriptions_are_machinery() -> None:
    """BLADE-DEMOLITION 处方对账主牙（R25/G-8）：语料处方的
    exec×blade destroy/revoke 命令必须被矩阵豁免——kubelet 停摆 case
    的首选恢复路径（operator/CRD 链路在停摆窗口内可达）与重武装协议
    的 inline 形态。修复前这些处方被归因为 kubectl_native 注入证据
    （combo 误标/伪 replan 证据/逆扫描污染——G-8 三链）。新 case 教
    了新 destroy 处方形态而矩阵没跟上是 G-8 的复发形状，此牙先红。"""
    from chaos_agent.agent.execution_artifacts import (
        issue_call_is_registered_teardown,
    )

    commands = _k8s_exec_blade_destroy_commands()
    assert commands, (
        "the corpus's exec-carried destroy prescriptions vanished — "
        "re-baseline deliberately (the kubelet-stall preferred-recovery "
        "form), not silently"
    )
    drift: list[str] = []
    for rel, v_args in commands:
        if not issue_call_is_registered_teardown(
            "kubectl", {"subcommand": "exec", "v_args": v_args}, [],
        ):
            drift.append(f"{rel}: {v_args[:90]}")
    assert not drift, (
        "A prescribed exec-carried blade destroy is NOT exempt by the "
        "BLADE-DEMOLITION face — the corpus taught a demolition form "
        "the matrix has not legislated; recovery machinery would be "
        "counted as fault injection (the G-8 recurrence of R20→R23's "
        "engine). Legislate the face or reshape the case first: "
        f"{drift}"
    )


# ---------------------------------------------------------------------------
# VERB domain teeth (the recurrence-engine tripwire)
# ---------------------------------------------------------------------------

#: The classified verb baseline for commands co-occurring with the
#: carrier signature. Domain classes:
#: - exec / delete — machinery maintenance/teardown: the matrix's CHANNEL
#:   and DEMOLITION faces legislate them, and the teeth above reconcile
#:   the corpus against those faces in full.
#: - create / run — stack BUILD verbs (pre-registration, pre-issue
#:   window): never exemption candidates, they run before attribution
#:   even starts.
#: - get — readonly forensics/conflict preflight: outside the write-verb
#:   vocabulary.
#: - patch — the two-step stack build's self-revoke rule append
#:   (BUILD window; a post-issue carrier patch stays attributed, the
#:   conservative direction — declared in the module docstring).
#: A NEW verb here is the recurrence engine turning: route the case at a
#: known form or legislate the face FIRST.
_VERB_BASELINE = frozenset({"create", "delete", "exec", "get", "patch", "run"})


def test_k8s_carrier_verb_domain_is_pinned() -> None:
    """动词域基线牙（复发引擎绊线）：与载体签名共现的 kubectl 动词
    全集钉住在已分类基线上。新动词出现 = 响亮审视点——第八扇门在
    开缝之前先红。"""
    verbs: set[str] = set()
    for _, text in _k8s_texts():
        for m in re.finditer(r"kubectl ([a-z]+)([^`\n]*)", text):
            if "drill-rc" in m.group(0):
                verbs.add(m.group(1))
    unexpected = verbs - _VERB_BASELINE
    assert not unexpected, (
        "A NEW kubectl verb co-occurs with the carrier signature: "
        f"{sorted(unexpected)}. Classify it (build/readonly/machinery) "
        "and either route the case at a known form or legislate the "
        "matrix face BEFORE merging — this tooth is the tripwire that "
        "keeps the corpus from drifting ahead of the matrix (R20→R23's "
        "recurrence engine)"
    )
    # the legislated pair must keep appearing — the matrix faces stay fed
    assert {"exec", "delete"} <= verbs, (
        "the machinery verbs (exec/delete) vanished from the carrier "
        "corpus — the CHANNEL/DEMOLITION faces' teeth went vacuous; "
        "re-baseline deliberately"
    )
