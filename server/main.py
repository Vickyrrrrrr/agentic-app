import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from openai import AzureOpenAI, OpenAI
from sse_starlette.sse import EventSourceResponse

from models import ChatRequest, ToolInstallPlanRequest, ToolInstallRequest, UsageBuildRequest
from workspace import list_artifacts, list_designs, read_artifact, read_workspace_artifact, ensure_workspace
from chat_agent import converse_stream
from local_tools import detect_environment, install_command_for, run_bash

app = FastAPI(title="AgentIC Local")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["null", *[origin.strip() for origin in os.environ.get("AGENTIC_ALLOWED_ORIGINS", "").split(",") if origin.strip()]],
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|file://.*|agentic://.*)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "agentic-local"}

WS_ROOT = os.environ.get("AGENTIC_WORKSPACE") or os.path.expanduser("~/AgentIC-workspace")
ensure_workspace(WS_ROOT)
STATE_DIR = Path(WS_ROOT) / ".agentic"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ENTITLEMENT_PATH = STATE_DIR / "entitlement.json"
USAGE_LOG_PATH = STATE_DIR / "usage.jsonl"
RUN_EVENTS_PATH = STATE_DIR / "run_events.jsonl"
ACTIVE_DESIGN_PATH = STATE_DIR / "active_design.json"
CANCELLED_RUNS: set[str] = set()
WORKSPACE_SECTION_DIRS = {
    "rtl", "tb", "dv", "sim", "synth", "pnr", "sta", "reports",
    "constraints", "formal", "layout", "logs", "scripts", "hardening",
    "signoff", "openlane", "openroad", "runs",
}

# Load local license.json values into environment if not set
try:
    _json_path = os.path.join(os.path.dirname(__file__), "..", "desktop", "resources", "license.json")
    if os.path.exists(_json_path):
        with open(_json_path, "r") as _f:
            _config = json.load(_f)
            if _config.get("license_server_url") and not os.environ.get("AGENTIC_LICENSE_SERVER_URL"):
                os.environ["AGENTIC_LICENSE_SERVER_URL"] = _config["license_server_url"]
            if _config.get("entitlement_public_key") and not os.environ.get("AGENTIC_ENTITLEMENT_PUBLIC_KEY"):
                os.environ["AGENTIC_ENTITLEMENT_PUBLIC_KEY"] = _config["entitlement_public_key"]
except Exception:
    pass



