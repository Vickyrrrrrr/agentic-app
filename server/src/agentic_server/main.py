import asyncio
import json
import logging
import os
import platform
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import threading as _threading


from pydantic import BaseModel
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from openai import AzureOpenAI, OpenAI
from sse_starlette.sse import EventSourceResponse

from agentic_server.models import (
    ChatRequest,
    OpenCodeDesktopMessageRequest,
    OpenCodeDesktopSessionRequest,
    OpenCodeRuntimeStartRequest,
    OpenCodeSessionRequest,
    OpenCodeToolRequest,
    ToolInstallPlanRequest,
    ToolInstallRequest,
)
from agentic_server.workspace import list_artifacts, list_designs, read_artifact, read_workspace_artifact, ensure_workspace
from agentic_server import config
from agentic_server import run_ledger
from agentic_server.chat_agent import converse_stream
from agentic_server.flow_runtime import recommend_flow
from agentic_server.local_tools import detect_environment, install_command_for, run_bash
from agentic_server.vlsi_state import DesignStateStore
from agentic_server.ipyt_harness import get_rlm_harness

SHUTDOWN_REQUESTED = False

logger = logging.getLogger("agentic.engine")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _job_queue, SHUTDOWN_REQUESTED
    _job_queue = get_job_queue()
    recovery = _job_queue.recover()
    if recovery.get('orphaned', 0) > 0:
        logging.info(f"Recovered {recovery['orphaned']} orphaned jobs from previous session")
    yield
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


@app.get("/version")
async def version() -> dict[str, str]:
    """Engine + protocol identity for UI compatibility checks."""
    return {"service": "agentic-local", "protocol": "v1", "mode": "local"}


# RLM harness endpoints (used by the desktop IPython console).

@app.post("/opencode/rlm/execute")
async def rlm_execute_code(request: Request):
    """Execute Python / IPython cell within persistent REPL kernel."""
    data = await request.json()
    code = data.get("code", "")
    harness = get_rlm_harness()
    result = await harness.execute_code(code)
    return result


@app.post("/opencode/rlm/subagent")
async def rlm_trigger_subagent(request: Request):
    """Trigger recursive sub-agent call (`await rlm(...)`)."""
    data = await request.json()
    prompt = data.get("prompt", "")
    scope_vars = data.get("scope_vars", {})
    harness = get_rlm_harness()
    result = await harness.spawn_rlm_subagent(prompt, scope_vars)
    return result


@app.post("/opencode/rlm/refine")
async def rlm_refine_harness():
    """Run self-refinement trajectory analysis (/refine)."""
    harness = get_rlm_harness()
    result = await harness.refine_memory()
    return result


@app.get("/opencode/rlm/state")
async def rlm_get_state():
    """Get active IPython kernel variables and execution trajectories."""
    harness = get_rlm_harness()
    state = await harness.get_state()
    return state


# Fast-path session cache: session_id -> (timestamp, payload), 30s TTL.
# ─────────────────────────────────────────────────────────────────────────────

_FAST_CACHE_LOCK = _threading.Lock()
_FAST_CACHE: dict[str, tuple[float, dict]] = {}
_FAST_CACHE_TTL = 30.0  # seconds


def _fast_cache_get(session_id: str) -> dict | None:
    with _FAST_CACHE_LOCK:
        entry = _FAST_CACHE.get(session_id)
    if entry and (time.time() - entry[0]) < _FAST_CACHE_TTL:
        return entry[1]
    return None


def _fast_cache_set(session_id: str, payload: dict) -> None:
    with _FAST_CACHE_LOCK:
        _FAST_CACHE[session_id] = (time.time(), payload)


