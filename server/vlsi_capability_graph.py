from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Any


CAPABILITY_GRAPH_VERSION = "agentic.vlsi_capability_graph.v1"

CONFIDENCE_SCORE = {
    "implementation_ready": 100,
    "physical_timing_ready": 82,
    "partial_collateral": 46,
    "name_only": 10,
    "selected_tool": 70,
    "unknown": 0,
}

MEMORY_VIEW_CONTRACT = ("lef", "liberty")
PHYSICAL_VIEW_CONTRACT = ("lef", "liberty", "gds")
SIM_VIEW_CONTRACT = ("verilog",)


def build_capability_graph(env: dict[str, Any]) -> dict[str, Any]:
    """Normalize detected PDK/IP/tool evidence into a queryable graph.

    The graph is intentionally evidence-first. It does not decide that a flow is
    valid because a node name sounds familiar; it records the files, adapters,
    decks, and views that are actually visible in the user's environment.
    """

    graph = {
        "schema_version": CAPABILITY_GRAPH_VERSION,
        "generated_at": time.time(),
        "nodes": {},
        "edges": [],
        "indexes": {
            "pdks": {},
            "memory_macros": {},
            "pad_cells": {},
            "tool_stages": {},
            "flows": {},
            "timing_corners": {},
            "signoff_decks": {},
            "declared_toolchains": {},
        },
        "manifest_status": {
            "files": [],
            "validation_errors": [],
        },
        "summary": {},
    }
    _add_toolchain_nodes(graph, env.get("capability_index") or {}, env.get("tool_adapters") or {})
    _add_flow_nodes(graph, env.get("flows") or {})
    _add_pdk_nodes(graph, env.get("capability_index") or {})
    _add_manifest_nodes(graph, env.get("capability_manifests") or {})
    graph["summary"] = _graph_summary(graph)
    return graph


def bind_memory_requirement(
    requirement: dict[str, Any],
    graph: dict[str, Any],
    *,
    target_pdk: str | None = None,
    require_physical: bool = True,
) -> dict[str, Any]:
    """Bind an abstract memory request to locally indexed macro evidence."""

    candidates = []
    for node_id in (graph.get("indexes") or {}).get("memory_macros", {}).values():
        node = (graph.get("nodes") or {}).get(node_id) or {}
        if target_pdk and _norm(node.get("pdk")) != _norm(target_pdk):
            continue
        scored = _score_memory_candidate(requirement, node, require_physical=require_physical)
        if scored["score"] <= 0:
            continue
        candidates.append(scored)
    candidates.sort(key=lambda item: (item["score"], item["fit"].get("waste_bits") is not None and -item["fit"].get("waste_bits", 0)), reverse=True)

    blockers = []
    if not candidates:
        blockers.append("No local memory macro matched the requested capacity, width/depth, port style, and collateral requirements.")
    selected = candidates[0] if candidates else None
    if selected and selected["fit"].get("requires_tiling"):
        blockers.append("Selected memory requires generated tiling/wrapper logic before RTL and physical implementation are safe.")
    if selected and selected["confidence"] not in {"implementation_ready", "physical_timing_ready"}:
        blockers.append("Selected memory lacks complete physical/timing collateral for implementation-ready binding.")

    return {
        "status": "selected" if selected and not blockers else "candidate" if selected else "unbound",
        "requirement": _compact_requirement(requirement),
        "selected": selected,
        "alternatives": candidates[1:8],
        "blockers": blockers,
        "integration_contract": _memory_integration_contract(requirement, selected),
    }


