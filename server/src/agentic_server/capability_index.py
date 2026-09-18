from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any


CAPABILITY_INDEX_VERSION = "agentic.capability_index.v1"
MAX_PDKS = 12
MAX_FILES_PER_VIEW = 160
MAX_MACROS_PER_PDK = 240
MAX_STDCELLS_PER_PDK = 320
MAX_PAD_CELLS_PER_PDK = 160

VIEW_PATTERNS = {
    "lef": ("*.lef", "*.tlef"),
    "liberty": ("*.lib", "*.lib.gz"),
    "gds": ("*.gds", "*.gds.gz", "*.oas", "*.oas.gz"),
    "verilog": ("*.v", "*.sv", "*.vh", "*.svh"),
    "spice": ("*.sp", "*.spice", "*.cdl", "*.cir", "*.pex"),
    "sdc": ("*.sdc",),
}

MEMORY_NAME_RE = re.compile(
    r"(?i)\b("
    r"sram|spram|sp_ram|1rw|1r1w|2rw|dp_ram|dpram|rf_|regfile|rom|bootrom|openram|sky130_sram|gf180.*sram"
    r")\b|(?:^|[_-])(?:\d+)x(?:\d+)(?:[_-]|$)"
)
PAD_NAME_RE = re.compile(r"(?i)\b(pad|gpio|vdd|vss|dvdd|dvss|iovdd|iovss|corner|esd|clamp)\b")
TIE_NAME_RE = re.compile(r"(?i)\b(tiehi|tielo|tieh|tiel|conb)\b")
FILL_NAME_RE = re.compile(r"(?i)\b(fill|filler|decap|tap|endcap|welltap)\b")
CLOCK_GATE_RE = re.compile(r"(?i)\b(clk.?gate|clock.?gate|dlclkp|clkgate|icg)\b")


def build_capability_index(
    *,
    pdk_index: dict[str, Any],
    tools: dict[str, bool],
    flows: dict[str, Any],
    adapter_matrix: dict[str, Any],
) -> dict[str, Any]:
    """Build a bounded local evidence index for VLSI planning.

    This is intentionally a capability index, not a flow decision. It records
    what local files/tools prove. Resolvers should bind to these entries later.
    """

    pdk_entries = []
    for pdk in list(pdk_index.get("pdks") or [])[:MAX_PDKS]:
        try:
            pdk_entries.append(_index_pdk_capabilities(pdk))
        except Exception as exc:
            pdk_entries.append({
                "pdk": _compact_pdk_identity(pdk),
                "error": str(exc),
                "memory_macros": [],
                "pad_cells": [],
                "stdcell_capabilities": {},
                "timing_corners": [],
            })
    toolchains = _toolchain_capabilities(tools, flows, adapter_matrix)
    return {
        "schema_version": CAPABILITY_INDEX_VERSION,
        "generated_at": time.time(),
        "toolchains": toolchains,
        "pdks": pdk_entries,
        "summary": _summary(pdk_entries, toolchains),
    }


def _index_pdk_capabilities(pdk: dict[str, Any]) -> dict[str, Any]:
    pdk_path = Path(str(pdk.get("path") or "")).resolve()
    views = _collect_views(pdk_path)
    lef_macros = _parse_lef_macros(views["lef"])
    liberty_cells = _parse_liberty_cells(views["liberty"])
    collateral = _collateral_by_stem(views)
    memory_macros = _memory_macros(lef_macros, liberty_cells, collateral, pdk_path)
    pad_cells = _pad_cells(lef_macros, liberty_cells, collateral, pdk_path)
    stdcells = _stdcell_capabilities(lef_macros, liberty_cells)
    return {
        "pdk": _compact_pdk_identity(pdk),
        "view_counts": {view: len(paths) for view, paths in views.items()},
        "timing_corners": _timing_corners(views["liberty"]),
        "memory_macros": memory_macros[:MAX_MACROS_PER_PDK],
        "pad_cells": pad_cells[:MAX_PAD_CELLS_PER_PDK],
        "stdcell_capabilities": stdcells,
        "physical_decks": {
            "drc_deck_count": ((pdk.get("tech") or {}).get("drc_deck_count") or 0),
            "lvs_deck_count": ((pdk.get("tech") or {}).get("lvs_deck_count") or 0),
            "routing_layers": ((pdk.get("tech") or {}).get("sample_routing_layers") or [])[:32],
        },
        "readiness": pdk.get("readiness") or {},
    }


def _compact_pdk_identity(pdk: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": pdk.get("name"),
        "family": pdk.get("family"),
        "node_nm": pdk.get("node_nm"),
        "class": pdk.get("class"),
        "path": pdk.get("path"),
        "preferred_open_flow": pdk.get("preferred_open_flow"),
        "active_scl": ((pdk.get("openlane") or {}).get("active_scl")),
    }


