import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from models import ChatRequest, ToolInstallPlanRequest, ToolInstallRequest, UsageBuildRequest
from workspace import list_artifacts, list_designs, read_artifact, ensure_workspace
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

WS_ROOT = os.environ.get("AGENTIC_WORKSPACE") or os.path.expanduser("~/AgentIC-workspace")
ensure_workspace(WS_ROOT)
STATE_DIR = Path(WS_ROOT) / ".agentic"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ENTITLEMENT_PATH = STATE_DIR / "entitlement.json"
USAGE_LOG_PATH = STATE_DIR / "usage.jsonl"
RUN_EVENTS_PATH = STATE_DIR / "run_events.jsonl"
ACTIVE_DESIGN_PATH = STATE_DIR / "active_design.json"

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


def _entitlement_verify_key() -> tuple[str, list[str]] | None:
    public_key = os.environ.get("AGENTIC_ENTITLEMENT_PUBLIC_KEY", "").strip()
    if public_key:
        return public_key.replace("\\n", "\n"), ["RS256"]

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
    except jwt.PyJWTError:
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
            return {
                "active": False,
                "plan": "unlicensed",
                "checked_at": time.time(),
                "source": "cloud_error",
                "reason": _safe_license_failure_reason(exc.code, body),
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            cached = _read_cached_entitlement()
            if cached:
                cached["reason"] = "Using cached entitlement because license cloud is temporarily unavailable."
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
    safe = {key: value for key, value in safe.items() if value not in (None, "")}
    with RUN_EVENTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(safe) + "\n")


def _set_active_design(design_name: str | None) -> None:
    if not design_name or design_name.startswith(".") or "/" in design_name or "\\" in design_name:
        return
    ACTIVE_DESIGN_PATH.write_text(json.dumps({
        "name": design_name,
        "updated_at": time.time(),
    }), encoding="utf-8")


def _get_active_design() -> dict | None:
    try:
        active = json.loads(ACTIVE_DESIGN_PATH.read_text(encoding="utf-8"))
        if active.get("name"):
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


@app.post("/profile/byok/test")
async def test_byok_connection():
    # Local mode: accept any key format, let the agent runtime handle errors
    return {"status": "ok", "message": "Local mode BYOK test skipped — agent will validate on first use"}


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
    run_id = uuid.uuid4().hex

    async def event_generator():
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        LLM_TIMEOUT = 300

        def run_sync_gen():
            """Run the synchronous generator and push events to the queue."""
            try:
                for event in converse_stream(
                    messages=req.messages,
                    api_key=api_key,
                    workspace_root=WS_ROOT,
                    base_url=base_url,
                    model=model,
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
                if event_type in {"progress", "needs_input", "response", "error", "stream_end"}:
                    _append_run_event(sse_data)

                yield {"event": "message", "data": json.dumps(sse_data)}

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


@app.post("/lab/syntax-check")
@app.post("/lab/synthesize")
@app.post("/lab/simulate")
@app.post("/lab/generate-testbench")
@app.post("/lab/ai-assist")
@app.post("/lab/gtkwave")
async def lab_endpoint_generic():
    return {
        "status": "ok",
        "message": "Lab endpoint available in local mode. Use the Design Studio for agent-driven flows.",
    }


@app.get("/workspace/active")
async def get_active_workspace(request: Request):
    license_status = resolve_license_status(request)
    if not license_status.get("active"):
        raise HTTPException(402, license_status.get("reason") or "Active license required")
    return {"active": _get_active_design()}

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=7860)
