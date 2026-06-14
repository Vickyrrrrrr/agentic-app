from __future__ import annotations

import hmac
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
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
SUPABASE_ANON_KEY = _optional_env("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = _optional_env("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_JWT_SECRET = _optional_env("SUPABASE_JWT_SECRET")
SUPABASE_JWKS_URL = _optional_env("SUPABASE_JWKS_URL")
LEMON_SQUEEZY_API_KEY = _optional_env("LEMON_SQUEEZY_API_KEY")
LEMON_SQUEEZY_STORE_ID = _optional_env("LEMON_SQUEEZY_STORE_ID")
LEMON_SQUEEZY_VARIANT_ID = _optional_env("LEMON_SQUEEZY_VARIANT_ID")
LEMON_SQUEEZY_WEBHOOK_SECRET = _optional_env("LEMON_SQUEEZY_WEBHOOK_SECRET")
LEMON_SQUEEZY_TEST_MODE = os.getenv("LEMON_SQUEEZY_TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
GOOGLE_CLIENT_ID = _optional_env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _optional_env("GOOGLE_CLIENT_SECRET")
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
PUBLIC_BASE_URL = os.getenv("AGENTIC_PUBLIC_BASE_URL", "https://api.buildstack.live").rstrip("/")
BUILDSTACK_AGENTIC_SUCCESS_URL = os.getenv(
    "AGENTIC_CHECKOUT_SUCCESS_URL",
    "https://buildstack.live/agentic/success",
).strip()
PLAN_ALIASES = {
    "starter": "starter",
    "builder": "starter",
    "pro": "pro",
    "studio": "pro",
}


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


class AuthPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=6, max_length=4096)


class AuthRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=8192)


class AuthUser(BaseModel):
    id: str
    email: str | None = None


class AuthSessionResponse(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    expires_at: int | None = None
    token_type: str = "bearer"
    user: AuthUser | None = None
    message: str | None = None


class PlanResponse(BaseModel):
    id: str
    name: str
    description: str
    build_limit: int | None = None
    price_display: str
    features: list[str]
    popular: bool = False


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


def _require_supabase_auth() -> tuple[str, str]:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=503, detail="Account sign-in is not configured")
    return SUPABASE_URL.rstrip("/"), SUPABASE_ANON_KEY


def _normalize_plan(plan: str | None) -> str:
    candidate = (plan or PLAN_NAME or "pro").strip().lower()
    normalized = PLAN_ALIASES.get(candidate)
    if not normalized:
        raise HTTPException(status_code=400, detail="Unknown AgentIC plan")
    return normalized


def _supabase_google_redirect(redirect_to: str) -> RedirectResponse:
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    target = (
        f"{SUPABASE_URL.rstrip('/')}/auth/v1/authorize"
        f"?provider=google"
        f"&redirect_to={quote(redirect_to, safe='')}"
    )
    return RedirectResponse(target)


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


def _auth_response(data: dict[str, Any], message: str | None = None) -> AuthSessionResponse:
    user_data = data.get("user") if isinstance(data.get("user"), dict) else {}
    expires_in = data.get("expires_in")
    expires_at = data.get("expires_at")
    if expires_at is None and isinstance(expires_in, int):
        expires_at = int(time.time()) + expires_in
    return AuthSessionResponse(
        access_token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        expires_in=expires_in,
        expires_at=expires_at,
        token_type=data.get("token_type") or "bearer",
        user=AuthUser(id=str(user_data.get("id") or user_data.get("sub") or ""), email=user_data.get("email"))
        if user_data
        else None,
        message=message,
    )


async def _supabase_auth_post(path: str, payload: dict[str, Any], params: dict[str, str] | None = None) -> dict[str, Any]:
    base_url, anon_key = _require_supabase_auth()
    headers = {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{base_url}/auth/v1/{path}",
            headers=headers,
            params=params,
            json=payload,
        )
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get("msg") or body.get("message") or body.get("error_description")
        if response.status_code in {400, 401, 422}:
            raise HTTPException(status_code=401, detail=message or "Sign-in failed")
        raise HTTPException(status_code=502, detail="Account service is temporarily unavailable")
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