def _fast_cache_invalidate(session_id: str) -> None:
    with _FAST_CACHE_LOCK:
        _FAST_CACHE.pop(session_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# Kernel Role API — on-demand, scope always re-derived from session state
# ─────────────────────────────────────────────────────────────────────────────

_KERNEL_VALID_ROLES = frozenset({
    "spec_architect", "flow_planner", "rtl_author",
    "verification_engineer", "debug_engineer", "signoff_critic",
})


def _kernel_role_context(req, mapping: dict) -> tuple:
    """Build a fully-resolved RoleContext. Scope is derived from session state,
    never from caller input. Returns (kernel_scope, flow_decision, env, ctx)."""
    from agentic_server.session_workflow import classify_session_workflow
    from agentic_server.agentic_kernel import build_context_contract, scope_for_turn

    messages = [{"role": "user", "content": req.user_text or ""}]
    workflow = classify_session_workflow(req.user_text or "", messages)
    is_design = workflow.requires_design_kernel or workflow.intent == "DESIGN_TASK"
    kernel_scope = scope_for_turn(
        is_design_task=is_design,
        is_planning_round=workflow.planning_round if is_design else False,
        execution_authorized=workflow.execution_authorized,
        wants_diagram_artifact=False,
        repairs_artifact=False,
    )
    env = detect_environment()
    flow_decision = recommend_flow(env, requested_pdk=mapping.get("pdk_profile") or req.pdk_profile or "")
    state_store = DesignStateStore(mapping["design_root"], mapping["design_name"])
    kernel_contract = build_context_contract(req.user_text or "", kernel_scope).to_dict()

    from agentic_server.agentic_role_runner import RoleContext
    ctx = RoleContext(
        user_text=req.user_text or "",
        workspace_root=mapping["design_root"],
        design_name=mapping["design_name"],
        context_contract=kernel_contract,
        flow_decision=flow_decision,
        env=env,
        design_state=state_store.load(),
        needs_spec_clarification=False,
    )
    return kernel_scope, flow_decision, env, ctx, state_store, kernel_contract


@app.post("/opencode/kernel/role/{role}")
async def kernel_role_execute(role: str, req: OpenCodeSessionRequest, request: Request):
    """Execute one kernel role on-demand. Scope is always re-derived — never trusted from the caller.

    Valid roles: spec_architect | flow_planner | rtl_author |
                 verification_engineer | debug_engineer | signoff_critic

    Returns the typed HandoffEnvelope payload(s) + validation result for this role.
    Persists facts and evidence into the session design state so subsequent calls
    can build on the output.
    """
    _require_active_local_runtime(request)
    if role not in _KERNEL_VALID_ROLES:
        raise HTTPException(400, f"Unknown role '{role}'. Valid: {sorted(_KERNEL_VALID_ROLES)}")

    mapping = _resolve_opencode_mapping(req)
    # Invalidate fast cache when explicit kernel work is requested
    _fast_cache_invalidate(mapping["session_id"])

    kernel_scope, flow_decision, env, ctx, state_store, kernel_contract = _kernel_role_context(req, mapping)

    from agentic_server.agentic_role_runner import run_single_role, persist_role_results
    result = run_single_role(role, ctx)
    persist_role_results(state_store, [result])

    envelopes_out = [
        {
            "kind": e.kind,
            "source_role": e.source_role,
            "target_role": e.target_role,
            "payload": e.payload,
            "evidence_refs": e.evidence_refs,
            "open_risks": e.open_risks,
        }
        for e in result.envelopes
    ]
    return {
        "success": True,
        "role": role,
        "scope": kernel_scope.name,
        "envelopes": envelopes_out,
        "facts": result.facts,
        "risks": result.risks,
        "validation_count": len(result.validation),
        "accepted_count": len(envelopes_out),
    }


@app.post("/opencode/kernel/dispatch")
async def kernel_dispatch(req: OpenCodeSessionRequest, request: Request):
    """Run the full role-chain pipeline for the current session.

    Scope is always re-derived from session workflow classification —
    the caller cannot escalate scope. Returns compact role outputs only
    (no environment dumps) to keep response size token-efficient.
    """
    _require_active_local_runtime(request)
    mapping = _resolve_opencode_mapping(req)
    _fast_cache_invalidate(mapping["session_id"])

    kernel_scope, flow_decision, env, ctx, state_store, kernel_contract = _kernel_role_context(req, mapping)

    from agentic_server.agentic_role_runner import run_role_pipeline, persist_role_results
    from agentic_server.agentic_handoffs import schema_catalog

    role_results = run_role_pipeline(ctx)
    role_counts = persist_role_results(state_store, role_results)

    return {
        "success": True,
        "scope": kernel_scope.name,
        "role_counts": role_counts,
        "pipeline": [
            {
                "role": r.role,
                "envelopes": [
                    {"kind": e.kind, "payload": e.payload, "open_risks": e.open_risks}
                    for e in r.envelopes
                ],
                "facts_count": len(r.facts),
                "risks": r.risks[:6],
            }
            for r in role_results
        ],
        "schema_catalog": schema_catalog(),
    }


@app.get("/opencode/kernel/schema")
async def kernel_schema():
    """Static typed contract catalog — cached forever, never changes at runtime.

    Returns: typed handoff schemas, validation schemas, pipeline definitions,
    role dependency graph. The LLM calls this once to understand what payload
    shapes to expect from agentic_kernel_role / agentic_kernel_dispatch.
    """
    from agentic_server.agentic_handoffs import schema_catalog
    from agentic_server.agentic_role_runner import PIPELINES, ROLE_DEPENDENCIES
    from agentic_server.agentic_validators import validation_schema_catalog
    return {
        "schema_catalog": schema_catalog(),
        "validation_schema_catalog": validation_schema_catalog(),
        "pipelines": {name: list(roles) for name, roles in PIPELINES.items()},
        "role_dependencies": {role: list(deps) for role, deps in ROLE_DEPENDENCIES.items()},
        "valid_roles": sorted(_KERNEL_VALID_ROLES),
    }


WS_ROOT = config.workspace_root()
ensure_workspace(WS_ROOT)
STATE_DIR = Path(WS_ROOT) / ".agentic"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# Build channel baked into the frozen bundle by server/scripts/freeze_backend.py.
# In the shipped prod binary this is "prod"; in dev (run.sh) the module is absent -> "dev".
try:
    from agentic_server._build_channel import CHANNEL as _BUILD_CHANNEL
except Exception:
    _BUILD_CHANNEL = "dev"

USAGE_LOG_PATH = STATE_DIR / "usage.jsonl"
RUN_EVENTS_PATH = STATE_DIR / "run_events.jsonl"
ACTIVE_DESIGN_PATH = STATE_DIR / "active_design.json"
OPENCODE_SESSIONS_PATH = STATE_DIR / "opencode_sessions.json"
AGENTIC_SESSIONS_PATH = STATE_DIR / "agentic_sessions.json"
CANCELLED_RUNS: set[str] = set()
ACTIVE_RUNS: dict[str, float] = {}

from agentic_server.job_queue import get_job_queue, JobQueue

_job_queue: JobQueue = None  # Initialized in lifespan
WORKSPACE_SECTION_DIRS = {
    "docs", "rtl", "tb", "dv", "sim", "synth", "pnr", "sta", "reports",
    "constraints", "formal", "layout", "logs", "scripts", "hardening",
    "signoff", "openlane", "openroad", "runs",
}

def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _rotate_jsonl(path: Path, max_lines: int = 10_000) -> None:
    """Trim a JSONL file to the last `max_lines` lines to prevent unbounded growth."""
    try:
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) >= max_lines:
            path.write_text("".join(lines[-max_lines:]), encoding="utf-8")
    except Exception:
        pass