def assess_design_readiness(intent: dict[str, Any] | None, graph: dict[str, Any]) -> dict[str, Any]:
    """Evaluate whether discovered capabilities satisfy a design intent."""

    intent = intent or {}
    deliverables = set((intent.get("target") or {}).get("deliverables") or [])
    if not deliverables:
        deliverables = {"rtl", "testbench", "simulation"}
    memories = intent.get("memories") or {}
    target_pdk = ((intent.get("target") or {}).get("pdk") or "").strip() or None
    gates = []

    gates.append(_stage_gate(graph, "simulation", "rtl_simulation", required=bool(deliverables & {"testbench", "simulation", "rtl"})))
    gates.append(_stage_gate(graph, "synthesis", "logic_synthesis", required=bool(deliverables & {"synthesis", "netlist", "pnr", "gdsii", "signoff_reports"})))
    gates.append(_stage_gate(graph, "pnr", "physical_implementation", required=bool(deliverables & {"pnr", "gdsii", "layout", "hardening"})))
    gates.append(_stage_gate(graph, "sta", "timing_analysis", required=bool(deliverables & {"sta", "timing", "gdsii", "signoff_reports"})))
    gates.append(_stage_gate(graph, "physical_verification", "physical_verification", required=bool(deliverables & {"drc", "lvs", "gdsii", "signoff_reports"})))
    gates.append(_pdk_view_gate(graph, target_pdk=target_pdk, deliverables=deliverables))
    gates.append(_signoff_deck_gate(graph, target_pdk=target_pdk, deliverables=deliverables))

    memory_bindings = {}
    for name, requirement in memories.items():
        if not isinstance(requirement, dict):
            continue
        binding = bind_memory_requirement(requirement | {"name": requirement.get("name") or name}, graph, target_pdk=target_pdk)
        memory_bindings[name] = binding
        gates.append({
            "name": f"memory_binding.{name}",
            "required": requirement.get("implementation_preference") in {"pdk_macro", "compiler_macro"} or bool(deliverables & {"pnr", "gdsii", "signoff_reports"}),
            "status": "pass" if binding["status"] == "selected" else "warn" if binding["selected"] else "fail",
            "evidence": _binding_evidence(binding),
            "blockers": binding.get("blockers", []),
        })

    required_gates = [gate for gate in gates if gate.get("required")]
    blockers = [blocker for gate in required_gates if gate.get("status") == "fail" for blocker in gate.get("blockers", [])]
    warnings = [blocker for gate in gates if gate.get("status") == "warn" for blocker in gate.get("blockers", [])]
    return {
        "schema_version": "agentic.design_readiness.v1",
        "status": "ready" if not blockers else "blocked",
        "target_pdk": target_pdk,
        "deliverables": sorted(deliverables),
        "gates": gates,
        "memory_bindings": memory_bindings,
        "blockers": blockers,
        "warnings": warnings[:24],
    }


def compact_capability_graph(graph: dict[str, Any]) -> dict[str, Any]:
    summary = graph.get("summary") or {}
    pdks = []
    for pdk_name, node_id in list(((graph.get("indexes") or {}).get("pdks") or {}).items())[:12]:
        node = (graph.get("nodes") or {}).get(node_id) or {}
        pdks.append({
            "name": pdk_name,
            "family": node.get("family"),
            "node_nm": node.get("node_nm"),
            "class": node.get("class"),
            "readiness": node.get("readiness"),
            "memory_macro_count": len(node.get("memory_macros") or []),
            "timing_corner_count": len(node.get("timing_corners") or []),
        })
    return {
        "schema_version": graph.get("schema_version"),
        "summary": summary,
        "pdks": pdks,
        "tool_stage_bindings": {
            stage: _compact_tool_node((graph.get("nodes") or {}).get(node_id) or {})
            for stage, node_id in ((graph.get("indexes") or {}).get("tool_stages") or {}).items()
        },
        "flows": {
            name: ((graph.get("nodes") or {}).get(node_id) or {}).get("available")
            for name, node_id in ((graph.get("indexes") or {}).get("flows") or {}).items()
        },
        "manifest_status": {
            "files": (graph.get("manifest_status") or {}).get("files", [])[:8],
            "validation_error_count": len((graph.get("manifest_status") or {}).get("validation_errors", [])),
        },
    }


def _add_toolchain_nodes(graph: dict[str, Any], capability_index: dict[str, Any], adapter_matrix: dict[str, Any]) -> None:
    stages = ((capability_index.get("toolchains") or {}).get("stages") or {}) or adapter_matrix
    for stage, data in stages.items():
        selected = data.get("selected") or {}
        node = {
            "id": _node_id("tool_stage", stage),
            "kind": "tool_stage",
            "stage": stage,
            "available": bool(data.get("available")),
            "adapter": selected.get("adapter"),
            "vendor": selected.get("vendor"),
            "openness": selected.get("openness"),
            "commands": selected.get("commands") or [],
            "license_hint_present": selected.get("license_hint_present"),
            "candidates": data.get("candidates", [])[:12],
        }
        _put_node(graph, node)
        graph["indexes"]["tool_stages"][stage] = node["id"]


