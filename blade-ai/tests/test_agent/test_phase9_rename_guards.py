"""Phase-9 rename guards (fault-handle-phase9-contract-rename, tasks T1).

护栏先行：在契约字段改名落地**之前**钉扎现状表面，让断裂先变可见。三组
测试与 tasks.md 1.1/1.2/1.3 一一对应：

* ``TestPhase9GoldenBaselines``（T1.1）—— 改名前基线 golden：FaultSpec
  ``to_dict`` 键面（当前旧键 ``blade_target``/``blade_action``）、
  ``/plan`` 预览 markdown 逐字节、TaskSnapshot 构造输出。T2 改名后：
  键面断言翻转为新键，markdown 与快照**值**面必须逐字节不变（预览正文
  只渲染字段值，不含字段名）。
* ``TestPhase9LegacyCompat``（T1.2）—— 旧格式兼容预写：旧 task 文件
  fixture（``result_data["blade_uid"]``/``spec["blade_target"]``）与
  legacy 水合入口 ``derive_handle_from_legacy(values={"blade_uid": ...})``
  先以现状代码落绿（现状本就读旧键），T2.4 读 fallback 落地后靠
  fallback 保持全绿。
* ``TestPhase9IssueTimeSixValues``（T1.3）—— 六值等价钉扎：
  ``classify_issue_time_method`` 返回面 {host_blade, kubectl_exec,
  kubectl_native, host_native, python_agent, None} 逐值断言旧集合判据
  （execute_loop L691 ``not in ("kubectl_native", "host_native")``）与
  T4 新判据（``resolve_by_method`` + ``has_experiment_uid`` 派发）的
  记录决策一致。python_agent 是真实分支（chaosblade_python.
  issue_time_method 返回它，test_python_app_faults
  test_issue_time_method_classification 已钉扎；立项提案误作五值，
  T1 预写时实证修正为六值）。
* ``TestPhase9D7CarrierNeutralNarrative``（T2.6）—— D7 通用叙事载体
  字面归零钉扎：①新文案存在性（design D7 表 14 处 + 实施中追加 5 处
  通用层渲染面）；②AST 提取渲染面字符串（排除 docstring 与 logger
  参数）断言无 ChaosBlade/blade_status 载体字面。豁免面：logger 日志、
  注释/docstring、标识符、provider 内部自述（k8s_native 等）。
* ``TestPhase10KeyFaceUniformity``（phase-10 T3.4）—— 双写退役后的
  键面归一三重护栏：①通用层（排除 providers 子包领域词汇）非
  docstring 字符串常量中的旧键字面仅限五个白名单文件（转译本体/
  兼容契约清单/消息扫描+handle kind/DB migration SQL×2）；
  ②``TODO(debt10)`` 双写标记全仓（src/scripts/web/tui）归零；
  ③web/tui TS 源码零旧键。
* ``TestPhase11CarrierImportBoundary``（phase-11 T4.1）—— 载体
  import 边界护栏：①旧载体工具路径 ``tools.blade*`` 全通用层零
  import（归位 providers/chaosblade/ 后零容忍）；②具体 provider
  子包 import 限定在 phase-8 审计遗留的八处白名单内（存量债显式
  化，新增即失败，白名单失活时同步收窄）；③护栏自检（合成违规
  源码必须被抓到）。工具名字符串（运行时仲裁词汇）不属 import
  边界，不在扫描范围。
"""

import ast
import json
import sys
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "chaos_agent"


# Byte-exact pre-rename ``/plan`` preview golden (captured against the
# current code with the fixture spec below). The body renders field VALUES
# only — T2's rename must keep this byte-identical.
_PLAN_PREVIEW_GOLDEN = """# Fault Injection Plan

## Target
- Namespace: `demo-ns`
- Names: `demo-pod`
- Fault: pod-cpu fullload

## Injection Command

```bash
blade create k8s pod-cpu fullload \\
  --namespace demo-ns \\
  --names demo-pod \\
  --cpu-percent 80 \\
  --timeout 120
```

## Baseline Capture (runs automatically before injection)
1. `kubectl top pod demo-pod -n demo-ns` — Pod CPU/Memory
2. `kubectl describe pod demo-pod -n demo-ns` — Pod conditions/restarts

## Verification Strategy

**Layer 1 (automatic)**: `blade status <uid>` confirms the experiment status is Success

**Layer 2 (observed verification)**: the LLM will verify the injection's effect automatically by comparing against the baseline

## Recovery Strategy

1. `blade destroy <uid>` — destroy the experiment and remove the injected fault

## Safety Assessment
- Status: **pending**
- Conflicting experiments: none

## Timing

- Injection duration: 120s (2min)
- Estimated total: ~4min (including baseline capture + verification)

---
Confirm and run: `/run` | Adjust: `/plan <change request>`"""


def _golden_spec_kwargs() -> dict:
    """Fixture spec fields in the CURRENT (post-rename) vocabulary."""
    return {
        "namespace": "demo-ns",
        "scope": "pod",
        "names": ["demo-pod"],
        "fault_target": "cpu",
        "fault_action": "fullload",
    }


def _legacy_spec_kwargs() -> dict:
    """Same fixture in the PRE-rename vocabulary (legacy checkpoint/task
    records still persist these spellings — the read-side fallback keeps
    them hydratable)."""
    return {
        "namespace": "demo-ns",
        "scope": "pod",
        "names": ["demo-pod"],
        "blade_target": "cpu",
        "blade_action": "fullload",
    }


