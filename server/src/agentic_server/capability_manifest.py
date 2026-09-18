from __future__ import annotations

import glob
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any


CAPABILITY_MANIFEST_VERSION = "agentic.capability_manifest.v1"

MANIFEST_ENVS = (
    "AGENTIC_CAPABILITY_MANIFEST",
    "AGENTIC_CAPABILITY_MANIFESTS",
    "AGENTIC_PDK_MANIFEST_FILE",
    "AGENTIC_PDK_MANIFESTS",
)

VIEW_ALIASES = {
    "lib": "liberty",
    "libs": "liberty",
    "nldm": "liberty",
    "ccs": "liberty",
    "timing": "liberty",
    "lef": "lef",
    "tech_lef": "tech_lef",
    "tlef": "tech_lef",
    "gds": "gds",
    "oas": "oas",
    "oasis": "oas",
    "verilog": "verilog",
    "rtl": "verilog",
    "simulation": "verilog",
    "spice": "spice",
    "cdl": "spice",
    "sdc": "sdc",
    "drc": "drc",
    "drc_decks": "drc",
    "lvs": "lvs",
    "lvs_decks": "lvs",
    "qrc": "qrc",
    "rc": "qrc",
    "milkyway": "milkyway",
    "ndm": "ndm",
    "db": "db",
}


def manifest_env_names() -> tuple[str, ...]:
    return MANIFEST_ENVS


def load_capability_manifests() -> dict[str, Any]:
    """Load optional user/company PDK/IP/tool manifests.

    A manifest is not trusted blindly: every path/glob is expanded locally and
    unresolved collateral is reported in validation_errors. This lets AgentIC be
    vendor/private-PDK aware without baking private naming conventions into code.
    """

    files = _manifest_files()
    result = {
        "schema_version": CAPABILITY_MANIFEST_VERSION,
        "generated_at": time.time(),
        "files": [],
        "pdks": [],
        "toolchains": [],
        "validation_errors": [],
    }
    for file_name in files:
        file_record = {"path": file_name, "loaded": False, "errors": []}
        try:
            raw = json.loads(Path(file_name).read_text(encoding="utf-8"))
            file_record["loaded"] = True
        except Exception as exc:
            file_record["errors"].append(str(exc))
            result["validation_errors"].append({"file": file_name, "error": str(exc)})
            result["files"].append(file_record)
            continue
        base = Path(file_name).resolve().parent
        normalized_pdks = []
        for pdk in _as_list(_items(raw, "pdks", "pdk")):
            normalized = _normalize_pdk_manifest(pdk, base, file_name)
            normalized_pdks.append(normalized)
            result["pdks"].append(normalized)
            result["validation_errors"].extend(normalized.get("validation_errors") or [])
        pdk_roots = {
            str(pdk.get("name")): Path(str(pdk.get("path"))).resolve()
            for pdk in normalized_pdks
            if pdk.get("name") and pdk.get("path") and Path(str(pdk.get("path"))).is_dir()
        }
        for toolchain in _as_list(_items(raw, "toolchains", "toolchain", "flows")):
            normalized = _normalize_toolchain_manifest(toolchain, base, file_name, pdk_roots)
            result["toolchains"].append(normalized)
            result["validation_errors"].extend(normalized.get("validation_errors") or [])
        result["files"].append(file_record)
    return result


def _manifest_files() -> list[str]:
    found: list[str] = []
    for env_name in MANIFEST_ENVS:
        for raw in os.environ.get(env_name, "").split(os.pathsep):
            stripped = raw.strip()
            if not stripped:
                continue
            expanded = os.path.expandvars(os.path.expanduser(stripped))
            matches = glob.glob(expanded)
            for item in matches or [expanded]:
                path = Path(item).resolve()
                if path.is_file() and path.suffix.lower() in {".json", ".jsonc"}:
                    found.append(str(path))
    return sorted(dict.fromkeys(found))


