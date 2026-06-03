"""Application configuration via pydantic-settings.

Configuration priority (highest to lowest):
  1. ~/.blade-ai/config.json (managed by `blade-ai config` CLI)
  2. Environment variables (BLADE_AI_* prefix)
  3. Code defaults
"""

import json
import logging
from pathlib import Path
from typing import Any, Tuple, Type

from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

logger = logging.getLogger(__name__)

# Path to the unified config file managed by `blade-ai config`
_CONFIG_FILE = Path.home() / ".blade-ai" / "config.json"

# Models we've already warned about, to silence repeat WARNINGs when
# resolve_context_budget is called many times for the same unconfigured
# model. Cleared on Settings.reload() so a user who edits
# model_budgets mid-session gets fresh feedback.
_WARNED_FALLBACK_MODELS: set[str] = set()


# v7 M2 — per-model context budgets.
#
# Each entry maps a model-name PREFIX (case-insensitive) to its
# context window size + the compact_ratio that's appropriate for
# that window. The resolver picks the longest matching prefix, then
# falls back to the global ``context_max_tokens`` /
# ``context_compact_ratio`` settings if nothing matches.
#
# Window sources: provider docs (claude.ai/docs, platform.openai.com,
# dashscope.aliyun.com, deepseek docs, bigmodel.cn).
# Compact-ratio rationale: smaller/cheaper models can fill more of the
# window before compacting (0.90); models with large windows want to
# leave more headroom for tool outputs (0.80–0.85).
_DEFAULT_MODEL_BUDGETS: dict[str, dict[str, float | int]] = {
    # Anthropic
    "claude-opus":      {"max_tokens": 200_000, "compact_ratio": 0.85},
    "claude-sonnet":    {"max_tokens": 200_000, "compact_ratio": 0.85},
    "claude-haiku":     {"max_tokens": 200_000, "compact_ratio": 0.90},
    # OpenAI
    "gpt-4o":           {"max_tokens": 128_000, "compact_ratio": 0.85},
    "gpt-4":            {"max_tokens": 128_000, "compact_ratio": 0.85},
    "o1":               {"max_tokens": 128_000, "compact_ratio": 0.85},
    # Alibaba Qwen (DashScope)
    "qwen3.6-max":      {"max_tokens": 131_072, "compact_ratio": 0.80},
    "qwen3-max":        {"max_tokens": 131_072, "compact_ratio": 0.80},
    "qwen3":            {"max_tokens": 131_072, "compact_ratio": 0.80},
    "qwen-max":         {"max_tokens":  32_768, "compact_ratio": 0.80},
    "qwen-plus":        {"max_tokens":  32_768, "compact_ratio": 0.80},
    # DeepSeek
    "deepseek":         {"max_tokens":  64_000, "compact_ratio": 0.80},
    # Zhipu GLM
    "glm-4":            {"max_tokens": 128_000, "compact_ratio": 0.85},
    "glm-5":            {"max_tokens": 128_000, "compact_ratio": 0.85},
}