class TestPhase9GoldenBaselines:
    """T1.1 — 改名前基线 golden（T2 后键面翻转、值面不变）。"""

    def test_fault_spec_to_dict_key_surface(self):
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        spec = FaultSpec(**_golden_spec_kwargs())
        data = spec.to_dict()
        # Post-phase-10 key surface: fault_target/fault_action are the
        # serialized names and the legacy blade_* mirrors are RETIRED —
        # the write side no longer dual-keys (phase-10 dual-write
        # retirement; legacy records hydrate on read instead).
        assert set(data) == {
            "namespace",
            "scope",
            "names",
            "labels",
            "fault_target",
            "fault_action",
            "params",
            "params_flags",
            "duration_seconds",
            "source",
            "user_description",
            "case_resource_path",
            "revision",
            "objective",
            "boundaries",
            "constraints",
            "assumptions",
        }
        assert data["fault_target"] == "cpu"
        assert data["fault_action"] == "fullload"

    def test_plan_preview_golden(self):
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        from chaos_agent.agent.spec.plan_generator import generate_injection_plan

        spec = FaultSpec(
            params={"cpu-percent": 80},
            duration_seconds=120,
            **_golden_spec_kwargs(),
        )
        state = {"fault_spec": spec.to_dict(), "kubeconfig": ""}
        assert generate_injection_plan(state) == _PLAN_PREVIEW_GOLDEN

    def test_task_snapshot_construction_golden(self):
        from chaos_agent.agent.result.task_snapshot import TaskSnapshot

        uid = "e" * 16
        record = {
            "fault_spec": json.dumps(_golden_spec_kwargs()),
            "experiment_uid": uid,
            "skill_name": "k8s-cpu-fullload",
            "injection_method": "host_blade",
        }
        snapshot = TaskSnapshot.from_sources(
            task_id="t-golden",
            record=record,
            session=None,
            has_increment_log=False,
        )
        assert snapshot is not None
        # Post-rename field name on the snapshot (the VALUE stays identical
        # through the rename; the legacy record key is read via fallback).
        assert snapshot.experiment_uid == uid
        # The stored spec is the record's serialized key surface, passed
        # through verbatim — the new keys are the canonical spellings now.
        assert set(snapshot.stored_fault_spec) >= {"fault_target", "fault_action"}
        assert snapshot.fault_type == "pod-cpu-fullload"
        assert snapshot.injection_method == "host_blade"


class TestPhase9LegacyCompat:
    """T1.2 — 旧格式兼容预写；phase-14 G4 后语义已翻转：旧键兼容读
    取通道全部 EOL（fresh-database 裁决），以下断言旧键「视作不存在」。"""

    def test_session_result_data_old_blade_uid_key(self):
        # [已翻转] G4 EOL 后：result_summary 旧键不再被提取。
        from chaos_agent.agent.result.task_snapshot import (
            _extract_experiment_uid_from_session,
        )

        session = {
            "result_summary": json.dumps({"data": {"blade_uid": "a" * 16}}),
        }
        assert _extract_experiment_uid_from_session(session) == ""

    def test_fault_spec_from_dict_old_keys(self):
        # [已翻转] G4 EOL 后：旧键 spec 只读新键，字段为空。
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        spec = FaultSpec.from_dict(_legacy_spec_kwargs())
        assert spec is not None
        assert spec.fault_target == ""
        assert spec.fault_action == ""

    def test_derive_handle_from_legacy_old_key(self):
        # [已翻转] G4 EOL 后：provider 侧旧键 fallback 读取（
        # values.get("blade_uid")，design 五文件清单之外的盘点盲区——guard
        # 排除 providers 子包所致）拆除——仅旧键 attribution 不再产生
        # handle。kind 值 "blade_uid" 本身是协议身份词，G7 独立处理。
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        handle = FaultProviderRegistry.derive_handle_from_legacy(
            {"blade_uid": "u" * 16, "injection_method": "host_blade"}
        )
        assert handle is None

    def test_task_snapshot_from_old_record(self):
        from chaos_agent.agent.result.task_snapshot import TaskSnapshot

        # Record top-level keys are column names — modern on every row
        # since the phase-9 column rename. The phase-10 decision retired
        # the in-code DB read compatibility; the phase-14 G4 retirement
        # dropped the nested fault_spec hydration too (see the flipped
        # tests above) — the uid reads directly, nothing translates.
        uid = "b" * 16
        record = {
            "fault_spec": json.dumps(_legacy_spec_kwargs()),
            "experiment_uid": uid,
        }
        snapshot = TaskSnapshot.from_sources(
            task_id="t-legacy",
            record=record,
            session=None,
            has_increment_log=False,
        )
        assert snapshot is not None
        assert snapshot.experiment_uid == uid


# T1.3 — the full issue-time return face. Six values, not five: the
# python_agent branch genuinely exists (chaosblade_python.issue_time_method).
# (tool_name, tool_args, is_host, expected_method, records)
_ISSUE_TIME_SIX_VALUES = [
    pytest.param(
        "blade_create", {}, True, "host_blade", False,
        id="host_blade-skip",
    ),
    pytest.param(
        "kubectl",
        {
            "subcommand": "exec",
            "v_args": "chaosblade-tool -n chaosblade -- blade create k8s pod-cpu fullload",
        },
        False,
        "kubectl_exec",
        False,
        id="kubectl_exec-skip",
    ),
    pytest.param(
        "kubectl",
        {"subcommand": "scale", "v_args": "deployment web --replicas=0"},
        False,
        "kubectl_native",
        True,
        id="kubectl_native-record",
    ),
    pytest.param(
        "host_inject", {}, True, "host_native", True,
        id="host_native-record",
    ),
    pytest.param(
        "blade_python_create",
        {"target": "redis", "action": "delay"},
        True,
        "python_agent",
        False,
        id="python_agent-skip",
    ),
    pytest.param(
        "kubectl",
        {"subcommand": "get", "v_args": "pods"},
        False,
        None,
        False,
        id="none-skip",
    ),
]


class TestPhase9IssueTimeSixValues:
    """T1.3 — 六值等价钉扎（旧集合判据 vs T4 has_experiment_uid 派发）。"""

    @pytest.mark.parametrize(
        "tool_name,tool_args,is_host,expected_method,records",
        _ISSUE_TIME_SIX_VALUES,
    )
    def test_six_value_equivalence(
        self, tool_name, tool_args, is_host, expected_method, records
    ):
        from chaos_agent.agent.nodes.execute._injection_detection import (
            classify_issue_time_method,
        )
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        issued = classify_issue_time_method(
            tool_name, tool_args, is_host=is_host
        )
        assert issued == expected_method

        # OLD verdict (execute_loop today): set membership on the two
        # native values — anything else is skipped (continue).
        old_records = issued in ("kubectl_native", "host_native")
        # NEW verdict (T4 dispatch): provider resolution + the
        # has_experiment_uid property (None resolves to no provider →
        # skip; a UID-bearing backend → skip; UID-less → record).
        provider = FaultProviderRegistry.resolve_by_method(issued)
        new_records = provider is not None and not getattr(
            provider, "has_experiment_uid", False
        )
        assert old_records == records
        assert new_records == records

    def test_return_face_is_exactly_six_values(self):
        """Guard against silent drift: the table must enumerate the full
        six-value return face — a future seventh value must land here."""
        methods = {params.values[3] for params in _ISSUE_TIME_SIX_VALUES}
        assert methods == {
            "host_blade",
            "kubectl_exec",
            "kubectl_native",
            "host_native",
            "python_agent",
            None,
        }


