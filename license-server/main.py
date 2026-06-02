from __future__ import annotations

import hmac
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from jwt import PyJWKClient
from pydantic import BaseModel, ConfigDict, Field


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


SUPABASE_URL = _optional_env("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = _optional_env("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_JWT_SECRET = _optional_env("SUPABASE_JWT_SECRET")
SUPABASE_JWKS_URL = _optional_env("SUPABASE_JWKS_URL")
LEMON_SQUEEZY_API_KEY = _optional_env("LEMON_SQUEEZY_API_KEY")
LEMON_SQUEEZY_STORE_ID = _optional_env("LEMON_SQUEEZY_STORE_ID")
LEMON_SQUEEZY_VARIANT_ID = _optional_env("LEMON_SQUEEZY_VARIANT_ID")
LEMON_SQUEEZY_WEBHOOK_SECRET = _optional_env("LEMON_SQUEEZY_WEBHOOK_SECRET")
LEMON_SQUEEZY_TEST_MODE = os.getenv("LEMON_SQUEEZY_TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
ENTITLEMENT_JWT_SECRET = _optional_env("ENTITLEMENT_JWT_SECRET")
ENTITLEMENT_JWT_PRIVATE_KEY = _optional_env("ENTITLEMENT_JWT_PRIVATE_KEY")
ENTITLEMENT_JWT_PRIVATE_KEY_FILE = _optional_env("ENTITLEMENT_JWT_PRIVATE_KEY_FILE")
ENTITLEMENT_TTL_SECONDS = int(os.getenv("ENTITLEMENT_TTL_SECONDS", "21600"))
PLAN_NAME = os.getenv("AGENTIC_PLAN_NAME", "pro")
BUILDS_PER_MONTH = int(os.getenv("AGENTIC_BUILDS_PER_MONTH", "200"))
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "AGENTIC_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,app://agentic,file://,null",
    ).split(",")
    if origin.strip()
]


app = FastAPI(title="AgentIC License Server", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Signature"],
)


class CheckoutCreateResponse(BaseModel):
    checkout_url: str


class CheckoutCreateRequest(BaseModel):
    plan: str | None = None


class LicenseStatusResponse(BaseModel):
    active: bool
    plan: str | None = None
    expires_at: str | None = None
    usage_limits: dict[str, int] = Field(default_factory=dict)
    signed_entitlement: str | None = None
    reason: str | None = None


class UsageBuildPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_status: str = Field(min_length=1, max_length=64)
    tool_capability_tier: str = Field(default="unknown", max_length=64)
    file_count: int = Field(default=0, ge=0, le=100000)
    artifact_count: int = Field(default=0, ge=0, le=100000)
    run_seconds: int | None = Field(default=None, ge=0, le=30 * 24 * 60 * 60)


class UsageBuildResponse(BaseModel):
    ok: bool


class UserContext(BaseModel):
    user_id: str
    email: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def _jwks_url() -> str | None:
    if SUPABASE_JWKS_URL:
        return SUPABASE_JWKS_URL
    if SUPABASE_URL:
        return f"{SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"
    return None


def _verify_supabase_token(token: str) -> UserContext:
    last_error: Exception | None = None

    jwks_url = _jwks_url()
    if jwks_url:
        try:
            signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience="authenticated",
                options={"verify_aud": False},
            )
            return UserContext(user_id=claims["sub"], email=claims.get("email"))
        except Exception as exc:  # noqa: BLE001 - fallback to legacy Supabase secret.
            last_error = exc

    if SUPABASE_JWT_SECRET:
        try:
            claims = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_aud": False},
            )
            return UserContext(user_id=claims["sub"], email=claims.get("email"))
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    raise HTTPException(status_code=401, detail=f"Invalid Supabase session: {last_error}")


def current_user(authorization: str | None = Header(default=None)) -> UserContext:
    return _verify_supabase_token(_bearer_token(authorization))


def _require_supabase() -> tuple[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=503, detail="Supabase service is not configured")
    return SUPABASE_URL.rstrip("/"), SUPABASE_SERVICE_ROLE_KEY