class JsonConfigSettingsSource(PydanticBaseSettingsSource):
    """Custom settings source that reads from ~/.blade-ai/config.json.

    Empty-string semantics: a string field whose JSON value is ``""``
    (or whitespace-only) is treated as **unset**, so the next source
    in the priority chain (env vars, then code defaults) gets to
    provide a value. Without this, an accidentally-blank
    ``"api_base_url": ""`` in the config file would override the
    sensible default and leave the LLM trying to dial an empty URL —
    LangChain's openai client builds successfully with an empty base
    but every request fails / hangs on timeout, with no obvious
    error surface for the user.

    Non-string types (int / float / bool / list / dict) pass through
    as-is — their "unset" sentinels are type-specific and the wizard
    never writes them blank anyway.
    """

    @staticmethod
    def _is_unset(value: Any) -> bool:
        """True if the JSON value should be treated as 'not provided'."""
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
        return False

    def get_field_value(
        self, field: Field, field_name: str
    ) -> Tuple[Any, str, bool]:
        if not _CONFIG_FILE.exists():
            return None, field_name, False
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None, field_name, False
        if field_name in data and not self._is_unset(data[field_name]):
            return data[field_name], field_name, True
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if not _CONFIG_FILE.exists():
            return data
        try:
            file_data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return data
        for field_name in self.settings_cls.model_fields:
            if field_name in file_data and not self._is_unset(file_data[field_name]):
                data[field_name] = file_data[field_name]
        return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BLADE_AI_",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,  # Allow JsonConfigSettingsSource to use field_name keys
                                 # (needed because kubeconfig_path has validation_alias)
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        """Priority: init > config.json > env vars > defaults."""
        return (
            init_settings,
            JsonConfigSettingsSource(settings_cls),
            env_settings,
        )

    # LLM配置 (提供商无关，支持任何OpenAI兼容API)
    llm_api_key: str = ""                     # BLADE_AI_LLM_API_KEY
    model_name: str = "qwen3.6-max-preview"               # BLADE_AI_MODEL_NAME
    api_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # BLADE_AI_API_BASE_URL
    # LLM retry budget. 1 (down from 3) keeps the UX honest: a single
    # silent retry covers a transient network blip; more retries just
    # delay the visible error by ``N × llm_read_timeout`` seconds while the
    # user stares at a spinner. The actual transient error is also
    # surfaced via the ``on_llm_error`` tracing callback so the user
    # sees the retry happening even before it resolves.
    llm_max_retries: int = 1                  # BLADE_AI_LLM_MAX_RETRIES
    llm_temperature: float = 0.7              # BLADE_AI_LLM_TEMPERATURE
    llm_enable_thinking: bool = True           # BLADE_AI_LLM_ENABLE_THINKING，启用模型深度思考模式(如Qwen的enable_thinking)

    # Verifier配置
    verifier_json_mode: bool = True            # BLADE_AI_VERIFIER_JSON_MODE，最终迭代启用 response_format JSON 模式强制结构化输出

    tokenizer_model_override: str = ""        # BLADE_AI_TOKENIZER_MODEL_OVERRIDE，非空时替代 model_name 用于 tokenizer 选型（如 fine-tune 模型回落已知 base）
    tokenizer_use_hf: bool = False            # BLADE_AI_TOKENIZER_USE_HF，启用 HuggingFace AutoTokenizer 兜底（Layer 3，按需加载 transformers）
    tokenizer_use_vendor_api: bool = False    # BLADE_AI_TOKENIZER_USE_VENDOR_API，预留：未来调用厂商 count_tokens API（当前 no-op）

    # Server配置
    server_port: int = 8089                   # BLADE_AI_SERVER_PORT
    server_host: str = "0.0.0.0"              # BLADE_AI_SERVER_HOST

    # Skill配置
    skills_dir: Path = Path("~/.blade-ai/skills")  # BLADE_AI_SKILLS_DIR，运行时通过 get_skills_dir() 动态解析
    disabled_skills: list[str] = []                # BLADE_AI_DISABLED_SKILLS，被用户主动禁用的技能（保留文件但加载时跳过）

    # ChaosBlade vendor 目录（pip install 用户的运行时安装目标）
    chaosblade_vendor_dir: Path = Path("~/.blade-ai/vendor")  # BLADE_AI_CHAOSBLADE_VENDOR_DIR

    # 确认开关
    confirmation_required: bool = True        # BLADE_AI_CONFIRMATION_REQUIRED

    # 经验自进化开关
    self_evolution: bool = False              # BLADE_AI_SELF_EVOLUTION

    # T6 postmortem 自动生成开关
    # 默认开 — TUI 用户主线场景；L4 lib 用户可通过环境变量 opt-out
    #
    # ⚠️ 隐私：postmortem 生成会把 fault_spec / user_description /
    #   最近 N 条 messages 摘要 / verification.side_effects 等数据
    #   塞进 LLM context。当配置的是云端 LLM (DashScope / OpenAI /
    #   Anthropic 等) 时，这些数据将**离开本地 host**。涉及敏感业务
    #   名 / 生产 namespace / 机密 pod 命名时，建议 opt-out (置
    #   BLADE_AI_POSTMORTEM_ENABLED=false) 或切到本地 LLM (Ollama 等)。
    postmortem_enabled: bool = True           # BLADE_AI_POSTMORTEM_ENABLED
    # LLM 调用上限（秒）；超时降级为 postmortem=None，不阻塞 result 输出
    # 默认 30s 覆盖典型场景；慢模型 / 大 prompt 可调到 60-120s
    postmortem_timeout_seconds: int = 300      # BLADE_AI_POSTMORTEM_TIMEOUT_SECONDS
    # 喂给 LLM 的 messages 尾部条数；超过此数取最后 N 条 + 一句"前面省略 X 条"
    # 也是隐私边界：减小可缩小上传 LLM 的对话窗口
    postmortem_max_messages: int = 100         # BLADE_AI_POSTMORTEM_MAX_MESSAGES

    # 工具路径 (blade_path 使用 get_bundled_blade_path() 自动检测内嵌/系统 blade)
    blade_path: str = ""                    # BLADE_AI_BLADE_PATH, 空值则自动检测
    kubectl_path: str = "kubectl"             # BLADE_AI_KUBECTL_PATH

    # K8s 集群连接配置
    # 同时支持 BLADE_AI_KUBECONFIG_PATH 和标准 KUBECONFIG 环境变量（前者优先）
    kubeconfig_path: str = Field(
        default="",
        validation_alias=AliasChoices("BLADE_AI_KUBECONFIG_PATH", "KUBECONFIG"),
    )  # BLADE_AI_KUBECONFIG_PATH / KUBECONFIG
    kube_context: str = ""        # BLADE_AI_KUBE_CONTEXT，空值则使用 kubeconfig 当前 context

    # 全局默认超时(秒)
    command_timeout: int = 60                # BLADE_AI_COMMAND_TIMEOUT

    # 分工具超时配置(秒)
    timeout_blade: int = 30                  # BLADE_AI_TIMEOUT_BLADE
    timeout_kubectl: int = 30                # BLADE_AI_TIMEOUT_KUBECTL
    timeout_kubectl_exec: int = 180          # BLADE_AI_TIMEOUT_KUBECTL_EXEC
    # LLM timeout split into connect vs read (httpx.Timeout semantics).
    # ``llm_connect_timeout`` bounds TCP/TLS connection establishment —
    # short (10s) so a misconfigured base URL / DNS / firewall surfaces a
    # clear error fast. ``llm_read_timeout`` bounds how long we wait for
    # the model's response (time-to-first-token + between-chunk gaps when
    # streaming, or whole-body wait when non-streaming) — generous (180s)
    # because thinking models (Qwen enable_thinking) can take well over
    # 30s to produce their reasoning on complex ReAct prompts. A single
    # scalar would have to choose one number for both, forcing either slow
    # connect-failure or premature read-timeout; splitting them avoids that.
    llm_connect_timeout: int = 10            # BLADE_AI_LLM_CONNECT_TIMEOUT
    llm_read_timeout: int = 600              # BLADE_AI_LLM_READ_TIMEOUT
    timeout_baseline_llm: int = 600            # BLADE_AI_TIMEOUT_BASELINE_LLM，baseline LLM 策略总超时
    timeout_default: int = 60                # BLADE_AI_TIMEOUT_DEFAULT
    timeout_skill_script: int = 60           # BLADE_AI_TIMEOUT_SKILL_SCRIPT，skill 脚本执行超时

    # 实验级默认超时(秒) — blade create 无 --timeout 时自动注入
    # NOTE: This must be >= _DEFAULT_MIN_DURATION in fault_type.py (currently 600)
    experiment_timeout: int = 600            # BLADE_AI_EXPERIMENT_TIMEOUT

    # Confirm gate 等待用户决策的最大秒数 — 超过则服务端礼貌中断 turn，避免用户离开后未回收 future
    # 默认 1800s (30 分钟) 对绝大多数排查场景够用；遇到复杂研判可调长，例如 7200 (2h)
    confirm_wait_timeout: int = 1800         # BLADE_AI_CONFIRM_WAIT_TIMEOUT

    # OpenTelemetry GenAI export (parallel to built-in tracing)
    otel_enabled: bool = False              # BLADE_AI_OTEL_ENABLED
    otel_endpoint: str = ""                 # BLADE_AI_OTEL_ENDPOINT (gRPC, e.g. http://localhost:4317)
    otel_service_name: str = "blade-ai"     # BLADE_AI_OTEL_SERVICE_NAME
    otel_provider_name: str = ""            # BLADE_AI_OTEL_PROVIDER_NAME (空=auto-detect from api_base_url)
    # When true, GET /metrics serves the OTel meter's data in Prometheus
    # text format (scraped via the same FastAPI port). Independent of
    # otel_enabled — you can run Prometheus-only or OTLP-only.
    prometheus_enabled: bool = False        # BLADE_AI_PROMETHEUS_ENABLED

    # E10 — multi-dimensional safety score (blast_radius / frequency /
    # time / topology). Always computed (cheap), advisory by default.
    # The ``time`` dimension uses Beijing time (UTC+8) per the project's
    # global timezone convention in ``chaos_agent.utils.time``.
    # Routing flag below lets a high overall upgrade safety_status.
    #
    # CAVEAT: enabling routing_enabled changes the inject graph's
    # routing. ``safe + needs_confirmation=False`` normally auto-executes
    # (skips confirmation_gate); after escalation to ``warning`` /
    # ``confirm_required`` the graph forces a confirmation_gate interrupt
    # which CLI / non-interactive runs cannot respond to and will block
    # on. Use only in TUI / HTTP modes that actually drive the confirm
    # response, or pair with ``--force-override`` in CLI.
    safety_score_routing_enabled: bool = False    # BLADE_AI_SAFETY_SCORE_ROUTING_ENABLED
    safety_score_warning_threshold: int = 70      # BLADE_AI_SAFETY_SCORE_WARNING_THRESHOLD
    safety_score_confirm_threshold: int = 90      # BLADE_AI_SAFETY_SCORE_CONFIRM_THRESHOLD
    # When true, topology dimension augments heuristic with a kubectl
    # query (replica count for deployments). Falls back silently on
    # kubectl error — never blocks safety_check.
    safety_score_topology_deep: bool = False      # BLADE_AI_SAFETY_SCORE_TOPOLOGY_DEEP

    # Per-server attach_to allowlist is the second-level gate.
    mcp_enabled: bool = False                     # BLADE_AI_MCP_ENABLED
    mcp_config_path: str = "~/.blade-ai/mcp.json" # BLADE_AI_MCP_CONFIG_PATH (empty → ~/.blade-ai/mcp.json)
    mcp_connect_timeout_seconds: int = 30         # BLADE_AI_MCP_CONNECT_TIMEOUT_SECONDS

    # kubectl 输出控制
    kubectl_max_output_bytes: int = 32768       # BLADE_AI_KUBECTL_MAX_OUTPUT_BYTES，超过此大小的 JSON 输出追加提示

    # 安全配置
    safety_blacklist_namespaces: str = ""  # BLADE_AI_SAFETY_BLACKLIST_NAMESPACES

    # Agent Loop上限
    max_agent_loop: int = 100                # BLADE_AI_MAX_AGENT_LOOP
    max_execute_loop: int = 100              # BLADE_AI_MAX_EXECUTE_LOOP
    max_verifier_loop: int = 60              # BLADE_AI_MAX_VERIFIER_LOOP
    max_recover_verifier_loop: int = 60      # BLADE_AI_MAX_RECOVER_VERIFIER_LOOP
    max_recover_layer1_iterations: int = 60  # BLADE_AI_MAX_RECOVER_LAYER1_ITERATIONS (non-ChaosBlade LLM sub-loop)
    max_plan_builder_rounds: int = 40        # BLADE_AI_MAX_PLAN_BUILDER_ROUNDS
    max_clarification_rounds: int = 10       # BLADE_AI_MAX_CLARIFICATION_ROUNDS
    max_dialogue_rounds: int = 999           # BLADE_AI_MAX_DIALOGUE_ROUNDS
    stagnation_threshold: int = 5            # BLADE_AI_STAGNATION_THRESHOLD，同一工具连续调用 N 次触发 action stagnation
    recursion_limit: int = 500               # BLADE_AI_RECURSION_LIMIT

    # 循环检测（重复工具调用）
    loop_detection_window: int = 10          # BLADE_AI_LOOP_DETECTION_WINDOW，检测最近 N 条消息
    loop_detection_threshold: int = 3        # BLADE_AI_LOOP_DETECTION_THRESHOLD，相同调用超过 N 次触发提示

    # 空闲轮次检测（连续无工具调用的AI响应）
    idle_turn_threshold: int = 3             # BLADE_AI_IDLE_TURN_THRESHOLD，连续 N 轮无工具调用触发收敛提示

    # Replan配置 (Phase 2 → Phase 1 错误回退)
    max_replan_count: int = 3                    # BLADE_AI_MAX_REPLAN_COUNT
    replan_auto_trigger: bool = True             # BLADE_AI_REPLAN_AUTO_TRIGGER, 自动检测可replan的错误模式
    replan_reset_execute_count: bool = True      # BLADE_AI_REPLAN_RESET_EXECUTE_COUNT, replan后重置execute_loop_count

    # Patch C — Wall-clock timeout 兜底
    # 单次 inject turn 的硬性墙钟上限。0 = 关闭（保留历史行为）；>0
    # 时所有 ``should_continue_*`` router 都会检查并强制走 "end" 分
    # 支。配合 patch B 的 INFRA_TRANSIENT short-retry 一起使用，避
    # 免基础设施抖动让 turn 跑数分钟还在转圈。
    max_inject_seconds: int = 0                  # BLADE_AI_MAX_INJECT_SECONDS

    # Patch B — INFRA_TRANSIENT 类错误的额外短重试预算
    # 当 ``classify_error`` 判定 ErrorAction.SHORT_RETRY 时，router
    # 允许 LLM 再发起最多 N 次同样的 tool 调用；超出后转 "end"。3
    # 是经验上不会让用户感到卡顿的合理上限。
    max_transient_retry: int = 3                 # BLADE_AI_MAX_TRANSIENT_RETRY

    # Patch D — Target health pre-check
    # ``target_health_check_enabled`` 控制 agent_loop 提交 fault_intent
    # 前是否调用 ``assess_target_health`` 把目标的 DiskPressure /
    # Evicted / Pending 等 blocker 注入 confirm card 的 payload。
    # ``target_health_check_block_on_blocker`` 控制检测到 BLOCK 时是
    # 否阻断 graph（默认仅 warn-only，把信息丢给用户/LLM 决策）。
    target_health_check_enabled: bool = True              # BLADE_AI_TARGET_HEALTH_CHECK_ENABLED
    target_health_check_block_on_blocker: bool = False    # BLADE_AI_TARGET_HEALTH_CHECK_BLOCK_ON_BLOCKER

    blade_agent_check_enabled: bool = True               # BLADE_AI_BLADE_AGENT_CHECK_ENABLED
    blade_agent_namespace: str = "chaosblade"            # BLADE_AI_BLADE_AGENT_NAMESPACE
    blade_agent_label: str = "app=chaosblade-tool"       # BLADE_AI_BLADE_AGENT_LABEL

    feasibility_check_enabled: bool = True               # BLADE_AI_FEASIBILITY_CHECK_ENABLED
    feasibility_check_block_on_impossible: bool = False   # BLADE_AI_FEASIBILITY_CHECK_BLOCK_ON_IMPOSSIBLE

    # Retry配置
    retry_max_retries: int = 3               # BLADE_AI_RETRY_MAX_RETRIES
    retry_base_delay: float = 1.0            # BLADE_AI_RETRY_BASE_DELAY
    retry_max_delay: float = 30.0            # BLADE_AI_RETRY_MAX_DELAY
    retry_exponential_base: float = 2.0      # BLADE_AI_RETRY_EXPONENTIAL_BASE
    retry_jitter: bool = True                # BLADE_AI_RETRY_JITTER

    # Checkpoint持久化 (默认存放在 memory_dir 下)
    checkpoint_db_path: Path = Path("")   # BLADE_AI_CHECKPOINT_DB_PATH, 空值则使用 memory_dir/checkpoints.db

    # TaskStore持久化 (默认存放在 memory_dir 下)
    tasks_db_path: Path = Path("")       # BLADE_AI_TASKS_DB_PATH, 空值则使用 memory_dir/tasks.db
    tasks_db_backend: str = "sqlite"     # BLADE_AI_TASKS_DB_BACKEND, "sqlite" 或 "postgresql"
    tasks_pg_dsn: str = ""              # BLADE_AI_TASKS_PG_DSN, PostgreSQL DSN (仅 postgresql 后端需要)

    # 存储目录
    memory_dir: Path = Path("~/.blade-ai/memory")  # BLADE_AI_MEMORY_DIR，与 config.json 同级
    working_dir: Path = Path(".")            # BLADE_AI_WORKING_DIR

    # 会话存储配置
    save_system_message: bool = True  # BLADE_AI_SAVE_SYSTEM_MESSAGE，是否在会话文件中保存SystemMessage

    # 上下文窗口配置（per-model 优先；这两项是兜底，仅当 model_budgets
    # 中没有匹配前缀时才生效。resolve_context_budget() 是单一入口）
    context_max_tokens: int = 128000  # BLADE_AI_CONTEXT_MAX_TOKENS，LLM上下文窗口大小（fallback）
    context_compact_ratio: float = 0.85  # BLADE_AI_CONTEXT_COMPACT_RATIO，压缩触发比例（fallback）

    # v7 M2 — per-model 上下文预算覆盖。键是模型名前缀（大小写不敏感），
    # 值是 {"max_tokens": int, "compact_ratio": float}。空 dict 时直接走
    # _DEFAULT_MODEL_BUDGETS；用户在此添加的条目会按"最长前缀优先"覆盖
    # 内置默认。整体匹配不到时回落 context_max_tokens / context_compact_ratio。
    # env 用 BLADE_AI_MODEL_BUDGETS 传 JSON 字符串。
    model_budgets: dict[str, dict[str, float | int]] = Field(default_factory=dict)

    # SSE token batching — server-side coalescing of token/thinking events.
    # 0 = disabled (each event yields immediately, legacy behaviour).
    sse_batch_interval_ms: int = 30  # BLADE_AI_SSE_BATCH_INTERVAL_MS
    sse_batch_chars: int = 30        # BLADE_AI_SSE_BATCH_CHARS

    # Skill 脚本执行配置
    skill_script_max_output: int = 4000  # BLADE_AI_SKILL_SCRIPT_MAX_OUTPUT，返回给 LLM 的 stdout 最大字符数

    # Target-drift guard 子系统 (chaos_agent.agent.target_guard).
    # 默认 False 是灰度开关——先在生产环境观察 screener 的 log-only
    # 判定，确认无误判后改为 True 才真正拦截 execute_loop 的偏离调用。
    target_guard_enforcing: bool = True  # BLADE_AI_TARGET_GUARD_ENFORCING
    # 是否允许 _execute_skill_script 工具（默认 False = 禁用）。脚本
    # 内容对 classifier 不透明，开启等同于给 execute_loop 一个无法
    # 审计的 escape hatch；只有信任 skill 来源的运营场景才打开。
    skill_script_default_allow: bool = True  # BLADE_AI_SKILL_SCRIPT_DEFAULT_ALLOW

    # Phase 1 (planning) screener enforcement (default True). When True,
    # any tool_call classified as non-readonly is rejected at the
    # phase1_screener node and the LLM is asked to retry. When False,
    # violations are logged at WARNING level but the call still proceeds
    # through phase1_tools — only useful for post-incident analysis;
    # production should stay True. See
    # ``chaos_agent.agent.nodes.phase1_screener`` for rationale.
    phase1_screener_enforcing: bool = True  # BLADE_AI_PHASE1_SCREENER_ENFORCING

    # 日志级别 (DEBUG=显示LLM迭代详情, INFO=正常输出, WARNING=静默模式)
    log_level: str = "DEBUG"                  # BLADE_AI_LOG_LEVEL

    def _resolve_blade_path(self) -> str:
        """Resolve blade path: explicit setting > auto-detect."""
        if self.blade_path:
            return self.blade_path
        from chaos_agent.utils.blade_paths import get_bundled_blade_path
        return get_bundled_blade_path()

    @property
    def blacklist_namespaces(self) -> list[str]:
        return [ns.strip() for ns in self.safety_blacklist_namespaces.split(",") if ns.strip()]

    @property
    def is_debug(self) -> bool:
        """Check if debug mode is enabled (log_level=DEBUG)."""
        return self.log_level.upper() == "DEBUG"

    @property
    def resolved_memory_dir(self) -> Path:
        """Return memory_dir with ~ expanded."""
        return self.memory_dir.expanduser()

    @property
    def resolved_checkpoint_db_path(self) -> Path:
        """Return checkpoint_db_path; if empty, use memory_dir/checkpoints.db."""
        if self.checkpoint_db_path and str(self.checkpoint_db_path) != ".":
            return self.checkpoint_db_path
        return self.resolved_memory_dir / "checkpoints.db"

    @property
    def resolved_tasks_db_path(self) -> Path:
        """Return tasks_db_path; if empty, use memory_dir/tasks.db."""
        if self.tasks_db_path and str(self.tasks_db_path) != ".":
            return self.tasks_db_path
        return self.resolved_memory_dir / "tasks.db"

    def resolve_context_budget(self, model: str | None = None) -> tuple[int, float]:
        """Return ``(max_tokens, compact_ratio)`` for ``model``.

        Lookup order:
          1. ``model_budgets`` (user-set; longest matching prefix wins)
          2. ``_DEFAULT_MODEL_BUDGETS`` (built-in; longest matching prefix wins)
          3. ``(context_max_tokens, context_compact_ratio)`` global fallback

        Prefix matching is case-insensitive; empty model falls straight to
        the global fallback.

        Logs a WARNING the first time a model name falls through to the
        global fallback — that path is "guess and hope," so the user
        should add a ``model_budgets`` entry. Subsequent calls for the
        same model name stay silent.
        """
        name = (model or self.model_name or "").lower()
        if not name:
            return self.context_max_tokens, self.context_compact_ratio

        for source in (self.model_budgets, _DEFAULT_MODEL_BUDGETS):
            best_prefix: str | None = None
            for prefix in source:
                if name.startswith(prefix.lower()):
                    if best_prefix is None or len(prefix) > len(best_prefix):
                        best_prefix = prefix
            if best_prefix is not None:
                budget = source[best_prefix]
                try:
                    return int(budget["max_tokens"]), float(budget["compact_ratio"])
                except (KeyError, ValueError, TypeError):
                    # Malformed user entry — try the next source rather than crash.
                    continue

        if name not in _WARNED_FALLBACK_MODELS:
            _WARNED_FALLBACK_MODELS.add(name)
            logger.warning(
                "Context budget for model=%r not found in model_budgets or "
                "_DEFAULT_MODEL_BUDGETS; falling back to globals "
                "(max_tokens=%d, compact_ratio=%.2f). If this model's real "
                "window differs significantly, add an entry to "
                "settings.model_budgets to avoid early compaction or "
                "context_length_exceeded errors.",
                model or self.model_name,
                self.context_max_tokens,
                self.context_compact_ratio,
            )
        return self.context_max_tokens, self.context_compact_ratio

    def reload(self) -> "Settings":
        """Re-read config.json and environment variables.

        Returns self for chaining. After calling this, all property accesses
        reflect the latest values from config.json / env vars.
        """
        new_settings = self.__class__()
        for field_name in self.__class__.model_fields:
            object.__setattr__(self, field_name, getattr(new_settings, field_name))
        # Reset the per-process warning dedup so users who fixed
        # model_budgets and reloaded get fresh feedback next call.
        _WARNED_FALLBACK_MODELS.clear()
        return self


def _get_settings() -> Settings:
    """Get or create the global Settings instance."""
    global settings
    if settings is None:
        settings = Settings()
    return settings


settings = Settings()
