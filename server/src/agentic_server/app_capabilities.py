"""Live AgentIC application capability contract.

This is deliberately derived from local evidence rather than a static feature
list.  It gives planners a small, truthful view of the UI surfaces, design
artifacts, and semantic sidecars usable for the *current* workspace.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any


CAPABILITY_CONTRACT_VERSION = "agentic.app_capabilities.v1"
_MAX_ARTIFACTS = 48
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


def build_app_capability_contract(workspace_root: str, env: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a bounded, evidence-backed contract for one workspace."""
    root = Path(workspace_root).expanduser().resolve()
    artifacts = _discover_artifacts(root)
    tool_adapters = (env or {}).get("tool_adapters") or {}
    flows = ((env or {}).get("flows") or {})
    semantic_binary = shutil.which("slang-server")
    sidecar_state = ((env or {}).get("sidecars") or {}).get("slang") or {}
    semantic_ready = bool(semantic_binary) and (
        sidecar_state.get("status") == "ready" or os.environ.get("AGENTIC_SLANG_SIDECAR_READY", "").lower() in {"1", "true", "yes"}
    )
    flow_available = _has_available_flow(tool_adapters, flows)

    capabilities = [
        _capability("rtl_editor", "ready" if artifacts["rtl"] else "available", "HDL Design Editor", artifacts["rtl"], "Open an RTL source file to edit it through AgentIC policy gates."),
        _capability("rtl_validation", "ready", "RTL evidence checks", [], "Use governed lint/repair diagnosis; do not call a result clean without its diagnostic evidence."),
        _capability("design_contract", "ready", "Design contract and hierarchy", [], "Use design_contract before top-level, integration, or signoff claims."),
        _capability("semantic_hdl", "ready" if semantic_ready else "unavailable", "Slang SystemVerilog semantics", [semantic_binary] if semantic_binary else [], "Use live definitions/references only after the managed Slang sidecar reports healthy."),
        _capability("waveform", "ready" if artifacts["waveform"] else "unavailable", "Waveform evidence", artifacts["waveform"], "Cross-probe only through an existing VCD/FST artifact."),
        _capability("layout", "ready" if artifacts["layout"] else "unavailable", "Layout evidence", artifacts["layout"], "Inspect only existing GDS/OAS artifacts."),
        _capability("timing_reports", "ready" if artifacts["timing"] else "unavailable", "Timing evidence", artifacts["timing"], "Base timing claims on parsed report evidence."),
        _capability("physical_reports", "ready" if artifacts["physical"] else "unavailable", "DRC/LVS evidence", artifacts["physical"], "Base signoff claims on parsed physical-verification reports."),
        _capability("tcl_flow", "available" if artifacts["tcl"] and flow_available else "unavailable", "Tcl/SDC flow scripts", artifacts["tcl"], "Use existing customer/foundry scripts and detected adapters; never invent a tool dialect."),
    ]
    return {
        "schema_version": CAPABILITY_CONTRACT_VERSION,
        "generated_at": time.time(),
        "workspace_root": str(root),
        "capabilities": capabilities,
        "planning_rules": [
            "Use only capabilities whose status is ready or available.",
            "Do not describe an unavailable capability as present; report its exact missing artifact, sidecar, tool, PDK view, or license instead.",
            "Prefer an AgentIC capability/action over ad-hoc shell work when both can produce the same evidence.",
            "A UI capability is not proof of chip correctness; measured reports and checkpoints remain the source of truth.",
        ],
    }


def _capability(identifier: str, status: str, label: str, evidence: list[str], instruction: str) -> dict[str, Any]:
    return {"id": identifier, "status": status, "label": label, "evidence": evidence[:8], "agent_instruction": instruction}


def _discover_artifacts(root: Path) -> dict[str, list[str]]:
    found = {"rtl": [], "tcl": [], "waveform": [], "layout": [], "timing": [], "physical": []}
    if not root.is_dir():
        return found
    for current, directories, names in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - len(root.parts)
        directories[:] = [name for name in directories if name not in _SKIP_DIRS and not name.startswith(".")]
        if depth > 7:
            directories[:] = []
            continue
        for name in names:
            path = current_path / name
            relative = str(path.relative_to(root)).replace("\\", "/")
            suffix = path.suffix.lower()
            if suffix in {".v", ".sv", ".vh", ".svh"}:
                _append(found["rtl"], relative)
            elif suffix in {".tcl", ".sdc", ".ys"}:
                _append(found["tcl"], relative)
            elif suffix in {".vcd", ".fst"}:
                _append(found["waveform"], relative)
            elif suffix in {".gds", ".oas"}:
                _append(found["layout"], relative)
            elif suffix in {".rpt", ".report"}:
                lower = relative.lower()
                if any(term in lower for term in ("timing", "sta", "slack")):
                    _append(found["timing"], relative)
                if any(term in lower for term in ("drc", "lvs", "antenna", "density")):
                    _append(found["physical"], relative)
    return found


def _append(items: list[str], value: str) -> None:
    if len(items) < _MAX_ARTIFACTS:
        items.append(value)


def _has_available_flow(tool_adapters: dict[str, Any], flows: dict[str, Any]) -> bool:
    for stage in tool_adapters.values():
        selected = stage.get("selected") if isinstance(stage, dict) else None
        if isinstance(selected, dict) and selected.get("available"):
            return True
    return any(isinstance(flow, dict) and flow.get("available") for flow in flows.values())