def _supabase_headers() -> dict[str, str]:
    _, key = _require_supabase()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _supabase_get(path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
    base_url, _ = _require_supabase()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{base_url}/rest/v1/{path}",
            headers=_supabase_headers(),
            params=params,
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Supabase read failed")
    return response.json()


async def _supabase_post(path: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    base_url, _ = _require_supabase()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{base_url}/rest/v1/{path}",
            headers=_supabase_headers(),
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Supabase write failed")
    return response.json()


async def _supabase_upsert(path: str, payload: dict[str, Any], on_conflict: str) -> list[dict[str, Any]]:
    base_url, _ = _require_supabase()
    headers = _supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{base_url}/rest/v1/{path}",
            headers=headers,
            params={"on_conflict": on_conflict},
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Supabase upsert failed")
    return response.json()


async def _active_subscription(user_id: str) -> dict[str, Any] | None:
    rows = await _supabase_get(
        "agentic_subscriptions",
        {
            "user_id": f"eq.{user_id}",
            "status": "in.(active,on_trial,paused,past_due)",
            "order": "updated_at.desc",
            "limit": "1",
        },
    )
    if not rows:
        return None
    subscription = rows[0]
    status = subscription.get("status")
    if status in {"active", "on_trial", "paused", "past_due"}:
        ends_at = subscription.get("ends_at")
        if ends_at:
            try:
                if datetime.fromisoformat(ends_at.replace("Z", "+00:00")) < _now():
                    return None
            except ValueError:
                return None
        return subscription
    return None


def _signed_entitlement(user: UserContext, subscription: dict[str, Any]) -> str:
    private_key_from_file = None
    if ENTITLEMENT_JWT_PRIVATE_KEY_FILE:
        try:
            private_key_from_file = Path(ENTITLEMENT_JWT_PRIVATE_KEY_FILE).read_text(encoding="utf-8")
        except OSError:
            private_key_from_file = None
    signing_key = private_key_from_file or ENTITLEMENT_JWT_PRIVATE_KEY or ENTITLEMENT_JWT_SECRET
    if not signing_key:
        raise HTTPException(status_code=503, detail="Entitlement signing is not configured")

    issued_at = int(time.time())
    expires_at = issued_at + ENTITLEMENT_TTL_SECONDS
    use_rsa = bool(private_key_from_file or ENTITLEMENT_JWT_PRIVATE_KEY)
    algorithm = "RS256" if use_rsa else "HS256"
    key_id = "agentic-rs256" if use_rsa else "agentic-hs256"
    payload = {
        "iss": "agentic-license-server",
        "aud": "agentic-desktop",
        "sub": user.user_id,
        "email": user.email,
        "plan": subscription.get("plan") or PLAN_NAME,
        "subscription_id": subscription.get("lemon_subscription_id"),
        "iat": issued_at,
        "exp": expires_at,
        "limits": {"builds_per_month": BUILDS_PER_MONTH},
    }
    return jwt.encode(
        payload,
        signing_key.replace("\\n", "\n"),
        algorithm=algorithm,
        headers={"kid": key_id},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/license/status", response_model=LicenseStatusResponse)
async def license_status(user: UserContext = Depends(current_user)) -> LicenseStatusResponse:
    subscription = await _active_subscription(user.user_id)
    if not subscription:
        return LicenseStatusResponse(active=False, reason="No active AgentIC subscription")

    return LicenseStatusResponse(
        active=True,
        plan=subscription.get("plan") or PLAN_NAME,
        expires_at=subscription.get("ends_at") or subscription.get("renews_at"),
        usage_limits={"builds_per_month": BUILDS_PER_MONTH},
        signed_entitlement=_signed_entitlement(user, subscription),
    )


@app.post("/checkout/create", response_model=CheckoutCreateResponse)
async def create_checkout_post(
    request: CheckoutCreateRequest,
    user: UserContext = Depends(current_user),
) -> CheckoutCreateResponse:
    return await _create_checkout_for_plan(request, user)


async def _create_checkout_for_plan(request: CheckoutCreateRequest, user: UserContext) -> CheckoutCreateResponse:
    if not all([LEMON_SQUEEZY_API_KEY, LEMON_SQUEEZY_STORE_ID]):
        raise HTTPException(status_code=503, detail="Lemon Squeezy checkout is not configured")

    plan = (request.plan or PLAN_NAME).lower().strip()
    variant_by_plan = {
        "starter": _optional_env("LEMON_SQUEEZY_STARTER_VARIANT_ID"),
        "pro": _optional_env("LEMON_SQUEEZY_PRO_VARIANT_ID") or LEMON_SQUEEZY_VARIANT_ID,
    }
    variant_id = variant_by_plan.get(plan) or LEMON_SQUEEZY_VARIANT_ID
    if not variant_id:
        raise HTTPException(status_code=400, detail=f"No Lemon Squeezy variant configured for plan: {plan}")

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "test_mode": LEMON_SQUEEZY_TEST_MODE,
                "checkout_data": {
                    "email": user.email,
                    "custom": {
                        "supabase_user_id": user.user_id,
                        "email": user.email,
                        "plan": plan,
                    },
                },
                "product_options": {
                    "enabled_variants": [int(variant_id)],
                },
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(LEMON_SQUEEZY_STORE_ID)}},
                "variant": {"data": {"type": "variants", "id": str(variant_id)}},
            },
        }
    }
    headers = {
        "Authorization": f"Bearer {LEMON_SQUEEZY_API_KEY}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://api.lemonsqueezy.com/v1/checkouts", headers=headers, json=payload)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Checkout creation failed")

    checkout_url = response.json()["data"]["attributes"]["url"]
    return CheckoutCreateResponse(checkout_url=checkout_url)


