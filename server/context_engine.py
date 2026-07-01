from __future__ import annotations

import json
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

CONTEXT_SCHEMA_VERSION = "agentic.context_packet.v2"
DEFAULT_CONTEXT_MAX_CHARS = 12000
MIN_CONTEXT_MAX_CHARS = 8000
MAX_FILE_MAP = 80
MAX_SYMBOLS = 48
MAX_RELEVANT_SNIPPETS = 4
MAX_SNIPPET_LINES = 18
MAX_SNIPPET_CHARS = 1600


def build_agent_context_packet(
    workspace_root: str,
    design_name: str,
    user_text: str,
    env: dict[str, Any],
    flow_decision: dict[str, Any],
    context_contract: dict[str, Any] | None = None,
    max_chars: int | None = None,
    session_id: str | None = None,
    agentic_mode: str | None = None,
) -> dict[str, Any]:
    root = Path(workspace_root).resolve()
    budget_chars = _context_max_chars(max_chars)
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
    repo_map = _repo_map(root)
    snippets = _relevant_snippets(root, user_text)
    env_summary = _environment_summary(env)
    flow_summary = _compact_flow_decision(flow_decision)
    design_memory = _design_memory_summary(state, raw_state)
    working_set = _working_set_summary(state, raw_state, repo_map, snippets)
    retrieval_index = _retrieval_index(repo_map, snippets, role_context, state, root)
    packet = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "budget": {
            "max_chars": budget_chars,
            "target": "8k-12k chars for normal turns",
            "policy": "summary_and_index_first",
        },
        "tier0_session": {
            "session_id": session_id,
            "design_name": design_name,
            "design_root": str(root),
            "agentic_mode": agentic_mode,
            "active_role": active_role,
            "flow_decision": flow_summary,
        },
        "tier1_working_set": working_set,
        "tier2_design_memory": design_memory,
        "tier3_retrieval_index": retrieval_index,
        "design_state": design_memory,
        "kernel_contract": context_contract or state.get("context_contract"),
        "role_context": role_context,
        "environment_summary": env_summary,
        "repo_map": repo_map,
        "relevant_snippets": snippets,
        "context_policy": {
            "principle": "This packet is an index and summary, not a full project dump. Full files, PDKs, and logs stay outside the LLM context.",
            "retrieval_mode": "Use workspace(read/search/list) for exact files, query_pdk for exact PDK/tool/macro facts, and bash/checkpoint paths for logs.",
            "edit_mode": "Prefer surgical old_string/new_string edits. Whole-file writes are for new files or deliberate small rewrites.",
            "log_mode": "Use checkpoint verdicts and short failing excerpts; never paste full EDA logs into chat context.",
            "omission_rule": "If a detail is omitted, retrieve it with tools before making design or signoff claims.",
        },
    }
    trimmed = _trim_packet(packet, budget_chars)
    trimmed.setdefault("budget", {}).setdefault("truncated", False)
    trimmed["budget"]["actual_chars"] = _packet_size(trimmed)
    return trimmed


