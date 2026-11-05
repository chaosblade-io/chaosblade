"""Phase-12 baseline snapshots — frozen BEFORE the spec-import retirement.

The phase-12 change (fault-handle-phase12-spec-import-retirement) rewrites how
``fault_registry`` aggregates carrier vocabulary (class-attribute reads at
import time → registered declaration data) and how ``plan_generator`` builds
the injection-command preview (direct ``build_blade_create_args`` → registry
seam). Both are load-bearing for downstream layers:

- ``INTENT_*`` constants feed the HTTP / CLI / LLM schemas and prompts at
  import time — aggregation ORDER is part of the contract (first-occurrence
  dedup over registration order, then family carrier precedence).
- The Injection Command section is user-visible markdown — the preview must
  keep matching what actually executes.

These tests freeze the CURRENT values byte-for-byte. They must stay green
across the refactor; a failure means the migration drifted, not that the
snapshot should be updated casually.
"""

import subprocess
import sys

import chaos_agent.transports as transports
from chaos_agent.agent.spec import fault_registry
from chaos_agent.agent.spec.fault_spec import (
    INTENT_ACTION_DESCRIPTION,
    INTENT_ACTIONS,
    INTENT_SCOPES,
    INTENT_TARGETS,
    INTENT_TARGET_DESCRIPTION,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.spec.plan_generator import (
    _section_inject_command,
    generate_injection_plan,
)


class TestIntentVocabularySnapshot:
    """INTENT_* aggregation — frozen values, order-sensitive."""

    def test_intent_scopes(self):
        assert INTENT_SCOPES == (
            "pod", "node", "container", "deployment", "statefulset",
            "daemonset", "service", "ingress", "configmap", "secret",
            # cronjob: victim-own-body scope for CronJob_任务异常 (4.2/4.3),
            # registered in fault_registry → derived here after secret.
            "cronjob",
            "host", "python",
        )

    def test_intent_targets(self):
        assert INTENT_TARGETS == (
            "cpu", "mem", "network", "disk", "process", "pod", "finalizer",
            "replicas", "schedule", "pvc", "dns", "image", "probe", "volume",
            "cni", "endpoint", "resources", "file", "redis", "mysql", "http",
            "httpx", "grpc", "kafka", "sqlalchemy",
        )

    def test_intent_actions(self):
        assert INTENT_ACTIONS == (
            "fullload", "load", "delay", "loss", "drop", "fill", "kill",
            "burn", "stop", "patch", "cordon", "taint", "delete", "drain",
            "scale", "corrupt", "duplicate", "throwCustomException",
            "returnValue",
        )

    def test_intent_target_description(self):
        assert INTENT_TARGET_DESCRIPTION == (
            "Subsystem under attack (NOT the resource instance name). "
            "Common values: cpu|mem|network|disk|process|pod|finalizer|"
            "replicas|schedule|pvc|dns|image|probe|volume|cni|endpoint|"
            "resources|file|redis|mysql|http|httpx|grpc|kafka|sqlalchemy. "
            "MUST be the fault target TYPE, never a pod/node name."
        )

    def test_intent_action_description(self):
        assert INTENT_ACTION_DESCRIPTION == (
            "Concrete fault action verb. "
            "ChaosBlade: fullload|load|delay|loss|drop|fill|kill|burn|stop. "
            "kubectl-native: patch|cordon|taint|delete|drain|scale|corrupt|"
            "duplicate. Python application (in-process): "
            "delay|throwCustomException|returnValue."
        )


class TestCarrierVocabularySnapshot:
    """Per-carrier declaration surface — the values D1 moves to
    ``providers/<carrier>/declaration.py``."""

    def test_chaosblade(self):
        assert fault_registry.carrier_targets("chaosblade") == (
            "cpu", "mem", "network", "disk", "process",
        )
        assert fault_registry.carrier_actions("chaosblade") == (
            "fullload", "load", "delay", "loss", "drop", "fill", "kill",
            "burn", "stop",
        )

    def test_k8s_native(self):
        assert fault_registry.carrier_targets("k8s_native") == (
            "pod", "finalizer", "replicas", "schedule", "pvc", "dns",
            "image", "probe", "volume", "cni", "endpoint", "resources",
            "file",
        )
        assert fault_registry.carrier_actions("k8s_native") == (
            "patch", "cordon", "taint", "delete", "drain", "scale",
            "corrupt", "duplicate",
        )

    def test_host_shell(self):
        # Shares the OS-subsystem target vocabulary with chaosblade but its
        # action set omits ``stop``.
        assert fault_registry.carrier_targets("host_shell") == (
            "cpu", "mem", "network", "disk", "process",
        )
        assert fault_registry.carrier_actions("host_shell") == (
            "fullload", "load", "delay", "loss", "drop", "fill", "kill",
            "burn",
        )

    def test_chaosblade_python(self):
        assert fault_registry.carrier_targets("chaosblade_python") == (
            "redis", "mysql", "http", "httpx", "grpc", "kafka", "sqlalchemy",
        )
        assert fault_registry.carrier_actions("chaosblade_python") == (
            "delay", "throwCustomException", "returnValue",
        )

    def test_unknown_carrier_is_empty(self):
        assert fault_registry.carrier_targets("nope") == ()
        assert fault_registry.carrier_actions("nope") == ()

    def test_derived_scope_predicates(self):
        assert fault_registry.host_scopes() == frozenset({"host"})
        assert fault_registry.python_scopes() == frozenset({"python"})
        assert fault_registry.aggregate_cluster_scoped() == frozenset({
            "node", "pv", "namespace", "clusterrole", "clusterrolebinding",
            "storageclass", "host", "python",
        })
        assert fault_registry.is_host_scope("host")
        assert not fault_registry.is_host_scope("pod")
        assert fault_registry.is_python_scope("python")


class TestInjectCommandSnapshot:
    """Injection Command preview — the exact text D3 moves behind the
    registry seam. Frozen byte-for-byte (kubewiz on/off × kubeconfig)."""

    SPEC = FaultSpec(
        scope="pod", fault_target="cpu", fault_action="fullload",
        namespace="prod", names=("api-0", "api-1"), labels={"app": "api"},
        params={"cpu-percent": "80"}, params_flags=("evict-count", "2"),
        duration_seconds=300,
    )

    def test_off_channel_with_kubeconfig(self, monkeypatch):
        monkeypatch.setattr(
            transports, "is_kubewiz_channel", lambda *a, **k: False
        )
        out = _section_inject_command(self.SPEC, {"kubeconfig": "/fake/kc"})
        assert out == (
            "## Injection Command\n\n"
            "```bash\n"
            "blade create k8s pod-cpu fullload \\\n"
            "  --namespace prod \\\n"
            "  --names api-0,api-1 \\\n"
            "  --labels app=api \\\n"
            "  --cpu-percent 80 \\\n"
            "  --evict-count \\\n"
            "  --2 \\\n"
            "  --timeout 300 \\\n"
            "  --kubeconfig /fake/kc\n"
            "```"
        )

    def test_on_channel_omits_kubeconfig(self, monkeypatch):
        monkeypatch.setattr(
            transports, "is_kubewiz_channel", lambda *a, **k: True
        )
        out = _section_inject_command(self.SPEC, {"kubeconfig": "/fake/kc"})
        assert out == (
            "## Injection Command\n\n"
            "```bash\n"
            "blade create k8s pod-cpu fullload \\\n"
            "  --namespace prod \\\n"
            "  --names api-0,api-1 \\\n"
            "  --labels app=api \\\n"
            "  --cpu-percent 80 \\\n"
            "  --evict-count \\\n"
            "  --2 \\\n"
            "  --timeout 300\n"
            "```"
        )

    def test_off_channel_without_kubeconfig(self, monkeypatch):
        # No kubeconfig in state → no --kubeconfig flag even off-channel.
        monkeypatch.setattr(
            transports, "is_kubewiz_channel", lambda *a, **k: False
        )
        out = _section_inject_command(self.SPEC, {})
        assert out == (
            "## Injection Command\n\n"
            "```bash\n"
            "blade create k8s pod-cpu fullload \\\n"
            "  --namespace prod \\\n"
            "  --names api-0,api-1 \\\n"
            "  --labels app=api \\\n"
            "  --cpu-percent 80 \\\n"
            "  --evict-count \\\n"
            "  --2 \\\n"
            "  --timeout 300\n"
            "```"
        )

    def test_incomplete_spec_returns_empty(self):
        assert _section_inject_command(FaultSpec(), {}) == ""
        assert _section_inject_command(
            FaultSpec(scope="pod", fault_target="cpu"), {}
        ) == ""

    def test_host_scope_falls_back_to_blade_carrier(self, monkeypatch):
        """家族优先序回退（tasks 4.3）：host 家族 carriers =
        (chaosblade, host_shell)，preview 委托命中优先序第一的
        chaosblade builder——文本与改前硬编码路径 byte-identical。"""
        monkeypatch.setattr(
            transports, "is_kubewiz_channel", lambda *a, **k: False
        )
        spec = FaultSpec(
            scope="host", fault_target="cpu", fault_action="fullload",
            params={"cpu-percent": "80"}, duration_seconds=60,
        )
        assert _section_inject_command(spec, {}) == (
            "## Injection Command\n\n"
            "```bash\n"
            "blade create k8s host-cpu fullload \\\n"
            "  --cpu-percent 80 \\\n"
            "  --timeout 60\n"
            "```"
        )

    def test_python_scope_omits_section(self):
        """python scope 的行为变化（phase-12 有意为之）：改前对任意
        完整 spec 渲染 blade 命令（python scope 会得到错误的
        ``blade create k8s python-...`` 前缀）；改后 python_app 家族
        唯一 carrier 未注册 preview builder，段整体省略。"""
        spec = FaultSpec(
            scope="python", fault_target="redis", fault_action="delay",
        )
        assert _section_inject_command(spec, {"kubeconfig": "/kc"}) == ""

    def test_full_plan_contains_command_section(self, monkeypatch):
        # Smoke: the section still composes into the full plan markdown.
        monkeypatch.setattr(
            transports, "is_kubewiz_channel", lambda *a, **k: True
        )
        # read_fault_spec expects the wire dict form, not the instance.
        state = {"kubeconfig": "/fake/kc", "fault_spec": self.SPEC.to_dict()}
        plan = generate_injection_plan(state)
        assert "## Injection Command" in plan
        assert "blade create k8s" in plan


class TestSettingsBladePathResolution:
    """Current ``settings._resolve_blade_path`` behaviour (D4 keeps the
    externally-observable resolution semantics: explicit setting wins over
    auto-detection)."""

    def test_explicit_setting_wins(self, monkeypatch):
        from chaos_agent.config import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.settings, "blade_path", "/explicit/blade", raising=False
        )
        assert settings_mod.settings._resolve_blade_path() == "/explicit/blade"

    def test_empty_falls_back_to_autodetect(self, monkeypatch):
        from chaos_agent.config import settings as settings_mod

        monkeypatch.setattr(
            settings_mod.settings, "blade_path", "", raising=False
        )
        # The fallback lazy-imports get_bundled_blade_path from the
        # carrier's declaration module (phase-12 D4 reroute); patch at the
        # source module it reads from.
        import chaos_agent.agent.providers.chaosblade.declaration as decl_mod

        monkeypatch.setattr(
            decl_mod, "get_bundled_blade_path", lambda: "/bundled/blade"
        )
        assert settings_mod.settings._resolve_blade_path() == "/bundled/blade"


