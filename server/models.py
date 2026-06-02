from pydantic import BaseModel
from typing import Optional


class ChatRequest(BaseModel):
    messages: list[dict]
    plan_type: str = "byok"
    api_key: Optional[str] = None
    pdk_profile: str = ""
    base_url: Optional[str] = None
    model: Optional[str] = None


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
