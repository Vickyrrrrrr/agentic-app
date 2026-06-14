from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from context_cache import build_role_context_packet
from vlsi_state import DesignStateStore
from vlsi_capability_graph import compact_capability_graph


SOURCE_EXTENSIONS = {
    ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl",
    ".tcl", ".sdc", ".ys", ".mk", ".json", ".yaml", ".yml",
    ".py", ".c", ".cpp", ".h", ".md",
}

SKIP_DIRS = {
    ".git", ".agentic", "node_modules", "__pycache__", ".pytest_cache",
    "dist", "build", ".venv", "venv", "runs", "tmp",
}

MAX_FILE_MAP = 120
MAX_SYMBOLS = 80
MAX_RELEVANT_SNIPPETS = 8
MAX_SNIPPET_LINES = 34


def build_agent_context_packet(
    workspace_root: str,
    design_name: str,
    user_text: str,
    env: dict[str, Any],
    flow_decision: dict[str, Any],
    context_contract: dict[str, Any] | None = None,
    max_chars: int = 18000,
) -> dict[str, Any]:
    root = Path(workspace_root).resolve()
    store = DesignStateStore(workspace_root, design_name)
    raw_state = store.load()
    state = store.summary(max_events=8)
    active_role = _active_role(context_contract or state.get("context_contract"))
    role_context = build_role_context_packet(
        workspace_root=workspace_root,
        design_name=design_name,
        role=active_role,
        user_text=user_text,
        state=raw_state,
    ).to_prompt_contract()
    packet = {
        "design_state": state,
        "kernel_contract": context_contract or state.get("context_contract"),
        "role_context": role_context,
        "environment_summary": _environment_summary(env),
        "flow_decision": _compact_flow_decision(flow_decision),
        "repo_map": _repo_map(root),
        "relevant_snippets": _relevant_snippets(root, user_text),
        "context_policy": {
            "principle": "Full files, full PDKs, and full logs stay outside the LLM context. Retrieve exact files/snippets with tools when needed.",
            "edit_mode": "Prefer surgical old_string/new_string edits. Whole-file writes are for new files or deliberate small rewrites.",
            "log_mode": "Use checkpoint verdicts and short failing excerpts; never paste full EDA logs into chat context.",
        },
    }
    return _trim_packet(packet, max_chars)


def _active_role(context_contract: dict[str, Any] | None) -> str:
    roles = []
    if isinstance(context_contract, dict):
        raw = context_contract.get("active_roles")
        roles = raw if isinstance(raw, list) else []
    for role in reversed(roles):
        if isinstance(role, str) and role.strip():
            return role.strip()
    return "supervisor"


def _environment_summary(env: dict[str, Any]) -> dict[str, Any]:
    tools = env.get("tools") or {}
    flows = env.get("flows") or {}
    pdk_index = env.get("pdk_index") or {}
    capability_index = env.get("capability_index") or {}
    capability_graph = env.get("capability_graph") or {}
    pdks = pdk_index.get("pdks") or []
    return {
        "capability_tier": env.get("capability_tier"),
        "available_tools": sorted([name for name, available in tools.items() if available])[:80],
        "flow_availability": {
            "openlane": bool((flows.get("openlane") or {}).get("available")),
            "orfs": bool((flows.get("orfs") or {}).get("available")),
            "proprietary": bool((flows.get("proprietary") or {}).get("available")),
            "licensed_proprietary_backend": bool((flows.get("proprietary") or {}).get("licensed_backend_available")),
        },
        "pdks": [
            {
                "name": pdk.get("name"),
                "family": pdk.get("family"),
                "node_nm": pdk.get("node_nm"),
                "class": pdk.get("class"),
                "readiness": (pdk.get("readiness") or {}).get("tier"),
                "active_scl": (pdk.get("openlane") or {}).get("active_scl"),
            }
            for pdk in pdks[:20]
        ],
        "capability_index": _compact_capability_index(capability_index),
        "capability_graph": compact_capability_graph(capability_graph) if capability_graph else {},
        "missing": env.get("missing", [])[:10],
    }


def _compact_capability_index(index: dict[str, Any]) -> dict[str, Any]:
    pdks = []
    for entry in (index.get("pdks") or [])[:8]:
        pdk = entry.get("pdk") or {}
        pdks.append({
            "name": pdk.get("name"),
            "family": pdk.get("family"),
            "memory_macros": [
                {
                    "name": macro.get("name"),
                    "kind": macro.get("kind"),
                    "width_bits": macro.get("width_bits"),
                    "depth_words": macro.get("depth_words"),
                    "confidence": macro.get("confidence"),
                    "views": sorted((macro.get("views") or {}).keys()),
                }
                for macro in (entry.get("memory_macros") or [])[:10]
            ],
            "pad_cells": [
                {
                    "name": pad.get("name"),
                    "kind": pad.get("kind"),
                    "confidence": pad.get("confidence"),
                    "views": sorted((pad.get("views") or {}).keys()),
                }
                for pad in (entry.get("pad_cells") or [])[:8]
            ],
            "stdcell_capabilities": {
                key: len(value) if isinstance(value, list) else value
                for key, value in (entry.get("stdcell_capabilities") or {}).items()
            },
            "timing_corner_count": len(entry.get("timing_corners") or []),
        })
    return {
        "schema_version": index.get("schema_version"),
        "summary": index.get("summary") or {},
        "pdks": pdks,
        "toolchain_stage_summary": {
            stage: {
                "available": data.get("available"),
                "selected": (data.get("selected") or {}).get("adapter"),
                "vendor": (data.get("selected") or {}).get("vendor"),
                "openness": (data.get("selected") or {}).get("openness"),
            }
            for stage, data in (((index.get("toolchains") or {}).get("stages") or {}).items())
        },
    }


