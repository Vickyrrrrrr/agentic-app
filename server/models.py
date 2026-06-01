from pydantic import BaseModel
from typing import Optional


class ChatRequest(BaseModel):
    messages: list[dict]
    plan_type: str = "byok"
    api_key: Optional[str] = None
    pdk_profile: str = ""
    base_url: Optional[str] = None
    model: Optional[str] = None
