"""Pydantic request/response schemas for the REST API.

Public response types (JSONEnvelope, ResponseStatus, ResponseCode) live in
chaos_agent.models.schemas and are re-exported here for backward compatibility.
New code should import from chaos_agent.models.schemas directly.
"""

from typing import Optional

from pydantic import BaseModel, Field, model_validator

# Re-export public types for backward compatibility
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode, ResponseStatus  # noqa: F401
from chaos_agent import __version__ as _PKG_VERSION
from chaos_agent.agent.spec.fault_registry import aggregate_cluster_scoped, aggregate_scopes


# --- Request Models ---


class InjectRequest(BaseModel):
    """Request body for POST /api/v1/inject."""

    scope: Optional[str] = Field(None, description="ChaosBlade scope: node, pod, or container")
    target: Optional[str] = Field(None, description="ChaosBlade target: cpu, network, disk, mem, process, pod")
    action: Optional[str] = Field(None, description="ChaosBlade action: fullload, delay, loss, fill, kill, delete, load, burn")
    target_name: Optional[str] = Field(None, description="Resource name(s), comma-separated for batch")
    namespace: Optional[str] = Field(None, description="K8s namespace")
    duration: Optional[int] = Field(None, description="Fault duration in seconds. Omit (or 0) in NL mode to let the agent extract it from the description; structured mode applies the fault-type floor when unset")
    params: Optional[dict] = Field(None, description="Additional fault parameters (key=value)")
    params_flags: Optional[list[str]] = Field(None, description="Boolean flags for blade (e.g. ['read', 'write'])")
    confirm: bool = Field(False, description="Whether to require confirmation before execution")
    labels: Optional[dict] = Field(None, description="K8s label selector for blade --labels targeting (e.g. {'app': 'accounting'})")
    input: Optional[str] = Field(None, description="Natural language description (alternative to structured params)")
    kubeconfig: Optional[str] = Field(None, description="Path to kubeconfig file (overrides BLADE_AI_KUBECONFIG_PATH and KUBECONFIG env)")
    context: Optional[str] = Field(None, description="Kubeconfig context name (overrides BLADE_AI_KUBE_CONTEXT)")
    # KubeWiz gateway targeting (overrides BLADE_AI_KUBEWIZ_* settings per-request)
    cluster_uuid: Optional[str] = Field(None, description="KubeWiz target cluster UUID (for kubewiz_k8s channel)")
    profile: Optional[str] = Field(None, description="KubeWiz wiz-task-exec profile")
    # Explicit channel override — empty = field-based auto inference.
    kube_connection_mode: Optional[str] = Field(None, description="Explicit transport channel: '' (auto) / kubeconfig / kubewiz_k8s / kubewiz_host / ssh")
    # Host transport parameters (for host-scope fault injection)
    host_name: Optional[str] = Field(None, description="Host name/IP for kubewiz-host channel")
    ssh_host: Optional[str] = Field(None, description="SSH host address for SSH channel")
    ssh_user: Optional[str] = Field(None, description="SSH login user")
    ssh_key_path: Optional[str] = Field(None, description="SSH private key path")
    ssh_port: Optional[int] = Field(None, ge=1, le=65535, description="SSH port (default: 22)")

    @model_validator(mode="after")
    def validate_mode(self):
        """Either input is provided, or all structured params are provided."""
        has_input = bool(self.input)
        has_target = bool(self.target_name or self.labels)
        # Cluster-scoped faults (node / host …) are namespace-less by nature, so
        # namespace is only required for namespace-scoped faults (pod / container …).
        namespace_optional = self.scope in aggregate_cluster_scoped()
        has_core = all([self.scope, self.target, self.action, has_target])
        has_structured = has_core and (bool(self.namespace) or namespace_optional)
        if not has_input and not has_structured:
            raise ValueError(
                "Provide either 'input' or all of: scope, target, action, (target_name or labels), namespace"
            )
        if has_structured and self.scope not in aggregate_scopes():
            raise ValueError(
                f"Invalid scope '{self.scope}', must be one of: {', '.join(aggregate_scopes())}"
            )
        if self.kube_connection_mode not in (None, "", "kubeconfig", "kubewiz_k8s", "kubewiz_host", "ssh"):
            raise ValueError(
                f"Invalid kube_connection_mode '{self.kube_connection_mode}'; "
                "must be '' / kubeconfig / kubewiz_k8s / kubewiz_host / ssh"
            )
        return self


class RecoverRequest(BaseModel):
    """Request body for POST /api/v1/recover-stream."""

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
    host_name: str = ""


class InjectResponse(BaseModel):
    """Response data for inject command."""

    task_id: str
    result: str = "pending"
    fault_type: str = ""
    experiment_uid: str = ""
    recovery_handle: Optional[dict] = None
    targets: list[TargetInfo] = []
    verification: Optional[dict] = None
    error: str = ""


class RecoverResponse(BaseModel):
    """Response data for recover command."""

    task_id: str
    result: str = "pending"
    experiment_uid: str = ""
    recovery_handle: Optional[dict] = None
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


class FaultCase(BaseModel):
    """A single skill case with generated inject commands."""

    category: str
    use_case_name: str = ""
    resource_path: str = ""
    fault_symptom: str = ""
    inject_kind: str = "unknown"
    nl_cmd: str = ""
    structured_cmd: str = ""


class CapabilitiesListResponse(BaseModel):
    """Response data for list command (new: from capabilities sync)."""

    total: int = 0
    blade_version: str = ""
    categories: list[CategoryInfo] = []


class VersionResponse(BaseModel):
    """Response data for version command."""

    # Default derives from the package's single source of truth
    # (chaos_agent.__version__) — no lockstep edit on version bump.
    version: str = _PKG_VERSION
    build_time: str = ""
    git_commit: str = ""
    blade_version: str = ""
    kubectl_version: str = ""
    skill_count: int = 0