def _add_flow_nodes(graph: dict[str, Any], flows: dict[str, Any]) -> None:
    for name, data in flows.items():
        if not isinstance(data, dict):
            continue
        node = {
            "id": _node_id("flow", name),
            "kind": "flow",
            "name": name,
            "available": bool(data.get("available")),
            "path": data.get("path"),
            "execution_modes": data.get("execution_modes") or {},
            "platforms": data.get("platforms") or [],
            "config_contract": data.get("config_contract") or {},
        }
        _put_node(graph, node)
        graph["indexes"]["flows"][name] = node["id"]


def _add_pdk_nodes(graph: dict[str, Any], capability_index: dict[str, Any]) -> None:
    for entry in capability_index.get("pdks") or []:
        pdk = entry.get("pdk") or {}
        pdk_name = str(pdk.get("name") or "unknown")
        pdk_node = {
            "id": _node_id("pdk", pdk_name),
            "kind": "pdk",
            "name": pdk_name,
            "family": pdk.get("family"),
            "node_nm": pdk.get("node_nm"),
            "class": pdk.get("class"),
            "path": pdk.get("path"),
            "active_scl": pdk.get("active_scl"),
            "readiness": entry.get("readiness") or {},
            "physical_decks": entry.get("physical_decks") or {},
            "memory_macros": [],
            "pad_cells": [],
            "timing_corners": [],
            "stdcell_capabilities": entry.get("stdcell_capabilities") or {},
        }
        _put_node(graph, pdk_node)
        graph["indexes"]["pdks"][pdk_name] = pdk_node["id"]
        _add_timing_corners(graph, pdk_node, entry.get("timing_corners") or [])
        _add_signoff_deck_node(graph, pdk_node)
        for macro in entry.get("memory_macros") or []:
            _add_macro_node(graph, pdk_node, macro)
        for pad in entry.get("pad_cells") or []:
            _add_pad_node(graph, pdk_node, pad)


def _add_manifest_nodes(graph: dict[str, Any], manifests: dict[str, Any]) -> None:
    if not manifests:
        return
    graph["manifest_status"] = {
        "files": manifests.get("files") or [],
        "validation_errors": manifests.get("validation_errors") or [],
    }
    for pdk in manifests.get("pdks") or []:
        _add_declared_pdk_node(graph, pdk)
    for toolchain in manifests.get("toolchains") or []:
        _add_declared_toolchain_node(graph, toolchain)


def _add_declared_pdk_node(graph: dict[str, Any], pdk: dict[str, Any]) -> None:
    pdk_name = str(pdk.get("name") or "declared_pdk")
    existing_id = graph["indexes"]["pdks"].get(pdk_name)
    if existing_id and existing_id in graph["nodes"]:
        pdk_node = graph["nodes"][existing_id]
        pdk_node["declared_source"] = pdk.get("source")
        pdk_node["path"] = pdk.get("path") or pdk_node.get("path")
        pdk_node["readiness"] = _merge_readiness(pdk_node.get("readiness") or {}, pdk.get("readiness") or {})
        pdk_node["physical_decks"] = _merge_decks(pdk_node.get("physical_decks") or {}, pdk.get("physical_decks") or {})
        pdk_node.setdefault("manifest_validation_errors", []).extend(pdk.get("validation_errors") or [])
    else:
        pdk_node = {
            "id": _node_id("pdk", pdk_name),
            "kind": "pdk",
            "name": pdk_name,
            "family": pdk.get("family"),
            "node_nm": pdk.get("node_nm"),
            "class": pdk.get("class") or "commercial_or_custom",
            "path": pdk.get("path"),
            "active_scl": pdk.get("active_scl"),
            "readiness": pdk.get("readiness") or {},
            "physical_decks": pdk.get("physical_decks") or {},
            "memory_macros": [],
            "pad_cells": [],
            "timing_corners": [],
            "stdcell_capabilities": {},
            "declared_source": pdk.get("source"),
            "manifest_validation_errors": pdk.get("validation_errors") or [],
        }
        _put_node(graph, pdk_node)
        graph["indexes"]["pdks"][pdk_name] = pdk_node["id"]
    _add_timing_corners(graph, pdk_node, pdk.get("timing_corners") or [])
    _add_signoff_deck_node(graph, pdk_node)
    for macro in pdk.get("memory_macros") or []:
        _add_macro_node(graph, pdk_node, macro)
    for pad in pdk.get("pad_cells") or []:
        _add_pad_node(graph, pdk_node, pad)


