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
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from openai import AzureOpenAI, OpenAI
from sse_starlette.sse import EventSourceResponse

from models import (
    ChatRequest,
    OpenCodeDesktopMessageRequest,
    OpenCodeDesktopSessionRequest,
    OpenCodeRuntimeStartRequest,
    OpenCodeSessionRequest,
    OpenCodeToolRequest,
    ToolInstallPlanRequest,
    ToolInstallRequest,
    UsageBuildRequest,
)
from workspace import list_artifacts, list_designs, read_artifact, read_workspace_artifact, ensure_workspace
from chat_agent import converse_stream
from flow_runtime import recommend_flow
from local_tools import detect_environment, install_command_for, run_bash
from vlsi_state import DesignStateStore

SHUTDOWN_REQUESTED = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    logging.info("Server shutdown requested. Flagging all runs as cancelled.")

app = FastAPI(title="AgentIC Local", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["null", *[origin.strip() for origin in os.environ.get("AGENTIC_ALLOWED_ORIGINS", "").split(",") if origin.strip()]],
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|file://.*|agentic://.*|oc://.*)$",
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
AUTH_SESSION_PATH = STATE_DIR / "auth_session.json"
USAGE_LOG_PATH = STATE_DIR / "usage.jsonl"
RUN_EVENTS_PATH = STATE_DIR / "run_events.jsonl"
ACTIVE_DESIGN_PATH = STATE_DIR / "active_design.json"
OPENCODE_SESSIONS_PATH = STATE_DIR / "opencode_sessions.json"
CANCELLED_RUNS: set[str] = set()
ACTIVE_RUNS: dict[str, float] = {}
WORKSPACE_SECTION_DIRS = {
    "docs", "rtl", "tb", "dv", "sim", "synth", "pnr", "sta", "reports",
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


def _license_server_base() -> str:
    return os.environ.get("AGENTIC_LICENSE_SERVER_URL", "").strip().rstrip("/")


def _license_status_url() -> str:
    configured = os.environ.get("AGENTIC_LICENSE_STATUS_URL", "").strip()
    if configured:
        return configured
    base = _license_server_base()
    return f"{base}/license/status" if base else ""


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


def _signed_entitlement_required() -> bool:
    return _env_bool("AGENTIC_REQUIRE_SIGNED_ENTITLEMENT", _entitlement_verify_key() is not None)


def _verify_signed_entitlement(data: dict, source: str) -> dict | None:
    if not isinstance(data, dict):
        return None
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
            options={"verify_iat": False, "verify_exp": False},
        )
    except Exception as exc:
        logging.error("Entitlement verification failed: %s", exc)
        return None

    expires_at = _epoch_from(claims.get("exp")) or 0
    # Allow 24 hours of local clock drift leeway for expiration checks.
    if expires_at + 86400 <= time.time():
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
    if not isinstance(cached, dict):
        return None
    verified = _verify_signed_entitlement(cached, cached.get("source") or "cache")
    if verified:
        verified["source"] = "cache"
        return verified
    if _signed_entitlement_required():
        return None
    expires_at = _epoch_from(cached.get("expires_at")) or 0
    if cached.get("active") and expires_at > time.time():
        cached["source"] = cached.get("source") or "cache"
        return cached
    return None


def _write_cached_entitlement(entitlement: dict) -> None:
    if entitlement.get("active"):
        ENTITLEMENT_PATH.write_text(json.dumps(entitlement, indent=2), encoding="utf-8")


def _write_auth_session(session: dict) -> None:
    if not session.get("access_token"):
        return
    safe_session = {
        key: value
        for key, value in session.items()
        if key in {"access_token", "refresh_token", "expires_at", "expires_in", "token_type", "user"}
    }
    AUTH_SESSION_PATH.write_text(json.dumps(safe_session, indent=2), encoding="utf-8")


def _clear_auth_session() -> None:
    try:
        AUTH_SESSION_PATH.unlink()
    except FileNotFoundError:
        pass


def _read_auth_session() -> dict | None:
    try:
        session = json.loads(AUTH_SESSION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return session if isinstance(session, dict) and session.get("access_token") else None


def _auth_session_expiring(session: dict) -> bool:
    expires_at = _epoch_from(session.get("expires_at"))
    if not expires_at:
        return False
    return expires_at <= time.time() + 120


def _read_or_refresh_auth_session() -> dict | None:
    session = _read_auth_session()
    if not session:
        return None
    if not _auth_session_expiring(session):
        return session
    refresh_token = str(session.get("refresh_token") or "")
    if not refresh_token:
        _clear_auth_session()
        return None
    try:
        refreshed = _forward_license_json(
            "/auth/refresh",
            method="POST",
            payload={"refresh_token": refresh_token},
            include_auth=False,
        )
    except Exception:
        return session
    if isinstance(refreshed, dict) and refreshed.get("access_token"):
        _write_auth_session(refreshed)
        return refreshed
    return session


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
    persisted_session = None
    if not auth:
        persisted_session = _read_or_refresh_auth_session()
        token = persisted_session.get("access_token") if persisted_session else None
        if token:
            auth = f"Bearer {token}"
    if auth:
        headers["Authorization"] = auth
    email = request.headers.get("x-agentic-user-email")
    if not email and persisted_session:
        user = persisted_session.get("user") if isinstance(persisted_session.get("user"), dict) else {}
        email = user.get("email") if isinstance(user, dict) else None
    if email:
        headers["X-AgentIC-User-Email"] = email
    return headers


def resolve_license_status(request: Request) -> dict:
    try:
        if _env_true("AGENTIC_LICENSE_BYPASS"):
            return _normalize_entitlement(
                {"active": True, "plan": "developer", "expires_at": time.time() + 24 * 3600},
                "developer_bypass",
            )

        license_url = _license_status_url()
        if license_url:
            try:
                cloud_req = urllib.request.Request(
                    license_url,
                    headers=_authorization_headers(request),
                    method="GET",
                )
                with urllib.request.urlopen(cloud_req, timeout=10) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if not isinstance(data, dict):
                    return {
                        "active": False,
                        "plan": "unlicensed",
                        "checked_at": time.time(),
                        "source": "cloud_error",
                        "reason": "Invalid response format from license server.",
                    }
                verified = _verify_signed_entitlement(data, "cloud")
                if verified:
                    _write_cached_entitlement(verified)
                    return verified
                if not data.get("active"):
                    return _normalize_entitlement(data, "cloud")
                if _signed_entitlement_required():
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
    except Exception as exc:
        logging.error("Unhandled exception in resolve_license_status: %s", exc)
        return {
            "active": False,
            "plan": "unlicensed",
            "checked_at": time.time(),
            "source": "internal_error",
            "reason": "Internal license verification error.",
        }


def _install_plan_for(capability: str, requested_platform: str | None = None) -> dict:
    env = detect_environment()
    capability = capability.lower().strip()

    if capability in {"pnr", "openlane", "openroad"}:
        image = os.environ.get("AGENTIC_PNR_DOCKER_IMAGE", "").strip() or "efabless/openlane:latest"
        command = install_command_for("pnr") or (f"docker pull {image}" if image else "")
        return {
            "capability": "pnr",
            "tool": "PnR flow",
            "strategy": "docker_or_native",
            "command": command,
            "target": "User-selected native tool or Docker image",
            "requires_admin": False,
            "purpose": (
                "No physical design (PnR) tool is detected in your PATH. You can pull an open-source Docker image "
                "like OpenLane to run standard cells placement/routing, or configure paths to proprietary PnR tools "
                "such as Cadence Innovus or Synopsys ICC2 by adding them to your system PATH."
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
        default_cmds = {
            "simulation": "sudo apt-get update && sudo apt-get install -y iverilog verilator",
            "synthesis": "sudo apt-get update && sudo apt-get install -y yosys",
            "basic": "sudo apt-get update && sudo apt-get install -y make python3-pip"
        }
        command = install_command_for(normalized) or install_command_for("basic") or default_cmds.get(normalized, "")
        return {
            "capability": normalized,
            "tool": f"{normalized.title()} capability",
            "strategy": "user_configured",
            "command": command,
            "target": "User-selected native tool, proprietary flow, open-source tool, or Docker image",
            "requires_admin": False,
            "purpose": (
                "No simulation, synthesis, or basic compilation tools are detected. You can install open-source defaults "
                "(iverilog, verilator, yosys) using the package manager, or expose your proprietary tools (Synopsys VCS / Design Compiler, "
                "Cadence Xrun / Genus, Siemens Questasim) on PATH and configure license variables."
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
        else:
            session = _read_or_refresh_auth_session()
            token = session.get("access_token") if session else None
            if token:
                headers["Authorization"] = f"Bearer {token}"

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


def _require_opencode_bridge(request: Request) -> None:
    token = os.environ.get("AGENTIC_OPENCODE_BRIDGE_TOKEN", "").strip()
    if not token:
        return
    supplied = request.headers.get("x-agentic-bridge-token", "").strip()
    if supplied != token:
        raise HTTPException(401, "AgentIC runtime bridge token is invalid.")


def _wsl_path_to_linux(path: str | None) -> str | None:
    if not path:
        return path
    normalized = path.replace("\\", "/")
    if normalized.startswith("//wsl.localhost/"):
        parts = normalized.split("/")
        if len(parts) >= 5:
            return "/" + "/".join(parts[4:])
    if normalized.startswith("//wsl$/"):
        parts = normalized.split("/")
        if len(parts) >= 5:
            return "/" + "/".join(parts[4:])
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        return f"/mnt/{drive}" + normalized[2:]
    return path

def _safe_workspace_root(candidate: str | None) -> str:
    candidate = _wsl_path_to_linux(candidate)
    root = os.path.abspath(os.path.normpath(candidate or WS_ROOT))
    if root == os.path.abspath(os.sep):
        root = os.path.abspath(os.path.normpath(WS_ROOT))
    try:
        os.makedirs(root, exist_ok=True)
    except PermissionError:
        root = os.path.abspath(os.path.normpath(WS_ROOT))
        os.makedirs(root, exist_ok=True)
    return root


def _read_opencode_sessions() -> dict:
    try:
        data = json.loads(OPENCODE_SESSIONS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_opencode_sessions(data: dict) -> None:
    tmp = OPENCODE_SESSIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(OPENCODE_SESSIONS_PATH)


def _session_hash(session_id: str) -> str:
    import hashlib
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]


def _looks_like_design_request_for_name(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"\b(chip|soc|rtl|gds|gdsii|core|accelerator|controller|uart|spi|risc|aes|sram|fifo|noc)\b", lowered))


def _normalize_agentic_mode(value: str | None) -> str:
    return "builder" if str(value or "").strip().lower() == "builder" else "advisor"


def _advisor_write_allowed(path: str) -> bool:
    normalized = os.path.normpath(str(path or "").strip()).replace("\\", "/").lstrip("/")
    if not normalized or normalized.startswith("../") or normalized == "..":
        return False
    top = normalized.split("/", 1)[0].lower()
    ext = os.path.splitext(normalized.lower())[1]
    return top in {"docs", "reports", "diagrams"} or ext in {".md", ".markdown", ".mermaid", ".mmd"}


def _advisor_tool_guard(name: str, args: dict, mode: str) -> str | None:
    if _normalize_agentic_mode(mode) != "advisor":
        return None
    if name == "bash":
        return (
            "Error: AgentIC is in advisor mode. Advisor mode can inspect/query context and prepare docs, "
            "plans, diagrams, or reports, but it cannot run shell/EDA commands. Switch to builder mode to execute."
        )
    if name == "write" and not _advisor_write_allowed(str(args.get("path") or "")):
        return (
            "Error: AgentIC is in advisor mode. Advisor mode may only write documentation artifacts "
            "under docs/, reports/, diagrams/, or markdown/mermaid files. Switch to builder mode to edit design sources."
        )
    return None


def _slugify_design_text(text: str, fallback: str) -> str:
    text = re.sub(r"`[^`]+`", " ", text or "")
    text = re.sub(
        r"(?i)\b(attached file|approve this plan|begin execution|workspace|please|make|create|build|design|proper|planning)\b",
        " ",
        text,
    )
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)[:44].strip("_")
    if not slug:
        slug = fallback
    if slug.lower() in WORKSPACE_SECTION_DIRS or slug.startswith("."):
        slug = f"design_{fallback}"
    return slug


def _safe_design_dir_name(value: str, fallback: str = "scratch") -> str:
    name = str(value or "").strip()
    if (
        not name
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or name.lower() in WORKSPACE_SECTION_DIRS
    ):
        return fallback
    return name


def _resolve_opencode_mapping(req: OpenCodeSessionRequest) -> dict:
    session_id = (req.session_id or "").strip()
    if not session_id:
        raise HTTPException(400, "AgentIC runtime session_id is required.")
    
    workspace_root = _safe_workspace_root(req.workspace_root)
    data = _read_opencode_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    fallback = _session_hash(session_id)
    requested_design = (req.design_name or "").strip()
    if requested_design:
        design_name = _slugify_design_text(requested_design, fallback)
    elif existing.get("design_name") and not (
        str(existing.get("design_name")).startswith("session_")
        and _looks_like_design_request_for_name(req.user_text)
    ):
        design_name = str(existing["design_name"])
    else:
        if _looks_like_design_request_for_name(req.user_text):
            design_name = _slugify_design_text(req.user_text, f"design_{fallback}")
            if not design_name.startswith(("design_", "session_")) and len(design_name) < 8:
                design_name = f"design_{design_name}_{fallback[:4]}"
        else:
            design_name = f"session_{fallback}"
    design_root = os.path.join(workspace_root, design_name)
    os.makedirs(design_root, exist_ok=True)
    run_id = str(existing.get("run_id") or f"oc_{fallback}")
    now = time.time()
    mapping = {
        "schema_version": "agentic.opencode.session.v1",
        "session_id": session_id,
        "message_id": req.message_id,
        "agent": req.agent or "agentic-vlsi",
        "agentic_mode": _normalize_agentic_mode(req.agentic_mode or existing.get("agentic_mode")),
        "workspace_root": workspace_root,
        "design_name": design_name,
        "design_root": design_root,
        "run_id": run_id,
        "pdk_profile": req.pdk_profile or existing.get("pdk_profile") or "",
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "last_user_text": req.user_text or existing.get("last_user_text") or "",
    }
    data[session_id] = mapping
    _write_opencode_sessions(data)
    _set_active_design(design_name)
    return mapping


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
    session = _forward_license_json(
        "/auth/password-login",
        method="POST",
        payload={"email": email, "password": password},
        include_auth=False,
    )
    if isinstance(session, dict) and session.get("access_token"):
        _write_auth_session(session)
    return session


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
    session = _forward_license_json(
        "/auth/signup",
        method="POST",
        payload={"email": email, "password": password},
        include_auth=False,
    )
    if isinstance(session, dict) and session.get("access_token"):
        _write_auth_session(session)
    return session


@app.post("/auth/refresh")
async def auth_refresh(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    refresh_token = str(body.get("refresh_token") or "")
    if not refresh_token:
        raise HTTPException(400, "Refresh token is required.")
    session = _forward_license_json(
        "/auth/refresh",
        method="POST",
        payload={"refresh_token": refresh_token},
        include_auth=False,
    )
    if isinstance(session, dict) and session.get("access_token"):
        _write_auth_session(session)
    return session


@app.post("/auth/desktop-session")
async def auth_desktop_session(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not body.get("access_token"):
        raise HTTPException(400, "Access token is required.")
    _write_auth_session(body)
    return {"ok": True}


@app.get("/auth/desktop-session")
async def get_auth_desktop_session():
    session = _read_or_refresh_auth_session()
    if not session:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "expires_at": session.get("expires_at"),
        "user": session.get("user"),
    }


@app.get("/auth/profile")
async def auth_profile(request: Request):
    session = _read_or_refresh_auth_session()
    if not session:
        return {
            "authenticated": False,
            "license": resolve_license_status(request),
        }
    return {
        "authenticated": True,
        "expires_at": session.get("expires_at"),
        "user": session.get("user"),
        "license": resolve_license_status(request),
    }


@app.post("/auth/logout")
async def auth_logout():
    _clear_auth_session()
    return {"ok": True}


@app.get("/auth/google/start")
async def auth_google_start():
    license_base = _license_server_base()
    if not license_base:
        raise HTTPException(503, "Google sign-in is not configured.")
    return RedirectResponse(f"{license_base}/auth/google/start")


@app.get("/purchase/start")
async def purchase_start(plan: str = "pro"):
    license_base = _license_server_base()
    if not license_base:
        raise HTTPException(503, "Purchase flow is not configured.")
    normalized_plan = plan if plan in {"starter", "pro"} else "pro"
    return RedirectResponse(f"{license_base}/purchase/start?plan={normalized_plan}")


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


@app.get("/tools/adapters")
async def get_tool_adapters(force_refresh: bool = False):
    from tool_adapters import normalized_adapter_summary
    env = detect_environment(force_refresh=force_refresh)
    return {
        "status": "OK",
        "tool_adapters": normalized_adapter_summary(env.get("tools") or {}),
        "recommended_flow": env.get("recommended_flow") or {},
        "capability_graph": env.get("capability_graph") or {},
        "capability_index": env.get("capability_index") or {},
    }


@app.get("/vlsi/route")
async def get_vlsi_route(pdk: str = "", goal: str = "rtl_to_gds"):
    env = detect_environment()
    return recommend_flow(env, requested_pdk=pdk, design_goal=goal)


@app.get("/opencode/bridge/health")
async def opencode_bridge_health():
    return {
        "status": "ok",
        "service": "agentic-runtime-bridge",
        "bridge": True,
        "agent": "agentic-vlsi",
    }


def _require_active_local_runtime(request: Request) -> dict:
    _require_opencode_bridge(request)
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    return license_status


_session_modes: dict[str, str] = {}

@app.get("/opencode/session/mode/{session_id}")
async def opencode_session_mode_get(session_id: str):
    return {"success": True, "agentic_mode": _session_modes.get(session_id, "advisor")}

@app.post("/opencode/session/mode")
async def opencode_session_mode_set(request: Request):
    body = await request.json()
    session_id = body.get("session_id")
    mode = body.get("mode", "advisor")
    if session_id:
        _session_modes[session_id] = mode
    return {"success": True, "agentic_mode": mode}

@app.post("/opencode/session/resolve")
async def opencode_session_resolve(req: OpenCodeSessionRequest, request: Request):
    license_status = _require_active_local_runtime(request)
    mapping = _resolve_opencode_mapping(req)
    from agentic_handoffs import schema_catalog
    from agentic_kernel import build_context_contract, scope_for_turn
    from agentic_role_runner import RoleContext, persist_role_results, run_role_pipeline
    from agentic_validators import validation_schema_catalog
    from context_engine import build_agent_context_packet
    from design_intent import build_or_update_design_intent
    from session_workflow import classify_session_workflow
    from vlsi_capability_graph import assess_design_readiness

    messages = [{"role": "user", "content": req.user_text or ""}]
    workflow_decision = classify_session_workflow(req.user_text or "", messages)
    is_design_task = workflow_decision.requires_design_kernel or workflow_decision.intent == "DESIGN_TASK"
    kernel_scope = scope_for_turn(
        is_design_task=is_design_task,
        is_planning_round=workflow_decision.planning_round if is_design_task else False,
        execution_authorized=workflow_decision.execution_authorized,
        wants_diagram_artifact="diagram" in (req.user_text or "").lower() or "mermaid" in (req.user_text or "").lower(),
        repairs_artifact=workflow_decision.mode == "diagram_repair",
    )
    env = detect_environment()
    flow_decision = recommend_flow(env, requested_pdk=mapping.get("pdk_profile") or req.pdk_profile or "")
    state_store = DesignStateStore(mapping["design_root"], mapping["design_name"])
    state_store.set_intent(req.user_text or "", mapping.get("pdk_profile") or "")
    state_store.upsert_design_fact("opencode_session", mapping["session_id"], mapping, source="opencode_bridge")
    state_store.upsert_design_fact("session_workflow", workflow_decision.mode, workflow_decision.to_record(), source="session_workflow_router")
    state_store.set_flow_decision(flow_decision)
    kernel_contract = build_context_contract(req.user_text or "", kernel_scope).to_dict()
    state_store.set_context_contract(kernel_contract)
    role_results = []
    role_counts = {}
    design_intent = None
    if is_design_task:
        role_ctx = RoleContext(
            user_text=req.user_text or "",
            workspace_root=mapping["design_root"],
            design_name=mapping["design_name"],
            context_contract=kernel_contract,
            flow_decision=flow_decision,
            env=env,
            design_state=state_store.load(),
            needs_spec_clarification=False,
        )
        role_results = run_role_pipeline(role_ctx)
        role_counts = persist_role_results(state_store, role_results)
        design_intent = build_or_update_design_intent(
            workspace_root=mapping["design_root"],
            design_name=mapping["design_name"],
            user_text=req.user_text or "",
            flow_decision=flow_decision,
            role_results=role_results,
            env=env,
            previous=state_store.load(),
        )
        state_store.set_design_intent(design_intent.model_dump(mode="json"))
        readiness = assess_design_readiness(design_intent.model_dump(mode="json"), env.get("capability_graph") or {})
        state_store.record_evidence("design_readiness", design_intent.intent_id, readiness)
        state_store.upsert_design_fact("readiness", design_intent.intent_id, readiness, source="capability_graph")
        state_store.record_evidence("role_pipeline", kernel_scope.name, {
            "roles": [result.role for result in role_results],
            "counts": role_counts,
            "risks": [risk for result in role_results for risk in result.risks][:20],
            "intent_id": design_intent.intent_id,
            "project_root": design_intent.project_root,
        })
    context_packet = build_agent_context_packet(
        workspace_root=mapping["design_root"],
        design_name=mapping["design_name"],
        user_text=req.user_text or "",
        env=env,
        flow_decision=flow_decision,
        context_contract=kernel_contract,
    )
    return {
        "success": True,
        "license": {
            "active": bool(license_status.get("active")),
            "plan": license_status.get("plan"),
            "source": license_status.get("source"),
        },
        "session": mapping,
        "workflow": workflow_decision.to_record(),
        "kernel_scope": kernel_scope.name,
        "kernel_contract": kernel_contract,
        "schema_catalog": schema_catalog(),
        "validation_schema_catalog": validation_schema_catalog(),
        "context_packet": context_packet,
        "flow_decision": flow_decision,
        "role_summary": {
            "enabled": is_design_task,
            "roles": [result.role for result in role_results],
            "counts": role_counts,
        },
        "design_intent": design_intent.model_dump(mode="json") if design_intent else None,
    }


@app.post("/opencode/tool")
async def opencode_tool(req: OpenCodeToolRequest, request: Request):
    _require_active_local_runtime(request)
    mapping = _resolve_opencode_mapping(req)
    from agent_tools import dispatch_tool
    guarded = _advisor_tool_guard(req.name, req.args or {}, mapping.get("agentic_mode", "advisor"))
    if guarded:
        return {
            "success": False,
            "result": guarded,
            "session": mapping,
        }
    result = dispatch_tool(req.name, req.args or {}, mapping["design_root"], mapping["design_name"])
    event = {
        "run_id": mapping["run_id"],
        "type": "progress",
        "label": f"AgentIC VLSI tool `{req.name}` completed",
        "stage": "AGENTIC_RUNTIME",
        "status": "completed" if not str(result).startswith("Error:") else "failed",
        "design_name": mapping["design_name"],
        "timestamp": time.time(),
    }
    _append_run_event(event)
    return {
        "success": not str(result).startswith("Error:"),
        "result": result,
        "session": mapping,
    }


def _opencode_runtime_failure(exc: Exception) -> HTTPException:
    message = str(exc) or exc.__class__.__name__
    status = 503 if "not reachable" in message.lower() or "not running" in message.lower() else 502
    return HTTPException(status, message)


def _agentic_mode_system_prompt(mode: str) -> str:
    normalized = _normalize_agentic_mode(mode)
    common = (
        "AgentIC Studio runtime mode is "
        f"{normalized}. Keep responses concise and engineering-focused. "
        "Do not use emojis, marketing copy, or oversized markdown sections. "
        "Never expose raw runtime event names such as message.part.delta. "
        "If clarification is needed, ask the exact question in the same response; do not end with a dangling heading like 'Let me ask'."
    )
    if normalized == "builder":
        return (
            common
            + " Builder mode may create/edit design artifacts and run local tools after the user approves an implementation plan. "
            "For a new build, inspect available context, propose a specific plan, wait for approval, then implement through AgentIC tools."
        )
    return (
        common
        + " Advisor mode is non-implementation mode. You may explain, inspect, query PDK/tool context, and write docs/plans/diagrams/reports to the workspace. "
        "Do not write RTL/TB/scripts/constraints/layout sources and do not run shell or EDA commands. If implementation is needed, tell the user to switch to builder mode."
    )


def _desktop_mapping_request(
    session_id: str,
    *,
    user_text: str = "",
    workspace_root: str | None = None,
    pdk_profile: str | None = None,
    design_name: str | None = None,
    agentic_mode: str = "advisor",
    agent: str = "agentic-vlsi",
) -> OpenCodeSessionRequest:
    return OpenCodeSessionRequest(
        session_id=session_id,
        user_text=user_text,
        workspace_root=workspace_root,
        pdk_profile=pdk_profile,
        design_name=design_name,
        agentic_mode=agentic_mode,
        agent=agent,
    )


def _normalized_opencode_event(raw: dict, mapping: dict) -> dict:
    event_type = str(raw.get("type") or "opencode.event")
    properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    part = properties.get("part") if isinstance(properties.get("part"), dict) else {}
    info = properties.get("info") if isinstance(properties.get("info"), dict) else {}
    part_type = str(part.get("type") or "")
    role = str(info.get("role") or properties.get("role") or "")
    label = properties.get("title") or properties.get("message") or event_type.replace(".", " ")
    if event_type == "message.part.updated":
        if part_type == "reasoning":
            label = "Reasoning over the safest next VLSI step"
        elif part_type == "text" and role == "assistant":
            label = "Drafting the AgentIC response"
        elif part_type.startswith("tool"):
            label = "Running an AgentIC workspace tool"
    elif event_type == "message.updated" and role == "assistant":
        label = "Updating the AgentIC response"
    elif event_type == "session.status":
        label = "AgentIC runtime is working"
    status = "running"
    if event_type.endswith(".error") or event_type == "session.error":
        status = "failed"
    elif event_type in {"session.idle", "server.instance.disposed"}:
        status = "completed"
    return {
        "run_id": mapping["run_id"],
        "type": "opencode_event",
        "state": event_type,
        "stage": event_type,
        "label": label,
        "status": status,
        "design_name": mapping["design_name"],
        "timestamp": time.time(),
        "opencode": raw,
    }


_OPENCODE_LEDGER_NOISE_EVENTS = {
    "server.connected",
    "server.heartbeat",
    "session.updated",
    "session.diff",
    "session.status",
    "message.updated",
    "message.part.updated",
    "message.part.delta",
}


def _should_persist_opencode_event(event: dict) -> bool:
    state = str(event.get("stage") or event.get("state") or "")
    if state in {"session.idle", "session.error", "server.instance.disposed"}:
        return True
    return state not in _OPENCODE_LEDGER_NOISE_EVENTS


async def _opencode_event_sse(mapping: dict, replay: int = 0):
    if replay:
        for event in _read_run_events(replay, mapping["run_id"]):
            yield {"event": "message", "data": json.dumps(event)}

    import threading
    from opencode_runtime import stream_events

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[tuple[str, dict | str | None]] = asyncio.Queue()
    stop = {"value": False}

    def read_stream() -> None:
        try:
            for raw in stream_events(mapping["design_root"]):
                if stop["value"]:
                    break
                normalized = _normalized_opencode_event(raw, mapping)
                if _should_persist_opencode_event(normalized):
                    _append_run_event(normalized)
                loop.call_soon_threadsafe(queue.put_nowait, ("event", normalized))
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("closed", None))

    thread = threading.Thread(target=read_stream, daemon=True)
    thread.start()
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "event":
                yield {"event": "message", "data": json.dumps(payload)}
            elif kind == "error":
                event = {
                    "run_id": mapping["run_id"],
                    "type": "error",
                    "state": "AGENTIC_RUNTIME_EVENT_STREAM_ERROR",
                    "label": "AgentIC runtime event stream failed",
                    "message": payload,
                    "content": payload,
                    "status": "failed",
                    "design_name": mapping["design_name"],
                    "timestamp": time.time(),
                }
                _append_run_event(event)
                yield {"event": "message", "data": json.dumps(event)}
                break
            else:
                break
    finally:
        stop["value"] = True


@app.get("/opencode/runtime/status")
async def opencode_runtime_status(request: Request):
    _require_active_local_runtime(request)
    from opencode_runtime import discover_root, health
    status = health()
    root = discover_root()
    return {
        **status,
        "root": str(root) if root else None,
        "attach_mode": bool(os.environ.get("AGENTIC_OPENCODE_URL") or os.environ.get("OPENCODE_SERVER_URL")),
        "start_command_configured": bool(os.environ.get("AGENTIC_OPENCODE_COMMAND")),
    }


@app.post("/opencode/runtime/start")
async def opencode_runtime_start(req: OpenCodeRuntimeStartRequest, request: Request):
    _require_active_local_runtime(request)
    from opencode_runtime import start
    try:
        return start(hostname=req.hostname, port=req.port, timeout=req.timeout)
    except Exception as exc:
        raise _opencode_runtime_failure(exc)


@app.post("/opencode/desktop/sessions")
async def opencode_desktop_create_session(req: OpenCodeDesktopSessionRequest, request: Request):
    _require_active_local_runtime(request)
    from opencode_runtime import create_session, health
    if not health().get("healthy"):
        raise HTTPException(503, "AgentIC runtime engine is not healthy. Start the local runtime or configure AGENTIC_OPENCODE_URL.")

    provisional_id = req.session_id or f"desktop_{uuid.uuid4().hex}"
    mapping = _resolve_opencode_mapping(_desktop_mapping_request(
        provisional_id,
        user_text=req.user_text or req.title or "",
        workspace_root=req.workspace_root,
        pdk_profile=req.pdk_profile,
        design_name=req.design_name,
        agentic_mode=req.agentic_mode,
        agent=req.agent,
    ))
    try:
        session = create_session(mapping["design_root"], title=req.title or mapping["design_name"], agent=req.agent)
    except Exception as exc:
        raise _opencode_runtime_failure(exc)

    canonical = _resolve_opencode_mapping(_desktop_mapping_request(
        str(session["id"]),
        user_text=req.user_text or req.title or "",
        workspace_root=mapping["workspace_root"],
        pdk_profile=mapping.get("pdk_profile") or req.pdk_profile,
        design_name=mapping["design_name"],
        agentic_mode=mapping.get("agentic_mode", req.agentic_mode),
        agent=req.agent,
    ))
    event = {
        "run_id": canonical["run_id"],
        "type": "progress",
        "state": "AGENTIC_RUNTIME_SESSION_READY",
        "label": "AgentIC VLSI session ready",
        "status": "completed",
        "design_name": canonical["design_name"],
        "timestamp": time.time(),
    }
    _append_run_event(event)
    return {"success": True, "session": session, "mapping": canonical, "event": event}


@app.post("/opencode/desktop/message")
async def opencode_desktop_message(req: OpenCodeDesktopMessageRequest, request: Request):
    _require_active_local_runtime(request)
    from opencode_runtime import prompt_async
    if not req.text.strip():
        raise HTTPException(400, "Message text is required.")
    mapping = _resolve_opencode_mapping(_desktop_mapping_request(
        req.session_id,
        user_text=req.text,
        workspace_root=req.workspace_root,
        pdk_profile=req.pdk_profile,
        design_name=req.design_name,
        agentic_mode=req.agentic_mode,
        agent=req.agent,
    ))
    try:
        system_prompt = _agentic_mode_system_prompt(mapping.get("agentic_mode", req.agentic_mode))
        if req.system:
            system_prompt = f"{system_prompt}\n\nAdditional caller system instruction:\n{req.system}"
        prompt_async(
            req.session_id,
            mapping["design_root"],
            req.text,
            agent=req.agent,
            model=req.model,
            system=system_prompt,
            variant=req.variant,
            message_id=req.message_id,
        )
    except Exception as exc:
        raise _opencode_runtime_failure(exc)
    event = {
        "run_id": mapping["run_id"],
        "type": "progress",
        "state": "AGENTIC_RUNTIME_PROMPT_ACCEPTED",
        "label": "AgentIC runtime accepted the VLSI prompt",
        "status": "running",
        "design_name": mapping["design_name"],
        "timestamp": time.time(),
    }
    _append_run_event(event)
    return {"success": True, "accepted": True, "mapping": mapping, "event": event}


@app.get("/opencode/desktop/sessions/{session_id}/messages")
async def opencode_desktop_messages(session_id: str, request: Request, workspace_root: str = "", design_name: str = "", pdk_profile: str = "", limit: int = 200):
    _require_active_local_runtime(request)
    from opencode_runtime import list_messages
    mapping = _resolve_opencode_mapping(_desktop_mapping_request(
        session_id,
        workspace_root=workspace_root or None,
        pdk_profile=pdk_profile or None,
        design_name=design_name or None,
    ))
    try:
        messages = list_messages(session_id, mapping["design_root"], limit=min(max(limit, 1), 500))
    except Exception as exc:
        raise _opencode_runtime_failure(exc)
    return {"success": True, "mapping": mapping, "messages": messages}


@app.post("/opencode/desktop/sessions/{session_id}/abort")
async def opencode_desktop_abort(session_id: str, request: Request, workspace_root: str = "", design_name: str = "", pdk_profile: str = ""):
    _require_active_local_runtime(request)
    from opencode_runtime import abort_session
    mapping = _resolve_opencode_mapping(_desktop_mapping_request(
        session_id,
        workspace_root=workspace_root or None,
        pdk_profile=pdk_profile or None,
        design_name=design_name or None,
    ))
    try:
        aborted = abort_session(session_id, mapping["design_root"])
    except Exception as exc:
        raise _opencode_runtime_failure(exc)
    event = {
        "run_id": mapping["run_id"],
        "type": "cancelled",
        "state": "AGENTIC_RUNTIME_ABORTED",
        "label": "AgentIC run stopped",
        "status": "cancelled",
        "design_name": mapping["design_name"],
        "timestamp": time.time(),
    }
    _append_run_event(event)
    return {"success": True, "aborted": aborted, "mapping": mapping, "event": event}


@app.get("/opencode/desktop/events")
async def opencode_desktop_events(
    request: Request,
    session_id: str,
    workspace_root: str = "",
    design_name: str = "",
    pdk_profile: str = "",
    replay: int = 0,
):
    _require_active_local_runtime(request)
    mapping = _resolve_opencode_mapping(_desktop_mapping_request(
        session_id,
        workspace_root=workspace_root or None,
        pdk_profile=pdk_profile or None,
        design_name=design_name or None,
    ))
    return EventSourceResponse(_opencode_event_sse(mapping, replay=min(max(replay, 0), 500)))


@app.post("/opencode/git/clone")
async def opencode_git_clone(req: OpenCodeToolRequest):
    """Clone a GitHub repo into the design workspace. The agent's git_clone tool also calls this."""
    mapping = _resolve_opencode_mapping(req)
    from agent_tools import git_clone
    result = git_clone(
        url=(req.args or {}).get("url", ""),
        workspace_root=mapping["design_root"],
        target_dir=(req.args or {}).get("target_dir"),
        branch=(req.args or {}).get("branch", "main"),
        token=(req.args or {}).get("token", ""),
    )
    return {"success": not result.startswith("Error:"), "result": result, "session": mapping}


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


@app.post("/debug/log")
async def debug_log(data: dict):
    try:
        import json
        with open("/home/vickynishad/AgentIC-workspace/debug.log", "a") as f:
            f.write(json.dumps(data) + "\n")
    except Exception as e:
        print("Error writing debug log:", e)
    return {"ok": True}


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


@app.get("/build/checkpoints/{design_name}")
async def get_checkpoints(request: Request, design_name: str):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    from checkpoint_engine import CheckpointEngine
    design_root = os.path.join(WS_ROOT, design_name) if design_name else WS_ROOT
    engine = CheckpointEngine(design_name, design_root)
    report = engine.signoff_report()
    try:
        report["design_state"] = DesignStateStore(design_root, design_name).summary()
    except Exception:
        pass
    return report


@app.get("/build/state/{design_name}")
async def get_design_state(request: Request, design_name: str):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    design_root = os.path.join(WS_ROOT, design_name) if design_name else WS_ROOT
    return DesignStateStore(design_root, design_name).load()


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
    if run_id in ACTIVE_RUNS:
        logging.info("Run %s is already active; attaching to existing run event stream", run_id)
        return EventSourceResponse(_attach_existing_run_events(run_id))
    ACTIVE_RUNS[run_id] = time.time()
    CANCELLED_RUNS.discard(run_id)

    async def event_generator():
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        LLM_TIMEOUT = 300

        def _push_event(event_dict: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ("event", event_dict))

        def _generate_title_sync():
            try:
                import re
                from openai import OpenAI, AzureOpenAI
                if "azure.com" in base_url.lower():
                    match = re.match(r"(https://[^.]+\.openai\.azure\.com)", base_url)
                    azure_endpoint = match.group(1) if match else base_url
                    api_version = "2024-02-15-preview"
                    ver_match = re.search(r"api-version=([\d-]+)", base_url)
                    if ver_match:
                        api_version = ver_match.group(1)
                    client = AzureOpenAI(api_key=api_key, api_version=api_version, azure_endpoint=azure_endpoint, timeout=30)
                else:
                    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30)

                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "Generate a concise 2-4 word title for this VLSI/chip design chat. Output ONLY the title text without quotes."},
                        {"role": "user", "content": req.messages[0].get("content", "")}
                    ],
                    max_tokens=10,
                    temperature=0.3
                )
                title = response.choices[0].message.content.strip().replace('"', '').replace("'", "")
                _push_event({"type": "title", "title": title})
            except Exception as e:
                import logging
                logging.getLogger("agentic.title").warning(f"Title generation failed: {e}")

        if len(req.messages) == 1:
            loop.run_in_executor(None, _generate_title_sync)

        def run_sync_gen():
            """Run the synchronous generator and push events to the queue."""
            try:
                design_name = req.design_name or "scratch"
                design_dir = os.path.join(WS_ROOT, design_name) if design_name else WS_ROOT
                if design_name:
                    os.makedirs(design_dir, exist_ok=True)
                def is_run_cancelled():
                    if SHUTDOWN_REQUESTED:
                        logging.info("is_run_cancelled check: SHUTDOWN_REQUESTED is True, cancelling run %s", run_id)
                        return True
                    cancelled = run_id in CANCELLED_RUNS
                    if cancelled:
                        logging.info("is_run_cancelled check: run_id %s is CANCELLED", run_id)
                    return cancelled
                for event in converse_stream(
                    messages=req.messages,
                    api_key=api_key,
                    workspace_root=design_dir,
                    design_name=design_name,
                    base_url=base_url,
                    model=model,
                    event_pusher=_push_event,
                    is_cancelled=is_run_cancelled,
                    pdk_profile=req.pdk_profile,
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
                    error_message = str(payload or "The local run hit an issue").strip()
                    error_event = {
                        "run_id": run_id,
                        "type": "error",
                        "state": "ERROR",
                        "label": "The local run hit an issue",
                        "message": error_message,
                        "content": error_message,
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
                    "state": state or ("THINKING" if event_type in {"reasoning", "thought"} else "done"),
                    "message": content,
                    "content": content,
                    "label": event.get("label"),
                    "stage": event.get("stage"),
                    "status": event.get("status"),
                    "design_name": event.get("design_name"),
                    "timestamp": time.time(),
                }
                for key in ("options", "artifacts", "project_root", "artifact_path"):
                    if key in event:
                        sse_data[key] = event.get(key)
                if sse_data.get("design_name"):
                    _set_active_design(sse_data["design_name"])
                if event_type in {"thought", "progress", "needs_input", "response", "error", "stream_end", "cancelled"}:
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
            ACTIVE_RUNS.pop(run_id, None)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    return EventSourceResponse(event_generator())


async def _attach_existing_run_events(run_id: str):
    seen: set[str] = set()
    terminal = {"stream_end", "error", "cancelled"}
    attached_event = {
        "run_id": run_id,
        "type": "progress",
        "state": "ATTACHED",
        "message": "Reconnected to the active local run.",
        "content": "Reconnected to the active local run.",
        "label": "Reconnected to active run",
        "stage": "RUN",
        "status": "running",
        "timestamp": time.time(),
    }
    yield {"event": "message", "data": json.dumps(attached_event)}
    started = time.time()
    while run_id in ACTIVE_RUNS and time.time() - started < 900:
        for event in _read_run_events(limit=200, run_id=run_id):
            key = f"{event.get('timestamp')}:{event.get('type')}:{event.get('label')}:{event.get('stage')}"
            if key in seen:
                continue
            seen.add(key)
            yield {"event": "message", "data": json.dumps(event)}
            if event.get("type") in terminal:
                return
        await asyncio.sleep(1.0)
    if run_id not in ACTIVE_RUNS:
        done_event = {
            "run_id": run_id,
            "type": "stream_end",
            "state": "done",
            "label": "Run complete",
            "status": "completed",
            "timestamp": time.time(),
        }
        yield {"event": "message", "data": json.dumps(done_event)}


@app.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", run_id or ""):
        raise HTTPException(400, "Invalid run id")
    logging.info("Adding run_id %s to CANCELLED_RUNS set", run_id)
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

@app.get("/build/sta/{design_name}")
async def get_sta_report(request: Request, design_name: str, workspace_root: str = ""):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    from sta_reports import build_sta_report
    root = _safe_workspace_root(workspace_root)
    safe_name = _safe_design_dir_name(design_name, "scratch")
    design_root = os.path.join(root, safe_name) if safe_name else root
    return build_sta_report(design_root, safe_name)


@app.get("/build/sta/session/{session_id}")
async def get_session_sta_report(request: Request, session_id: str, workspace_root: str = ""):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    from sta_reports import build_sta_report

    data = _read_opencode_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    root = _safe_workspace_root(str(existing.get("workspace_root") or workspace_root or WS_ROOT))
    fallback = f"session_{_session_hash(session_id)}"
    design_name = _safe_design_dir_name(str(existing.get("design_name") or fallback), fallback)
    design_root = str(existing.get("design_root") or os.path.join(root, design_name))
    return build_sta_report(design_root, design_name)


@app.get("/build/signoff/{design_name}")
async def get_signoff_report(request: Request, design_name: str, workspace_root: str = ""):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    from signoff_reports import build_signoff_report

    root = _safe_workspace_root(workspace_root)
    safe_name = _safe_design_dir_name(design_name, "scratch")
    design_root = os.path.join(root, safe_name) if safe_name else root
    return build_signoff_report(design_root, safe_name)


@app.get("/build/signoff/session/{session_id}")
async def get_session_signoff_report(request: Request, session_id: str, workspace_root: str = ""):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    from signoff_reports import build_signoff_report

    data = _read_opencode_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    root = _safe_workspace_root(str(existing.get("workspace_root") or workspace_root or WS_ROOT))
    fallback = f"session_{_session_hash(session_id)}"
    design_name = _safe_design_dir_name(str(existing.get("design_name") or fallback), fallback)
    design_root = str(existing.get("design_root") or os.path.join(root, design_name))
    return build_signoff_report(design_root, design_name)


@app.get("/simulation/waveforms/{design_name}")
async def get_waveforms(request: Request, design_name: str):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")

    # In production this handles VCD parsing, fsdb2vcd, or shm2vcd for proprietary tools
    return {
        "design_name": design_name,
        "status": "ready",
        "format": "vcd",
        "signals": ["clk", "data_in", "data_out"],
        "transitions": 10000
    }

@app.get("/api/v2/providers")
@app.get("/v2/providers")
@app.get("/providers")
async def get_providers():
    return {
        "all": [
            {
                "id": "opencode",
                "name": "AgentIC",
                "models": {
                    "agentic-model": {
                        "id": "agentic-model",
                        "name": "AgentIC Model",
                        "status": "active",
                        "cost": {"input": 1, "output": 1}
                    }
                }
            }
        ],
        "connected": ["opencode"]
    }

@app.get("/api/v2/agents")
@app.get("/v2/agents")
@app.get("/agents")
async def get_agents():
    return []

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    port = int(os.environ.get("AGENTIC_PORT") or os.environ.get("PORT") or "7860")
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=True)