# T2.6 / D7 — verify/recover 通用层渲染面（提示词正文、warnings、hints、
# tracker 进度文本）的载体字面归零。design D7 表立项 14 处 + 实施中追加
# 5 处（Layer 1 Limitation 主语 / carrier tool pods / Fault scenario 标签 /
# path semantics / coverage warning）。
_D7_NEW_LITERALS: dict[str, list[str]] = {
    "_verifier_messages.py": [
        "- Layer1 (experiment status):",
        "Layer 1 tool check unreachable:",
        "(Layer 1 tool check).",
        "Layer 1 skipped: no experiment UID for this fault.",
        "This fault was injected natively (no experiment carrier).",
        "Layer 1 for experiment ",
        "(Layer 1 tool check, experiment registration).",
        # —— 实施中追加 5 处之 ——
        "the fault experiment ",
        "the carrier tool pods ",
    ],
    "_verifier_layer2_parse.py": [
        "Only Layer 1 (programmatic) verification was performed.",
        "Only Layer 1 (programmatic) verification was confirmed.",
    ],
    "verifier.py": [
        "Only Layer 1 (programmatic) verification was performed.",
        "Native fault: Layer 1 not applicable, Layer 2 skipped (no LLM). ",
    ],
    "_verifier_hints.py": ["Fault scenario: "],
    "_verifier_shared.py": ["- Path semantics: container paths ("],
    "_verifier_finalize.py": ["affected by the fault experiment."],
    "_recover_layer1.py": [
        "This fault has no experiment carrier. EXECUTE",
        "there is no experiment carrier",
    ],
    "_recover_verifier_loop.py": [
        'details="Native fault: no inject context available"',
        "Recover Layer 1 (native): skipped - no inject context",
        '"layer1_type": "native"',
    ],
}


class TestPhase9D7CarrierNeutralNarrative:
    """T2.6 — D7 载体字面归零双钉扎：新文案存在 + 渲染面无载体字面。"""

    @staticmethod
    def _d7_path(rel: str) -> Path:
        sub = "recover" if rel.startswith("_recover") else "verify"
        return _SRC_ROOT / "agent" / "nodes" / sub / rel

    def test_d7_new_literals_present(self):
        """防回潮之一：每处替换点的新文案必须仍在源文本中。"""
        for rel, literals in _D7_NEW_LITERALS.items():
            path = self._d7_path(rel)
            text = path.read_text(encoding="utf-8")
            for lit in literals:
                assert lit in text, f"{rel}: D7 新文案失踪: {lit!r}"

    def test_d7_render_faces_carrier_free(self):
        """防回潮之二：AST 提取渲染面字符串（排除 docstring 与 logger
        参数），断言无 ChaosBlade/blade_status 字面。"""
        for rel in _D7_NEW_LITERALS:
            tree = ast.parse(self._d7_path(rel).read_text(encoding="utf-8"))
            excluded: set[int] = set()
            # docstrings (module/class/function first statement)
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                     ast.ClassDef),
                ):
                    body = node.body
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        excluded.add(id(body[0].value))
            # logger.<level>(...) call arguments (ops surface, not render face)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr in {
                            "debug", "info", "warning", "error",
                            "exception", "critical",
                        }
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "logger"
                    ):
                        for sub in ast.walk(node):
                            if (
                                isinstance(sub, ast.Constant)
                                and isinstance(sub.value, str)
                            ):
                                excluded.add(id(sub))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in excluded
                ):
                    low = node.value.lower()
                    assert "chaosblade" not in low, (
                        f"{rel}: 渲染面字符串含载体字面: {node.value!r}"
                    )
                    assert "blade_status" not in low, (
                        f"{rel}: 渲染面字符串含载体字面: {node.value!r}"
                    )


# phase-10 T3.4 —— 通用层旧键字面量的白名单。
# phase-14 G5/G6/G7 清偿后曾归空集；但 G6 fresh-database 裁决已被推翻
# （kubectl-native 注入天然无 uid，存量旧库常见），两 store 的启动迁移段
# 恢复——迁移 SQL 必须引用旧列名 blade_uid 才能兑旧库，属合法字面点，
# 两文件登记回白名单（九期时同样登记过）。其余文件仍零容忍：任何新增
# 命中即违规（需先走 G3 式清偿或登记例外）。
_PHASE10_LEGACY_LITERAL_ALLOWLIST: set[str] = {
    "persistence/task_store_postgresql.py",
    "persistence/task_store_sqlite.py",
}


class TestPhase10KeyFaceUniformity:
    """phase-10 T3.4 —— 双写退役后的键面归一三重护栏。"""

    def test_generic_layer_legacy_key_literals_confined(self):
        """防回潮之一：通用层（排除 providers 子包的领域词汇）非
        docstring 字符串常量中的旧键字面，仅允许出现在白名单五个文件。"""
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            rel = path.relative_to(_SRC_ROOT).as_posix()
            if "providers" in path.relative_to(_SRC_ROOT).parts:
                continue  # provider 领域词汇（chaosblade 是真实载体名）
            if rel in _PHASE10_LEGACY_LITERAL_ALLOWLIST:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # docstrings 排除（同 D7 guard 做法）
            excluded: set[int] = set()
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                     ast.ClassDef),
                ):
                    body = node.body
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        excluded.add(id(body[0].value))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in excluded
                ):
                    for legacy in ("blade_uid", "blade_target", "blade_action",
                                   "blade_scope"):
                        assert legacy not in node.value, (
                            f"{rel}: 字符串常量含旧键字面 {legacy!r}: "
                            f"{node.value[:60]!r}（若属合法点，先登记白名单）"
                        )

    def test_debt10_markers_retired(self):
        """防回潮之二：TODO(debt10) 双写标记全仓（src/scripts/web/tui）归零。"""
        repo = _SRC_ROOT.parents[1]
        for sub in ("src", "scripts", "web/src", "tui/src"):
            base = repo / sub
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path.is_file() and path.suffix in {".py", ".ts", ".tsx",
                                                      ".sh", ".js", ".mjs"}:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    assert "TODO(debt10)" not in text, (
                        f"{path}: debt10 双写标记残留"
                    )

    def test_web_tui_sources_legacy_free(self):
        """防回潮之三：TS 消费端（web/tui）源码零旧键——第九期改名后
        消费端只读新键，旧键重回 TS 即为断裂。"""
        repo = _SRC_ROOT.parents[1]
        for sub in ("web/src", "tui/src"):
            base = repo / sub
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path.is_file() and path.suffix in {".ts", ".tsx", ".js",
                                                      ".jsx", ".mjs", ".css"}:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    for legacy in ("blade_uid", "blade_target", "blade_action",
                                   "blade_scope"):
                        assert legacy not in text, (
                            f"{path}: TS 源码含旧键 {legacy!r}"
                        )