def _add_declared_toolchain_node(graph: dict[str, Any], toolchain: dict[str, Any]) -> None:
    name = str(toolchain.get("name") or "declared_toolchain")
    node = {
        "id": _node_id("declared_toolchain", name),
        "kind": "declared_toolchain",
        "name": name,
        "pdk": toolchain.get("pdk"),
        "vendor": toolchain.get("vendor"),
        "root": toolchain.get("root"),
        "source": toolchain.get("source"),
        "stages": toolchain.get("stages") or {},
        "validation_errors": toolchain.get("validation_errors") or [],
    }
    _put_node(graph, node)
    graph["indexes"]["declared_toolchains"][name] = node["id"]
    for stage, spec in (toolchain.get("stages") or {}).items():
        _merge_declared_stage(graph, stage, spec, node)


def _merge_declared_stage(graph: dict[str, Any], stage: str, spec: dict[str, Any], toolchain_node: dict[str, Any]) -> None:
    if not isinstance(spec, dict):
        return
    existing_id = graph["indexes"]["tool_stages"].get(stage)
    existing = graph["nodes"].get(existing_id or "") if existing_id else None
    declared_available = bool(spec.get("available") or spec.get("scripts") or spec.get("decks"))
    declared_license = spec.get("license_hint_present")
    declared = {
        "adapter": spec.get("adapter") or spec.get("tool"),
        "vendor": spec.get("vendor") or toolchain_node.get("vendor"),
        "openness": spec.get("openness") or "proprietary",
        "commands": spec.get("commands") or [],
        "license_hint_present": declared_license,
        "scripts": spec.get("scripts") or [],
        "decks": spec.get("decks") or [],
        "toolchain": toolchain_node.get("name"),
    }
    if existing:
        existing.setdefault("declared_bindings", []).append(declared)
        if declared_available and not existing.get("available"):
            existing["available"] = True
            existing["adapter"] = declared["adapter"]
            existing["vendor"] = declared["vendor"]
            existing["openness"] = declared["openness"]
            existing["commands"] = declared["commands"]
            existing["license_hint_present"] = declared_license
    else:
        node = {
            "id": _node_id("tool_stage", stage),
            "kind": "tool_stage",
            "stage": stage,
            "available": declared_available,
            **declared,
            "candidates": [],
            "declared_bindings": [declared],
        }
        _put_node(graph, node)
        graph["indexes"]["tool_stages"][stage] = node["id"]
    _edge(graph, toolchain_node["id"], graph["indexes"]["tool_stages"][stage], "declares_stage")


def _add_timing_corners(graph: dict[str, Any], pdk_node: dict[str, Any], corners: list[dict[str, Any]]) -> None:
    for corner in corners[:96]:
        name = str(corner.get("name") or "unknown")
        node = {
            "id": _node_id("timing_corner", f"{pdk_node['name']}:{name}"),
            "kind": "timing_corner",
            "name": name,
            "pdk": pdk_node["name"],
            "liberty_count": corner.get("liberty_count") or 0,
            "sample_files": corner.get("sample_files") or [],
        }
        _put_node(graph, node)
        pdk_node["timing_corners"].append(node["id"])
        graph["indexes"]["timing_corners"][f"{pdk_node['name']}:{name}"] = node["id"]
        _edge(graph, pdk_node["id"], node["id"], "has_timing_corner")