def normalize_pem_private_key(key_str: str) -> str:
    key_str = key_str.strip().strip('"').strip("'")
    key_str = key_str.replace("\\n", "\n").replace("\\r", "\r")

    header_marker = "-----BEGIN RSA PRIVATE KEY-----"
    footer_marker = "-----END RSA PRIVATE KEY-----"
    if header_marker not in key_str:
        header_marker = "-----BEGIN PRIVATE KEY-----"
        footer_marker = "-----END PRIVATE KEY-----"

    if header_marker in key_str:
        parts = key_str.split(header_marker)
        if len(parts) > 1:
            body_and_footer = parts[1]
            body_parts = body_and_footer.split(footer_marker)
            if len(body_parts) > 0:
                body = body_parts[0]
                body_clean = "".join(body.split())
                lines = [body_clean[i:i+64] for i in range(0, len(body_clean), 64)]
                normalized = f"{header_marker}\n" + "\n".join(lines) + f"\n{footer_marker}"
                return normalized
    return key_str


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

    if use_rsa:
        try:
            signing_key = normalize_pem_private_key(signing_key)
        except Exception:
            pass
    else:
        signing_key = signing_key.strip('"').strip("'").replace("\\n", "\n").replace("\\r", "\r")

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
        signing_key,
        algorithm=algorithm,
        headers={"kid": key_id},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/plans", response_model=list[PlanResponse])
async def plans() -> list[PlanResponse]:
    starter_limit = int(os.getenv("AGENTIC_STARTER_BUILDS", "10"))
    return [
        PlanResponse(
            id="starter",
            name=os.getenv("AGENTIC_STARTER_PLAN_NAME", "Starter"),
            description=f"{starter_limit} successful chip builds",
            build_limit=starter_limit,
            price_display=os.getenv("AGENTIC_STARTER_PRICE_DISPLAY", "$20"),
            features=[
                f"{starter_limit} successful chip builds",
                "RTL generation and verification",
                "Local EDA tool execution",
                "Open-source or proprietary flow support",
                "Sanitized build progress",
                "Email support",
            ],
        ),
        PlanResponse(
            id="pro",
            name=os.getenv("AGENTIC_PRO_PLAN_NAME", "Pro (Unlimited)"),
            description="Unlimited successful chip builds",
            build_limit=None,
            price_display=os.getenv("AGENTIC_PRO_PRICE_DISPLAY", "$200"),
            popular=True,
            features=[
                "Unlimited successful chip builds",
                "RTL generation and verification",
                "Local EDA tool execution",
                "Open-source or proprietary flow support",
                "Sanitized build progress",
                "Priority support",
            ],
        ),
    ]


@app.post("/auth/password-login", response_model=AuthSessionResponse)
async def password_login(payload: AuthPasswordRequest) -> AuthSessionResponse:
    data = await _supabase_auth_post(
        "token",
        {"email": payload.email, "password": payload.password},
        params={"grant_type": "password"},
    )
    return _auth_response(data)


@app.post("/auth/signup", response_model=AuthSessionResponse)
async def signup(payload: AuthPasswordRequest) -> AuthSessionResponse:
    data = await _supabase_auth_post("signup", {"email": payload.email, "password": payload.password})
    response = _auth_response(data, message="Account created. Check your email if confirmation is required.")
    if not response.access_token:
        response.message = "Account created. Check your email to confirm your sign-in before continuing."
    return response


@app.post("/auth/refresh", response_model=AuthSessionResponse)
async def refresh(payload: AuthRefreshRequest) -> AuthSessionResponse:
    data = await _supabase_auth_post(
        "token",
        {"refresh_token": payload.refresh_token},
        params={"grant_type": "refresh_token"},
    )
    return _auth_response(data)


def _require_google_oauth() -> tuple[str, str]:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured on the license server")
    return GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET


@app.get("/auth/google/start")
async def google_start() -> RedirectResponse:
    client_id, _ = _require_google_oauth()
    state_str = json.dumps({"flow": "desktop", "plan": ""})
    params = {
        "client_id": client_id,
        "redirect_uri": f"{PUBLIC_BASE_URL}/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state_str,
    }
    target = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    response = RedirectResponse(target)
    response.set_cookie("agentic_flow", "desktop", max_age=3600, secure=True, samesite="lax")
    return response


@app.get("/purchase/start")
async def purchase_start(plan: str = "pro") -> RedirectResponse:
    client_id, _ = _require_google_oauth()
    normalized_plan = _normalize_plan(plan)
    state_str = json.dumps({"flow": "checkout", "plan": normalized_plan})
    params = {
        "client_id": client_id,
        "redirect_uri": f"{PUBLIC_BASE_URL}/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state_str,
    }
    target = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    response = RedirectResponse(target)
    response.set_cookie("agentic_flow", "checkout", max_age=3600, secure=True, samesite="lax")
    response.set_cookie("agentic_plan", normalized_plan, max_age=3600, secure=True, samesite="lax")
    return response