def resolve_license_status(request: Request | None = None) -> dict:
    """Local-only stub: always active. Kept so existing call sites work."""
    return {
        "active": True,
        "plan": "local",
        "source": "local",
        "checked_at": time.time(),
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
    try:
        run_ledger.append_event(run_ledger.db_path_for(STATE_DIR), safe)
    except Exception:
        logging.getLogger("agentic.ledger").warning(
            "sqlite ledger write failed, using JSONL fallback", exc_info=True
        )
        _rotate_jsonl(RUN_EVENTS_PATH, max_lines=10_000)
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
        parts = [part for part in normalized.split("/") if part]
        if len(parts) >= 3:
            return "/" + "/".join(parts[2:])
    if normalized.startswith("//wsl$/"):
        parts = [part for part in normalized.split("/") if part]
        if len(parts) >= 3:
            return "/" + "/".join(parts[2:])
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        return f"/mnt/{drive}" + normalized[2:]
    return path

def _safe_workspace_root(candidate: str | None) -> str:
    if platform.system().lower() != "windows":
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


def _linux_workspace_root(host_workspace_root: str) -> str | None:
    linux_root = _wsl_path_to_linux(host_workspace_root)
    return linux_root if linux_root and linux_root != host_workspace_root else None


def _read_agentic_sessions() -> dict:
    try:
        if AGENTIC_SESSIONS_PATH.exists():
            data = json.loads(AGENTIC_SESSIONS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        if OPENCODE_SESSIONS_PATH.exists():
            data = json.loads(OPENCODE_SESSIONS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _write_agentic_sessions(data: dict) -> None:
    tmp = AGENTIC_SESSIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(AGENTIC_SESSIONS_PATH)


def _session_hash(session_id: str) -> str:
    import hashlib
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]


def _looks_like_design_request_for_name(text: str) -> bool:
    lowered = (text or "").lower()
    return bool(re.search(r"\b(chip|soc|rtl|gds|gdsii|core|accelerator|controller|uart|spi|risc|aes|sram|fifo|noc)\b", lowered))


def _normalize_agentic_mode(value: str | None) -> str:
    return "builder" if str(value or "").strip().lower() == "builder" else "advisor"


def _stored_agentic_mode(session_id: str) -> str:
    if session_id in _session_modes:
        return _normalize_agentic_mode(_session_modes.get(session_id))
    data = _read_agentic_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    if existing.get("agentic_mode"):
        return _normalize_agentic_mode(existing.get("agentic_mode"))
    if any(m == "builder" for m in _session_modes.values()):
        return "builder"
    return "advisor"


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


def _session_directory_as_design_root(workspace_root: str) -> tuple[str, str, str] | None:
    root = os.path.abspath(os.path.normpath(workspace_root))
    if root == os.path.abspath(os.path.normpath(WS_ROOT)):
        return None
    design_name = _safe_design_dir_name(os.path.basename(root), "")
    if not design_name:
        return None
    parent = os.path.dirname(root) or root
    return parent, design_name, root


def _resolve_opencode_mapping(req: OpenCodeSessionRequest) -> dict:
    session_id = (req.session_id or "").strip()
    if not session_id:
        raise HTTPException(400, "AgentIC runtime session_id is required.")
    
    workspace_root = _safe_workspace_root(req.workspace_root)
    data = _read_agentic_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    fallback = _session_hash(session_id)
    requested_design = (req.design_name or "").strip()
    design_root = None
    session_root = _session_directory_as_design_root(workspace_root)
    if requested_design:
        design_name = _slugify_design_text(requested_design, fallback)
    elif session_root:
        workspace_root, design_name, design_root = session_root
    elif existing.get("design_name"):
        design_name = str(existing["design_name"])
    else:
        if _looks_like_design_request_for_name(req.user_text):
            design_name = _slugify_design_text(req.user_text, f"design_{fallback}")
            if not design_name.startswith(("design_", "session_")) and len(design_name) < 8:
                design_name = f"design_{design_name}_{fallback[:4]}"
        else:
            design_name = f"session_{fallback}"
    if design_root is None:
        if design_name == os.path.basename(os.path.abspath(os.path.normpath(workspace_root))):
            design_root = workspace_root
        else:
            design_root = os.path.join(workspace_root, design_name)
    linux_workspace_root = _linux_workspace_root(workspace_root)
    if design_root == workspace_root:
        linux_design_root = linux_workspace_root
    else:
        linux_design_root = os.path.join(linux_workspace_root, design_name) if linux_workspace_root else None
    os.makedirs(design_root, exist_ok=True)
    run_id = str(existing.get("run_id") or f"oc_{fallback}")
    now = time.time()
    agentic_mode = _normalize_agentic_mode(_session_modes.get(session_id) or _global_agentic_mode or existing.get("agentic_mode") or req.agentic_mode)
    mapping = {
        "schema_version": "agentic.opencode.session.v1",
        "session_id": session_id,
        "message_id": req.message_id,
        "agent": req.agent or "agentic-vlsi",
        "agentic_mode": agentic_mode,
        "workspace_root": workspace_root,
        "design_name": design_name,
        "design_root": design_root,
        "linux_workspace_root": linux_workspace_root,
        "linux_design_root": linux_design_root,
        "run_id": run_id,
        "pdk_profile": req.pdk_profile or existing.get("pdk_profile") or "",
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "last_user_text": (req.user_text or existing.get("last_user_text") or "")[:500],
    }
    _session_modes[session_id] = agentic_mode
    data[session_id] = mapping
    _write_agentic_sessions(data)
    _set_active_design(design_name)
    return mapping


def _read_run_events(limit: int = 200, run_id: str | None = None) -> list[dict]:
    try:
        return run_ledger.read_events(
            run_ledger.db_path_for(STATE_DIR), limit=limit, run_id=run_id
        )
    except Exception:
        logging.getLogger("agentic.ledger").warning(
            "sqlite ledger read failed, using JSONL fallback", exc_info=True
        )
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


def _fast_opencode_session_response(req: OpenCodeSessionRequest, mapping: dict) -> dict:
    """Minimal resolve packet for first-token latency.

    The OpenCode system hook runs before the model request, so heavy environment
    scans, role pipelines, repo walks, and schema catalogs directly delay the
    user's first token. This packet gives the agent the session contract and
    tells it to call AgentIC tools when it needs exact VLSI evidence.
    """
    user_text = req.user_text or ""
    mode = "design_task" if _looks_like_design_request_for_name(user_text) else "chat"
    workflow = {
        "mode": mode,
        "intent": "DESIGN_TASK" if mode == "design_task" else "GENERAL",
        "requires_design_kernel": mode == "design_task",
        "execution_authorized": mapping.get("agentic_mode") == "builder",
        "planning_round": mode == "design_task",
        "fast": True,
    }
    kernel_scope = "FAST_CONTEXT"
    kernel_contract = {
        "schema_version": "agentic.kernel_contract.fast.v1",
        "scope": kernel_scope,
        "active_roles": ["supervisor"],
        "fast_context": True,
        "retrieval_required_for_claims": True,
    }
    context_packet = {
        "schema_version": "agentic.context_packet.fast.v1",
        "budget": {
            "policy": "first_token_fast_path",
            "actual_chars": 0,
            "truncated": True,
        },
        "tier0_session": {
            "session_id": mapping.get("session_id"),
            "design_name": mapping.get("design_name"),
            "design_root": mapping.get("design_root"),
            "linux_design_root": mapping.get("linux_design_root"),
            "agentic_mode": mapping.get("agentic_mode"),
            "active_role": "supervisor",
        },
        "retrieval_index": {
            "tools": {
                "context": "agentic_context for full AgentIC context",
                "design_state": "agentic_design_state for durable chip facts",
                "capability": "agentic_eda_capability(scope='agent_context') for tool/PDK evidence",
                "files": "native read/grep/glob for exact files",
                "pdk": "agentic_query_pdk for exact PDK/macro facts",
                "eda": "agentic_run_flow for checkpointed EDA execution",
            }
        },
        "context_policy": {
            "principle": "Fast context intentionally omits repo maps, schema catalogs, role handoffs, and environment scans to avoid first-token latency.",
            "omission_rule": "Before making PDK, timing, signoff, macro, or tool-availability claims, call the AgentIC tools listed in retrieval_index.",
        },
    }
    context_packet["budget"]["actual_chars"] = len(json.dumps(context_packet, default=str))
    return {
        "success": True,
        "fast": True,
        "license": {
            "active": True,
            "plan": "local",
            "source": "local",
        },
        "session": mapping,
        "workflow": workflow,
        "kernel_scope": kernel_scope,
        "kernel_contract": kernel_contract,
        "schema_catalog": {},
        "validation_schema_catalog": {},
        "context_packet": context_packet,
        "flow_decision": {
            "profile": "fast_context",
            "backend": "deferred",
            "confidence": "deferred",
            "rationale": ["Full EDA/PDK flow detection is deferred until the agent calls AgentIC capability tools."],
        },
        "role_summary": {
            "enabled": False,
            "roles": [],
            "counts": {},
            "fast": True,
        },
        "design_intent": None,
    }


@app.get("/pdks")
async def get_pdks():
    return detect_environment()


@app.get("/tools/status")
async def get_tools_status():
    return detect_environment()


@app.get("/tools/adapters")
async def get_tool_adapters(force_refresh: bool = False):
    from agentic_server.tool_adapters import normalized_adapter_summary
    env = detect_environment(force_refresh=force_refresh)
    return {
        "status": "OK",
        "tool_adapters": normalized_adapter_summary(env.get("tools") or {}),
        "wsl_tools": env.get("wsl_tools") or {},
        "wsl_capabilities": env.get("wsl_capabilities") or {},
        "wsl": env.get("wsl") or {},
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
    """Bridge-token check only; no license gate."""
    _require_opencode_bridge(request)
    return resolve_license_status(request)


_session_modes: dict[str, str] = {}
_global_agentic_mode: str = config.default_mode()

@app.get("/opencode/session/mode/{session_id}")
async def opencode_session_mode_get(session_id: str):
    return {"success": True, "agentic_mode": _session_modes.get(session_id) or _global_agentic_mode}

@app.post("/opencode/session/mode")
async def opencode_session_mode_set(request: Request):
    global _global_agentic_mode
    body = await request.json()
    session_id = str(body.get("session_id") or "").strip()
    mode = _normalize_agentic_mode(body.get("mode"))
    _global_agentic_mode = mode
    
    # Invalidate fast cache for ALL sessions
    _fast_cache.clear()

    if session_id:
        _session_modes[session_id] = mode

    data = _read_agentic_sessions()
    if session_id and session_id in data and isinstance(data[session_id], dict):
        data[session_id]["agentic_mode"] = mode
        data[session_id]["updated_at"] = time.time()
    
    for sid, sess in data.items():
        if isinstance(sess, dict):
            sess["agentic_mode"] = mode
            _session_modes[sid] = mode

    _write_agentic_sessions(data)
    return {"success": True, "agentic_mode": mode}

@app.post("/opencode/session/resolve")
async def opencode_session_resolve(req: OpenCodeSessionRequest, request: Request):
    _require_active_local_runtime(request)
    mapping = _resolve_opencode_mapping(req)
    if req.fast:
        # Cache hit: return within microseconds — no I/O, no file reads.
        # Invalidated by any kernel role call or mode change.
        cached = _fast_cache_get(mapping["session_id"])
        if cached:
            return cached
        result = _fast_opencode_session_response(req, mapping)
        _fast_cache_set(mapping["session_id"], result)
        return result

    try:
        from agentic_server.agentic_handoffs import schema_catalog
        from agentic_server.app_capabilities import build_app_capability_contract
        from agentic_server.agentic_kernel import build_context_contract, scope_for_turn
        from agentic_server.agentic_role_runner import RoleContext, persist_role_results, run_role_pipeline
        from agentic_server.agentic_validators import validation_schema_catalog
        from agentic_server.context_engine import build_agent_context_packet
        from agentic_server.design_intent import build_or_update_design_intent
        from agentic_server.session_workflow import classify_session_workflow
        from agentic_server.vlsi_capability_graph import assess_design_readiness

        messages = [{"role": "user", "content": req.user_text or ""}]
        workflow_decision = classify_session_workflow(req.user_text or "", messages)
        is_design_task = workflow_decision.requires_design_kernel or workflow_decision.intent == "DESIGN_TASK"
        execution_authorized = (mapping.get("agentic_mode") == "builder") or workflow_decision.execution_authorized
        kernel_scope = scope_for_turn(
            is_design_task=is_design_task,
            is_planning_round=workflow_decision.planning_round if is_design_task else False,
            execution_authorized=execution_authorized,
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
        kernel_contract["app_capabilities"] = build_app_capability_contract(mapping["design_root"], env)
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
            session_id=mapping.get("session_id"),
            agentic_mode=mapping.get("agentic_mode"),
        )
        return {
            "success": True,
            "license": {
                "active": True,
                "plan": "local",
                "source": "local",
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
    except Exception as exc:
        logging.error(f"Error during opencode_session_resolve pipeline: {exc}", exc_info=True)
        return _fast_opencode_session_response(req, mapping)


@app.post("/opencode/tool")
async def opencode_tool(req: OpenCodeToolRequest, request: Request):
    _require_active_local_runtime(request)
    try:
        mapping = _resolve_opencode_mapping(req)
        from agentic_server.agent_tools import dispatch_tool
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
    except Exception as exc:
        logger.error(f"Error executing tool {req.name}: {exc}", exc_info=True)
        return {
            "success": False,
            "result": json.dumps({"error": f"Tool execution error: {exc}"}),
            "session": {},
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
            + " You are in BUILDER mode. You are AUTHORIZED to create/edit design artifacts, write RTL/testbenches/scripts, "
            "run shell/EDA commands, and execute tools. Do NOT ask the user to switch to builder mode — you are already in it. "
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
    from agentic_server.agentic_runtime import stream_events

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
    from agentic_server.agentic_runtime import discover_root, health
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
    from agentic_server.agentic_runtime import start
    try:
        return start(hostname=req.hostname, port=req.port, timeout=req.timeout)
    except Exception as exc:
        raise _opencode_runtime_failure(exc)


@app.post("/opencode/desktop/sessions")
async def opencode_desktop_create_session(req: OpenCodeDesktopSessionRequest, request: Request):
    _require_active_local_runtime(request)
    from agentic_server.agentic_runtime import create_session, health
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
    from agentic_server.agentic_runtime import prompt_async
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
    from agentic_server.agentic_runtime import list_messages
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
    from agentic_server.agentic_runtime import abort_session
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
    from agentic_server.agent_tools import git_clone
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
    # Local mode: no license gate.
    return {"events": _read_run_events(min(max(limit, 1), 500), run_id or None)}


@app.post("/tools/install-plan")
async def get_tool_install_plan(req: ToolInstallPlanRequest):
    return _install_plan_for(req.capability, req.platform)


@app.post("/tools/install")
async def install_tool(req: ToolInstallRequest, request: Request):
    # Local mode: no license gate.
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


def _get_project_config() -> dict[str, Any]:
    """Return project config if present, or auto-detect solo vs team project via Git history."""
    config_path = os.path.join(WS_ROOT, ".agentic", "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                cfg.setdefault("is_single_project", False)
                return cfg
        except Exception:
            pass

    folder_name = os.path.basename(os.path.normpath(WS_ROOT)) or "project"
    is_team = False
    contributors: list[str] = []

    # Check Git contributor history
    if os.path.exists(os.path.join(WS_ROOT, ".git")):
        res = run_bash("git shortlog -sn HEAD", WS_ROOT)
        if res.get("success") and res.get("stdout"):
            lines = [l.strip() for l in res["stdout"].splitlines() if l.strip()]
            for line in lines:
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    contributors.append(parts[1].strip())
            if len(contributors) > 1:
                is_team = True

    if is_team:
        return {
            "group_project_id": f"team_{folder_name}",
            "project_name": folder_name,
            "team_leads": contributors[:2] or ["CAD Lead"],
            "strict_signoff": True,
            "is_single_project": False,
            "detected_contributors": contributors,
        }

    return {
        "group_project_id": f"single_{folder_name}",
        "project_name": folder_name,
        "team_leads": ["local_developer"],
        "strict_signoff": False,
        "is_single_project": True,
        "detected_contributors": contributors or ["local_developer"],
    }


@app.get("/opencode/project/config")
async def get_project_config_endpoint():
    """Endpoint for UI to query active project config and team leads."""
    return _get_project_config()


class ProjectConfigUpdateRequest(BaseModel):
    is_single_project: bool
    team_leads: list[str] = ["CAD Lead"]


@app.post("/opencode/project/config")
async def update_project_config_endpoint(req: ProjectConfigUpdateRequest):
    """Explicitly set or toggle Solo vs Team mode for the active project."""
    config_dir = os.path.join(WS_ROOT, ".agentic")
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.json")

    existing = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    existing["is_single_project"] = req.is_single_project
    existing["team_leads"] = req.team_leads
    existing["strict_signoff"] = not req.is_single_project

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    return {"ok": True, "config": _get_project_config()}


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


@app.get("/designs")
async def get_designs(request: Request):
    # Local mode: no license gate.
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
    # Local mode: no license gate.
    return list_artifacts(design_name, WS_ROOT)


@app.get("/build/artifacts/file/{file_name:path}")
async def get_workspace_artifact(request: Request, file_name: str):
    # Local mode: no license gate.
    content = read_workspace_artifact(file_name, WS_ROOT)
    if content is None:
        raise HTTPException(404, "Artifact not found")
    return content


@app.get("/opencode/binary-file")
async def get_binary_file(request: Request, directory: str = "", path: str = ""):
    """Serve a binary file (GDS, OAS, etc.) from the workspace as a streaming response."""
    # Local mode: no license gate.
    root = _safe_workspace_root(directory or WS_ROOT)
    # Resolve path safely (inline — _safe_workspace_path is in agent_tools.py)
    import os as _os
    full = _os.path.abspath(_os.path.normpath(_os.path.join(root, path))) if path else None
    try:
        if full and _os.path.commonpath([root, full]) != root:
            full = None
    except ValueError:
        full = None
    if not full or not _os.path.isfile(full):
        raise HTTPException(404, f"File not found: {path}")
    file_size = _os.path.getsize(full)
    from starlette.responses import StreamingResponse
    def iter_file():
        with open(full, "rb") as f:
            while chunk := f.read(1024 * 1024):
                yield chunk
    return StreamingResponse(
        iter_file(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{_os.path.basename(full)}"',
            "Content-Length": str(file_size),
        },
    )


@app.get("/build/artifacts/{design_name}/{file_name:path}")
async def get_artifact(request: Request, design_name: str, file_name: str):
    # Local mode: no license gate.
    content = read_artifact(design_name, file_name, WS_ROOT)
    if content is None:
        raise HTTPException(404, "Artifact not found")
    return content


@app.get("/build/checkpoints/{design_name}")
async def get_checkpoints(request: Request, design_name: str):
    # Local mode: no license gate.
    from agentic_server.checkpoint_engine import CheckpointEngine
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
    # Local mode: no license gate.
    design_root = os.path.join(WS_ROOT, design_name) if design_name else WS_ROOT
    return DesignStateStore(design_root, design_name).load()


@app.post("/chat/converse")
async def chat_converse(req: ChatRequest, request: Request):
    # Local mode: no license gate.

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
                    agentic_mode=_session_modes.get(req.session_id or "") or _global_agentic_mode or req.agentic_mode or _stored_agentic_mode(req.session_id or ""),
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
    # Local mode: no license gate.
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
    # Local mode: no license gate.
    return {"active": _get_active_design()}

@app.get("/build/sta/{design_name}")
async def get_sta_report(request: Request, design_name: str, workspace_root: str = ""):
    # Local mode: no license gate.
    from agentic_server.sta_reports import build_sta_report
    root = _safe_workspace_root(workspace_root)
    safe_name = _safe_design_dir_name(design_name, "scratch")
    design_root = os.path.join(root, safe_name) if safe_name else root
    return build_sta_report(design_root, safe_name)


@app.get("/build/sta/session/{session_id}")
async def get_session_sta_report(request: Request, session_id: str, workspace_root: str = ""):
    # Local mode: no license gate.
    from agentic_server.sta_reports import build_sta_report

    data = _read_agentic_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    root = _safe_workspace_root(str(existing.get("workspace_root") or workspace_root or WS_ROOT))
    fallback = f"session_{_session_hash(session_id)}"
    design_name = _safe_design_dir_name(str(existing.get("design_name") or fallback), fallback)
    design_root = str(existing.get("design_root") or os.path.join(root, design_name))
    return build_sta_report(design_root, design_name)


@app.get("/build/signoff/{design_name}")
async def get_signoff_report(request: Request, design_name: str, workspace_root: str = ""):
    # Local mode: no license gate.
    from agentic_server.signoff_reports import build_signoff_report

    root = _safe_workspace_root(workspace_root)
    safe_name = _safe_design_dir_name(design_name, "scratch")
    design_root = os.path.join(root, safe_name) if safe_name else root
    return build_signoff_report(design_root, safe_name)


@app.get("/build/signoff/session/{session_id}")
async def get_session_signoff_report(request: Request, session_id: str, workspace_root: str = ""):
    # Local mode: no license gate.
    from agentic_server.signoff_reports import build_signoff_report

    data = _read_agentic_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    root = _safe_workspace_root(str(existing.get("workspace_root") or workspace_root or WS_ROOT))
    fallback = f"session_{_session_hash(session_id)}"
    design_name = _safe_design_dir_name(str(existing.get("design_name") or fallback), fallback)
    design_root = str(existing.get("design_root") or os.path.join(root, design_name))
    return build_signoff_report(design_root, design_name)


@app.get("/simulation/waveforms/{design_name}")
async def get_waveforms(request: Request, design_name: str):
    # Local mode: no license gate.

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
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--bridge":
        # Bridge mode: the Hono sidecar invokes the compiled binary with --bridge.
        # Read JSON from stdin, dispatch to the opencode_bridge logic, write JSON to stdout.
        from agentic_server.opencode_bridge import main as _bridge_main
        _bridge_main()
    else:
        import uvicorn
        import socket
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        target_port = config.port()
        
        def _find_available_port(start_port: int, max_attempts: int = 100) -> int:
            for p in range(start_port, start_port + max_attempts):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        s.bind(("0.0.0.0", p))
                        return p
                    except OSError:
                        continue
            return start_port

        port = _find_available_port(target_port)
        if port != target_port:
            logging.warning("Port %d was in use; automatically bound to free port %d", target_port, port)
            os.environ["AGENTIC_PORT"] = str(port)

        uvicorn.run(app, host="0.0.0.0", port=port, access_log=True)