def _add_signoff_deck_node(graph: dict[str, Any], pdk_node: dict[str, Any]) -> None:
    decks = pdk_node.get("physical_decks") or {}
    node = {
        "id": _node_id("signoff_decks", pdk_node["name"]),
        "kind": "signoff_decks",
        "pdk": pdk_node["name"],
        "drc_deck_count": int(decks.get("drc_deck_count") or 0),
        "lvs_deck_count": int(decks.get("lvs_deck_count") or 0),
        "routing_layers": decks.get("routing_layers") or [],
        "ready_for_physical_verification": bool((decks.get("drc_deck_count") or 0) or (decks.get("lvs_deck_count") or 0)),
    }
    _put_node(graph, node)
    graph["indexes"]["signoff_decks"][pdk_node["name"]] = node["id"]
    _edge(graph, pdk_node["id"], node["id"], "has_signoff_decks")


def _add_macro_node(graph: dict[str, Any], pdk_node: dict[str, Any], macro: dict[str, Any]) -> None:
    name = str(macro.get("name") or "unknown")
    node = {
        "id": _node_id("memory_macro", f"{pdk_node['name']}:{name}"),
        "kind": "memory_macro",
        "name": name,
        "pdk": pdk_node["name"],
        "macro_kind": macro.get("kind"),
        "width_bits": macro.get("width_bits"),
        "depth_words": macro.get("depth_words"),
        "capacity_bits": macro.get("capacity_bits") or _capacity_bits(macro.get("width_bits"), macro.get("depth_words")),
        "ports": _as_list(macro.get("ports") or macro.get("pins") or []),
        "views": macro.get("views") or {},
        "size": macro.get("size"),
        "confidence": macro.get("confidence") or "unknown",
        "binding_contract": macro.get("binding_contract") or {},
        "view_readiness": _view_readiness(macro.get("views") or {}),
        "source": macro.get("source"),
    }
    _put_node(graph, node)
    pdk_node["memory_macros"].append(node["id"])
    graph["indexes"]["memory_macros"][f"{pdk_node['name']}:{name}"] = node["id"]
    _edge(graph, pdk_node["id"], node["id"], "offers_memory_macro")


def _add_pad_node(graph: dict[str, Any], pdk_node: dict[str, Any], pad: dict[str, Any]) -> None:
    name = str(pad.get("name") or "unknown")
    node = {
        "id": _node_id("pad_cell", f"{pdk_node['name']}:{name}"),
        "kind": "pad_cell",
        "name": name,
        "pdk": pdk_node["name"],
        "pad_kind": pad.get("kind"),
        "pins": pad.get("pins") or [],
        "views": pad.get("views") or {},
        "confidence": pad.get("confidence") or "unknown",
        "view_readiness": _view_readiness(pad.get("views") or {}),
        "source": pad.get("source"),
    }
    _put_node(graph, node)
    pdk_node["pad_cells"].append(node["id"])
    graph["indexes"]["pad_cells"][f"{pdk_node['name']}:{name}"] = node["id"]
    _edge(graph, pdk_node["id"], node["id"], "offers_pad_cell")


def _score_memory_candidate(requirement: dict[str, Any], macro: dict[str, Any], *, require_physical: bool) -> dict[str, Any]:
    req_width = _int_or_none(requirement.get("width_bits"))
    req_depth = _int_or_none(requirement.get("depth_words"))
    req_capacity = _int_or_none(requirement.get("capacity_bits"))
    macro_width = _int_or_none(macro.get("width_bits"))
    macro_depth = _int_or_none(macro.get("depth_words"))
    macro_capacity = _int_or_none(macro.get("capacity_bits"))
    fit = _memory_fit(req_width, req_depth, req_capacity, macro_width, macro_depth, macro_capacity)
    views = macro.get("views") or {}
    score = CONFIDENCE_SCORE.get(str(macro.get("confidence") or "unknown"), 0)
    if require_physical and not all(views.get(view) for view in MEMORY_VIEW_CONTRACT):
        score -= 45
    if req_width and macro_width:
        score += max(0, 28 - int(abs(math.log2(max(req_width, macro_width) / max(1, min(req_width, macro_width)))) * 12))
    if req_depth and macro_depth:
        score += max(0, 28 - int(abs(math.log2(max(req_depth, macro_depth) / max(1, min(req_depth, macro_depth)))) * 12))
    if req_capacity and macro_capacity:
        if macro_capacity >= req_capacity:
            score += max(0, 24 - int((macro_capacity - req_capacity) / max(req_capacity, 1) * 12))
        else:
            score -= 12
    score += _port_score(str(requirement.get("ports") or "unspecified"), macro)
    if fit.get("macro_count_estimate", 1) > 16:
        score -= 20
    return {
        "score": score,
        "macro": _compact_macro(macro),
        "confidence": macro.get("confidence") or "unknown",
        "fit": fit,
        "evidence": {
            "pdk": macro.get("pdk"),
            "views": {view: paths[:4] for view, paths in views.items()},
            "view_readiness": macro.get("view_readiness") or {},
        },
    }