@app.get("/auth/google/callback")
async def google_callback(code: str, state: str | None = None) -> RedirectResponse:
    client_id, client_secret = _require_google_oauth()

    # 1. Exchange code for Google ID token and access token
    async with httpx.AsyncClient(timeout=20) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": f"{PUBLIC_BASE_URL}/auth/google/callback",
                "grant_type": "authorization_code",
            }
        )
        if token_resp.status_code >= 400:
            raise HTTPException(status_code=400, detail="Google authentication failed to exchange code")
        tokens = token_resp.json()
        id_token = tokens.get("id_token")
        google_access_token = tokens.get("access_token")
        if not id_token:
            raise HTTPException(status_code=400, detail="Google did not return an ID token")

    # 2. Exchange Google ID token for Supabase session
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=503, detail="Supabase connection is not configured")

    async with httpx.AsyncClient(timeout=20) as client:
        sb_resp = await client.post(
            f"{SUPABASE_URL.rstrip('/')}/auth/v1/token",
            params={"grant_type": "id_token"},
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json={
                "provider": "google",
                "id_token": id_token,
                "access_token": google_access_token,
            }
        )
        if sb_resp.status_code >= 400:
            try:
                err_body = sb_resp.json()
                err_msg = err_body.get("msg") or err_body.get("error_description") or "Supabase registration failed"
            except Exception:
                err_msg = "Supabase registration failed"
            raise HTTPException(status_code=400, detail=err_msg)

        sb_session = sb_resp.json()

    # 3. Decode flow state
    flow = "desktop"
    plan = ""
    if state:
        try:
            state_data = json.loads(state)
            flow = state_data.get("flow", "desktop")
            plan = state_data.get("plan", "")
        except Exception:
            pass

    access_token = sb_session.get("access_token")
    refresh_token = sb_session.get("refresh_token")
    expires_in = sb_session.get("expires_in")
    expires_at = int(time.time()) + expires_in if expires_in else ""

    hash_params = {
        "access_token": access_token or "",
        "refresh_token": refresh_token or "",
        "expires_in": str(expires_in or ""),
        "expires_at": str(expires_at or ""),
        "token_type": "bearer",
    }

    target_url = f"{PUBLIC_BASE_URL}/auth/google/landing?flow={flow}&plan={plan}#{urlencode(hash_params)}"
    response = RedirectResponse(target_url)
    response.set_cookie("agentic_flow", flow, max_age=3600, secure=True, samesite="lax")
    response.set_cookie("agentic_plan", plan, max_age=3600, secure=True, samesite="lax")
    return response