def _normalize_pdk_manifest(raw: dict[str, Any], base: Path, source_file: str) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    name = str(raw.get("name") or raw.get("id") or raw.get("pdk") or "").strip()
    root = _resolve_root(raw.get("root") or raw.get("path") or raw.get("pdk_root"), base)
    errors = []
    if not name:
        name = _fallback_name(root, source_file)
        errors.append(_error(source_file, "pdk.name", "PDK manifest missing explicit name; generated a stable fallback."))
    if not root or not root.is_dir():
        errors.append(_error(source_file, f"pdk.{name}.root", f"PDK root does not exist: {raw.get('root') or raw.get('path') or raw.get('pdk_root')}"))

    views = _resolve_view_map(raw.get("views") or {}, root or base, source_file, f"pdk.{name}.views")
    drc = _view_paths(views, "drc") or _resolve_patterns(raw.get("drc_decks"), root or base, source_file, f"pdk.{name}.drc_decks")["paths"]
    lvs = _view_paths(views, "lvs") or _resolve_patterns(raw.get("lvs_decks"), root or base, source_file, f"pdk.{name}.lvs_decks")["paths"]
    routing_layers = _as_list(raw.get("routing_layers") or raw.get("metals") or [])
    timing_corners = _normalize_timing_corners(raw.get("timing_corners") or raw.get("corners") or [], views)
    memory_macros = [
        _normalize_memory_macro(item, root or base, source_file, name)
        for item in _as_list(raw.get("memory_macros") or raw.get("memories") or raw.get("srams") or [])
    ]
    pad_cells = [
        _normalize_pad_cell(item, root or base, source_file, name)
        for item in _as_list(raw.get("pad_cells") or raw.get("pads") or [])
    ]
    for item in [*memory_macros, *pad_cells]:
        errors.extend(item.get("validation_errors") or [])

    readiness = _readiness_from_declared_views(views, drc, lvs)
    return {
        "source": source_file,
        "name": name,
        "family": raw.get("family") or raw.get("node") or name,
        "node_nm": _int_or_none(raw.get("node_nm") or raw.get("node")),
        "class": raw.get("class") or raw.get("type") or "commercial_or_custom",
        "path": str(root) if root else None,
        "active_scl": raw.get("active_scl") or raw.get("std_cell_library"),
        "views": views,
        "timing_corners": timing_corners,
        "memory_macros": memory_macros,
        "pad_cells": pad_cells,
        "physical_decks": {
            "drc_deck_count": len(drc),
            "lvs_deck_count": len(lvs),
            "routing_layers": routing_layers[:64],
        },
        "readiness": readiness,
        "validation_errors": errors,
    }


def _normalize_memory_macro(raw: dict[str, Any], root: Path, source_file: str, pdk_name: str) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {"name": str(raw)}
    name = str(raw.get("name") or raw.get("macro") or raw.get("cell") or "").strip()
    errors = []
    if not name:
        name = "declared_memory_" + str(abs(hash(json.dumps(raw, sort_keys=True, default=str))))[:8]
        errors.append(_error(source_file, f"pdk.{pdk_name}.memory", "Memory macro missing explicit name; generated fallback."))
    views = _resolve_view_map(raw.get("views") or raw.get("files") or {}, root, source_file, f"pdk.{pdk_name}.memory.{name}.views")
    width = _int_or_none(raw.get("width_bits") or raw.get("width") or raw.get("word_width"))
    depth = _int_or_none(raw.get("depth_words") or raw.get("depth") or raw.get("words"))
    dims = _dims_from_name(name)
    width = width or dims.get("width_bits")
    depth = depth or dims.get("depth_words")
    confidence = _macro_confidence(views)
    if not views:
        errors.append(_error(source_file, f"pdk.{pdk_name}.memory.{name}.views", "No local memory macro views resolved."))
    return {
        "source": source_file,
        "name": name,
        "pdk": pdk_name,
        "kind": raw.get("kind") or raw.get("type") or "sram",
        "width_bits": width,
        "depth_words": depth,
        "capacity_bits": _capacity_bits(width, depth),
        "ports": raw.get("ports") or raw.get("port_type") or "unspecified",
        "pins": _as_list(raw.get("pins") or []),
        "views": views,
        "size": raw.get("size") or {},
        "confidence": confidence,
        "binding_contract": {
            "requires_wrapper": bool(raw.get("requires_wrapper", True)),
            "must_match_clocking": True,
            "must_match_byte_write_mask": bool(raw.get("byte_write_mask") or raw.get("byte_enable")),
            "source": "capability_manifest",
        },
        "validation_errors": errors,
    }