def _verify_lemonsqueezy_signature(raw_body: bytes, signature: str | None) -> None:
    if not LEMON_SQUEEZY_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing webhook signature")
    digest = hmac.new(
        LEMON_SQUEEZY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(digest, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def _webhook_subscription_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    meta = event.get("meta") or {}
    data = event.get("data") or {}
    attributes = data.get("attributes") or {}
    custom_data = meta.get("custom_data") or attributes.get("custom_data") or {}
    user_id = custom_data.get("supabase_user_id")
    if not user_id:
        return None

    event_name = meta.get("event_name") or ""
    if not event_name.startswith("subscription_") and event_name not in {"order_created"}:
        return None

    renews_at = attributes.get("renews_at")
    ends_at = attributes.get("ends_at")
    raw_status = attributes.get("status") or "active"
    status = "active" if event_name == "order_created" and raw_status in {"paid", "paid_out"} else raw_status
    subscription_id = str(data.get("id") or attributes.get("subscription_id") or "")
    customer_id = str(attributes.get("customer_id") or "")
    order_id = str(attributes.get("order_id") or "")
    variant_id = str(attributes.get("variant_id") or LEMON_SQUEEZY_VARIANT_ID or "")
    plan = custom_data.get("plan") or PLAN_NAME
    if variant_id and variant_id == _optional_env("LEMON_SQUEEZY_STARTER_VARIANT_ID"):
        plan = "starter"
    if variant_id and variant_id == (_optional_env("LEMON_SQUEEZY_PRO_VARIANT_ID") or LEMON_SQUEEZY_VARIANT_ID):
        plan = "pro"

    return {
        "user_id": user_id,
        "email": custom_data.get("email") or attributes.get("user_email"),
        "lemon_customer_id": customer_id,
        "lemon_subscription_id": subscription_id or order_id,
        "lemon_order_id": order_id,
        "variant_id": variant_id,
        "plan": plan,
        "status": status,
        "renews_at": renews_at,
        "ends_at": ends_at,
        "updated_at": _now().isoformat(),
        "raw_event_id": meta.get("webhook_id") or data.get("id"),
    }


@app.post("/webhooks/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request, x_signature: str | None = Header(default=None)) -> dict[str, bool]:
    raw_body = await request.body()
    _verify_lemonsqueezy_signature(raw_body, x_signature)
    event = await request.json()
    payload = _webhook_subscription_payload(event)
    if payload:
        await _supabase_upsert("agentic_subscriptions", payload, on_conflict="lemon_subscription_id")
    return {"ok": True}


@app.post("/usage/build", response_model=UsageBuildResponse)
async def usage_build(payload: UsageBuildPayload, user: UserContext = Depends(current_user)) -> UsageBuildResponse:
    await _supabase_post(
        "agentic_usage_events",
        {
            "user_id": user.user_id,
            "build_status": payload.build_status,
            "tool_capability_tier": payload.tool_capability_tier,
            "file_count": payload.file_count,
            "artifact_count": payload.artifact_count,
            "run_seconds": payload.run_seconds,
            "created_at": _now().isoformat(),
        },
    )
    return UsageBuildResponse(ok=True)