class TestAssemblyDefence:
    """phase-12 D2 组装防御——两条加载路径都必须拿到完整词汇。

    子进程冷导入是唯一可信的验证方式：pytest 进程内 fault_spec /
    providers 大概率已被其他测试加载，import 顺序已被污染，验证不了
    「谁是第一个 importer」对组装的影响。
    """

    @staticmethod
    def _run_cold(code: str):
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"cold-import failed:\n{result.stderr}"
        )

    def test_direct_fault_registry_import_aggregates(self):
        """绕过 fault_spec 直接 import fault_registry：聚合函数的
        lazy 组装防御（_ensure_carrier_vocab）必须让词汇完整，
        而不是静默返回空聚合。"""
        self._run_cold(
            "from chaos_agent.agent.spec.fault_registry import "
            "aggregate_targets, aggregate_actions, carrier_actions\n"
            "t = aggregate_targets()\n"
            "assert 'cpu' in t and 'file' in t and 'sqlalchemy' in t, t\n"
            "a = aggregate_actions()\n"
            "assert 'fullload' in a and 'returnValue' in a, a\n"
            "assert 'stop' in carrier_actions('chaosblade')\n"
        )

    def test_cold_fault_spec_import_derives_full_intent(self):
        """冷导入 fault_spec：INTENT_* 无需任何 runtime bootstrap 即完整
        （fault_spec 顶层 import providers 组装点触发 declaration 注册）。"""
        self._run_cold(
            "from chaos_agent.agent.spec.fault_spec import "
            "INTENT_SCOPES, INTENT_TARGETS, INTENT_ACTIONS\n"
            "assert 'host' in INTENT_SCOPES and 'python' in INTENT_SCOPES\n"
            "assert len(INTENT_TARGETS) == 25, INTENT_TARGETS\n"
            "assert len(INTENT_ACTIONS) == 19, INTENT_ACTIONS\n"
        )

    def test_cold_providers_import_assembles_vocabulary(self):
        """冷导入直接 import providers（第三条加载路径，不经 fault_spec
        也不先经 fault_registry）：组装段在 register_builtins 之前执行，
        fault_registry 聚合与 preview 接缝同样完整可用。"""
        self._run_cold(
            "import chaos_agent.agent.providers\n"
            "from chaos_agent.agent.spec import fault_registry\n"
            "assert 'cpu' in fault_registry.aggregate_targets()\n"
            "assert 'sqlalchemy' in fault_registry.aggregate_targets()\n"
            "assert fault_registry.command_preview_for("
            "scope='pod', target='cpu', action='fullload') is not None\n"
        )

    def test_command_preview_for_delegates_by_family_precedence(self):
        """command_preview_for 按家族 carrier_types 优先序委托：blade
        scope 命中 chaosblade builder；无 builder 的 carrier 家族返回
        None（plan 层据此省略该段）。"""
        out = fault_registry.command_preview_for(
            scope="pod", target="cpu", action="fullload",
            namespace="demo", names="", labels="", kubeconfig="",
            params=None, params_flags=None, duration=0,
        )
        assert out is not None
        assert out.startswith("## Injection Command")
        assert "blade create k8s pod-cpu fullload" in out
        # Unknown scope → no family → None (plan renders no section).
        assert fault_registry.command_preview_for(
            scope="nope", target="cpu", action="fullload"
        ) is None
        # Family exists but its sole carrier registered no preview builder
        # → None. This is the explicit D3 behaviour decision for non-blade
        # families: omitting the section beats rendering a wrong
        # ``blade create k8s python-...`` prefix.
        assert fault_registry.command_preview_for(
            scope="python", target="redis", action="delay"
        ) is None

    def test_preview_falls_back_to_next_carrier_in_family(self, monkeypatch):
        """家族候选序回退（spec Scenario「家族候选序决定预览载体」）：
        host 家族 carriers = (chaosblade, host_shell)，首选 chaosblade
        未注册 builder 而次选已注册时，接缝必须委托次选 builder。"""
        monkeypatch.setattr(
            fault_registry,
            "_PREVIEW_BUILDERS",
            {"host_shell": lambda **fields: "host_shell-preview"},
        )
        out = fault_registry.command_preview_for(
            scope="host", target="cpu", action="fullload"
        )
        assert out == "host_shell-preview"