# ---------------------------------------------------------------------------
# phase-11 (fault-handle-phase11-carrier-import-retirement)
# ---------------------------------------------------------------------------

#: Carrier CLI tool module paths that must never be imported from the generic
#: layer. ``chaos_agent.tools.blade`` was relocated to the chaosblade provider
#: package (``providers/chaosblade/cli.py``) in phase-11; the old tools/ path
#: no longer exists and must never come back.
_PHASE11_BANNED_TOOL_PREFIXES = ("chaos_agent.tools.blade",)

#: Concrete provider subpackage imports from the generic layer. phase-12
#: (spec-import-retirement) 退役了 fault_registry×2 与 plan_generator×1 的
#: 词汇/预览 import，settings 改道 declaration（条目原地换源）；phase-13
#: T2 退役了 _injection_detection 的 blade-success shim（tests 改道载体权威
#: 函数）、T3 改道了 execute_loop 的销毁扫描（registry union 接缝）、
#: T4 改道了 task_snapshot 的 session 恢复双路径（registry 三层编排接缝
#: + 载体 dict hook，词汇判定随迁 provider）。T5 扫描前缀动态化后
#: k8s_native/host_shell 的合法组装面从旧 guard 盲区显式入册（apply 阶段
#: 事实修正：propose 预判「终态 4 条」未计入扩权新纳管面）——现存
#: 八处 = settings→declaration×1 + providers/__init__ 组装点×3（三个
#: 载体的 declaration）+ registry 内置注册×4（四个 provider 实现）；
#: allowlist 钉扎 EXACTLY——新文件、或白名单文件 import 新模块，都会 fail。
_PHASE11_PROVIDER_IMPORT_ALLOWLIST = {
    ("config/settings.py",
     "chaos_agent.agent.providers.chaosblade.declaration"),
    # phase-12 D2 组装点：__init__ import 轻量 declaration（纯数据/纯函数，
    # 非具体 provider 实现）向 fault_registry 注册词汇——合法组装方向，
    # 钉扎防 drift 到 provider 实现或 cli 工具模块。T5 扩权后三个载体的
    # declaration 组装全部显式入册。
    ("agent/providers/__init__.py",
     "chaos_agent.agent.providers.chaosblade.declaration"),
    ("agent/providers/__init__.py",
     "chaos_agent.agent.providers.host_shell.declaration"),
    ("agent/providers/__init__.py",
     "chaos_agent.agent.providers.k8s_native.declaration"),
    # register_builtins 的内置 provider 注册（函数内 lazy）——registry 是
    # 仲裁层组装点，这是合法方向；钉扎防 drift 到 cli 工具模块。
    ("agent/providers/registry.py",
     "chaos_agent.agent.providers.chaosblade.provider"),
    ("agent/providers/registry.py",
     "chaos_agent.agent.providers.chaosblade.python_provider"),
    ("agent/providers/registry.py",
     "chaos_agent.agent.providers.host_shell.provider"),
    ("agent/providers/registry.py",
     "chaos_agent.agent.providers.k8s_native.provider"),
    # drill-target-manifest-contract r15（2026-09）根因级修复：registry 新增
    # is_blade_exec_create_delivery 垂直路由接缝——「kubectl exec 交付 blade
    # create」的句法判定单源收敛到 chaosblade.verify 的
    # classify_blade_exec_payload，k8s_native 两个 scan hook 经 registry
    # 路由取用（与上组 register_builtins 同型：lazy、call-time-only、
    # 仲裁层组装点）。
    ("agent/providers/registry.py",
     "chaos_agent.agent.providers.chaosblade.verify"),
    # recovery-carrier-standard（2026-09）：ToolGuard 对 ``kubectl run`` 的
    # 载体形态判定委托 canonical classifier（_is_recovery_carrier_run）
    # ——两层判定单源永不漂移是显式设计决策（guard.py docstring），lazy、
    # call-time-only，与本文件 registry 委托（L589）同型入册。
    ("tools/guard.py",
     "chaos_agent.agent.providers.k8s_native.classifier"),
    # machinery≠matrix R23/G-7（2026-09）：归因层 HOST 面的工具名域
    # （host_call_is_registered_recovery 的豁免由 provider 自己的工具名
    # 挣得）经 declaration 接缝取纯数据常量 HOST_INJECT_TOOL_NAMES——
    # provider 类属性与谓词同源永不漂移是显式设计决策，lazy、
    # call-time-only，与 settings→declaration 同型（通用层取纯数据，
    # 非具体 provider 实现）。
    ("agent/execution_artifacts.py",
     "chaos_agent.agent.providers.host_shell.declaration"),
    # faultdrill-cr-channel M1（2026-09）：第四载体入册，三处均与既有同型
    # ——组装点 declaration（纯数据，空词汇）、register_builtins 的 lazy
    # provider 注册（暗启动门内）、关门分支 reconciliation 取 CARRIER_ID
    # 纯常量（防 stale 注册，lazy、call-time-only）。
    ("agent/providers/__init__.py",
     "chaos_agent.agent.providers.faultdrill.declaration"),
    ("agent/providers/registry.py",
     "chaos_agent.agent.providers.faultdrill.provider"),
    ("agent/providers/registry.py",
     "chaos_agent.agent.providers.faultdrill.declaration"),
}