def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _epoch_from(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def _normalize_entitlement(data: dict, source: str) -> dict:
    now = time.time()
    expires_at = (
        _epoch_from(data.get("expires_at"))
        or _epoch_from(data.get("expiry"))
        or _epoch_from(data.get("entitlement_expires_at"))
        or now + int(os.environ.get("AGENTIC_LICENSE_CACHE_SECONDS", "3600"))
    )
    active = bool(data.get("active") or data.get("has_subscription") or data.get("licensed"))
    return {
        "active": active and expires_at > now,
        "plan": data.get("plan") or data.get("tier") or ("licensed" if active else "unlicensed"),
        "expires_at": expires_at,
        "checked_at": now,
        "source": source,
        "usage_limit": data.get("usage_limit") or data.get("build_limit"),
        "used_builds": data.get("used_builds", 0),
        "reason": data.get("reason") or (None if active else "No active purchased license was found."),
    }


def _safe_license_failure_reason(status_code: int, body: str = "") -> str:
    text = (body or "").lower()
    if status_code == 401:
        return "Your sign-in session could not be verified. Please sign in again."
    if status_code == 402:
        return "No active AgentIC license was found for this account."
    if status_code in {500, 502, 503, 504}:
        return "We could not verify your license right now. Please try again in a moment."
    if "supabase" in text or "traceback" in text or "\"detail\"" in text or "{'" in text or "{\"" in text:
        return "We could not verify your license right now. Please try again in a moment."
    return "License verification failed. Please try again."


def normalize_pem_public_key(key_str: str) -> str:
    key_str = key_str.strip().strip('"').strip("'")
    key_str = key_str.replace("\\n", "\n").replace("\\r", "\r")
    
    if "-----BEGIN PUBLIC KEY-----" in key_str:
        parts = key_str.split("-----BEGIN PUBLIC KEY-----")
        if len(parts) > 1:
            body_and_footer = parts[1]
            body_parts = body_and_footer.split("-----END PUBLIC KEY-----")
            if len(body_parts) > 0:
                body = body_parts[0]
                body_clean = "".join(body.split())
                lines = [body_clean[i:i+64] for i in range(0, len(body_clean), 64)]
                normalized = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----"
                return normalized
    return key_str


def _entitlement_verify_key() -> tuple[str, list[str]] | None:
    public_key = os.environ.get("AGENTIC_ENTITLEMENT_PUBLIC_KEY", "").strip()
    if not public_key:
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "desktop", "resources", "license.json"),  # dev mode
            os.path.abspath(os.path.join(os.getcwd(), "..", "license.json")),                       # packaged mode
            os.path.abspath(os.path.join(os.getcwd(), "license.json")),                            # fallback
        ]
        for path in candidates:
            try:
                if os.path.exists(path):
                    with open(path, "r") as _f:
                        _config = json.load(_f)
                        val = _config.get("entitlement_public_key", "").strip()
                        if val:
                            public_key = val
                            break
            except Exception:
                pass

    if public_key:
        try:
            public_key = normalize_pem_public_key(public_key)
        except Exception as e:
            logging.error("Failed to normalize public key: %s", e)
        return public_key, ["RS256"]

    # Development fallback only. Production desktop builds should verify RS256
    # entitlements with AGENTIC_ENTITLEMENT_PUBLIC_KEY.
    shared_secret = os.environ.get("AGENTIC_ENTITLEMENT_SECRET", "").strip()
    if shared_secret and _env_true("AGENTIC_ALLOW_HS256_ENTITLEMENTS"):
        return shared_secret, ["HS256"]
    return None


def _verify_signed_entitlement(data: dict, source: str) -> dict | None:
    token = data.get("signed_entitlement")
    if not token:
        return None
    try:
        unverified_header = jwt.get_unverified_header(token)
        logging.error("DEBUG: JWT unverified header: %s", unverified_header)
    except Exception as e:
        logging.error("DEBUG: Failed to read JWT header: %s", e)
    verify_config = _entitlement_verify_key()
    if not verify_config:
        return None
    key, algorithms = verify_config
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience="agentic-desktop",
            issuer="agentic-license-server",
        )
    except jwt.PyJWTError as exc:
        logging.error("Entitlement verification failed: %s", exc)
        return None

    expires_at = _epoch_from(claims.get("exp")) or 0
    if expires_at <= time.time():
        return None

    limits = claims.get("limits") if isinstance(claims.get("limits"), dict) else {}
    return {
        "active": True,
        "plan": claims.get("plan") or data.get("plan") or "licensed",
        "expires_at": expires_at,
        "checked_at": time.time(),
        "source": source,
        "usage_limit": limits.get("builds_per_month") or data.get("usage_limit"),
        "used_builds": data.get("used_builds", 0),
        "signed_entitlement": token,
    }


def _read_cached_entitlement() -> dict | None:
    try:
        cached = json.loads(ENTITLEMENT_PATH.read_text())
    except Exception:
        return None
    verified = _verify_signed_entitlement(cached, cached.get("source") or "cache")
    if verified:
        verified["source"] = "cache"
        return verified
    if _env_bool("AGENTIC_REQUIRE_SIGNED_ENTITLEMENT", True):
        return None
    expires_at = _epoch_from(cached.get("expires_at")) or 0
    if cached.get("active") and expires_at > time.time():
        cached["source"] = cached.get("source") or "cache"
        return cached
    return None


def _write_cached_entitlement(entitlement: dict) -> None:
    if entitlement.get("active"):
        ENTITLEMENT_PATH.write_text(json.dumps(entitlement, indent=2), encoding="utf-8")