def _collect_views(root: Path) -> dict[str, list[str]]:
    views: dict[str, list[str]] = {view: [] for view in VIEW_PATTERNS}
    if not root.is_dir():
        return views
    skip = {".git", "__pycache__", "runs", "tmp", "node_modules", ".pytest_cache"}
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - len(root.parts)
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
        if depth > 8:
            dirs[:] = []
            continue
        for name in files:
            lower = name.lower()
            for view, patterns in VIEW_PATTERNS.items():
                if len(views[view]) >= MAX_FILES_PER_VIEW:
                    continue
                if any(_matches_pattern(lower, pattern) for pattern in patterns):
                    views[view].append(str(current_path / name))
    for view in views:
        views[view] = sorted(dict.fromkeys(views[view]))
    return views


def _matches_pattern(name: str, pattern: str) -> bool:
    suffix = pattern.replace("*", "")
    return name.endswith(suffix.lower())


def _parse_lef_macros(paths: list[str]) -> dict[str, dict[str, Any]]:
    macros: dict[str, dict[str, Any]] = {}
    for file_name in paths:
        try:
            with open(file_name, "r", errors="ignore") as fh:
                current: dict[str, Any] | None = None
                for line in fh:
                    macro = re.match(r"\s*MACRO\s+([A-Za-z0-9_.$:-]+)", line)
                    if macro:
                        name = macro.group(1)
                        current = {
                            "name": name,
                            "lef": file_name,
                            "class": None,
                            "size": None,
                            "pins": [],
                        }
                        macros.setdefault(name, current)
                        continue
                    if not current:
                        continue
                    klass = re.match(r"\s*CLASS\s+([^;]+)", line)
                    if klass:
                        current["class"] = klass.group(1).strip()
                    size = re.match(r"\s*SIZE\s+([\d.]+)\s+BY\s+([\d.]+)", line)
                    if size:
                        current["size"] = {"width": float(size.group(1)), "height": float(size.group(2))}
                    pin = re.match(r"\s*PIN\s+([A-Za-z0-9_.$:-]+)", line)
                    if pin and len(current["pins"]) < 80:
                        current["pins"].append(pin.group(1))
                    if re.match(r"\s*END\s+" + re.escape(str(current["name"])) + r"\b", line):
                        current = None
        except OSError:
            continue
    return macros


def _parse_liberty_cells(paths: list[str]) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = {}
    for file_name in paths:
        try:
            with open(file_name, "r", errors="ignore") as fh:
                for line in fh:
                    match = re.match(r"\s*cell\s*\(\s*([A-Za-z0-9_.$:-]+)\s*\)", line)
                    if match:
                        name = match.group(1)
                        entry = cells.setdefault(name, {"name": name, "liberty": [], "corners": set()})
                        entry["liberty"].append(file_name)
                        corner = _corner_from_liberty_path(file_name)
                        if corner:
                            entry["corners"].add(corner)
        except OSError:
            continue
    for entry in cells.values():
        entry["corners"] = sorted(entry["corners"])
        entry["liberty"] = sorted(dict.fromkeys(entry["liberty"]))[:12]
    return cells


