from pydantic import BaseModel, Field
from typing import Any, Optional


class ChatRequest(BaseModel):
    messages: list[dict]
    plan_type: str = "byok"
    api_key: Optional[str] = None
    pdk_profile: str = ""
    base_url: Optional[str] = None
    model: Optional[str] = None
    run_id: Optional[str] = None
    design_name: Optional[str] = None


class ToolInstallPlanRequest(BaseModel):
    capability: str = "pnr"
    platform: Optional[str] = None


class ToolInstallRequest(BaseModel):
    capability: str = "pnr"
    command: str
    approved: bool = False
    timeout: int = 1200


class UsageBuildRequest(BaseModel):
    user_id: Optional[str] = None
    status: str
    capability_tier: Optional[str] = None
    successful_builds: int = 0
    total_builds: int = 0


class OpenCodeSessionRequest(BaseModel):
    session_id: str
    message_id: Optional[str] = None
    agent: str = "agentic-vlsi"
    agentic_mode: str = "advisor"
    user_text: str = ""
    workspace_root: Optional[str] = None
    pdk_profile: Optional[str] = None
    design_name: Optional[str] = None


class OpenCodeToolRequest(OpenCodeSessionRequest):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class OpenCodeRuntimeStartRequest(BaseModel):
    hostname: str = "127.0.0.1"
    port: int = 4096
    timeout: float = 20.0


class OpenCodeDesktopSessionRequest(BaseModel):
    session_id: Optional[str] = None
    title: Optional[str] = None
    agent: str = "agentic-vlsi"
    agentic_mode: str = "advisor"
    user_text: str = ""
    workspace_root: Optional[str] = None
    pdk_profile: Optional[str] = None
    design_name: Optional[str] = None


class OpenCodeDesktopMessageRequest(BaseModel):
    session_id: str
    text: str
    agent: str = "agentic-vlsi"
    agentic_mode: str = "advisor"
    workspace_root: Optional[str] = None
    pdk_profile: Optional[str] = None
    design_name: Optional[str] = None
    model: Optional[dict[str, str]] = None
    system: Optional[str] = None
    variant: Optional[str] = None
    message_id: Optional[str] = None
