"""Pydantic request/response schemas for the REST API.

Public response types (JSONEnvelope, ResponseStatus, ResponseCode) live in
chaos_agent.models.schemas and are re-exported here for backward compatibility.
New code should import from chaos_agent.models.schemas directly.
"""

from typing import Optional

from pydantic import BaseModel, Field, model_validator

# Re-export public types for backward compatibility
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode, ResponseStatus  # noqa: F401


# --- Request Models ---


class InjectRequest(BaseModel):
    """Request body for POST /api/v1/inject."""

    scope: Optional[str] = Field(None, description="ChaosBlade scope: node, pod, or container")
    target: Optional[str] = Field(None, description="ChaosBlade target: cpu, network, disk, mem, process, pod")
    action: Optional[str] = Field(None, description="ChaosBlade action: fullload, delay, loss, fill, kill, delete, load, burn")
    target_name: Optional[str] = Field(None, description="Resource name(s), comma-separated for batch")
    namespace: Optional[str] = Field(None, description="K8s namespace")
    duration: int = Field(600, description="Fault duration in seconds, 0 for manual recovery")
    params: Optional[dict] = Field(None, description="Additional fault parameters (key=value)")
    params_flags: Optional[list[str]] = Field(None, description="Boolean flags for blade (e.g. ['read', 'write'])")
    confirm: bool = Field(False, description="Whether to require confirmation before execution")
    labels: Optional[dict] = Field(None, description="K8s label selector for blade --labels targeting (e.g. {'app': 'accounting'})")
    input: Optional[str] = Field(None, description="Natural language description (alternative to structured params)")
    direct: bool = Field(False, description="Skip LLM, execute blade command directly")
    kubeconfig: Optional[str] = Field(None, description="Path to kubeconfig file (overrides BLADE_AI_KUBECONFIG_PATH and KUBECONFIG env)")
    context: Optional[str] = Field(None, description="Kubeconfig context name (overrides BLADE_AI_KUBE_CONTEXT)")

    @model_validator(mode="after")
    def validate_mode(self):
        """Either input is provided, or all structured params are provided."""
        has_input = bool(self.input)
        has_target = bool(self.target_name or self.labels)
        has_structured = all([self.scope, self.target, self.action, has_target, self.namespace])
        if not has_input and not has_structured:
            raise ValueError(
                "Provide either 'input' or all of: scope, target, action, (target_name or labels), namespace"
            )
        if self.direct and self.input:
            raise ValueError("'direct' is not compatible with 'input'")
        if self.direct and not has_structured:
            raise ValueError(
                "'direct' requires all structured params: scope, target, action, (target_name or labels), namespace"
            )
        if has_structured and self.scope not in {"node", "pod", "container"}:
            raise ValueError(f"Invalid scope '{self.scope}', must be node/pod/container")
        return self


class RecoverRequest(BaseModel):
    """Request body for POST /api/v1/recover."""

    task_id: str = Field(..., description="Task ID to recover")
    target_name: Optional[str] = Field(None, description="Specific target to recover (partial recovery)")
    force: bool = Field(False, description="Force recovery, skip pre-checks")


class ConfirmRequest(BaseModel):
    """Request body for POST /api/v1/confirm/{task_id}."""

    action: str = Field(..., description="approve or reject")
    reason: Optional[str] = Field(None, description="Reason for approval/rejection")


# --- Response Models ---


class TargetInfo(BaseModel):
    """Compact target info in inject/recover response."""

    name: str = ""
    namespace: str = ""


class InjectResponse(BaseModel):
    """Response data for inject command."""

    task_id: str
    result: str = "pending"
    fault_type: str = ""
    blade_uid: str = ""
    targets: list[TargetInfo] = []
    verification: Optional[dict] = None
    error: str = ""


class RecoverResponse(BaseModel):
    """Response data for recover command."""

    task_id: str
    result: str = "pending"
    blade_uid: str = ""
    targets: list[TargetInfo] = []
    verification: Optional[dict] = None
    error: str = ""


class ConfirmResponse(BaseModel):
    """Response data for confirm command."""

    task_id: str
    action: str
    reason: Optional[str] = None
    confirmed_at: str = ""


class SkillParameterInfo(BaseModel):
    """Parameter definition for a skill."""

    key: str
    type: str = "string"
    required: bool = False
    default: Optional[str] = None
    description: str = ""
    example: Optional[str] = None


class FaultTypeInfo(BaseModel):
    """Information about a supported fault type."""

    fault_type: str
    name: str = ""
    description: str = ""
    target_types: list[str] = []
    params: list[SkillParameterInfo] = []
    example_cmd: str = ""


class CategoryInfo(BaseModel):
    """A category of fault types."""

    category: str
    description: str = ""
    faults: list[FaultTypeInfo] = []


class SkillsListResponse(BaseModel):
    """Response data for list skills command."""

    total: int = 0
    categories: list[CategoryInfo] = []


class VersionResponse(BaseModel):
    """Response data for version command."""

    version: str = "0.1.0"
    build_time: str = ""
    git_commit: str = ""
    blade_version: str = ""
    kubectl_version: str = ""
    supported_fault_count: int = 0