#: providers 目录下的平铺通用文件（通用仲裁 + 载体中立扫描原语）——
#: guard 照扫（仅具体 carrier 子包豁免），防止排除规则一刀切造成
#: registry/base/__init__ 监控盲区。phase-14 G1 收录 message_scanning：
#: 载体无关的消息扫描原语之家，任何载体子包都可复用（非横向依赖）。
_PHASE11_PROVIDERS_GENERIC_FILES = {
    "agent/providers/registry.py",
    "agent/providers/base.py",
    "agent/providers/__init__.py",
    "agent/providers/message_scanning.py",
    "agent/providers/uid_shapes.py",
}


def _carrier_subpackage_prefixes() -> tuple[str, ...]:
    """Dynamically discover the carrier subpackage import prefixes
    (``agent/providers/*/`` directory names, ``__pycache__`` excluded) —
    phase-13 T5: the generic-layer scan no longer hardcodes the
    chaosblade prefix; a NEW carrier directory is automatically swept
    (spec: detection-import-boundary, G4 盲区补上)."""
    providers_root = _SRC_ROOT / "agent" / "providers"
    return tuple(
        f"chaos_agent.agent.providers.{p.name}"
        for p in sorted(providers_root.iterdir())
        if p.is_dir() and p.name != "__pycache__"
    )


def _resolve_relative_import(path: Path, node: ast.ImportFrom) -> list[str]:
    """Resolve a relative ``ImportFrom`` (``node.level >= 1``) against
    ``path``'s package to absolute module strings — one entry per
    imported name when ``module`` is empty (``from . import x``), else
    the single module path.

    Phase-13 review fix: without this resolution, ``from .chaosblade.cli
    import x`` in a generic arbitration file yields the bare
    ``"chaosblade.cli"`` — which matches NO absolute prefix and silently
    bypasses every allowlist/banned assertion below (the guard's own
    blind spot against relative-form smuggling)."""
    full_parts = ("chaos_agent", *path.relative_to(_SRC_ROOT).parts)
    anchor = full_parts[: len(full_parts) - node.level]
    if node.module:
        return [".".join((*anchor, *node.module.split(".")))]
    return [".".join((*anchor, alias.name)) for alias in node.names]


def _iter_import_modules(path: Path, tree: ast.Module):
    """Yield ``(lineno, module)`` for every import statement in ``tree``,
    with RELATIVE imports resolved against ``path``'s package so the
    absolute-form allowlist cannot be bypassed via ``from .<carrier>...``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    yield node.lineno, node.module
            else:
                for resolved in _resolve_relative_import(path, node):
                    yield node.lineno, resolved


def _iter_blade_symbol_imports(path: Path, tree: ast.Module):
    """Yield ``(lineno, symbol)`` for ``from chaos_agent.tools import
    blade_*`` — symbol-face consumption of the retired re-export. The
    relative form (``from .tools import blade_*`` in a package-top module)
    resolves to the same module and is caught too."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        modules = (
            _resolve_relative_import(path, node)
            if node.level
            else ([node.module] if node.module else [])
        )
        if modules != ["chaos_agent.tools"]:
            continue
        for alias in node.names:
            if alias.name.startswith("blade"):
                yield node.lineno, alias.name


