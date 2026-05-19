"""Application configuration via pydantic-settings.

Configuration priority (highest to lowest):
  1. ~/.blade-ai/config.json (managed by `blade-ai config` CLI)
  2. Environment variables (BLADE_AI_* prefix)
  3. Code defaults
"""

import json
from pathlib import Path
from typing import Any, Tuple, Type

from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Path to the unified config file managed by `blade-ai config`
_CONFIG_FILE = Path.home() / ".blade-ai" / "config.json"


class JsonConfigSettingsSource(PydanticBaseSettingsSource):
    """Custom settings source that reads from ~/.blade-ai/config.json."""

    def get_field_value(
        self, field: Field, field_name: str
    ) -> Tuple[Any, str, bool]:
        if not _CONFIG_FILE.exists():
            return None, field_name, False
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None, field_name, False
        if field_name in data:
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
            if field_name in file_data:
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
    llm_max_retries: int = 3                  # BLADE_AI_LLM_MAX_RETRIES
    llm_temperature: float = 0.7              # BLADE_AI_LLM_TEMPERATURE
    llm_enable_thinking: bool = True           # BLADE_AI_LLM_ENABLE_THINKING，启用模型深度思考模式(如Qwen的enable_thinking)

    # Verifier配置
    verifier_json_mode: bool = True            # BLADE_AI_VERIFIER_JSON_MODE，最终迭代启用 response_format JSON 模式强制结构化输出

    # Server配置
    server_port: int = 8089                   # BLADE_AI_SERVER_PORT
    server_host: str = "0.0.0.0"              # BLADE_AI_SERVER_HOST

    # Skill配置
    skills_dir: Path = Path("~/.blade-ai/skills")  # BLADE_AI_SKILLS_DIR，运行时通过 get_skills_dir() 动态解析
    disabled_skills: list[str] = []                # BLADE_AI_DISABLED_SKILLS，被用户主动禁用的技能（保留文件但加载时跳过）

    # 确认开关
    confirmation_required: bool = True        # BLADE_AI_CONFIRMATION_REQUIRED

    # 经验自进化开关
    self_evolution: bool = False              # BLADE_AI_SELF_EVOLUTION

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
    timeout_kubectl_exec: int = 60           # BLADE_AI_TIMEOUT_KUBECTL_EXEC
    timeout_llm: int = 180                   # BLADE_AI_TIMEOUT_LLM
    timeout_default: int = 60                # BLADE_AI_TIMEOUT_DEFAULT
    timeout_skill_script: int = 60           # BLADE_AI_TIMEOUT_SKILL_SCRIPT，skill 脚本执行超时

    # 实验级默认超时(秒) — blade create 无 --timeout 时自动注入
    # NOTE: This must be >= _DEFAULT_MIN_DURATION in fault_type.py (currently 600)
    experiment_timeout: int = 600            # BLADE_AI_EXPERIMENT_TIMEOUT

    # Confirm gate 等待用户决策的最大秒数 — 超过则服务端礼貌中断 turn，避免用户离开后未回收 future
    # 默认 1800s (30 分钟) 对绝大多数排查场景够用；遇到复杂研判可调长，例如 7200 (2h)
    confirm_wait_timeout: int = 1800         # BLADE_AI_CONFIRM_WAIT_TIMEOUT

    # kubectl 输出控制
    kubectl_max_output_bytes: int = 32768       # BLADE_AI_KUBECTL_MAX_OUTPUT_BYTES，超过此大小的 JSON 输出追加提示

    # 安全配置
    safety_blacklist_namespaces: str = "kube-system,kube-public"  # BLADE_AI_SAFETY_BLACKLIST_NAMESPACES

    # Agent Loop上限
    max_agent_loop: int = 50                 # BLADE_AI_MAX_AGENT_LOOP
    max_execute_loop: int = 50               # BLADE_AI_MAX_EXECUTE_LOOP
    max_verifier_loop: int = 30              # BLADE_AI_MAX_VERIFIER_LOOP
    max_recover_verifier_loop: int = 30      # BLADE_AI_MAX_RECOVER_VERIFIER_LOOP
    max_recover_layer1_iterations: int = 30  # BLADE_AI_MAX_RECOVER_LAYER1_ITERATIONS (non-ChaosBlade LLM sub-loop)
    recursion_limit: int = 150               # BLADE_AI_RECURSION_LIMIT

    # 循环检测（重复工具调用）
    loop_detection_window: int = 10          # BLADE_AI_LOOP_DETECTION_WINDOW，检测最近 N 条消息
    loop_detection_threshold: int = 3        # BLADE_AI_LOOP_DETECTION_THRESHOLD，相同调用超过 N 次触发提示

    # 空闲轮次检测（连续无工具调用的AI响应）
    idle_turn_threshold: int = 3             # BLADE_AI_IDLE_TURN_THRESHOLD，连续 N 轮无工具调用触发收敛提示

    # Replan配置 (Phase 2 → Phase 1 错误回退)
    max_replan_count: int = 3                    # BLADE_AI_MAX_REPLAN_COUNT
    replan_auto_trigger: bool = True             # BLADE_AI_REPLAN_AUTO_TRIGGER, 自动检测可replan的错误模式
    replan_reset_execute_count: bool = True      # BLADE_AI_REPLAN_RESET_EXECUTE_COUNT, replan后重置execute_loop_count

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

    # 上下文窗口配置
    context_max_tokens: int = 128000  # BLADE_AI_CONTEXT_MAX_TOKENS，LLM上下文窗口大小，用于记忆压缩阈值计算
    context_compact_ratio: float = 0.85  # BLADE_AI_CONTEXT_COMPACT_RATIO，压缩触发比例

    # Skill 脚本执行配置
    skill_script_max_output: int = 4000  # BLADE_AI_SKILL_SCRIPT_MAX_OUTPUT，返回给 LLM 的 stdout 最大字符数

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

    def reload(self) -> "Settings":
        """Re-read config.json and environment variables.

        Returns self for chaining. After calling this, all property accesses
        reflect the latest values from config.json / env vars.
        """
        new_settings = self.__class__()
        for field_name in self.__class__.model_fields:
            object.__setattr__(self, field_name, getattr(new_settings, field_name))
        return self


def _get_settings() -> Settings:
    """Get or create the global Settings instance."""
    global settings
    if settings is None:
        settings = Settings()
    return settings


settings = Settings()