def _context_max_chars(max_chars: int | None) -> int:
    raw = max_chars if max_chars is not None else os.environ.get("AGENTIC_CONTEXT_MAX_CHARS", str(DEFAULT_CONTEXT_MAX_CHARS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_CONTEXT_MAX_CHARS
    return max(MIN_CONTEXT_MAX_CHARS, min(50000, value))


def _active_role(context_contract: dict[str, Any] | None) -> str:
    roles = []
    if isinstance(context_contract, dict):
        raw = context_contract.get("active_roles")
        roles = raw if isinstance(raw, list) else []
    for role in reversed(roles):
        if isinstance(role, str) and role.strip():
            return role.strip()
    return "supervisor"


def _compact_flow_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(decision, dict):
        decision = {}
    selected = decision.get("selected_pdk") if isinstance(decision.get("selected_pdk"), dict) else {}
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
        "blocked_reason": decision.get("blocked_reason"),
        "rationale": (decision.get("rationale") or [])[:3],
    }


def _design_memory_summary(state: dict[str, Any], raw_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "design_name": state.get("design_name"),
        "updated_at": state.get("updated_at"),
        "intent": state.get("intent") or {},
        "design_intent": _compact_design_intent(state.get("design_intent")),
        "module_ownership": _compact_mapping(state.get("module_ownership") or {}, limit=24),
        "implementation_policy": state.get("implementation_policy") or {},
        "flow_decision": state.get("flow_decision") or {},
        "stage_status": state.get("stage_status") or {},
        "artifact_count": state.get("artifact_count", 0),
        "artifact_index": state.get("artifact_index") or {},
        "checkpoint_count": state.get("checkpoint_count", 0),
        "latest_failing_checkpoint": _latest_checkpoint(raw_state.get("checkpoints") or [], passed=False),
        "latest_checkpoints": _latest_checkpoints(raw_state.get("checkpoints") or [], limit=5),
        "latest_evidence": (state.get("latest_evidence") or [])[:6],
        "unresolved_risks": _unresolved_risks(state.get("design_intent")),
    }


def _compact_design_intent(intent: Any) -> Any:
    if not isinstance(intent, dict):
        return intent
    modules = intent.get("modules") if isinstance(intent.get("modules"), dict) else {}
    return {
        "schema_version": intent.get("schema_version"),
        "intent_id": intent.get("intent_id"),
        "design_name": intent.get("design_name"),
        "project_root": intent.get("project_root"),
        "target": intent.get("target"),
        "module_count": len(modules),
        "modules": {
            name: {
                "kind": module.get("kind"),
                "owner_file": module.get("owner_file"),
                "verification_owner": module.get("verification_owner"),
                "status": module.get("status"),
                "dialect": module.get("dialect"),
            }
            for name, module in list(modules.items())[:20]
            if isinstance(module, dict)
        },
        "implementation_policy": intent.get("implementation_policy"),
        "acceptance": intent.get("acceptance"),
        "unresolved": (intent.get("unresolved") or [])[:12],
        "revision": intent.get("revision"),
    }


def _compact_mapping(value: dict[str, Any], limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in list(value)[:limit]}


def _unresolved_risks(intent: Any) -> list[Any]:
    if not isinstance(intent, dict):
        return []
    unresolved = intent.get("unresolved")
    return unresolved[:12] if isinstance(unresolved, list) else []


def _latest_checkpoints(checkpoints: list[Any], limit: int = 5) -> list[dict[str, Any]]:
    compact = [_checkpoint_summary(item) for item in checkpoints if isinstance(item, dict)]
    return [item for item in compact if item][-limit:]


def _latest_checkpoint(checkpoints: list[Any], passed: bool) -> dict[str, Any] | None:
    for item in reversed(checkpoints):
        if isinstance(item, dict) and bool(item.get("pass")) is passed:
            return _checkpoint_summary(item)
    return None


def _checkpoint_summary(item: dict[str, Any]) -> dict[str, Any]:
    diagnostics = item.get("semantic_diagnostics") if isinstance(item.get("semantic_diagnostics"), dict) else {}
    return {
        "tool": item.get("tool"),
        "stage": item.get("stage"),
        "pass": bool(item.get("pass")),
        "exit_code": item.get("exit_code"),
        "errors": (item.get("errors") or [])[:5],
        "warnings": (item.get("warnings") or [])[:5],
        "metrics": item.get("metrics") or {},
        "dominant_class": diagnostics.get("dominant_class"),
        "captured_at": item.get("captured_at"),
    }


def _working_set_summary(
    state: dict[str, Any],
    raw_state: dict[str, Any],
    repo_map: dict[str, Any],
    snippets: list[dict[str, Any]],
) -> dict[str, Any]:
    artifacts = raw_state.get("artifacts") if isinstance(raw_state.get("artifacts"), dict) else {}
    recent_artifacts = sorted(
        artifacts.values(),
        key=lambda item: item.get("updated_at", 0) if isinstance(item, dict) else 0,
        reverse=True,
    )
    return {
        "stage_status": state.get("stage_status") or {},
        "latest_failing_checkpoint": _latest_checkpoint(raw_state.get("checkpoints") or [], passed=False),
        "recent_artifacts": [
            {
                "path": item.get("path"),
                "stage": item.get("stage"),
                "kind": item.get("kind"),
                "owner_module": item.get("owner_module"),
                "updated_at": item.get("updated_at"),
            }
            for item in recent_artifacts[:12]
            if isinstance(item, dict)
        ],
        "active_modules": sorted((state.get("module_ownership") or {}).keys())[:20],
        "matching_snippet_paths": [item.get("path") for item in snippets[:MAX_RELEVANT_SNIPPETS]],
        "repo_file_count_in_packet": len(repo_map.get("files") or []),
        "repo_symbol_count_in_packet": len(repo_map.get("symbols") or []),
    }


def _retrieval_index(
    repo_map: dict[str, Any],
    snippets: list[dict[str, Any]],
    role_context: dict[str, Any],
    state: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    omitted = role_context.get("omitted") if isinstance(role_context, dict) else []
    return {
        "design_root": str(root),
        "tools": {
            "files": "workspace(action='read'|'search'|'list')",
            "pdk_tools_macros": "query_pdk(query_type=...)",
            "shell_and_logs": "bash(command=..., eda_tool=..., log_file=...)",
        },
        "repo_files": (repo_map.get("files") or [])[:40],
        "symbols": (repo_map.get("symbols") or [])[:30],
        "snippet_refs": [
            {key: item.get(key) for key in ("path", "start_line", "end_line")}
            for item in snippets[:MAX_RELEVANT_SNIPPETS]
        ],
        "omitted_role_segments": [
            {
                "segment_id": item.get("segment_id"),
                "kind": item.get("kind"),
                "owner": item.get("owner"),
                "path": item.get("path"),
                "token_estimate": item.get("token_estimate"),
            }
            for item in (omitted or [])[:20]
            if isinstance(item, dict)
        ],
        "latest_evidence_refs": [
            {key: item.get(key) for key in ("id", "kind", "ref")}
            for item in (state.get("latest_evidence") or [])[:8]
        ],
    }


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
        "wsl": _compact_wsl_environment(env),
        "missing": env.get("missing", [])[:10],
    }


def _compact_wsl_environment(env: dict[str, Any]) -> dict[str, Any]:
    wsl = env.get("wsl") or {}
    inventory = []
    for item in (wsl.get("tool_inventory") or [])[:8]:
        tools = item.get("tools") or {}
        paths = item.get("paths") or {}
        inventory.append({
            "distro": item.get("distro"),
            "can_execute": bool(item.get("can_execute")),
            "available_tools": sorted([name for name, available in tools.items() if available])[:80],
            "paths": {name: paths.get(name) for name in sorted(paths)[:80]},
            "license_env": sorted([name for name, present in (item.get("license_env") or {}).items() if present])[:20],
            "error": item.get("error"),
        })
    return {
        "available": bool(wsl.get("available")),
        "distros": (wsl.get("distros") or [])[:12],
        "tool_inventory": inventory,
        "wsl_tools": sorted([name for name, available in (env.get("wsl_tools") or {}).items() if available])[:120],
        "wsl_capabilities": env.get("wsl_capabilities") or {},
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
            "text": "\n".join(lines[start:end])[:MAX_SNIPPET_CHARS],
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
    original_size = _packet_size(packet)
    if original_size <= max_chars:
        return packet
    trimmed = dict(packet)
    trimmed.setdefault("budget", {})["truncated"] = True
    repo_map = dict(trimmed.get("repo_map") or {})
    snippets = list(trimmed.get("relevant_snippets") or [])
    while _packet_size(trimmed) > max_chars and snippets:
        snippets.pop()
        trimmed["relevant_snippets"] = snippets
        retrieval = dict(trimmed.get("tier3_retrieval_index") or {})
        retrieval["snippet_refs"] = [
            {key: item.get(key) for key in ("path", "start_line", "end_line")}
            for item in snippets
        ]
        trimmed["tier3_retrieval_index"] = retrieval
    files = list(repo_map.get("files") or [])
    while _packet_size(trimmed) > max_chars and len(files) > 24:
        files = files[: max(24, len(files) // 2)]
        repo_map["files"] = files
        trimmed["repo_map"] = repo_map
        retrieval = dict(trimmed.get("tier3_retrieval_index") or {})
        retrieval["repo_files"] = files[:30]
        trimmed["tier3_retrieval_index"] = retrieval
    symbols = list(repo_map.get("symbols") or [])
    while _packet_size(trimmed) > max_chars and len(symbols) > 16:
        symbols = symbols[: max(16, len(symbols) // 2)]
        repo_map["symbols"] = symbols
        trimmed["repo_map"] = repo_map
        retrieval = dict(trimmed.get("tier3_retrieval_index") or {})
        retrieval["symbols"] = symbols[:24]
        trimmed["tier3_retrieval_index"] = retrieval
    if _packet_size(trimmed) > max_chars:
        trimmed["environment_summary"] = _thin_environment_summary(trimmed.get("environment_summary") or {})
    if _packet_size(trimmed) > max_chars:
        design_memory = dict(trimmed.get("tier2_design_memory") or {})
        design_memory["latest_evidence"] = (design_memory.get("latest_evidence") or [])[:3]
        design_memory["latest_checkpoints"] = (design_memory.get("latest_checkpoints") or [])[-3:]
        design_memory["artifact_index"] = _thin_artifact_index(design_memory.get("artifact_index") or {})
        trimmed["tier2_design_memory"] = design_memory
        trimmed["design_state"] = design_memory
    if _packet_size(trimmed) > max_chars:
        role_context = dict(trimmed.get("role_context") or {})
        role_context = _thin_role_context(role_context, max_selected=8)
        trimmed["role_context"] = role_context
    if _packet_size(trimmed) > max_chars:
        repo_map = dict(trimmed.get("repo_map") or {})
        repo_map["files"] = (repo_map.get("files") or [])[:12]
        repo_map["symbols"] = (repo_map.get("symbols") or [])[:8]
        trimmed["repo_map"] = repo_map
        trimmed["relevant_snippets"] = []
        retrieval = dict(trimmed.get("tier3_retrieval_index") or {})
        retrieval["repo_files"] = repo_map["files"]
        retrieval["symbols"] = repo_map["symbols"]
        retrieval["snippet_refs"] = []
        retrieval["omitted_role_segments"] = (retrieval.get("omitted_role_segments") or [])[:8]
        trimmed["tier3_retrieval_index"] = retrieval
        trimmed["role_context"] = _thin_role_context(dict(trimmed.get("role_context") or {}), max_selected=4)
    trimmed.setdefault("budget", {})["truncated"] = True
    return trimmed


def _packet_size(packet: dict[str, Any]) -> int:
    try:
        return len(json.dumps(packet, sort_keys=True, default=str))
    except TypeError:
        return len(repr(packet))


def _thin_environment_summary(summary: dict[str, Any]) -> dict[str, Any]:
    wsl = summary.get("wsl") if isinstance(summary.get("wsl"), dict) else {}
    return {
        "capability_tier": summary.get("capability_tier"),
        "available_tools": (summary.get("available_tools") or [])[:40],
        "flow_availability": summary.get("flow_availability") or {},
        "pdks": (summary.get("pdks") or [])[:8],
        "capability_index": {
            "schema_version": (summary.get("capability_index") or {}).get("schema_version"),
            "summary": (summary.get("capability_index") or {}).get("summary") or {},
            "toolchain_stage_summary": (summary.get("capability_index") or {}).get("toolchain_stage_summary") or {},
        },
        "wsl": {
            "available": bool(wsl.get("available")),
            "distros": (wsl.get("distros") or [])[:8],
            "wsl_tools": (wsl.get("wsl_tools") or [])[:60],
            "wsl_capabilities": wsl.get("wsl_capabilities") or {},
        },
        "missing": (summary.get("missing") or [])[:6],
    }


def _thin_artifact_index(index: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(index, dict):
        return {}
    return {
        "schema_version": index.get("schema_version"),
        "current_by_key_count": len(index.get("current_by_key") or {}),
        "entry_count": len(index.get("entries") or {}),
        "superseded_count": len(index.get("superseded") or {}),
        "recent_revisions": (index.get("revisions") or [])[-6:],
    }


def _thin_role_context(role_context: dict[str, Any], max_selected: int) -> dict[str, Any]:
    selected = []
    for item in (role_context.get("selected_segments") or [])[:max_selected]:
        if not isinstance(item, dict):
            continue
        selected.append({
            "segment_id": item.get("segment_id"),
            "kind": item.get("kind"),
            "owner": item.get("owner"),
            "path": item.get("path"),
            "summary": item.get("summary"),
            "token_estimate": item.get("token_estimate"),
        })
    return {
        "schema_version": role_context.get("schema_version"),
        "design_name": role_context.get("design_name"),
        "role": role_context.get("role"),
        "workspace_digest": role_context.get("workspace_digest"),
        "budget": role_context.get("budget") or {},
        "selected_segments": selected,
        "omitted": (role_context.get("omitted") or [])[:12],
        "cache_stats": role_context.get("cache_stats") or {},
        "truncated_for_prompt": True,
    }