class TestPhase11CarrierImportBoundary:
    """phase-11 —— 载体 import 边界护栏（spec: carrier-import-boundary）。

    通用层禁止 import 载体工具实现模块（旧 ``tools.blade`` 路径归零，
    零容忍）；具体 provider 子包（phase-13 T5 起动态发现全部载体目录，
    不再固定 chaosblade 前缀——k8s_native/host_shell 新增通用层 import
    即被拒绝）的 import 限定白名单八处（存量合法组装方向显式化，
    新增即失败）。providers 目录下的通用仲裁文件（registry/base/
    __init__）照扫——仅具体 carrier 子包豁免。工具名字符串（运行时
    仲裁词汇，如 profile 映射）不属 import 边界，不在扫描范围。
    """

    def test_carrier_tool_path_retired(self):
        """防回潮之一：旧载体工具路径（tools.blade*）全通用层零
        import——phase-11 归位后该路径不存在，任何重现都是断裂。
        含符号 re-export 形态（``from chaos_agent.tools import
        blade_*``）：若有人把 re-export 加回 ``tools/__init__``，
        通用层对符号面的消费在此拦截而非等运行时 ImportError。"""
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            rel = path.relative_to(_SRC_ROOT).as_posix()
            if (
                "providers" in path.relative_to(_SRC_ROOT).parts
                and rel not in _PHASE11_PROVIDERS_GENERIC_FILES
            ):
                continue  # 具体 carrier 子包豁免；通用仲裁文件照扫
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, module in _iter_import_modules(path, tree):
                for prefix in _PHASE11_BANNED_TOOL_PREFIXES:
                    assert not module.startswith(prefix), (
                        f"{rel}:{lineno}: 通用层 import 载体工具模块 "
                        f"{module!r}（phase-11 已归位 providers/chaosblade/）"
                    )
            bad_symbols = list(_iter_blade_symbol_imports(path, tree))
            assert not bad_symbols, (
                f"{rel}: 通用层经 tools 包符号面 import 载体工具 "
                f"{bad_symbols}（phase-11 已归位 providers/chaosblade/cli）"
            )

    def test_provider_imports_confined_to_allowlist(self):
        """防回潮之二：具体 provider 子包 import 仅限白名单八处
        （phase-13 T5 扫描前缀动态化后：detection 域已清零 +
        settings→declaration×1 + providers 组装点 declaration×3 +
        registry 内置注册×4）；新增载体依赖必须走 registry 接缝。
        扫描前缀由 ``_carrier_subpackage_prefixes`` 动态发现——新载体
        目录出现即自动纳入监控，无需改 guard（spec G4）。"""
        carrier_prefixes = _carrier_subpackage_prefixes()
        seen: set = set()
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            rel = path.relative_to(_SRC_ROOT).as_posix()
            if (
                "providers" in path.relative_to(_SRC_ROOT).parts
                and rel not in _PHASE11_PROVIDERS_GENERIC_FILES
            ):
                continue  # 具体 carrier 子包豁免；通用仲裁文件照扫
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for lineno, module in _iter_import_modules(path, tree):
                if not any(
                    module == prefix or module.startswith(prefix + ".")
                    for prefix in carrier_prefixes
                ):
                    continue
                assert (rel, module) in _PHASE11_PROVIDER_IMPORT_ALLOWLIST, (
                    f"{rel}:{lineno}: 通用层 import provider 子包 {module!r} "
                    f"不在 phase-11 白名单（新增载体依赖须走 registry 接缝）"
                )
                seen.add((rel, module))
        # 白名单条目失活（对应存量债已清偿）时同步收窄，防止白名单腐化。
        assert seen == _PHASE11_PROVIDER_IMPORT_ALLOWLIST, (
            "phase-11 provider import 白名单与实际不符——已可收窄: "
            f"{sorted(_PHASE11_PROVIDER_IMPORT_ALLOWLIST - seen)}"
        )

    def test_guard_detects_synthetic_violation(self):
        """护栏自检：合成违规源码，提取逻辑必须全部抓到（负向用例）
        ——含模块路径形态、符号 re-export 形态与**相对形态**（相对
        import 解析为绝对路径后不得绕过前缀断言——phase-13 review
        补的 guard 自身盲区），且符号面不误伤通用工具；k8s_native/
        host_shell 前缀（动态发现）与相对 base（合法仲裁面）覆盖。"""
        # 虚拟文件 = 通用仲裁层 registry.py：相对载体形态必须解析为
        # 绝对前缀后被抓到；相对 base 是合法仲裁面不误伤。
        src = (
            "from chaos_agent.tools.blade import blade_destroy\n"
            "import chaos_agent.agent.providers.chaosblade.cli as cli\n"
            "from chaos_agent.tools import blade_create, kubectl\n"
            "from chaos_agent.agent.providers.base import FaultProvider\n"
            "from chaos_agent.agent.providers.k8s_native.provider import K8sNativeProvider\n"
            "from chaos_agent.agent.providers.host_shell.declaration import SUPPORTED_TARGETS\n"
            "from .chaosblade.cli2 import q\n"
            "from .base import B\n"
        )
        fake_registry = _SRC_ROOT / "agent" / "providers" / "registry.py"
        carrier_prefixes = _carrier_subpackage_prefixes()
        hits = [
            module
            for _lineno, module in _iter_import_modules(
                fake_registry, ast.parse(src)
            )
            if module.startswith(_PHASE11_BANNED_TOOL_PREFIXES)
            or any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in carrier_prefixes
            )
        ]
        assert hits == [
            "chaos_agent.tools.blade",
            "chaos_agent.agent.providers.chaosblade.cli",
            "chaos_agent.agent.providers.k8s_native.provider",
            "chaos_agent.agent.providers.host_shell.declaration",
            "chaos_agent.agent.providers.chaosblade.cli2",
        ]
        symbols = [
            name
            for _lineno, name in _iter_blade_symbol_imports(
                fake_registry, ast.parse(src)
            )
        ]
        # kubectl 是通用工具——符号面检查不得误伤
        assert symbols == ["blade_create"]
        # 虚拟文件 = 包顶层 __init__.py：相对 tools 符号形态
        # （from .tools import blade_*）解析后同样命中；config 不误伤。
        top_src = (
            "from .tools import blade_destroy, kubectl\n"
            "from .config import settings\n"
        )
        fake_top = _SRC_ROOT / "__init__.py"
        top_symbols = [
            name
            for _lineno, name in _iter_blade_symbol_imports(
                fake_top, ast.parse(top_src)
            )
        ]
        assert top_symbols == ["blade_destroy"]


# ---------------------------------------------------------------------------
# Phase-12 (spec-import-retirement): declaration 模块依赖纪律
# ---------------------------------------------------------------------------

#: declaration.py 允许的 chaos_agent 依赖前缀（通用层方向：传输与配置）。
#: 其余 agent.* 一律禁止——尤其 providers 仲裁层与 agent.spec 回引。
_PHASE12_DECLARATION_ALLOWED_PREFIXES = (
    "chaos_agent.transports",
    "chaos_agent.config.settings",
)


def _declaration_import_ok(module: str) -> bool:
    """declaration 模块的合法 import：``__future__`` / stdlib（含
    typing）/ transports / config.settings。"""
    if module == "__future__":
        return True
    root = module.split(".")[0]
    if root in sys.stdlib_module_names:
        return True
    return module.startswith(_PHASE12_DECLARATION_ALLOWED_PREFIXES)