def _compact_flow_decision(decision: dict[str, Any]) -> dict[str, Any]:
    selected = decision.get("selected_pdk") or {}
    return {
        "profile": decision.get("profile"),
        "backend": decision.get("backend"),
        "confidence": decision.get("confidence"),
        "selected_pdk": {
            "name": selected.get("name"),
            "family": selected.get("family"),
            "node_nm": selected.get("node_nm"),
            "class": selected.get("class"),
            "readiness": selected.get("readiness"),
        } if selected else None,
        "rationale": decision.get("rationale", [])[:5],
        "blockers": decision.get("blockers", [])[:8],
        "setup_actions": decision.get("setup_actions", [])[:5],
        "run_strategy": decision.get("run_strategy", {}),
    }


def _repo_map(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    if not root.is_dir():
        return {"files": files, "symbols": symbols}

    for path in _iter_source_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        files.append({"path": rel, "stage": _stage_for_path(rel), "size": size})
        if len(symbols) < MAX_SYMBOLS:
            symbols.extend(_extract_symbols(path, root, MAX_SYMBOLS - len(symbols)))
        if len(files) >= MAX_FILE_MAP:
            break
    return {"files": files, "symbols": symbols[:MAX_SYMBOLS]}


def _iter_source_files(root: Path):
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        current_path = Path(current)
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = current_path / name
            if path.suffix.lower() in SOURCE_EXTENSIONS or name.lower() in {"makefile", "dockerfile"}:
                yield path


def _extract_symbols(path: Path, root: Path, limit: int) -> list[dict[str, Any]]:
    rel = path.relative_to(root).as_posix()
    symbols: list[dict[str, Any]] = []
    patterns = [
        ("module", re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)")),
        ("interface", re.compile(r"^\s*interface\s+([A-Za-z_][A-Za-z0-9_$]*)")),
        ("package", re.compile(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_$]*)")),
        ("entity", re.compile(r"^\s*entity\s+([A-Za-z_][A-Za-z0-9_$]*)\s+is", re.IGNORECASE)),
        ("proc", re.compile(r"^\s*proc\s+([A-Za-z_][A-Za-z0-9_:]*)")),
        ("function", re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
        ("class", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")),
    ]
    try:
        with path.open("r", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                for kind, pattern in patterns:
                    match = pattern.search(line)
                    if match:
                        symbols.append({"path": rel, "line": lineno, "kind": kind, "name": match.group(1)})
                        if len(symbols) >= limit:
                            return symbols
    except OSError:
        pass
    return symbols


def _relevant_snippets(root: Path, user_text: str) -> list[dict[str, Any]]:
    terms = _query_terms(user_text)
    if not terms:
        return []
    snippets: list[dict[str, Any]] = []
    for path in _iter_source_files(root):
        if len(snippets) >= MAX_RELEVANT_SNIPPETS:
            break
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        match_idx = _first_matching_line(lines, terms)
        if match_idx is None:
            continue
        start = max(0, match_idx - 8)
        end = min(len(lines), start + MAX_SNIPPET_LINES)
        snippets.append({
            "path": path.relative_to(root).as_posix(),
            "start_line": start + 1,
            "end_line": end,
            "text": "\n".join(lines[start:end])[:3000],
        })
    return snippets


def _query_terms(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
    stop = {
        "the", "and", "for", "with", "chip", "design", "create", "build",
        "make", "using", "please", "agentic", "module", "verify",
    }
    terms: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered not in stop and lowered not in terms:
            terms.append(lowered)
    return terms[:12]


def _first_matching_line(lines: list[str], terms: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if any(term in lowered for term in terms):
            return idx
    return None


def _stage_for_path(path: str) -> str:
    lower = path.lower()
    if "/rtl/" in f"/{lower}" or lower.endswith((".v", ".sv", ".vhd", ".vhdl")):
        return "rtl"
    if "/tb/" in f"/{lower}" or "testbench" in lower:
        return "testbench"
    if "/constraints/" in f"/{lower}" or lower.endswith(".sdc"):
        return "constraints"
    if "/synth/" in f"/{lower}":
        return "synthesis"
    if "/pnr/" in f"/{lower}" or "/hardening/" in f"/{lower}":
        return "implementation"
    if "/sta/" in f"/{lower}":
        return "sta"
    if "/signoff/" in f"/{lower}":
        return "signoff"
    if "/reports/" in f"/{lower}":
        return "reporting"
    return "workspace"


def _trim_packet(packet: dict[str, Any], max_chars: int) -> dict[str, Any]:
    text = repr(packet)
    if len(text) <= max_chars:
        return packet
    trimmed = dict(packet)
    repo_map = dict(trimmed.get("repo_map") or {})
    snippets = list(trimmed.get("relevant_snippets") or [])
    while len(repr(trimmed)) > max_chars and snippets:
        snippets.pop()
        trimmed["relevant_snippets"] = snippets
    files = list(repo_map.get("files") or [])
    while len(repr(trimmed)) > max_chars and len(files) > 30:
        files = files[: max(30, len(files) // 2)]
        repo_map["files"] = files
        trimmed["repo_map"] = repo_map
    symbols = list(repo_map.get("symbols") or [])
    while len(repr(trimmed)) > max_chars and len(symbols) > 20:
        symbols = symbols[: max(20, len(symbols) // 2)]
        repo_map["symbols"] = symbols
        trimmed["repo_map"] = repo_map
    return trimmed