def _collateral_by_stem(views: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    collateral: dict[str, dict[str, list[str]]] = {}
    for view, paths in views.items():
        for file_name in paths:
            path = Path(file_name)
            stems = _candidate_stems(path)
            for stem in stems:
                bucket = collateral.setdefault(stem.lower(), {})
                bucket.setdefault(view, []).append(file_name)
    for bucket in collateral.values():
        for view, paths in bucket.items():
            bucket[view] = sorted(dict.fromkeys(paths))[:16]
    return collateral


def _candidate_stems(path: Path) -> list[str]:
    name = path.name
    for suffix in (".lib.gz", ".gds.gz", ".oas.gz"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    else:
        name = path.stem
    parts = [name]
    if "__" in name:
        parts.append(name.split("__", 1)[0])
    if "." in name:
        parts.append(name.split(".", 1)[0])
    return [part for part in parts if part]


def _memory_macros(
    lef_macros: dict[str, dict[str, Any]],
    liberty_cells: dict[str, dict[str, Any]],
    collateral: dict[str, dict[str, list[str]]],
    pdk_path: Path,
) -> list[dict[str, Any]]:
    candidates = set()
    for name in list(lef_macros) + list(liberty_cells):
        if MEMORY_NAME_RE.search(name):
            candidates.add(name)
    for stem in collateral:
        if MEMORY_NAME_RE.search(stem):
            candidates.add(stem)
    out = []
    for name in sorted(candidates):
        evidence = _views_for_name(name, lef_macros, liberty_cells, collateral, pdk_path)
        dims = _memory_dimensions(name, evidence.get("pins", []))
        confidence = _macro_confidence(evidence, required=("lef", "liberty"))
        out.append({
            "name": name,
            "kind": _memory_kind(name),
            "width_bits": dims.get("width_bits"),
            "depth_words": dims.get("depth_words"),
            "ports": evidence.get("pins", [])[:80],
            "views": evidence.get("views", {}),
            "confidence": confidence,
            "binding_contract": {
                "requires_wrapper": True,
                "must_match_clocking": True,
                "must_match_byte_write_mask": _has_write_mask(evidence.get("pins", [])),
                "source": "local_pdk_collateral",
            },
        })
    return sorted(out, key=lambda item: (item["confidence"], bool(item["width_bits"]), item["name"]), reverse=True)


def _pad_cells(
    lef_macros: dict[str, dict[str, Any]],
    liberty_cells: dict[str, dict[str, Any]],
    collateral: dict[str, dict[str, list[str]]],
    pdk_path: Path,
) -> list[dict[str, Any]]:
    candidates = set()
    for name, macro in lef_macros.items():
        klass = str(macro.get("class") or "")
        if "PAD" in klass.upper() or PAD_NAME_RE.search(name):
            candidates.add(name)
    for name in liberty_cells:
        if PAD_NAME_RE.search(name):
            candidates.add(name)
    out = []
    for name in sorted(candidates):
        evidence = _views_for_name(name, lef_macros, liberty_cells, collateral, pdk_path)
        out.append({
            "name": name,
            "kind": _pad_kind(name, str((lef_macros.get(name) or {}).get("class") or "")),
            "pins": evidence.get("pins", [])[:64],
            "views": evidence.get("views", {}),
            "confidence": _macro_confidence(evidence, required=("lef",)),
        })
    return sorted(out, key=lambda item: (item["confidence"], item["name"]), reverse=True)


def _stdcell_capabilities(lef_macros: dict[str, dict[str, Any]], liberty_cells: dict[str, dict[str, Any]]) -> dict[str, Any]:
    names = sorted(set(lef_macros) | set(liberty_cells))
    sampled = names[:MAX_STDCELLS_PER_PDK]
    buckets = {
        "tie_cells": [name for name in sampled if TIE_NAME_RE.search(name)],
        "fill_tap_decap_cells": [name for name in sampled if FILL_NAME_RE.search(name)],
        "clock_gates": [name for name in sampled if CLOCK_GATE_RE.search(name)],
        "flops": [name for name in sampled if re.search(r"(?i)\b(dff|dfxtp|sdff|flop)\b", name)],
        "level_shifters": [name for name in sampled if re.search(r"(?i)(level|ls|shifter)", name)],
        "isolation_cells": [name for name in sampled if re.search(r"(?i)(isolation|iso)", name)],
    }
    return {
        "cell_count_sample": len(sampled),
        "lef_macro_count": len(lef_macros),
        "liberty_cell_count": len(liberty_cells),
        **{key: value[:48] for key, value in buckets.items()},
    }


def _views_for_name(
    name: str,
    lef_macros: dict[str, dict[str, Any]],
    liberty_cells: dict[str, dict[str, Any]],
    collateral: dict[str, dict[str, list[str]]],
    pdk_path: Path,
) -> dict[str, Any]:
    views: dict[str, list[str]] = {}
    macro = lef_macros.get(name)
    if macro:
        views["lef"] = [_relpath(macro["lef"], pdk_path)]
    cell = liberty_cells.get(name)
    if cell:
        views["liberty"] = [_relpath(path, pdk_path) for path in cell.get("liberty", [])]
    for stem in {name.lower(), _strip_corner_suffix(name).lower()}:
        for view, paths in (collateral.get(stem) or {}).items():
            views.setdefault(view, [])
            views[view].extend(_relpath(path, pdk_path) for path in paths)
    return {
        "views": {view: sorted(dict.fromkeys(paths))[:12] for view, paths in views.items()},
        "pins": list((macro or {}).get("pins") or []),
        "class": (macro or {}).get("class"),
        "size": (macro or {}).get("size"),
    }


def _macro_confidence(evidence: dict[str, Any], required: tuple[str, ...]) -> str:
    views = evidence.get("views") or {}
    if all(views.get(view) for view in required) and views.get("verilog"):
        return "implementation_ready"
    if all(views.get(view) for view in required):
        return "physical_timing_ready"
    if views.get("lef") or views.get("liberty"):
        return "partial_collateral"
    return "name_only"


def _memory_dimensions(name: str, pins: list[str]) -> dict[str, int | None]:
    lowered = name.lower()
    match = re.search(r"(?:^|[_-])(\d+)x(\d+)(?:[_-]|$)", lowered)
    if match:
        first = int(match.group(1))
        second = int(match.group(2))
        if first <= 256 and second >= first:
            width = first
            depth = second
        else:
            depth = first
            width = second
        return {"depth_words": depth, "width_bits": width}
    width = None
    depth = None
    for pin in pins:
        p = pin.lower()
        bus = re.search(r"\[(\d+):0\]", p)
        if ("dout" in p or "din" in p or p.startswith("q")) and bus:
            width = max(width or 0, int(bus.group(1)) + 1)
        if ("addr" in p or p.startswith("a")) and bus:
            depth = 2 ** (int(bus.group(1)) + 1)
    return {"depth_words": depth, "width_bits": width}


def _memory_kind(name: str) -> str:
    lowered = name.lower()
    if "rom" in lowered:
        return "rom"
    if "regfile" in lowered or "rf" in lowered:
        return "register_file_macro"
    if "2rw" in lowered or "dpram" in lowered or "dp_ram" in lowered:
        return "dual_port_sram"
    return "sram"


def _pad_kind(name: str, klass: str) -> str:
    lowered = name.lower()
    if "corner" in lowered:
        return "corner_pad"
    if "vdd" in lowered or "vss" in lowered or "power" in lowered or "ground" in lowered:
        return "power_pad"
    if "gpio" in lowered or "io" in lowered or "pad" in lowered or "PAD" in klass.upper():
        return "signal_pad"
    return "pad_candidate"


def _has_write_mask(pins: list[str]) -> bool:
    return any(re.search(r"(?i)\b(wmask|wm|web|byte|be)\b", pin) for pin in pins)


def _timing_corners(liberty_files: list[str]) -> list[dict[str, Any]]:
    corners: dict[str, dict[str, Any]] = {}
    for file_name in liberty_files:
        corner = _corner_from_liberty_path(file_name) or "unknown"
        entry = corners.setdefault(corner, {"name": corner, "liberty_count": 0, "sample_files": []})
        entry["liberty_count"] += 1
        if len(entry["sample_files"]) < 8:
            entry["sample_files"].append(file_name)
    return sorted(corners.values(), key=lambda item: item["name"])[:64]


def _corner_from_liberty_path(file_name: str) -> str:
    name = Path(file_name).name
    name = re.sub(r"\.lib(?:\.gz)?$", "", name, flags=re.I)
    for token in re.split(r"[._-]+", name):
        if re.match(r"(?i)^(tt|ff|ss|sf|fs|typ|slow|fast)\w*$", token):
            return token
    return name[-48:]


def _toolchain_capabilities(tools: dict[str, bool], flows: dict[str, Any], adapter_matrix: dict[str, Any]) -> dict[str, Any]:
    stages = {}
    for stage in ("simulation", "lint", "synthesis", "pnr", "sta", "physical_verification", "power"):
        selected = (adapter_matrix.get(stage) or {}).get("selected")
        stages[stage] = {
            "available": bool((adapter_matrix.get(stage) or {}).get("available")),
            "selected": selected,
            "candidates": (adapter_matrix.get(stage) or {}).get("candidates", [])[:8],
        }
    return {
        "available_commands": sorted([name for name, available in tools.items() if available]),
        "flows": {
            "openlane": _flow_summary(flows.get("openlane") or {}),
            "orfs": _flow_summary(flows.get("orfs") or {}),
            "proprietary": {
                "available": bool((flows.get("proprietary") or {}).get("available")),
                "licensed_backend_available": bool((flows.get("proprietary") or {}).get("licensed_backend_available")),
                "stacks": (flows.get("proprietary") or {}).get("stacks", {}),
            },
        },
        "stages": stages,
    }


def _flow_summary(flow: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": bool(flow.get("available")),
        "path": flow.get("path"),
        "execution_modes": flow.get("execution_modes") or {},
        "platforms": (flow.get("platforms") or [])[:40],
        "commands": flow.get("commands") or {},
    }


def _summary(pdk_entries: list[dict[str, Any]], toolchains: dict[str, Any]) -> dict[str, Any]:
    memory_count = sum(len(entry.get("memory_macros") or []) for entry in pdk_entries)
    pad_count = sum(len(entry.get("pad_cells") or []) for entry in pdk_entries)
    implementation_ready_memories = sum(
        1
        for entry in pdk_entries
        for macro in (entry.get("memory_macros") or [])
        if macro.get("confidence") in {"implementation_ready", "physical_timing_ready"}
    )
    return {
        "pdk_count": len(pdk_entries),
        "memory_macro_count": memory_count,
        "implementation_ready_memory_macro_count": implementation_ready_memories,
        "pad_cell_count": pad_count,
        "available_stage_count": sum(1 for stage in (toolchains.get("stages") or {}).values() if stage.get("available")),
    }


def _strip_corner_suffix(name: str) -> str:
    return re.sub(r"(?i)[._-](tt|ff|ss|sf|fs|typ|slow|fast).*$", "", name)


def _relpath(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except Exception:
        return str(path)