def _memory_fit(
    req_width: int | None,
    req_depth: int | None,
    req_capacity: int | None,
    macro_width: int | None,
    macro_depth: int | None,
    macro_capacity: int | None,
) -> dict[str, Any]:
    width_tiles = 1
    depth_tiles = 1
    if req_width and macro_width:
        width_tiles = max(1, math.ceil(req_width / macro_width))
    if req_depth and macro_depth:
        depth_tiles = max(1, math.ceil(req_depth / macro_depth))
    elif req_capacity and macro_width and macro_depth:
        per_width_capacity = macro_width * macro_depth * width_tiles
        depth_tiles = max(1, math.ceil(req_capacity / max(1, per_width_capacity)))
    estimated_capacity = None
    if macro_width and macro_depth:
        estimated_capacity = macro_width * macro_depth * width_tiles * depth_tiles
    needed_capacity = req_capacity or (req_width * req_depth if req_width and req_depth else None)
    return {
        "exact": bool(req_width and req_depth and macro_width == req_width and macro_depth == req_depth),
        "requires_width_tiling": width_tiles > 1,
        "requires_depth_banking": depth_tiles > 1,
        "requires_tiling": width_tiles > 1 or depth_tiles > 1,
        "width_tiles": width_tiles,
        "depth_tiles": depth_tiles,
        "macro_count_estimate": width_tiles * depth_tiles,
        "estimated_capacity_bits": estimated_capacity,
        "requested_capacity_bits": needed_capacity,
        "waste_bits": (estimated_capacity - needed_capacity) if estimated_capacity is not None and needed_capacity is not None else None,
    }


def _memory_integration_contract(requirement: dict[str, Any], selected: dict[str, Any] | None) -> dict[str, Any]:
    if not selected:
        return {
            "required_steps": [
                "configure SRAM compiler output or install/index PDK memory macros",
                "re-run capability graph scan",
            ],
            "may_generate_rtl": False,
        }
    fit = selected.get("fit") or {}
    macro = selected.get("macro") or {}
    steps = [
        "validate LEF/Liberty/GDS/RTL view consistency for the selected macro",
        "generate blackbox or simulation model binding from local macro views",
        "connect clock/reset/write-enable/read-enable/address/data pins by parsed macro port contract",
        "generate SDC constraints for macro interface timing",
    ]
    if fit.get("requires_tiling"):
        steps.append("generate deterministic width/depth tiling wrapper and banking decode")
    if macro.get("view_readiness", {}).get("physical"):
        steps.extend([
            "reserve floorplan area with halos and route blockages",
            "emit macro placement/PDN constraints for the selected backend",
        ])
    return {
        "selected_macro": macro.get("name"),
        "pdk": macro.get("pdk"),
        "required_steps": steps,
        "may_generate_rtl": bool(macro.get("view_readiness", {}).get("timing")),
        "requires_wrapper": bool(fit.get("requires_tiling") or selected.get("confidence") != "implementation_ready"),
    }


def _stage_gate(graph: dict[str, Any], stage: str, name: str, *, required: bool) -> dict[str, Any]:
    node_id = ((graph.get("indexes") or {}).get("tool_stages") or {}).get(stage)
    node = (graph.get("nodes") or {}).get(node_id or "") or {}
    available = bool(node.get("available"))
    licensed = node.get("license_hint_present") is not False
    status = "pass" if available and licensed else "warn" if available else "fail"
    return {
        "name": name,
        "stage": stage,
        "required": required,
        "status": status,
        "evidence": _compact_tool_node(node),
        "blockers": [] if status == "pass" else [f"No ready adapter for required `{stage}` stage." if not available else f"`{stage}` adapter is present but license/configuration evidence is incomplete."],
    }