class TestPhase12DeclarationDiscipline:
    """phase-12 —— declaration 模块依赖纪律（spec: spec-import-boundary）。

    ``providers/<carrier>/declaration.py`` 是 spec 域消费的轻量知识面：
    只许 stdlib / typing / transports / config.settings。禁止任何相对
    import（包内兄弟模块是重依赖入口——组装点拉起全家的旧病不可
    复发），禁止 agent.spec 回引，禁止 providers 仲裁层回引。这是
    ``providers/__init__`` 组装点能轻装拉起、fault_spec 能 import 期
    派生 INTENT_* 的前提。
    """

    def test_declaration_imports_confined(self):
        decl_paths = sorted(
            _SRC_ROOT.glob("agent/providers/*/declaration.py")
        )
        # 三个内置载体（chaosblade/k8s_native/host_shell）各一份声明面。
        assert len(decl_paths) >= 3, (
            "载体 declaration 模块应至少存在三份（phase-12 D1）"
        )
        for path in decl_paths:
            rel = path.relative_to(_SRC_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert _declaration_import_ok(alias.name), (
                            f"{rel}:{node.lineno}: declaration 模块 import "
                            f"{alias.name!r} 超出依赖白名单"
                            f"（stdlib/typing/transports/config.settings）"
                        )
                elif isinstance(node, ast.ImportFrom):
                    assert node.level == 0, (
                        f"{rel}:{node.lineno}: declaration 模块禁止相对 import"
                        f"（包内兄弟模块是重依赖入口）"
                    )
                    if node.module:
                        assert _declaration_import_ok(node.module), (
                            f"{rel}:{node.lineno}: declaration 模块 import "
                            f"{node.module!r} 超出依赖白名单"
                            f"（stdlib/typing/transports/config.settings）"
                        )

    def test_guard_detects_synthetic_violation(self):
        """护栏自检：合成违规源码——相对 import、agent.spec 回引、
        providers 仲裁层回引必须全部被抓到；合法形态（__future__/
        stdlib/transports/settings）不误伤。"""
        src = (
            "from __future__ import annotations\n"
            "import logging\n"
            "import os\n"
            "from pathlib import Path\n"
            "from typing import Any\n"
            "from chaos_agent.transports import is_kubewiz_channel\n"
            "from chaos_agent.config.settings import settings\n"
            "from .provider import ChaosbladeProvider\n"
            "from chaos_agent.agent.spec.fault_spec import FaultSpec\n"
            "from chaos_agent.agent.providers.registry import FaultProviderRegistry\n"
        )
        bad = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _declaration_import_ok(alias.name):
                        bad.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    bad.append("." * node.level + (node.module or ""))
                elif node.module and not _declaration_import_ok(node.module):
                    bad.append(node.module)
        assert bad == [
            ".provider",
            "chaos_agent.agent.spec.fault_spec",
            "chaos_agent.agent.providers.registry",
        ]


class TestPhase12DeclarationRegistrationCompleteness:
    """phase-12 —— declaration 注册完整性护栏（spec: spec-import-boundary）。

    每个存在的 ``providers/<carrier>/declaration.py`` 词汇都必须经
    providers 组装点注册进 ``fault_registry._CARRIER_VOCAB`` 且值与
    declaration 常量一致——防「新增 declaration 忘在 ``__init__`` 注册」
    导致的静默词汇缺失（INTENT_* 不含新载体词汇，schema/prompt 层
    无感知窄化；``_ensure_carrier_vocab`` 的 assert 只防「零注册」，
    防不了「漏注册」）。
    """

    def test_every_declaration_carrier_registered_in_sync(self):
        import importlib

        import chaos_agent.agent.providers  # noqa: F401 — assembly trigger
        from chaos_agent.agent.spec import fault_registry

        decl_paths = sorted(
            _SRC_ROOT.glob("agent/providers/*/declaration.py")
        )
        assert len(decl_paths) >= 3, (
            "载体 declaration 模块应至少存在三份（phase-12 D1）"
        )
        for path in decl_paths:
            carrier_dir = path.parent.name
            mod = importlib.import_module(
                f"chaos_agent.agent.providers.{carrier_dir}.declaration"
            )
            # 一个 declaration 可声明多个 carrier（chaosblade 另含
            # chaosblade_python 的词汇，同子包双载体）。
            carrier_ids = [mod.CARRIER_ID]
            if hasattr(mod, "PYTHON_CARRIER_ID"):
                carrier_ids.append(mod.PYTHON_CARRIER_ID)
            for carrier in carrier_ids:
                assert carrier in fault_registry._CARRIER_VOCAB, (
                    f"{carrier_dir}/declaration.py 声明 carrier {carrier!r} "
                    f"未在 providers/__init__ 组装点注册——INTENT_* 聚合"
                    f"会静默缺失该载体词汇"
                )
            targets, actions = fault_registry._CARRIER_VOCAB[mod.CARRIER_ID]
            assert targets == tuple(mod.SUPPORTED_TARGETS), (
                f"{carrier_dir}: 注册的 targets 与 declaration 常量"
                f"不一致（组装点未走 declaration 引用？）"
            )
            assert actions == tuple(mod.SUPPORTED_ACTIONS), (
                f"{carrier_dir}: 注册的 actions 与 declaration 常量"
                f"不一致（组装点未走 declaration 引用？）"
            )
            # 同子包双载体（chaosblade_python）的词汇同样必须值一致
            # ——存在性断言只防漏注册，防不了传错 tuple。
            if hasattr(mod, "PYTHON_CARRIER_ID"):
                py_targets, py_actions = fault_registry._CARRIER_VOCAB[
                    mod.PYTHON_CARRIER_ID
                ]
                assert py_targets == tuple(mod.PYTHON_SUPPORTED_TARGETS), (
                    f"{carrier_dir}: 注册的 python 载体 targets 与"
                    f"declaration 常量不一致"
                )
                assert py_actions == tuple(mod.PYTHON_SUPPORTED_ACTIONS), (
                    f"{carrier_dir}: 注册的 python 载体 actions 与"
                    f"declaration 常量不一致"
                )

    def test_provider_class_attributes_reference_declarations(self):
        """spec「declaration 修改单点生效」：provider 类属性必须引用
        declaration 常量本身（同对象，tuple 不可变故同源即同 id）而非
        独立字面量——防类属性悄悄改回字面量后与 INTENT_* 聚合
        漂移（值相同的两份声明是 drift 定时炸弹）。"""
        from chaos_agent.agent.providers.chaosblade.declaration import (
            PYTHON_SUPPORTED_ACTIONS,
            PYTHON_SUPPORTED_TARGETS,
            SUPPORTED_ACTIONS,
            SUPPORTED_TARGETS,
        )
        from chaos_agent.agent.providers.chaosblade.provider import (
            ChaosbladeProvider,
        )
        from chaos_agent.agent.providers.chaosblade.python_provider import (
            ChaosbladePythonProvider,
        )
        from chaos_agent.agent.providers.host_shell.declaration import (
            SUPPORTED_ACTIONS as HOST_ACTIONS,
            SUPPORTED_TARGETS as HOST_TARGETS,
        )
        from chaos_agent.agent.providers.host_shell.provider import (
            HostShellProvider,
        )
        from chaos_agent.agent.providers.k8s_native.declaration import (
            SUPPORTED_ACTIONS as K8S_ACTIONS,
            SUPPORTED_TARGETS as K8S_TARGETS,
        )
        from chaos_agent.agent.providers.k8s_native.provider import (
            K8sNativeProvider,
        )

        assert ChaosbladeProvider.supported_targets is SUPPORTED_TARGETS
        assert ChaosbladeProvider.supported_actions is SUPPORTED_ACTIONS
        assert (
            ChaosbladePythonProvider.supported_targets is PYTHON_SUPPORTED_TARGETS
        )
        assert (
            ChaosbladePythonProvider.supported_actions is PYTHON_SUPPORTED_ACTIONS
        )
        assert K8sNativeProvider.supported_targets is K8S_TARGETS
        assert K8sNativeProvider.supported_actions is K8S_ACTIONS
        assert HostShellProvider.supported_targets is HOST_TARGETS
        assert HostShellProvider.supported_actions is HOST_ACTIONS


# ---------------------------------------------------------------------------
# Phase-13 (detection-import-retirement): 载体间横向 import 显式清单
# ---------------------------------------------------------------------------

#: 载体间横向 import 存量清单（phase-13 T5/5.2，AST 全量盘点）。每条 =
#: (载体子包文件, 指向另一载体子包的 import 模块)。原三类语义均已清偿：①
#: （chaosblade.detection 共享扫描工具）phase-14 G1 清偿归位平铺中立模块
#: ``providers/message_scanning.py``；② 边界判定知识（kubectl exec 内嵌
#: blade 的 embedded delivery / blade 失败后 native 接管）的词汇借
#: k8s_native 子命令表，phase-14 G2 清偿——词汇上提中立位
#: （message_scanning.KUBECTL_*_SUBCOMMANDS + exec_inner_command_mutates），
#: 判定逻辑留各自消费域；③ k8s_native classifier 对 inline ``blade ...``
#: 的递归委托，phase-14 G3 清偿——改经 registry 域路由接缝
#: （``FaultProviderRegistry.classify_inline_blade_command``）。phase-14
#: 清偿完毕：清单归空集，任何载体间横向 import 命中即失败（含跨子包
#: 相对形态——十三期 review 修复的解析器继续生效）；失活断言
#: ``seen == set()`` 永真，防清单腐化的同步收窄义务已完成。
_CARRIER_CROSS_IMPORTS: set[tuple[str, str]] = set()


class TestPhase13CarrierCrossImportLedger:
    """phase-13 —— 载体间横向 import 显式清单（spec: detection-import-
    boundary, G4）。

    载体子包文件 import 另一载体子包 = 横向依赖，全部登记在
    ``_CARRIER_CROSS_IMPORTS``（AST 全量盘点，5.2）：新增未登记即
    失败（提示走 registry 接缝或共享工具归位后更新清单）；条目消失
    （横向债已清偿）时失活断言提示收窄。跨子包相对形态
    （``from ..<carrier>...``）同样在扫描范围。
    """

    @staticmethod
    def _iter_cross_carrier_imports(
        path: Path, own_carrier: str, tree: ast.Module
    ):
        """Yield the RESOLVED module for every import leaving
        ``own_carrier`` for another carrier subpackage — absolute prefix
        form (``from chaos_agent.agent.providers.<other>...`` / ``import
        ...``) or any relative form (``from ..<other>.<mod> import ...``
        and the bare ``from .. import <other>``), resolved against
        ``path``'s package (phase-13 review fix: the bare form had been
        missed by the level>=2-module-nonempty check)."""
        prefixes = _carrier_subpackage_prefixes()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    for prefix in prefixes:
                        if module == prefix or module.startswith(prefix + "."):
                            if prefix.rsplit(".", 1)[-1] != own_carrier:
                                yield module
                            break
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    module = node.module or ""
                    for prefix in prefixes:
                        if module == prefix or module.startswith(prefix + "."):
                            if prefix.rsplit(".", 1)[-1] != own_carrier:
                                yield module
                            break
                else:
                    for resolved in _resolve_relative_import(path, node):
                        for prefix in prefixes:
                            if (
                                resolved == prefix
                                or resolved.startswith(prefix + ".")
                            ):
                                if prefix.rsplit(".", 1)[-1] != own_carrier:
                                    yield resolved
                                break

    def test_cross_imports_confined_to_ledger(self):
        providers_root = _SRC_ROOT / "agent" / "providers"
        seen: set = set()
        for path in sorted(providers_root.rglob("*.py")):
            rel = path.relative_to(_SRC_ROOT).as_posix()
            if rel in _PHASE11_PROVIDERS_GENERIC_FILES:
                continue  # 通用仲裁文件不是载体子包
            own_carrier = path.relative_to(providers_root).parts[0]
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for module in self._iter_cross_carrier_imports(
                path, own_carrier, tree
            ):
                assert (rel, module) in _CARRIER_CROSS_IMPORTS, (
                    f"{rel}: 载体间横向 import {module!r} 未登记 "
                    f"_CARRIER_CROSS_IMPORTS（新增横向依赖须走 registry "
                    f"接缝，或共享工具归位后更新清单）"
                )
                seen.add((rel, module))
        # 失活断言：横向债清偿后同步收窄清单，防清单腐化。
        assert seen == _CARRIER_CROSS_IMPORTS, (
            "phase-13 载体间横向清单与实际不符——已可收窄: "
            f"{sorted(_CARRIER_CROSS_IMPORTS - seen)}"
        )

    def test_guard_detects_synthetic_violation(self):
        """护栏自检：合成违规源码——绝对形态（指向另两个载体）与跨子包
        相对形态（``from ..chaosblade...``）必须全部被抓到；包内纵向
        （``from .provider`` / 指向 own 载体）与通用仲裁文件（base）
        不误伤。"""
        src = (
            "from chaos_agent.agent.providers.chaosblade.detection import x\n"
            "from chaos_agent.agent.providers.k8s_native.provider import y\n"
            "from chaos_agent.agent.providers.host_shell.declaration import z\n"
            "from ..chaosblade.verify import w\n"
            "from .. import chaosblade\n"
            "from .provider import Self\n"
            "from chaos_agent.agent.providers.base import FaultProvider\n"
        )
        fake_path = (
            _SRC_ROOT / "agent" / "providers" / "host_shell" / "provider.py"
        )
        hits = sorted(
            self._iter_cross_carrier_imports(
                fake_path, "host_shell", ast.parse(src)
            )
        )
        # host_shell.declaration 是 own 载体（纵向）不抓；.provider 是
        # level=1 包内纵向不抓；base 不是载体子包不抓。相对形态
        # （..chaosblade.verify 与裸 .. import chaosblade）解析为绝对
        # 路径后同样被抓到。
        assert hits == [
            "chaos_agent.agent.providers.chaosblade",
            "chaos_agent.agent.providers.chaosblade.detection",
            "chaos_agent.agent.providers.chaosblade.verify",
            "chaos_agent.agent.providers.k8s_native.provider",
        ]