def _normalize_pad_cell(raw: dict[str, Any], root: Path, source_file: str, pdk_name: str) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {"name": str(raw)}
    name = str(raw.get("name") or raw.get("cell") or "").strip()
    errors = []
    if not name:
        name = "declared_pad_" + str(abs(hash(json.dumps(raw, sort_keys=True, default=str))))[:8]
        errors.append(_error(source_file, f"pdk.{pdk_name}.pad", "Pad cell missing explicit name; generated fallback."))
    views = _resolve_view_map(raw.get("views") or raw.get("files") or {}, root, source_file, f"pdk.{pdk_name}.pad.{name}.views")
    return {
        "source": source_file,
        "name": name,
        "kind": raw.get("kind") or raw.get("type") or "pad_candidate",
        "pins": _as_list(raw.get("pins") or []),
        "views": views,
        "confidence": _macro_confidence(views, required=("lef",)),
        "validation_errors": errors,
    }


def _normalize_toolchain_manifest(raw: dict[str, Any], base: Path, source_file: str, pdk_roots: dict[str, Path] | None = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    name = str(raw.get("name") or raw.get("id") or "declared_toolchain").strip()
    pdk_name = str(raw.get("pdk") or "").strip()
    default_root = (pdk_roots or {}).get(pdk_name) or base
    root = _resolve_root(raw.get("root") or raw.get("path"), default_root) or default_root
    stages = {}
    errors = []
    for stage, spec in (raw.get("stages") or {}).items():
        if not isinstance(spec, dict):
            continue
        tool = str(spec.get("tool") or spec.get("adapter") or "").strip()
        commands = _as_list(spec.get("commands") or ([tool] if tool else []))
        scripts = _resolve_patterns(spec.get("script") or spec.get("scripts"), root, source_file, f"toolchain.{name}.{stage}.scripts")
        decks = _resolve_patterns(spec.get("deck") or spec.get("decks") or spec.get("drc_deck") or spec.get("lvs_deck"), root, source_file, f"toolchain.{name}.{stage}.decks")
        errors.extend(scripts.get("errors") or [])
        errors.extend(decks.get("errors") or [])
        available_commands = [command for command in commands if shutil.which(str(command))]
        license_env = _as_list(spec.get("license_env") or raw.get("license_env") or [])
        stages[str(stage)] = {
            "tool": tool or None,
            "adapter": spec.get("adapter") or tool or None,
            "vendor": spec.get("vendor") or raw.get("vendor") or "declared",
            "openness": spec.get("openness") or raw.get("openness") or "proprietary",
            "commands": commands,
            "available": bool(available_commands) if commands else bool(spec.get("available")),
            "available_commands": available_commands,
            "license_env": {env: bool(os.environ.get(str(env))) for env in license_env},
            "license_hint_present": any(os.environ.get(str(env)) for env in license_env) if license_env else spec.get("license_hint_present", True),
            "scripts": scripts["paths"],
            "decks": decks["paths"],
            "config_contract": spec.get("config_contract") or {},
        }
    return {
        "source": source_file,
        "name": name,
        "pdk": raw.get("pdk"),
        "vendor": raw.get("vendor") or "declared",
        "root": str(root) if root else None,
        "stages": stages,
        "validation_errors": errors,
    }


def _resolve_view_map(raw_views: dict[str, Any], root: Path, source_file: str, ref: str) -> dict[str, list[str]]:
    views: dict[str, list[str]] = {}
    if not isinstance(raw_views, dict):
        return views
    for raw_name, patterns in raw_views.items():
        view = VIEW_ALIASES.get(str(raw_name).strip().lower(), str(raw_name).strip().lower())
        resolved = _resolve_patterns(patterns, root, source_file, f"{ref}.{view}")
        if resolved["paths"]:
            views.setdefault(view, [])
            views[view].extend(resolved["paths"])
    return {view: sorted(dict.fromkeys(paths)) for view, paths in views.items()}


def _resolve_patterns(patterns: Any, root: Path, source_file: str, ref: str) -> dict[str, Any]:
    paths = []
    errors = []
    for pattern in _as_list(patterns):
        raw = str(pattern).strip()
        if not raw:
            continue
        expanded = os.path.expandvars(os.path.expanduser(raw))
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = root / expanded
        matches = glob.glob(str(candidate), recursive=True)
        existing = [str(Path(item).resolve()) for item in matches if Path(item).is_file()]
        if not existing and Path(candidate).is_file():
            existing = [str(candidate.resolve())]
        if not existing:
            errors.append(_error(source_file, ref, f"No files matched pattern: {raw}"))
        paths.extend(existing)
    return {"paths": sorted(dict.fromkeys(paths))[:256], "errors": errors}


def _normalize_timing_corners(raw: Any, views: dict[str, list[str]]) -> list[dict[str, Any]]:
    corners = []
    for item in _as_list(raw):
        if isinstance(item, dict):
            corners.append({"name": item.get("name") or item.get("corner") or "declared", "liberty_count": len(_as_list(item.get("liberty") or item.get("libs") or [])), "sample_files": _as_list(item.get("liberty") or item.get("libs") or [])[:8]})
        else:
            corners.append({"name": str(item), "liberty_count": 0, "sample_files": []})
    if not corners and views.get("liberty"):
        corners.append({"name": "declared", "liberty_count": len(views["liberty"]), "sample_files": views["liberty"][:8]})
    return corners[:96]


def _readiness_from_declared_views(views: dict[str, list[str]], drc: list[str], lvs: list[str]) -> dict[str, Any]:
    has_liberty = bool(views.get("liberty"))
    has_lef = bool(views.get("lef") or views.get("tech_lef"))
    can_harden = has_liberty and has_lef
    signoff_ready = can_harden and bool(drc or lvs)
    return {
        "tier": "layout_signoff_candidate" if signoff_ready else "layout_generation_candidate" if can_harden else "synthesis_only" if has_liberty else "declared_metadata_only",
        "can_synthesize": has_liberty,
        "can_harden": can_harden,
        "signoff_decks_present": bool(drc or lvs),
    }


def _macro_confidence(views: dict[str, list[str]], required: tuple[str, ...] = ("lef", "liberty")) -> str:
    if all(views.get(view) for view in required) and views.get("verilog") and (views.get("gds") or views.get("oas")):
        return "implementation_ready"
    if all(views.get(view) for view in required):
        return "physical_timing_ready"
    if views.get("lef") or views.get("liberty"):
        return "partial_collateral"
    return "name_only"


def _dims_from_name(name: str) -> dict[str, int | None]:
    lowered = name.lower()
    match = re.search(r"(?:^|[_-])(\d+)x(\d+)(?:[_-]|$)", lowered)
    if not match:
        match = re.search(r"(\d+)x(\d+)", lowered)
    if not match:
        return {"depth_words": None, "width_bits": None}
    first = int(match.group(1))
    second = int(match.group(2))
    if first <= 256 and second >= first:
        return {"depth_words": second, "width_bits": first}
    return {"depth_words": first, "width_bits": second}


def _resolve_root(value: Any, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    expanded = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve()


def _items(raw: Any, *keys: str) -> Any:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    for key in keys:
        if key in raw:
            return raw[key]
    return []


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _view_paths(views: dict[str, list[str]], key: str) -> list[str]:
    return list(views.get(key) or [])


def _capacity_bits(width: int | None, depth: int | None) -> int | None:
    return width * depth if width and depth else None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None
    except (TypeError, ValueError):
        return None


def _fallback_name(root: Path | None, source_file: str) -> str:
    if root:
        return root.name or "declared_pdk"
    return Path(source_file).stem or "declared_pdk"


def _error(source_file: str, ref: str, message: str) -> dict[str, str]:
    return {"source": source_file, "ref": ref, "message": message}