def _pdk_view_gate(graph: dict[str, Any], *, target_pdk: str | None, deliverables: set[str]) -> dict[str, Any]:
    pdk_nodes = _target_pdk_nodes(graph, target_pdk)
    physical_required = bool(deliverables & {"pnr", "gdsii", "layout", "hardening", "sta", "signoff_reports"})
    any_ready = any((node.get("readiness") or {}).get("can_harden") for node in pdk_nodes)
    return {
        "name": "pdk_physical_views",
        "required": physical_required,
        "status": "pass" if any_ready else "fail" if physical_required else "warn",
        "evidence": [{"pdk": node.get("name"), "readiness": node.get("readiness")} for node in pdk_nodes[:8]],
        "blockers": [] if any_ready else ["No selected/indexed PDK has enough Liberty/LEF/tech evidence for physical implementation."],
    }


def _signoff_deck_gate(graph: dict[str, Any], *, target_pdk: str | None, deliverables: set[str]) -> dict[str, Any]:
    required = bool(deliverables & {"drc", "lvs", "signoff_reports", "gdsii"})
    pdk_nodes = _target_pdk_nodes(graph, target_pdk)
    deck_nodes = []
    for pdk_node in pdk_nodes:
        deck_id = ((graph.get("indexes") or {}).get("signoff_decks") or {}).get(pdk_node.get("name"))
        if deck_id:
            deck_nodes.append((graph.get("nodes") or {}).get(deck_id) or {})
    ready = any(node.get("ready_for_physical_verification") for node in deck_nodes)
    return {
        "name": "physical_signoff_decks",
        "required": required,
        "status": "pass" if ready else "fail" if required else "warn",
        "evidence": deck_nodes[:8],
        "blockers": [] if ready else ["No DRC/LVS/signoff deck evidence is indexed for the selected PDK."],
    }


def _target_pdk_nodes(graph: dict[str, Any], target_pdk: str | None) -> list[dict[str, Any]]:
    pdk_index = (graph.get("indexes") or {}).get("pdks") or {}
    nodes = graph.get("nodes") or {}
    if target_pdk:
        for name, node_id in pdk_index.items():
            if _norm(name) == _norm(target_pdk):
                return [nodes.get(node_id) or {}]
    return [nodes.get(node_id) or {} for node_id in pdk_index.values()]


def _binding_evidence(binding: dict[str, Any]) -> dict[str, Any]:
    selected = binding.get("selected") or {}
    return {
        "status": binding.get("status"),
        "selected_macro": ((selected.get("macro") or {}).get("name")),
        "pdk": ((selected.get("macro") or {}).get("pdk")),
        "fit": selected.get("fit"),
        "confidence": selected.get("confidence"),
    }


def _view_readiness(views: dict[str, Any]) -> dict[str, bool]:
    return {
        "timing": bool(views.get("liberty")),
        "physical": bool(views.get("lef") and (views.get("gds") or views.get("oas"))),
        "simulation": bool(views.get("verilog")),
        "lvs": bool(views.get("spice")),
        "implementation": bool(views.get("lef") and views.get("liberty")),
        "signoff": bool(views.get("lef") and (views.get("gds") or views.get("oas")) and views.get("spice")),
    }


def _port_score(required_ports: str, macro: dict[str, Any]) -> int:
    required_ports = (required_ports or "unspecified").lower()
    if required_ports == "unspecified":
        return 0
    haystack = " ".join([
        str(macro.get("name") or ""),
        str(macro.get("macro_kind") or ""),
        " ".join(str(port) for port in (macro.get("ports") or [])),
    ]).lower()
    if required_ports in haystack:
        return 20
    if required_ports == "1rw" and any(term in haystack for term in ("1rw", "rw", "sram")):
        return 10
    if required_ports == "rom" and "rom" in haystack:
        return 20
    return -8


