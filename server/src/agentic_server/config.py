"""Canonical AGENTIC_* registry. Env is read here; everything else imports getters."""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def workspace_root() -> str:
    return _env("AGENTIC_WORKSPACE") or os.path.expanduser("~/AgentIC-workspace")


def port() -> int:
    return _env_int("AGENTIC_PORT", _env_int("PORT", 7860))


def default_mode() -> str:
    return "builder" if _env("AGENTIC_MODE").strip().lower() == "builder" else "advisor"


def bridge_token() -> str:
    return _env("AGENTIC_OPENCODE_BRIDGE_TOKEN").strip()


def allowed_origins() -> list[str]:
    return [o.strip() for o in _env("AGENTIC_ALLOWED_ORIGINS").split(",") if o.strip()]


def max_llm_rounds(planning: bool = False) -> int:
    return _env_int("AGENTIC_MAX_LLM_ROUNDS", 8 if planning else 34)


def max_tool_calls(planning: bool = False) -> int:
    return _env_int("AGENTIC_MAX_TOOL_CALLS", 8 if planning else 52)


def max_identical_tool_calls() -> int:
    return _env_int("AGENTIC_MAX_IDENTICAL_TOOL_CALLS", 2)


def max_writes_per_path() -> int:
    return _env_int("AGENTIC_MAX_WRITES_PER_PATH", 3)


def context_max_chars(default: int = 12000) -> int:
    return _env_int("AGENTIC_CONTEXT_MAX_CHARS", default)


REGISTRY: tuple[tuple[str, str], ...] = (
    ("AGENTIC_WORKSPACE", "workspace root (see workspace_root())"),
    ("AGENTIC_PORT", "engine HTTP port (see port())"),
    ("AGENTIC_MODE", "default advisor|builder mode (see default_mode())"),
    ("AGENTIC_OPENCODE_BRIDGE_TOKEN", "optional shared secret for bridge calls"),
    ("AGENTIC_ALLOWED_ORIGINS", "extra CORS origins, comma-separated"),
    ("AGENTIC_OPENCODE_URL", "attach to an external opencode runtime instead of spawning"),
    ("AGENTIC_OPENCODE_COMMAND", "command used to start the runtime"),
    ("AGENTIC_OPENCODE_ROOT", "search root for runtime discovery"),
    ("AGENTIC_OPENCODE_USERNAME", "runtime basic-auth username"),
    ("AGENTIC_OPENCODE_PASSWORD", "runtime basic-auth password"),
    ("AGENTIC_PDK_SEARCH_PATHS", "extra PDK search roots (pdk_index)"),
    ("AGENTIC_PDK_MANIFESTS", "capability manifest files (capability_manifest)"),
    ("AGENTIC_PDK_MANIFEST_FILE", "single manifest file override"),
    ("AGENTIC_CAPABILITY_MANIFESTS", "alias for manifests"),
    ("AGENTIC_CAPABILITY_MANIFEST", "alias for manifests"),
    ("AGENTIC_EDA_TOOLS", "override EDA tool probing (local_tools)"),
    ("AGENTIC_SIM_TOOLS", "simulation tool override"),
    ("AGENTIC_SYNTH_TOOLS", "synthesis tool override"),
    ("AGENTIC_PNR_TOOLS", "PnR tool override"),
    ("AGENTIC_TOOL_ADAPTERS_JSON", "inline custom adapter registry"),
    ("AGENTIC_TOOL_ADAPTERS_FILE", "custom adapter registry file"),
    ("AGENTIC_TOOL_SEARCH_PATHS", "extra PATH entries for tool probing"),
    ("AGENTIC_FLOW_SEARCH_PATHS", "extra flow roots (openlane/orfs)"),
    ("AGENTIC_OPENLANE_ROOT", "openlane installation root"),
    ("AGENTIC_ORFS_ROOT", "OpenROAD-flow-scripts root"),
    ("AGENTIC_PNR_DOCKER_IMAGE", "docker image for PnR install plans"),
    ("AGENTIC_BASIC_INSTALL_COMMAND", "install command override"),
    ("AGENTIC_ALLOW_CUSTOM_INSTALL_COMMANDS", "widen install-command policy"),
    ("AGENTIC_ENABLE_WEB_SEARCH", "gate for the web_search tool"),
    ("AGENTIC_ALLOW_SENSITIVE_WEB_SEARCH", "widen web-search policy"),
    ("AGENTIC_SCHEMATIC_FULL_JSON_CELL_LIMIT", "schematic JSON guard"),
    ("AGENTIC_SCHEMATIC_FULL_JSON_NET_LIMIT", "schematic JSON guard"),
    ("AGENTIC_MAX_JOBS", "job queue cap (job_queue)"),
    ("AGENTIC_MAX_PENDING", "pending cap"),
    ("AGENTIC_JOBS_HISTORY", "finished-job retention"),
    ("AGENTIC_SESSIONS_PATH", "sessions file override"),
    ("AGENTIC_RUNTIME", "runtime marker"),
    ("AGENTIC_DEBUG_EVENTS", "verbose agent event stream"),
    ("AGENTIC_CONTEXT_MAX_CHARS", "context packet budget"),
)
