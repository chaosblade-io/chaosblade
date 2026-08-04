"""Application configuration via pydantic-settings.

Configuration priority (highest to lowest):
  1. ~/.blade-ai/config.json (managed by `blade-ai config` CLI)
  2. Environment variables (BLADE_AI_* prefix)
  3. Code defaults
"""

import contextvars
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Tuple, Type

from pydantic import Field, AliasChoices, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

logger = logging.getLogger(__name__)

# Messages per ReAct turn, measured over 9 real drills (task JSONL, gap between
# consecutive AI messages): min 2, median 3, mean 4.6, max 39. The median is used
# to check that ``loop_detection_window`` (a MESSAGE cap) is wide enough not to
# mask ``loop_detection_turns`` (the real window). No constant can cover the
# worst case — 6 turns × 39 would demand a 234-message cap — so this guarantees
# the typical shape, and the turn window remains the primary control.
_TYPICAL_MESSAGES_PER_TURN = 3

# Path to the unified config file managed by `blade-ai config`
_CONFIG_FILE = Path(os.path.expanduser("~/.blade-ai/config.json"))


def _active_config_file() -> Path:
    """Resolve the config.json path to read/write at call time.

    ``_CONFIG_FILE`` is frozen at import via ``expanduser("~")``, so a test
    or tool that redirects HOME *after* this module is imported cannot move
    the config target — which is exactly how the wizard tests once clobbered
    a developer's real ``~/.blade-ai/config.json``. Honour
    ``BLADE_AI_CONFIG_DIR`` dynamically (checked on every call) so such
    redirects take effect, while still falling back to the module-level
    ``_CONFIG_FILE`` (which tests may monkeypatch directly) when the env
    override is absent.
    """
    override = os.environ.get("BLADE_AI_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser() / "config.json"
    return _CONFIG_FILE

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
    # Anthropic — Opus/Sonnet 4.6 起 1M 窗口已 GA（2026-03 标准定价），
    # 5 代在 API 上恒为 1M；4.5 及更早代际仍是 200K，由泛化前缀兜底
    "claude-opus-5":    {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "claude-sonnet-5":  {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "claude-opus-4-6":  {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "claude-sonnet-4-6": {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "claude-opus":      {"max_tokens": 200_000, "compact_ratio": 0.85},
    "claude-sonnet":    {"max_tokens": 200_000, "compact_ratio": 0.85},
    "claude-haiku":     {"max_tokens": 200_000, "compact_ratio": 0.90},
    # OpenAI
    "gpt-5":            {"max_tokens": 400_000, "compact_ratio": 0.80},
    "gpt-4.1":          {"max_tokens": 1_047_576, "compact_ratio": 0.80},
    "gpt-4o":           {"max_tokens": 128_000, "compact_ratio": 0.85},
    "gpt-4":            {"max_tokens": 128_000, "compact_ratio": 0.85},
    "o1":               {"max_tokens": 128_000, "compact_ratio": 0.85},
    "o3":               {"max_tokens": 200_000, "compact_ratio": 0.85},
    "o4-mini":          {"max_tokens": 200_000, "compact_ratio": 0.90},
    # Google Gemini
    "gemini-3":         {"max_tokens": 1_048_576, "compact_ratio": 0.80},
    "gemini-2.5":       {"max_tokens": 1_048_576, "compact_ratio": 0.80},
    # Alibaba Qwen (DashScope) — 窗口值取自百炼官方模型列表：
    # 3.7 全系 1M；3.6-max / 3-max 为 256k；3.5 代整代下界 256k；
    # qwen3-coder-plus/flash 1M，coder-next 256k（泛化 coder 取下界）
    "qwen3.7-max":      {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "qwen3.7-plus":     {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "qwen3.7":          {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "qwen3.6-max":      {"max_tokens": 262_144, "compact_ratio": 0.80},
    "qwen3.6-plus":     {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "qwen3.5":          {"max_tokens": 262_144, "compact_ratio": 0.80},
    "qwen3-max":        {"max_tokens": 262_144, "compact_ratio": 0.80},
    "qwen3-coder-plus": {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "qwen3-coder-flash": {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "qwen3-coder":      {"max_tokens": 262_144, "compact_ratio": 0.80},
    "qwen3":            {"max_tokens": 131_072, "compact_ratio": 0.80},
    # qwen-max/plus 前缀覆盖新旧全部快照，取历史下界 32K 保守兜底
    "qwen-max":         {"max_tokens":  32_768, "compact_ratio": 0.80},
    "qwen-plus":        {"max_tokens":  32_768, "compact_ratio": 0.80},
    "qwen-turbo":       {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "qwen-flash":       {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    # DeepSeek — V4（2026-04）起官方 chat/reasoner 端点升至 1M 窗口；
    # 泛化 "deepseek" 保持 64K 作为老版本/第三方部署的保守兜底
    "deepseek-v4":      {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "deepseek-chat":    {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "deepseek-reasoner": {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "deepseek":         {"max_tokens":  64_000, "compact_ratio": 0.80},
    # Zhipu GLM — 5.2 升至 1M；5.0/5.1 与 4.6 为 200K
    "glm-5.2":          {"max_tokens": 1_000_000, "compact_ratio": 0.80},
    "glm-5":            {"max_tokens": 200_000, "compact_ratio": 0.85},
    "glm-4.6":          {"max_tokens": 200_000, "compact_ratio": 0.85},
    "glm-4":            {"max_tokens": 128_000, "compact_ratio": 0.85},
    # Moonshot Kimi — K3（2026-07）1M（max_completion_tokens 上限
    # 1048576 = 2^20，故窗口取二进制 1M）；K2 系列 256K
    "kimi-k3":          {"max_tokens": 1_048_576, "compact_ratio": 0.80},
    "kimi-k2":          {"max_tokens": 262_144, "compact_ratio": 0.80},
    "moonshot":         {"max_tokens": 131_072, "compact_ratio": 0.85},
    # xAI Grok
    "grok-4-fast":      {"max_tokens": 2_000_000, "compact_ratio": 0.80},
    "grok-4":           {"max_tokens": 256_000, "compact_ratio": 0.80},
    # ByteDance Doubao
    "doubao-seed":      {"max_tokens": 256_000, "compact_ratio": 0.80},
    # MiniMax
    "minimax":          {"max_tokens": 200_000, "compact_ratio": 0.80},
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
        config_file = _active_config_file()
        if not config_file.exists():
            return None, field_name, False
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None, field_name, False
        if field_name in data and not self._is_unset(data[field_name]):
            value = data[field_name]
            # Coerce numeric JSON values to str when the field expects str.
            # Users often write e.g. "kubewiz_profile": 526255 without
            # quotes — valid JSON but pydantic rejects int→str.
            if isinstance(value, (int, float)) and field.annotation is str:
                value = str(int(value)) if isinstance(value, float) and value == int(value) else str(value)
            return value, field_name, True
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        config_file = _active_config_file()
        if not config_file.exists():
            return data
        try:
            file_data = json.loads(config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return data
        for field_name in self.settings_cls.model_fields:
            if field_name in file_data and not self._is_unset(file_data[field_name]):
                value = file_data[field_name]
                field_info = self.settings_cls.model_fields[field_name]
                if isinstance(value, (int, float)) and field_info.annotation is str:
                    value = str(int(value)) if isinstance(value, float) and value == int(value) else str(value)
                data[field_name] = value
        return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BLADE_AI_",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,  # Allow JsonConfigSettingsSource to use field_name keys
                                 # (needed because kubeconfig_path has validation_alias)
    )

    @field_validator("kube_connection_mode")
    @classmethod
    def _validate_connection_mode(cls, v: str) -> str:
        """Validate the explicit channel override at config-load time.

        Empty string means field-based auto inference; any non-empty value
        must be an exact channel name.  Fail fast here (config load) rather
        than deep in the runtime (env_info / executor / preflight).
        """
        valid = {"", "kubeconfig", "kubewiz_k8s", "kubewiz_host", "ssh"}
        if v not in valid:
            raise ValueError(
                f"Invalid kube_connection_mode: {v!r}; allowed values: "
                f"'' (empty = auto-infer) / kubeconfig / kubewiz_k8s / kubewiz_host / ssh"
            )
        return v

    @field_validator("ssh_port")
    @classmethod
    def _validate_ssh_port(cls, v: int) -> int:
        """Reject out-of-range SSH ports at config-load time.

        Without this a negative/oversized value would silently flow through
        state (``ssh_port if ssh_port else 22`` treats -1 as truthy) and only
        fail deep inside ``ssh`` at execution.
        """
        if not (1 <= v <= 65535):
            raise ValueError(f"ssh_port out of range (1-65535): {v}")
        return v

    @field_validator("ssh_strict_host_key_checking")
    @classmethod
    def _validate_ssh_strict_host_key_checking(cls, v: str) -> str:
        """Restrict to the three values ``ssh -o StrictHostKeyChecking`` accepts.

        Fail fast at config load rather than surfacing a cryptic ssh error at
        execution. Mirrors the other transport validators above.
        """
        valid = {"accept-new", "yes", "no"}
        if v not in valid:
            raise ValueError(
                f"Invalid ssh_strict_host_key_checking: {v!r}; allowed values: accept-new / yes / no"
            )
        return v

    @field_validator("hint_escalate_after")
    @classmethod
    def _validate_hint_escalate_after(cls, v: int) -> int:
        """A corrective hint must escalate at some finite count.

        ``0`` or a negative value would make ``count > escalate_after`` true on
        the FIRST occurrence, so every hint would get its own message id and the
        history would grow one entry per turn from the start — the pile-up the
        overwrite mode exists to avoid. Refusing is the honest response: silently
        clamping would hide an operator's mistake behind behaviour they did not
        ask for, and this project's convention is to raise rather than clamp.
        """
        if v < 1:
            raise ValueError(
                f"Invalid hint_escalate_after: {v}; must be >= 1.\n"
                f"It is the occurrence count at which a corrective hint escalates from "
                f"'overwrite a single entry' to 'accumulate one per occurrence'; "
                f"<= 0 makes the very first hint start piling up, adding one history "
                f"entry every turn.\n"
                f"Fix: set it to >= 1 (default 3; smaller starts piling up sooner, "
                f"at the cost of context)."
            )
        return v

    @model_validator(mode="after")
    def _validate_loop_detection_window_fits_thresholds(self) -> "Settings":
        """A detection window smaller than its own threshold can never fire.

        This is the exact defect this invariant exists to prevent, and it has
        already happened once: the window was counted in MESSAGES (10) while
        repeats sat 5-8 messages apart, so it held at most 2 occurrences against
        ``loop_detection_threshold=3``. Both detectors were dead for the entire
        life of that code and nobody noticed, because failing to fire produces
        no error — just a drill that loops 89 times.

        Switching the window to AI turns fixed the default, but left the same
        trap one step away: ``loop_detection_turns`` defaults to 6 against
        thresholds of 5 and 3, and all three are env-tunable. Verified failure
        modes on a 30-turn stalled history:

          turns=6 stag=5 rep=3   → both fire
          stagnation_threshold=7 → stagnation permanently silent
          loop_detection_threshold=8 → repeated permanently silent
          turns=2                → BOTH permanently silent

        Raising here (rather than clamping) matches the other validators in this
        class and forces the operator to state a coherent intent: a laxer
        threshold must come with a wider window.

        Both bounds are checked, because ``_recent_window`` applies both and the
        TIGHTER one wins:

        * ``loop_detection_turns`` must reach the widest threshold, or that
          detector can never accumulate enough occurrences.
        * ``loop_detection_window`` is a MESSAGE cap sitting OUTSIDE the turn
          window, so it can silently mask it. Measured with 9-message turns
          (a 4-call batch + its results): ``turns=6, window=10`` leaves only 2
          turns visible and BOTH detectors go quiet — while passing a bound that
          only demanded ``widest × 2``. The cap must therefore hold the turn
          window at a realistic per-turn size, not the theoretical minimum.

        Verified transitions on a 30-turn stalled history (thresholds 5/3):

            turns=0 window=120 → both fire     turns=6 window=120 → both fire
            turns=0 window=8   → stagnation quiet
            turns=0 window=4   → BOTH quiet    turns=6 window=4   → BOTH quiet

        ``turns <= 0`` disables the turn window, leaving the cap as the sole
        bound; it is then sized against the widest threshold instead.

        ``stagnation_frequency_ceiling`` belongs in ``widest`` for exactly the
        same reason, and leaving it out reproduced the original defect one layer
        up. The ceiling is compared against a streak counted INSIDE the turn
        window, so ``turns=6, ceiling=10`` made it arithmetically unreachable:
        replaying sess_41dc42aa (30 consecutive ``blade_help`` turns, help text
        differing each time so the output check stayed False) the window held 6
        turns, ``best_streak`` capped at 6, and stagnation returned None for all
        30 turns. The frequency path — the whole point of which is to fire when
        output comparison cannot — was dead on the shipped defaults, silently,
        which is the failure class this validator exists to make impossible.
        """
        widest = max(
            self.loop_detection_threshold,
            self.stagnation_threshold,
            self.stagnation_frequency_ceiling,
        )

        # Outer bound: the message cap must not bind before the real window.
        turns_to_fit = (
            self.loop_detection_turns if self.loop_detection_turns > 0 else widest
        )
        min_messages = turns_to_fit * _TYPICAL_MESSAGES_PER_TURN
        if self.loop_detection_window < min_messages:
            raise ValueError(
                f"loop_detection_window ({self.loop_detection_window}) is the message cap "
                f"that sits outside the turn window; too small a value masks the turn "
                f"window and leaves the loop detectors permanently silent "
                f"(no error, never fires).\n"
                f"Needs >= {min_messages} (= {turns_to_fit} turn(s) to fit × "
                f"{_TYPICAL_MESSAGES_PER_TURN} typical messages per turn); current "
                f"loop_detection_turns={self.loop_detection_turns}, loop_detection_threshold="
                f"{self.loop_detection_threshold}, stagnation_threshold="
                f"{self.stagnation_threshold}\n"
                f"Fix: raise loop_detection_window to >= {min_messages}, "
                f"or lower loop_detection_turns / the thresholds."
            )

        if self.loop_detection_turns <= 0:
            # Turn window explicitly disabled — the message cap above is the
            # only bound, and it has just been validated.
            return self

        if self.loop_detection_turns < widest:
            raise ValueError(
                f"loop_detection_turns ({self.loop_detection_turns}) is below the detection "
                f"threshold ({widest}), which leaves both loop detectors permanently silent "
                f"(no error, never fires).\n"
                f"Current: loop_detection_turns={self.loop_detection_turns}, "
                f"loop_detection_threshold={self.loop_detection_threshold}, "
                f"stagnation_threshold={self.stagnation_threshold}\n"
                f"Fix: raise loop_detection_turns to >= {widest}, or lower the thresholds; "
                f"set it to 0 to explicitly disable the turn window (using the message cap instead)."
            )
        return self

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
    model_name: str = "qwen3.7-plus"               # BLADE_AI_MODEL_NAME
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
    # Thinking 模型把"我为什么做这个/我已经做过什么"写在 reasoning_content 通道，
    # content 常为空。若不回传该字段，历史里只剩无理由的裸工具调用，模型每轮都要
    # 从原始输入重新推导意图 —— 单步任务下"重推导"恰好等于正确的下一步所以无害，
    # 多步任务下永远推导出第 1 步，形成不收敛循环。上限只是防单条异常膨胀的安全
    # 阈值，不是常规裁剪手段（正常 thinking 在 100~2500 字符区间），上下文预算由
    # PreReasoningHook 压缩系统统一管理。
    reasoning_replay_max_chars: int = 8000    # BLADE_AI_REASONING_REPLAY_MAX_CHARS，回传 reasoning_content 的单条长度上限（超出保留尾部，结论在思考末尾）

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

    # Kubewiz 连接模式配置（网络隔离场景下通过 kubewiz 通道连接集群）
    # 显式通道覆盖开关：空=按字段自动推断，非空=强制指定通道（唯一标准）
    kube_connection_mode: str = ""            # BLADE_AI_KUBE_CONNECTION_MODE ("" | "kubeconfig" | "kubewiz_k8s" | "kubewiz_host" | "ssh")
    kubewiz_url: str = ""                      # BLADE_AI_KUBEWIZ_URL (kubewiz-core 服务地址，blade 用)
    kubewiz_cluster_uuid: str = ""             # BLADE_AI_KUBEWIZ_CLUSTER_UUID (目标集群 UUID)
    kubewiz_token: str = ""                    # BLADE_AI_KUBEWIZ_TOKEN (认证 token，blade 用)
    kubewiz_profile: str = ""                  # BLADE_AI_KUBEWIZ_PROFILE (wiz task exec 的 --profile)
    wiz_path: str = "wiz"                      # BLADE_AI_WIZ_PATH (wiz 二进制路径)
    kubewiz_wait_timeout: int = 30             # BLADE_AI_KUBEWIZ_WAIT_TIMEOUT (wiz task exec --wait-timeout 秒；0=跟随每条命令的 timeout，>0=固定覆盖。wiz 内置默认 10s 会让长命令过早超时)
    kubewiz_task_timeout: int = 600            # BLADE_AI_KUBEWIZ_TASK_TIMEOUT (wiz task exec --timeout 秒，任务服务端执行预算；独立于 --wait-timeout)

    # 主机故障注入连接配置（host scope — kubewiz-host / SSH 通道）
    host_name: str = ""                        # BLADE_AI_HOST_NAME (kubewiz-host 用，主机 IP/hostname)
    # host scope 下 blade 在远端主机本地执行，命令经 wiz task exec 下发；
    # 空值则默认用远端 PATH 的裸 blade（切勿复用本机的 blade_path，否则会把本机绝对路径泄露到远端）。
    host_blade_path: str = ""                   # BLADE_AI_HOST_BLADE_PATH, 空值则默认远端 PATH 的裸 blade
    ssh_host: str = ""                         # BLADE_AI_SSH_HOST
    ssh_user: str = ""                         # BLADE_AI_SSH_USER
    ssh_key_path: str = ""                     # BLADE_AI_SSH_KEY_PATH
    ssh_port: int = 22                         # BLADE_AI_SSH_PORT
    # SSH StrictHostKeyChecking policy. Default "accept-new": trust a host on
    # first connect but reject if its key later changes (MITM detection).
    # Set "no" only in throwaway/lab networks; "yes" for pre-provisioned
    # known_hosts. Value is passed verbatim to ssh -o StrictHostKeyChecking=.
    ssh_strict_host_key_checking: str = "accept-new"  # BLADE_AI_SSH_STRICT_HOST_KEY_CHECKING

    # 全局默认超时(秒)
    command_timeout: int = 60                # BLADE_AI_COMMAND_TIMEOUT

    # 分工具超时配置(秒)
    timeout_blade: int = 60                  # BLADE_AI_TIMEOUT_BLADE
    timeout_kubectl: int = 60                # BLADE_AI_TIMEOUT_KUBECTL
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
    # 默认 21600s (6 小时)：确认卡片弹出后用户常被别的事打断，1 小时的窗口实测太短
    # ——回来时 turn 已被回收，只能从头再来。这个超时的目的是回收资源，不是催促决策，
    # 所以宁可给足时间。需要更严格的窗口时调小，例如 3600 (1h)。
    confirm_wait_timeout: int = 21600        # BLADE_AI_CONFIRM_WAIT_TIMEOUT

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
    # 频次上限：达到该连续轮数后，streak 本身即证据，不再要求输出一致。
    # 两个检测器必须各自独立：detect_repeated_tool_calls 回答"相同调用+相同结果"，
    # 输出有变化时理应沉默；本检测器覆盖的是它的盲区——频次。若同样以输出一致为门，
    # 两层会塌缩成同一个失效模式（实时指标采样永不字节相同 → 都不触发）。
    # 实测 14 次演练：单工具连续轮数 中位 1 / p90 2 / p95 3；异常案例为 8 和 12，
    # 而两个 8 是参数确实不同的合法探索（node/pods/events/sts），故上限取其之上。
    stagnation_frequency_ceiling: int = 10   # BLADE_AI_STAGNATION_FREQUENCY_CEILING
    # 纠错提示从"覆盖单条"升级为"逐条累积"的触发次数。
    # 前若干次用固定 message id，由 add_messages 按 id 覆盖，历史恒 1 条，靠文案里
    # 的 "reminder #N" 表达累计；这依赖模型读懂计数。超过本值说明覆盖模式已被实测
    # 证伪，改为每次一条独立 id，让弱模型不必理解计数也能感到警告在堆积。
    #
    # 计的是提示下发次数，不是工具调用次数——检测器要先触发一次才有第 1 条提示，
    # 而它自带门槛，所以提示 #1 时已经重复很多次了。两条触发路径下的换算：
    #   输出一致（工具反复报同样的错）: 首触发=第 5 次调用 → 本值 3 = 第 8 次开始累积
    #   输出漂移（实时指标，task-e9ee12d6）: 首触发=第 11 次 → 本值 3 = 第 14 次
    # 取 3 而非更大值：能走到"同一 tool:subcommand 被提示 3 次"必然是真重复。
    # 实测 14 次演练的单工具连续轮数 中位 1 / p90 2 / p95 3，而检测门槛本身是 5，
    # 已在 p95 之上；那两个连续 8 轮的合法探索用的是不同 subcommand，
    # _stagnation_key 把它们分成不同 key 计数，不会累积到同一条提示上。
    #
    # 不参与 loop_detection_window 的容量校验：它计的是提示下发次数，与窗口内
    # streak 无关，不存在算术不可达问题。
    hint_escalate_after: int = 3             # BLADE_AI_HINT_ESCALATE_AFTER
    recursion_limit: int = 500               # BLADE_AI_RECURSION_LIMIT

    # 循环检测（重复工具调用）
    # 窗口按 AI 轮次而非消息条数计：一轮 = 1 条 AI + N 条 Tool + 可能的系统消息，
    # 实测重复调用的相邻间距是 5~8 条消息，按条数计的 10 条窗口最多装 2 次，
    # 永远达不到 threshold=3，检测器全程无法触发。两个界同时生效，取更紧的一个。
    # 轮数必须容纳最宽的阈值（含 stagnation_frequency_ceiling=10），否则该判据
    # 在算术上不可达：曾经 turns=6 配 ceiling=10，实测 30 轮连续停滞全程不触发，
    # 因为 streak 在窗口内最多只能数到 6。见 _validate_loop_detection_window_fits_thresholds。
    loop_detection_turns: int = 12            # BLADE_AI_LOOP_DETECTION_TURNS，检测最近 N 个 AI 轮次（主判据）
    loop_detection_window: int = 120         # BLADE_AI_LOOP_DETECTION_WINDOW，消息数硬上限兜底（防超长会话全量扫描）；须 >= turns × 每轮典型消息数，否则会盖住轮次窗口（有校验）
    loop_detection_threshold: int = 3        # BLADE_AI_LOOP_DETECTION_THRESHOLD，相同调用超过 N 次触发提示

    # 空闲轮次检测（连续无工具调用的AI响应）
    idle_turn_threshold: int = 3             # BLADE_AI_IDLE_TURN_THRESHOLD，连续 N 轮无工具调用触发收敛提示
    max_execute_text_stalls: int = 3         # BLADE_AI_MAX_EXECUTE_TEXT_STALLS，Phase 2 连续纯文本空转（无工具调用/无注入/无replan）达到 N 次判死；期间每次先 nudge，遇任一工具调用即清零
    max_plan_text_stalls: int = 3            # BLADE_AI_MAX_PLAN_TEXT_STALLS，Phase 1 连续纯文本空转（无工具调用/无skill激活）达到 N 次判死；期间每次先 nudge，遇工具调用或skill激活即清零

    # Replan配置 (Phase 2 → Phase 1 错误回退)
    max_replan_count: int = 3                    # BLADE_AI_MAX_REPLAN_COUNT (execute_loop replan budget)
    max_verify_replan_count: int = 3             # BLADE_AI_MAX_VERIFY_REPLAN_COUNT (verify-replan budget, independent)
    replan_auto_trigger: bool = True             # BLADE_AI_REPLAN_AUTO_TRIGGER, 自动检测可replan的错误模式
    replan_reset_execute_count: bool = True      # BLADE_AI_REPLAN_RESET_EXECUTE_COUNT, replan后重置execute_loop_count

    # Patch C — Wall-clock timeout 兜底
    # 单次 inject turn 的硬性墙钟上限。0 = 关闭（保留历史行为）；>0
    # 时所有 ``should_continue_*`` router 都会检查并强制走 "end" 分
    # 支。配合 patch B 的 INFRA_TRANSIENT short-retry 一起使用，避
    # 免基础设施抖动让 turn 跑数分钟还在转圈。
    max_inject_seconds: int = 0                  # BLADE_AI_MAX_INJECT_SECONDS

    # Patch B — INFRA_TRANSIENT 类错误的额外短重试预算
    # 当 ``classify_error`` 判定 ErrorAction.SHORT_RETRY 时，允许 LLM 再发起
    # 最多 N 次同样的 tool 调用；超出后由
    # ``react_helpers.detect_transient_retry_exhaustion`` 在 execute 阶段
    # 注入升级提示（要求停止重试、replan 或判定失败）。0 = 关闭。
    # 3 是经验上不会让用户感到卡顿的合理上限。
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
    retry_max_retries: int = 2               # BLADE_AI_RETRY_MAX_RETRIES (LLM 调用失败退避重试次数；2=重试2次后第3次直接抛出原始错误)
    retry_base_delay: float = 1.0            # BLADE_AI_RETRY_BASE_DELAY
    retry_max_delay: float = 30.0            # BLADE_AI_RETRY_MAX_DELAY
    retry_exponential_base: float = 2.0      # BLADE_AI_RETRY_EXPONENTIAL_BASE
    retry_jitter: bool = True                # BLADE_AI_RETRY_JITTER

    # Checkpoint持久化 (默认存放在 memory_dir 下)
    checkpoint_db_path: Path = Path("")   # BLADE_AI_CHECKPOINT_DB_PATH, 空值则使用 memory_dir/checkpoints.db
    checkpoint_backend: str = "sqlite"    # BLADE_AI_CHECKPOINT_BACKEND, "sqlite" 或 "postgresql"
    checkpoint_pg_dsn: str = ""           # BLADE_AI_CHECKPOINT_PG_DSN, PostgreSQL conninfo (仅 postgresql 后端需要)

    # TaskStore持久化 (默认存放在 memory_dir 下)
    tasks_db_path: Path = Path("")       # BLADE_AI_TASKS_DB_PATH, 空值则使用 memory_dir/tasks.db
    tasks_db_backend: str = "sqlite"     # BLADE_AI_TASKS_DB_BACKEND, "sqlite" 或 "postgresql"
    tasks_pg_dsn: str = ""              # BLADE_AI_TASKS_PG_DSN, PostgreSQL DSN (仅 postgresql 后端需要)
    tenant_id: str = ""                # BLADE_AI_TENANT_ID, 多租户隔离标识 (SDK 模式由平台注入)

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
    # SSE token batching: accumulates token/thinking chunks before yield.
    # 0 = disabled (each event yields immediately). Default disabled because
    # at typical LLM token rates (10-100 tokens/sec) the per-yield asyncio
    # overhead is negligible, and batching adds unnecessary latency. Can be
    # re-enabled via env var for high-concurrency proxy scenarios.
    sse_batch_interval_ms: int = 0  # BLADE_AI_SSE_BATCH_INTERVAL_MS
    sse_batch_chars: int = 0        # BLADE_AI_SSE_BATCH_CHARS

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
    # ``chaos_agent.agent.nodes.planning.phase1_screener`` for rationale.
    phase1_screener_enforcing: bool = True  # BLADE_AI_PHASE1_SCREENER_ENFORCING

    # host-escape carrier(debug pod)存活复核的新鲜度窗口(秒)。本任务
    # 新建、且在该窗口内确认过存活(status==active)的 debug pod,在 exec
    # 注入时信任内存登记值,跳过 registered_carrier_is_current 的实时
    # kubectl get pod 复核——避免网络类故障扩散时,复核探针与故障走同一
    # 条 API 通道被拖垮,把"注入生效切断连接"误判成"carrier 不可用"而拒绝。
    # 设 0(或负值)禁用窗口:每次 exec 都实时复核,退回本优化前的行为。
    carrier_liveness_ttl_seconds: int = 120  # BLADE_AI_CARRIER_LIVENESS_TTL_SECONDS

    # 日志级别 (DEBUG=显示LLM迭代详情, INFO=正常输出, WARNING=静默模式)
    log_level: str = "DEBUG"                  # BLADE_AI_LOG_LEVEL

    def _resolve_blade_path(self) -> str:
        """Resolve blade path: explicit setting > auto-detect."""
        if self.blade_path:
            return self.blade_path
        from chaos_agent.utils.blade_paths import get_bundled_blade_path
        return get_bundled_blade_path()

    def _resolve_wiz_path(self) -> str:
        """Resolve wiz path to absolute path for posix_spawn."""
        from chaos_agent.utils.blade_paths import resolve_exec_path
        return resolve_exec_path(self.wiz_path or "wiz")

    def _resolve_kubectl_path(self) -> str:
        """Resolve kubectl path to absolute path for posix_spawn."""
        from chaos_agent.utils.blade_paths import resolve_exec_path
        return resolve_exec_path(self.kubectl_path or "kubectl")

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


_settings_var: contextvars.ContextVar[Settings | None] = contextvars.ContextVar(
    "blade_ai_settings", default=None
)

_default_settings = Settings()


class _SettingsProxy:
    """Transparent proxy routing attribute access to the active Settings.

    Reads from ContextVar if set (SDK multi-tenant), otherwise falls
    back to _default_settings (CLI/TUI single-tenant). The 97 existing
    ``from chaos_agent.config.settings import settings`` sites import
    this proxy object and see no difference.
    """

    def _current(self) -> Settings:
        return _settings_var.get() or _default_settings

    def __getattr__(self, name: str):
        return getattr(self._current(), name)

    def __setattr__(self, name: str, value):
        object.__setattr__(self._current(), name, value)

    def __repr__(self) -> str:
        current = self._current()
        source = "contextvar" if _settings_var.get() is not None else "default"
        return f"<_SettingsProxy source={source} {current!r}>"


@contextmanager
def blade_ai_context(**overrides):
    """Create an isolated Settings scope for the current execution context.

    Usage (SDK multi-tenant)::

        with blade_ai_context(kube_connection_mode="kubewiz_k8s",
                              kubewiz_cluster_uuid="xxx"):
            agent.execute(runtime, task)

    Settings is constructed from env/config.json + overrides (same
    priority chain as normal). The ContextVar is reset on exit, so
    the scope is bounded to the ``with`` block. Works in sync threads
    (L4 execute), async coroutines, and asyncio.gather concurrency.
    """
    new_settings = Settings(**overrides)
    token = _settings_var.set(new_settings)
    try:
        yield new_settings
    finally:
        _settings_var.reset(token)


def get_current_settings() -> Settings:
    """Return the active Settings instance (ContextVar or default).

    Use this when you need the actual Settings object (e.g. for
    isinstance checks or passing as a parameter). For normal attribute
    access, just use ``settings.xxx`` directly.
    """
    return _settings_var.get() or _default_settings


settings: Settings = _SettingsProxy()  # type: ignore[assignment]