def _cached_entitlement_for_temporary_failure(reason: str | None = None) -> dict | None:
    cached = _read_cached_entitlement()
    if not cached:
        return None
    cached["source"] = "cache"
    cached["reason"] = reason or "Using cached entitlement while license verification refreshes."
    return cached


def _authorization_headers(request: Request) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    auth = request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth
    email = request.headers.get("x-agentic-user-email")
    if email:
        headers["X-AgentIC-User-Email"] = email
    return headers


def resolve_license_status(request: Request) -> dict:
    if _env_true("AGENTIC_LICENSE_BYPASS"):
        return _normalize_entitlement(
            {"active": True, "plan": "developer", "expires_at": time.time() + 24 * 3600},
            "developer_bypass",
        )

    license_url = os.environ.get("AGENTIC_LICENSE_STATUS_URL", "").strip()
    if license_url:
        try:
            cloud_req = urllib.request.Request(
                license_url,
                headers=_authorization_headers(request),
                method="GET",
            )
            with urllib.request.urlopen(cloud_req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            verified = _verify_signed_entitlement(data, "cloud")
            if verified:
                _write_cached_entitlement(verified)
                return verified
            if not data.get("active"):
                return _normalize_entitlement(data, "cloud")
            if _env_bool("AGENTIC_REQUIRE_SIGNED_ENTITLEMENT", True):
                cached = _cached_entitlement_for_temporary_failure(
                    "Using cached entitlement while license verification refreshes."
                )
                if cached:
                    return cached
                return {
                    "active": False,
                    "plan": "unlicensed",
                    "checked_at": time.time(),
                    "source": "cloud_unsigned",
                    "reason": "We could not verify your license securely. Please try again in a moment.",
                }
            entitlement = _normalize_entitlement(data, "cloud")
            _write_cached_entitlement(entitlement)
            return entitlement
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                cached = _cached_entitlement_for_temporary_failure(
                    "Using cached entitlement while your sign-in session refreshes."
                )
                if cached:
                    return cached
                return {
                    "active": False,
                    "plan": "unlicensed",
                    "checked_at": time.time(),
                    "source": "cloud_unauthorized",
                    "reason": _safe_license_failure_reason(exc.code),
                }
            if exc.code == 402:
                return {
                    "active": False,
                    "plan": "unlicensed",
                    "checked_at": time.time(),
                    "source": "cloud_inactive",
                    "reason": _safe_license_failure_reason(exc.code),
                }
            body = exc.read().decode("utf-8", errors="replace")
            cached = _cached_entitlement_for_temporary_failure(
                "Using cached entitlement because license verification is temporarily unavailable."
            )
            if cached:
                return cached
            return {
                "active": False,
                "plan": "unlicensed",
                "checked_at": time.time(),
                "source": "cloud_error",
                "reason": _safe_license_failure_reason(exc.code, body),
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            cached = _cached_entitlement_for_temporary_failure(
                "Using cached entitlement because license cloud is temporarily unavailable."
            )
            if cached:
                return cached
            return {
                "active": False,
                "plan": "unlicensed",
                "checked_at": time.time(),
                "source": "cloud_error",
                "reason": "We could not verify your license right now. Please try again in a moment.",
            }

    cached = _read_cached_entitlement()
    if cached:
        return cached
    return {
        "active": False,
        "plan": "unlicensed",
        "checked_at": time.time(),
        "source": "not_configured",
        "reason": "License cloud is not configured. Set AGENTIC_LICENSE_STATUS_URL for paid desktop verification.",
    }


def _install_plan_for(capability: str, requested_platform: str | None = None) -> dict:
    env = detect_environment()
    capability = capability.lower().strip()

    if capability in {"pnr", "openlane", "openroad"}:
        image = os.environ.get("AGENTIC_PNR_DOCKER_IMAGE", "").strip()
        command = install_command_for("pnr") or (f"docker pull {image}" if image else "")
        return {
            "capability": "pnr",
            "tool": "PnR flow",
            "strategy": "docker_or_native",
            "command": command,
            "target": "User-selected native tool or Docker image",
            "requires_admin": False,
            "purpose": (
                "No PnR path is assumed. Configure AGENTIC_PNR_DOCKER_IMAGE for an open-source Docker flow, "
                "or install/configure your proprietary PnR tool and expose it on PATH."
            ),
            "options": [
                {"key": "configure", "label": "Configure tool path", "description": "Add your PnR binary to PATH or set AGENTIC_PNR_TOOLS."},
                {"key": "docker", "label": "Use Docker flow", "description": "Set AGENTIC_PNR_DOCKER_IMAGE, then approve the pull."},
                {"key": "skip", "label": "Skip PnR", "description": "Continue with RTL, simulation, and synthesis only."},
            ],
            "approved": False,
        }

    if capability in {"simulation", "synthesis", "basic"}:
        normalized = "basic" if capability == "basic" else capability
        command = install_command_for(normalized) or install_command_for("basic")
        return {
            "capability": normalized,
            "tool": f"{normalized.title()} capability",
            "strategy": "user_configured",
            "command": command,
            "target": "User-selected native tool, proprietary flow, open-source tool, or Docker image",
            "requires_admin": False,
            "purpose": (
                "No EDA tool is assumed. Configure tools on PATH, set AGENTIC_EDA_TOOLS and capability env vars, "
                "or provide an install command through AGENTIC_*_INSTALL_COMMAND."
            ),
            "options": [
                {"key": "configure", "label": "Configure existing tools", "description": "Expose your tools on PATH or set AGENTIC_EDA_TOOLS plus AGENTIC_SIM_TOOLS / AGENTIC_SYNTH_TOOLS."},
                {"key": "install", "label": "Use configured install command", "description": "Set AGENTIC_BASIC_INSTALL_COMMAND or the capability-specific install command, then approve it."},
                {"key": "skip", "label": "Skip unavailable stage", "description": "Continue only with stages that are available locally."},
            ],
            "approved": False,
        }

    if capability in {"pdk", "pdks"}:
        return {
            "capability": "pdk",
            "tool": "PDK",
            "strategy": "manual_configuration",
            "command": "Set PDK_ROOT, PDKPATH, PDK_HOME, or AGENTIC_PDK_SEARCH_PATHS to your installed PDK directory.",
            "target": "User-configured PDK path",
            "requires_admin": False,
            "purpose": "Point AgentIC at local process design kit files before hardening.",
            "options": [
                {"key": "configure", "label": "Configure PDK path", "description": "Set PDK_ROOT, PDKPATH, PDK_HOME, or AGENTIC_PDK_SEARCH_PATHS."},
                {"key": "skip", "label": "Skip physical stages", "description": "Continue with RTL-oriented stages only."},
            ],
            "approved": False,
        }

    raise HTTPException(400, f"Unknown install capability: {capability}")


def _is_allowed_install_command(command: str) -> bool:
    stripped = command.strip()
    if _env_true("AGENTIC_ALLOW_CUSTOM_INSTALL_COMMANDS"):
        return bool(stripped) and not any(fragment in f" {stripped} " for fragment in (" rm ", " rm -", "&& rm", "; rm", ">", ">>"))
    allowed_prefixes = (
        "docker pull ",
        "sudo apt-get ",
        "apt-get ",
        "brew install ",
        "wsl -d ",
        "python3 -m pip install ",
        "pip install ",
    )
    denied_fragments = (" rm ", " rm -", "&& rm", "; rm", ">", ">>")
    return stripped.startswith(allowed_prefixes) and not any(fragment in f" {stripped} " for fragment in denied_fragments)


def _forward_usage(entry: dict, request: Request) -> bool:
    usage_url = os.environ.get("AGENTIC_USAGE_URL", "").strip()
    if not usage_url:
        return False
    try:
        payload = json.dumps({
            "build_status": entry.get("status") or "unknown",
            "tool_capability_tier": entry.get("capability_tier") or "unknown",
            "file_count": entry.get("file_count", 0),
            "artifact_count": entry.get("artifact_count", 0),
        }).encode("utf-8")
        cloud_req = urllib.request.Request(
            usage_url,
            data=payload,
            headers=_authorization_headers(request),
            method="POST",
        )
        with urllib.request.urlopen(cloud_req, timeout=10):
            return True
    except Exception:
        return False


def _forward_checkout(plan: str, request: Request) -> dict:
    checkout_url = os.environ.get("AGENTIC_CHECKOUT_URL", "").strip()
    if not checkout_url:
        license_base = os.environ.get("AGENTIC_LICENSE_SERVER_URL", "").strip().rstrip("/")
        checkout_url = f"{license_base}/checkout/create" if license_base else ""
    if not checkout_url:
        raise HTTPException(503, "Checkout cloud is not configured. Set AGENTIC_LICENSE_SERVER_URL.")
    try:
        payload = json.dumps({"plan": plan}).encode("utf-8")
        cloud_req = urllib.request.Request(
            checkout_url,
            data=payload,
            headers=_authorization_headers(request),
            method="POST",
        )
        with urllib.request.urlopen(cloud_req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _ = exc.read()
        if exc.code == 401:
            raise HTTPException(401, "Please sign in again before checkout.")
        if exc.code == 402:
            raise HTTPException(402, "This account does not have checkout access yet.")
        raise HTTPException(502, "Unable to start checkout. Please try again in a moment.")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        raise HTTPException(502, "Unable to start checkout. Please try again in a moment.")


def _license_server_base() -> str:
    return os.environ.get("AGENTIC_LICENSE_SERVER_URL", "").strip().rstrip("/")


def _forward_license_json(
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    request: Request | None = None,
    include_auth: bool = True,
    timeout: int = 15,
) -> dict | list:
    license_base = _license_server_base()
    if not license_base:
        raise HTTPException(503, "Account service is not configured.")

    headers = {"Content-Type": "application/json"}
    if request is not None and include_auth:
        auth = request.headers.get("authorization")
        if auth:
            headers["Authorization"] = auth

    data = json.dumps(payload or {}).encode("utf-8") if method.upper() != "GET" else None
    cloud_req = urllib.request.Request(
        f"{license_base}{path}",
        data=data,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(cloud_req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        reason = _safe_license_failure_reason(exc.code, body)
        if path.startswith("/auth/"):
            if exc.code == 401:
                reason = "Sign-in failed. Check your email and password."
            elif exc.code == 503:
                reason = "Account sign-in is temporarily unavailable."
        raise HTTPException(exc.code if exc.code in {400, 401, 402, 422, 503} else 502, reason)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        raise HTTPException(502, "Account service is temporarily unavailable. Please try again in a moment.")


def _append_run_event(event: dict) -> None:
    event_type = event.get("type")
    label = event.get("label")
    if not label:
        if event_type == "response":
            label = "Build summary ready"
        elif event_type == "needs_input":
            label = event.get("content") or event.get("message")
        elif event_type == "error":
            label = "The local run hit an issue"
        else:
            label = event.get("content") or event.get("message")
    safe = {
        "run_id": event.get("run_id"),
        "timestamp": event.get("timestamp", time.time()),
        "type": event_type,
        "label": label,
        "stage": event.get("stage") or event.get("state"),
        "status": event.get("status"),
        "design_name": event.get("design_name"),
    }
    safe = {key: value for key, value in safe.items() if value is not None}
    with RUN_EVENTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(safe) + "\n")


def _set_active_design(design_name: str | None) -> None:
    if design_name is None:
        return
    if design_name != "":
        if (
            design_name.startswith(".")
            or "/" in design_name
            or "\\" in design_name
            or design_name.lower() in WORKSPACE_SECTION_DIRS
        ):
            return
    ACTIVE_DESIGN_PATH.write_text(json.dumps({
        "name": design_name,
        "updated_at": time.time(),
    }), encoding="utf-8")


def _get_active_design() -> dict | None:
    try:
        active = json.loads(ACTIVE_DESIGN_PATH.read_text(encoding="utf-8"))
        active_name = str(active.get("name") or "")
        if active_name:
            if active_name.lower() in WORKSPACE_SECTION_DIRS:
                return None
            return active
    except Exception:
        pass
    designs = list_designs(WS_ROOT)
    if not designs:
        return None
    latest = max(designs, key=lambda item: item.get("updated_at", 0))
    return {"name": latest["name"], "updated_at": latest.get("updated_at")}


def _read_run_events(limit: int = 200, run_id: str | None = None) -> list[dict]:
    try:
        lines = RUN_EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    events = []
    for line in lines[-max(limit * 2, limit):]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run_id and event.get("run_id") != run_id:
            continue
        events.append(event)
    return events[-limit:]


@app.get("/pdks")
async def get_pdks():
    return detect_environment()


@app.get("/license/status")
async def get_license_status(request: Request):
    return resolve_license_status(request)


@app.get("/plans")
async def get_plans():
    data = _forward_license_json("/plans", method="GET", include_auth=False)
    return {"plans": data if isinstance(data, list) else []}


@app.post("/auth/password-login")
async def auth_password_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    if not email or not password:
        raise HTTPException(400, "Email and password are required.")
    return _forward_license_json(
        "/auth/password-login",
        method="POST",
        payload={"email": email, "password": password},
        include_auth=False,
    )


@app.post("/auth/signup")
async def auth_signup(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    if not email or not password:
        raise HTTPException(400, "Email and password are required.")
    return _forward_license_json(
        "/auth/signup",
        method="POST",
        payload={"email": email, "password": password},
        include_auth=False,
    )


@app.post("/auth/refresh")
async def auth_refresh(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    refresh_token = str(body.get("refresh_token") or "")
    if not refresh_token:
        raise HTTPException(400, "Refresh token is required.")
    return _forward_license_json(
        "/auth/refresh",
        method="POST",
        payload={"refresh_token": refresh_token},
        include_auth=False,
    )


@app.post("/auth/logout")
async def auth_logout():
    return {"ok": True}


@app.get("/auth/google/start")
async def auth_google_start():
    license_base = _license_server_base()
    if not license_base:
        raise HTTPException(503, "Google sign-in is not configured.")
    return RedirectResponse(f"{license_base}/auth/google/start")


@app.post("/usage/build")
async def report_build_usage(req: UsageBuildRequest, request: Request):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    entry = {
        "timestamp": time.time(),
        "user_id": req.user_id,
        "status": req.status,
        "capability_tier": req.capability_tier,
        "successful_builds": req.successful_builds,
        "total_builds": req.total_builds,
        "license_source": license_status.get("source"),
    }
    with USAGE_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return {"status": "recorded", "forwarded": _forward_usage(entry, request)}


@app.post("/checkout/create")
async def create_checkout(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    plan = str(body.get("plan") or "pro")
    if plan not in {"starter", "pro"}:
        raise HTTPException(400, "Unknown checkout plan")
    return _forward_checkout(plan, request)


@app.get("/tools/status")
async def get_tools_status():
    return detect_environment()


@app.get("/runs/events")
async def get_run_events(request: Request, limit: int = 200, run_id: str = ""):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    return {"events": _read_run_events(min(max(limit, 1), 500), run_id or None)}


@app.post("/tools/install-plan")
async def get_tool_install_plan(req: ToolInstallPlanRequest):
    return _install_plan_for(req.capability, req.platform)


@app.post("/tools/install")
async def install_tool(req: ToolInstallRequest, request: Request):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    if not req.approved:
        raise HTTPException(400, "Tool installation requires explicit user approval.")
    planned = _install_plan_for(req.capability)
    if not planned.get("command"):
        raise HTTPException(400, "This capability needs user-selected tooling or a configured install source before AgentIC can install it.")
    if req.command.strip() != planned["command"].strip():
        raise HTTPException(400, "Install command does not match the approved AgentIC install plan.")
    if not _is_allowed_install_command(req.command):
        raise HTTPException(400, "Install command is not allowed by the local safety policy.")
    result = run_bash(req.command, WS_ROOT, timeout=req.timeout)
    return {
        "success": result["success"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "code": result["code"],
        "tools": detect_environment(),
    }


@app.get("/profile")
async def get_profile():
    return {
        "auth_enabled": False,
        "plan": "local",
        "has_byok_key": True,
        "email": "local@agentic.app",
    }


def _safe_model_error(error: Exception) -> str:
    text = str(error).lower()
    if any(token in text for token in ("401", "unauthorized", "api key", "authentication")):
        return "Model provider rejected the API key."
    if any(token in text for token in ("404", "model", "not found", "does not exist")):
        return "Model provider did not accept the selected model."
    if any(token in text for token in ("base_url", "connection", "connect", "dns", "ssl", "timeout", "timed out")):
        return "Could not reach the model provider. Check the base URL and network connection."
    if any(token in text for token in ("quota", "rate limit", "429", "billing")):
        return "Model provider is rate-limiting or out of quota."
    return "Model connection failed. Check the key, model name, and OpenAI-compatible base URL."


@app.post("/profile/byok/test")
async def test_byok_connection(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    group = body.get(str(body.get("group") or "group1")) if isinstance(body, dict) else {}
    if not isinstance(group, dict):
        group = {}
    api_key = str(group.get("api_key") or body.get("api_key") or "").strip()
    base_url = str(group.get("base_url") or body.get("base_url") or "https://api.openai.com/v1").strip()
    model = str(group.get("model") or body.get("model") or "gpt-4o").strip()
    if not api_key:
        raise HTTPException(400, "Model API key is required.")
    try:
        if "azure.com" in base_url.lower():
            match = re.match(r"(https://[^.]+\.openai\.azure\.com)", base_url)
            azure_endpoint = match.group(1) if match else base_url
            version_match = re.search(r"api-version=([\d-]+)", base_url)
            api_version = version_match.group(1) if version_match else "2024-08-01-preview"
            client = AzureOpenAI(api_key=api_key, api_version=api_version, azure_endpoint=azure_endpoint, timeout=20)
        else:
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=20)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with OK."}],
            max_tokens=4,
            temperature=0,
        )
        return {"status": "ok", "message": "Model connection verified."}
    except Exception as exc:
        raise HTTPException(400, _safe_model_error(exc))


@app.post("/profile/byok")
async def save_byok():
    return {"status": "ok"}


@app.get("/jobs")
async def get_jobs():
    return {"jobs": []}


@app.get("/billing/status")
async def get_billing_status(request: Request):
    license_status = resolve_license_status(request)
    return {
        "has_subscription": bool(license_status.get("active")),
        "plan": license_status.get("plan"),
        "build_limit": license_status.get("usage_limit"),
        "used_builds": license_status.get("used_builds", 0),
        "license_source": license_status.get("source"),
    }


@app.get("/designs")
async def get_designs(request: Request):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    return {"designs": list_designs(WS_ROOT)}


@app.get("/build/artifacts")
@app.get("/build/artifacts/{design_name}")
async def get_artifacts(request: Request, design_name: str = ""):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    return list_artifacts(design_name, WS_ROOT)


@app.get("/build/artifacts/file/{file_name:path}")
async def get_workspace_artifact(request: Request, file_name: str):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    content = read_workspace_artifact(file_name, WS_ROOT)
    if content is None:
        raise HTTPException(404, "Artifact not found")
    return content


@app.get("/build/artifacts/{design_name}/{file_name:path}")
async def get_artifact(request: Request, design_name: str, file_name: str):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    content = read_artifact(design_name, file_name, WS_ROOT)
    if content is None:
        raise HTTPException(404, "Artifact not found")
    return content


@app.post("/chat/converse")
async def chat_converse(req: ChatRequest, request: Request):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")

    api_key = req.api_key or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(400, "BYOK model key required before running the local agent.")
    base_url = req.base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = req.model or os.environ.get("OPENAI_MODEL", "gpt-4o")
    run_id = req.run_id or uuid.uuid4().hex
    CANCELLED_RUNS.discard(run_id)

    async def event_generator():
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        LLM_TIMEOUT = 300

        def _push_event(event_dict: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ("event", event_dict))

        def run_sync_gen():
            """Run the synchronous generator and push events to the queue."""
            try:
                for event in converse_stream(
                    messages=req.messages,
                    api_key=api_key,
                    workspace_root=WS_ROOT,
                    base_url=base_url,
                    model=model,
                    event_pusher=_push_event,
                    is_cancelled=lambda: run_id in CANCELLED_RUNS,
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))

        task = loop.run_in_executor(None, run_sync_gen)

        try:
            while True:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=LLM_TIMEOUT)

                if kind == "done":
                    if run_id in CANCELLED_RUNS:
                        done_event = {
                            "run_id": run_id,
                            "type": "cancelled",
                            "state": "cancelled",
                            "label": "Run stopped",
                            "status": "cancelled",
                            "timestamp": time.time(),
                        }
                        _append_run_event(done_event)
                        yield {"event": "message", "data": json.dumps(done_event)}
                        break
                    done_event = {
                        "run_id": run_id,
                        "type": "stream_end",
                        "state": "done",
                        "label": "Run complete",
                        "status": "completed",
                        "timestamp": time.time(),
                    }
                    _append_run_event(done_event)
                    yield {"event": "message", "data": json.dumps(done_event)}
                    break

                if kind == "error":
                    error_event = {
                        "run_id": run_id,
                        "type": "error",
                        "state": "ERROR",
                        "label": "The local run hit an issue",
                        "message": "The local run hit an issue",
                        "status": "failed",
                        "timestamp": time.time(),
                    }
                    _append_run_event(error_event)
                    yield {"event": "message", "data": json.dumps(error_event)}
                    break

                event = payload
                event_type = event.get("type", "")
                content = event.get("content", "")
                state = event.get("state", "")

                sse_data = {
                    "run_id": run_id,
                    "type": event_type,
                    "state": state or ("THINKING" if event_type == "reasoning" else "done"),
                    "message": content,
                    "content": content,
                    "label": event.get("label"),
                    "stage": event.get("stage"),
                    "status": event.get("status"),
                    "design_name": event.get("design_name"),
                    "timestamp": time.time(),
                }
                if sse_data.get("design_name"):
                    _set_active_design(sse_data["design_name"])
                if event_type in {"progress", "needs_input", "response", "error", "stream_end", "cancelled"}:
                    _append_run_event(sse_data)

                yield {"event": "message", "data": json.dumps(sse_data)}
                if event_type == "cancelled":
                    break

        except asyncio.TimeoutError:
            timeout_event = {
                "run_id": run_id,
                "type": "error", "state": "ERROR",
                "message": "The run timed out. Try again or check your model provider connection.",
                "label": "The run timed out",
                "status": "failed",
                "timestamp": time.time(),
            }
            _append_run_event(timeout_event)
            yield {"event": "message", "data": json.dumps(timeout_event)}
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    return EventSourceResponse(event_generator())


@app.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", run_id or ""):
        raise HTTPException(400, "Invalid run id")
    CANCELLED_RUNS.add(run_id)
    cancel_event = {
        "run_id": run_id,
        "type": "cancelled",
        "state": "cancelled",
        "label": "Run stop requested",
        "status": "cancelling",
        "timestamp": time.time(),
    }
    _append_run_event(cancel_event)
    return {"status": "cancelling", "run_id": run_id}


@app.get("/workspace/active")
async def get_active_workspace(request: Request):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    return {"active": _get_active_design()}

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=7860, log_config=None, access_log=False)