@app.get("/auth/google/landing", response_class=HTMLResponse)
async def google_landing(request: Request) -> HTMLResponse:
    flow = (request.query_params.get("flow") or request.cookies.get("agentic_flow") or "desktop").strip().lower()
    if flow not in {"desktop", "checkout"}:
        flow = "desktop"
    plan_raw = request.query_params.get("plan") or request.cookies.get("agentic_plan")
    plan = _normalize_plan(plan_raw) if flow == "checkout" else ""
    checkout_url = f"{PUBLIC_BASE_URL}/checkout/create"

    html_content = (
        r"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title id="pageTitle">AgentIC Account</title>
    <style>
      :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #050505; color: #f5f1ea; }
      main { width: min(560px, calc(100vw - 40px)); padding: 34px; border: 1px solid rgba(255,255,255,.12); border-radius: 18px; background: #0d0d0c; box-shadow: 0 24px 80px rgba(0,0,0,.35); text-align: center; }
      .mark { width: 34px; height: 34px; margin: 0 auto 18px; display: grid; place-items: center; border-radius: 10px; background: rgba(230,116,62,.14); color: #e6743e; font-weight: 800; }
      h1 { margin: 0; font-size: clamp(30px, 5vw, 48px); line-height: 1; letter-spacing: 0; }
      p { margin: 16px auto 0; color: #aaa39a; line-height: 1.55; max-width: 440px; }
      a, button { margin-top: 28px; display: inline-flex; align-items: center; justify-content: center; min-height: 46px; padding: 0 22px; border-radius: 999px; border: 0; background: #f1e8d9; color: #16110d; font-weight: 800; text-decoration: none; cursor: pointer; }
      .muted { margin-top: 16px; font-size: 13px; color: #756f68; }
      .error { display: none; margin-top: 22px; padding: 12px 14px; border: 1px solid rgba(230,116,62,.45); border-radius: 12px; color: #f0a37a; background: rgba(230,116,62,.08); }
      .hidden { display: none; }
    </style>
  </head>
  <body>
    <main>
      <div class="mark">A</div>
      <h1 id="headline">You're signed in</h1>
      <p id="copy">Return to AgentIC Desktop to verify your license, configure your model, and continue building locally.</p>
      <a id="openDesktop" href="#">Open AgentIC Desktop</a>
      <button id="retryCheckout" class="hidden" type="button">Continue to Checkout</button>
      <div class="muted">If nothing happens, make sure AgentIC Desktop is running and click again.</div>
      <div id="error" class="error">Google sign-in completed, but the secure account token was not returned. Please try signing in again.</div>
    </main>
    <script>
      const flow = __FLOW__;
      const plan = __PLAN__;
      const checkoutUrl = __CHECKOUT_URL__;
      const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
      const searchParams = new URLSearchParams(window.location.search);
      const accessToken = hash.get("access_token");
      const refreshToken = hash.get("refresh_token");
      const expiresAt = hash.get("expires_at");
      const expiresIn = hash.get("expires_in");
      const tokenType = hash.get("token_type") || "bearer";

      let errorMsg = hash.get("error_description") || hash.get("error") || searchParams.get("error_description") || searchParams.get("error");
      if (errorMsg) {
        errorMsg = decodeURIComponent(errorMsg.replace(/\+/g, ' '));
      }

      const link = document.getElementById("openDesktop");
      const retryCheckout = document.getElementById("retryCheckout");
      const error = document.getElementById("error");
      const headline = document.getElementById("headline");
      const copy = document.getElementById("copy");
      const muted = document.querySelector(".muted");

      function showError(defaultMsg) {
        error.style.display = "block";
        error.textContent = errorMsg ? "Error: " + errorMsg : defaultMsg;
      }

      async function continueToCheckout() {
        if (!accessToken) {
          link.style.display = "none";
          retryCheckout.classList.add("hidden");
          showError("Google sign-in completed, but the secure account token was not returned. Please try signing in again.");
          return;
        }
        error.style.display = "none";
        retryCheckout.disabled = true;
        retryCheckout.textContent = "Preparing checkout...";
        try {
          const response = await fetch(checkoutUrl, {
            method: "POST",
            headers: {
              "Authorization": `Bearer ${accessToken}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ plan }),
          });
          if (!response.ok) {
            throw new Error("checkout_failed");
          }
          const body = await response.json();
          if (!body.checkout_url) {
            throw new Error("checkout_missing_url");
          }
          window.location.href = body.checkout_url;
        } catch (_) {
          retryCheckout.disabled = false;
          retryCheckout.textContent = "Try Checkout Again";
          error.textContent = "We could not prepare checkout securely. Please try again in a moment.";
          error.style.display = "block";
        }
      }

      if (flow === "checkout") {
        document.title = "Preparing AgentIC Checkout";
        headline.textContent = "Preparing checkout";
        copy.textContent = "Your Google account is verified. AgentIC is opening the secure Lemon Squeezy checkout for your selected plan.";
        link.style.display = "none";
        muted.textContent = "No chip source, prompts, logs, PDK files, or artifacts are sent during checkout.";
        retryCheckout.classList.remove("hidden");
        retryCheckout.addEventListener("click", continueToCheckout);
        continueToCheckout();
      } else {
        document.title = "Open AgentIC Desktop";
        retryCheckout.classList.add("hidden");
      if (accessToken && refreshToken) {
        const deepLink = new URL("agentic://auth-callback");
        deepLink.hash = new URLSearchParams({
          access_token: accessToken,
          refresh_token: refreshToken,
          expires_at: expiresAt || "",
          expires_in: expiresIn || "",
          token_type: tokenType,
        }).toString();
        link.href = deepLink.toString();
        setTimeout(() => { window.location.href = link.href; }, 600);
      } else {
        link.style.display = "none";
        showError("Google sign-in completed, but the secure account token was not returned. Please try signing in again.");
      }
      }
    </script>
  </body>
</html>
        """
        .replace("__FLOW__", json.dumps(flow))
        .replace("__PLAN__", json.dumps(plan))
        .replace("__CHECKOUT_URL__", json.dumps(checkout_url))
    )

    response = HTMLResponse(html_content)
    response.delete_cookie("agentic_flow", secure=True, samesite="lax")
    response.delete_cookie("agentic_plan", secure=True, samesite="lax")
    return response


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

    plan = _normalize_plan(request.plan)
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
                    "redirect_url": BUILDSTACK_AGENTIC_SUCCESS_URL,
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