def _graph_summary(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = graph.get("nodes") or {}
    kinds: dict[str, int] = {}
    for node in nodes.values():
        kind = str(node.get("kind") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
    tool_stage_ready = [
        node.get("stage")
        for node in nodes.values()
        if node.get("kind") == "tool_stage" and node.get("available") and node.get("license_hint_present") is not False
    ]
    memories = [node for node in nodes.values() if node.get("kind") == "memory_macro"]
    return {
        "node_count": len(nodes),
        "edge_count": len(graph.get("edges") or []),
        "kind_counts": kinds,
        "ready_tool_stages": sorted([stage for stage in tool_stage_ready if stage]),
        "memory_macro_count": len(memories),
        "implementation_ready_memory_macro_count": sum(1 for node in memories if node.get("confidence") in {"implementation_ready", "physical_timing_ready"}),
        "signoff_ready_pdk_count": sum(
            1 for node in nodes.values()
            if node.get("kind") == "signoff_decks" and node.get("ready_for_physical_verification")
        ),
    }


def _compact_macro(macro: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": macro.get("name"),
        "pdk": macro.get("pdk"),
        "kind": macro.get("macro_kind"),
        "width_bits": macro.get("width_bits"),
        "depth_words": macro.get("depth_words"),
        "capacity_bits": macro.get("capacity_bits"),
        "confidence": macro.get("confidence"),
        "views": sorted((macro.get("views") or {}).keys()),
        "view_readiness": macro.get("view_readiness") or {},
    }


def _compact_tool_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": bool(node.get("available")),
        "adapter": node.get("adapter"),
        "vendor": node.get("vendor"),
        "openness": node.get("openness"),
        "commands": node.get("commands") or [],
        "license_hint_present": node.get("license_hint_present"),
        "declared_binding_count": len(node.get("declared_bindings") or []),
    }


def _compact_requirement(requirement: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": requirement.get("name"),
        "kind": requirement.get("kind"),
        "width_bits": requirement.get("width_bits"),
        "depth_words": requirement.get("depth_words"),
        "capacity_bits": requirement.get("capacity_bits"),
        "ports": requirement.get("ports"),
        "implementation_preference": requirement.get("implementation_preference"),
    }


def _capacity_bits(width: Any, depth: Any) -> int | None:
    width_i = _int_or_none(width)
    depth_i = _int_or_none(depth)
    return width_i * depth_i if width_i and depth_i else None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_readiness(existing: dict[str, Any], declared: dict[str, Any]) -> dict[str, Any]:
    rank = {
        "indexed_metadata_only": 0,
        "declared_metadata_only": 0,
        "synthesis_only": 1,
        "layout_generation_candidate": 2,
        "layout_signoff_candidate": 3,
    }
    out = dict(existing or {})
    out["can_synthesize"] = bool(out.get("can_synthesize") or declared.get("can_synthesize"))
    out["can_harden"] = bool(out.get("can_harden") or declared.get("can_harden"))
    out["signoff_decks_present"] = bool(out.get("signoff_decks_present") or declared.get("signoff_decks_present"))
    existing_tier = str(out.get("tier") or "")
    declared_tier = str(declared.get("tier") or "")
    if rank.get(declared_tier, -1) > rank.get(existing_tier, -1):
        out["tier"] = declared_tier
    elif not existing_tier and declared_tier:
        out["tier"] = declared_tier
    return out


def _merge_decks(existing: dict[str, Any], declared: dict[str, Any]) -> dict[str, Any]:
    out = dict(existing or {})
    out["drc_deck_count"] = max(int(out.get("drc_deck_count") or 0), int(declared.get("drc_deck_count") or 0))
    out["lvs_deck_count"] = max(int(out.get("lvs_deck_count") or 0), int(declared.get("lvs_deck_count") or 0))
    layers = [*(out.get("routing_layers") or []), *(declared.get("routing_layers") or [])]
    out["routing_layers"] = list(dict.fromkeys(str(layer) for layer in layers if str(layer).strip()))[:64]
    return out


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _put_node(graph: dict[str, Any], node: dict[str, Any]) -> None:
    graph["nodes"][node["id"]] = node


def _edge(graph: dict[str, Any], source: str, target: str, kind: str) -> None:
    graph["edges"].append({"source": source, "target": target, "kind": kind})


def _node_id(kind: str, key: str) -> str:
    digest = hashlib.sha256(f"{kind}:{key}".encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", key or "unknown")[:80]
    return f"{kind}:{safe}:{digest}"
