from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from agentic_server.pdk_index import build_pdk_index, select_pdk
from agentic_server.tool_adapters import capability_matrix


FLOW_SEARCH_ENVS = ("AGENTIC_FLOW_SEARCH_PATHS", "AGENTIC_TOOL_SEARCH_PATHS")

PROPRIETARY_STACKS = {
    "cadence": {
        "simulation": ("xrun", "irun"),
        "synthesis": ("genus",),
        "pnr": ("innovus",),
        "sta": ("tempus",),
        "physical_verification": (),
        "license_env": ("CDS_LIC_FILE", "LM_LICENSE_FILE"),
    },
    "synopsys": {
        "simulation": ("vcs",),
        "synthesis": ("dc_shell",),
        "pnr": ("icc2_shell",),
        "sta": ("pt_shell",),
        "physical_verification": (),
        "license_env": ("SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE"),
    },
    "siemens": {
        "simulation": ("vsim", "questasim"),
        "synthesis": (),
        "pnr": (),
        "sta": (),
        "physical_verification": ("calibre",),
        "license_env": ("MGLS_LICENSE_FILE", "LM_LICENSE_FILE"),
    },
}


def _split_paths(value: str) -> list[str]:
    return [item.strip() for item in value.split(os.pathsep) if item.strip()]


def _expand_candidate(raw: str) -> str:
    return os.path.expandvars(os.path.expanduser(raw)).strip()


def _candidate_dirs(explicit_env: str, marker_sets: tuple[tuple[str, ...], ...]) -> list[str]:
    candidates: list[Path] = []
    explicit = os.environ.get(explicit_env, "").strip()
    if explicit:
        expanded = Path(_expand_candidate(explicit))
        if expanded.is_dir():
            candidates.append(expanded)

    roots: list[str] = []
    for env_name in FLOW_SEARCH_ENVS:
        roots.extend(_split_paths(os.environ.get(env_name, "")))
    for raw_root in roots:
        root = Path(_expand_candidate(raw_root))
        if not root.is_dir():
            continue
        candidates.append(root)
        try:
            candidates.extend([item for item in root.iterdir() if item.is_dir() and not item.name.startswith(".")])
        except OSError:
            continue

    matched: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if str(resolved) in seen:
            continue
        seen.add(str(resolved))
        if any(all((resolved / marker).exists() for marker in markers) for markers in marker_sets):
            matched.append(str(resolved))
    return matched


def _first_existing_dir(candidates: list[str]) -> str | None:
    for raw in candidates:
        expanded = _expand_candidate(raw)
        if expanded and os.path.isdir(expanded):
            return str(Path(expanded).resolve())
    return None


def _dir_markers(path: str | None, markers: tuple[str, ...]) -> dict[str, bool]:
    if not path:
        return {marker: False for marker in markers}
    root = Path(path)
    return {marker: (root / marker).exists() for marker in markers}


def _subdirs(path: str | None) -> list[str]:
    if not path:
        return []
    try:
        return sorted([item.name for item in Path(path).iterdir() if item.is_dir() and not item.name.startswith(".")])
    except OSError:
        return []


def _command_status(command: str) -> dict[str, Any]:
    path = shutil.which(command)
    return {"available": path is not None, "path": path}


def _docker_flow_images(images: list[str]) -> dict[str, list[str]]:
    lower_pairs = [(image, image.lower()) for image in images]
    return {
        "openlane": [image for image, lower in lower_pairs if "openlane" in lower or "librelane" in lower],
        "openroad": [image for image, lower in lower_pairs if "openroad" in lower],
        "orfs": [image for image, lower in lower_pairs if "openroad-flow" in lower or "orfs" in lower],
    }


def _detect_openlane(images: list[str]) -> dict[str, Any]:
    commands = {
        "openlane": _command_status("openlane"),
        "openlane2": _command_status("openlane2"),
        "volare": _command_status("volare"),
    }
    root = _first_existing_dir(_candidate_dirs(
        "AGENTIC_OPENLANE_ROOT",
        (("flow.tcl",), ("openlane",), ("openlane2",), ("Makefile", "designs")),
    ))
    markers = _dir_markers(root, ("flow.tcl", "Makefile", "openlane", "openlane2"))
    docker_images = _docker_flow_images(images)["openlane"]
    return {
        "available": bool(root or docker_images or any(item["available"] for item in commands.values())),
        "path": root,
        "execution_modes": {
            "native_cli": any(item["available"] for item in commands.values()),
            "repository": bool(root),
            "docker": bool(docker_images),
        },
        "commands": commands,
        "docker_images": docker_images,
        "markers": markers,
        "config_contract": {
            "openlane2": "JSON/YAML/Tcl config with DESIGN_NAME, VERILOG_FILES, CLOCK_PORT/CLOCK_PERIOD, and PDK_ROOT supplied by CLI/env.",
            "openlane1": "design directory with config.json/config.tcl and src/ RTL; flow entry point is flow.tcl when using the legacy repository flow.",
        },
    }


def _detect_orfs(images: list[str]) -> dict[str, Any]:
    root = _first_existing_dir(_candidate_dirs(
        "AGENTIC_ORFS_ROOT",
        (("flow/Makefile", "flow/platforms"), ("Makefile", "platforms")),
    ))
    markers = _dir_markers(root, ("flow/Makefile", "flow/platforms", "flow/designs", "flow/scripts"))
    platforms = _subdirs(str(Path(root) / "flow" / "platforms")) if root else []
    docker_images = _docker_flow_images(images)["orfs"] or _docker_flow_images(images)["openroad"]
    return {
        "available": bool(root and markers.get("flow/Makefile")),
        "path": root,
        "execution_modes": {
            "repository_make": bool(root and markers.get("flow/Makefile")),
            "docker": bool(docker_images),
        },
        "platforms": platforms,
        "docker_images": docker_images,
        "markers": markers,
        "config_contract": {
            "design_config": "flow/designs/<PLATFORM>/<DESIGN_NAME>/config.mk or an explicit DESIGN_CONFIG passed to make.",
            "make_variables": "PLATFORM, DESIGN_NAME, VERILOG_FILES, SDC_FILE, CLOCK_PERIOD, CORE_UTILIZATION, PLACE_DENSITY, and stage-specific overrides.",
        },
    }


def _detect_proprietary(tools: dict[str, bool]) -> dict[str, Any]:
    stacks: dict[str, Any] = {}
    for vendor, spec in PROPRIETARY_STACKS.items():
        capabilities: dict[str, list[str]] = {}
        available_tools: list[str] = []
        for capability in ("simulation", "synthesis", "pnr", "sta", "physical_verification"):
            found = [tool for tool in spec[capability] if tools.get(tool)]
            capabilities[capability] = found
            available_tools.extend(found)
        license_env = {name: bool(os.environ.get(name)) for name in spec["license_env"]}
        stage_count = sum(1 for found in capabilities.values() if found)
        stacks[vendor] = {
            "available": bool(available_tools),
            "license_env": license_env,
            "license_hint_present": any(license_env.values()),
            "capabilities": capabilities,
            "stage_count": stage_count,
            "complete_digital_backend": bool(capabilities["synthesis"] and capabilities["pnr"] and capabilities["sta"]),
        }
    return {
        "available": any(stack["available"] for stack in stacks.values()),
        "licensed_backend_available": any(
            stack["complete_digital_backend"] and stack["license_hint_present"]
            for stack in stacks.values()
        ),
        "complete_backend_available": any(stack["complete_digital_backend"] for stack in stacks.values()),
        "stacks": stacks,
    }


def _best_proprietary_stack(proprietary: dict[str, Any]) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    stacks = proprietary.get("stacks") or {}
    ranked = sorted(
        stacks.items(),
        key=lambda item: (
            bool(item[1].get("complete_digital_backend") and item[1].get("license_hint_present")),
            bool(item[1].get("complete_digital_backend")),
            int(item[1].get("stage_count") or 0),
            bool(item[1].get("license_hint_present")),
        ),
        reverse=True,
    )
    for vendor, stack in ranked:
        if stack.get("available"):
            return vendor, stack
    return None, None


def detect_flows(tools: dict[str, bool] | None = None, docker_images: list[str] | None = None) -> dict[str, Any]:
    tools = tools or {}
    docker_images = docker_images or []
    return {
        "openlane": _detect_openlane(docker_images),
        "orfs": _detect_orfs(docker_images),
        "proprietary": _detect_proprietary(tools),
    }


def recommend_flow(
    env: dict[str, Any],
    requested_pdk: str = "",
    design_goal: str = "rtl_to_gds",
) -> dict[str, Any]:
    pdk_index = env.get("pdk_index") or build_pdk_index()
    pdk = select_pdk(pdk_index, requested_pdk)
    flows = env.get("flows") or {}
    tools = env.get("tools") or {}
    capabilities = env.get("capabilities") or {}
    adapters = env.get("tool_adapters") or capability_matrix(tools)
    blockers: list[str] = []
    rationale: list[str] = []
    alternatives: list[dict[str, Any]] = []
    setup_actions: list[dict[str, Any]] = []
    profile = "setup_required"
    backend = "none"
    confidence = "low"
    run_strategy: dict[str, Any] = {}

    pdk_class = (pdk or {}).get("class")
    pdk_family = (pdk or {}).get("family")
    node_nm = (pdk or {}).get("node_nm")
    proprietary = flows.get("proprietary", {})
    openlane = flows.get("openlane", {})
    orfs = flows.get("orfs", {})
    prop_vendor, prop_stack = _best_proprietary_stack(proprietary)

    stage_plan = _stage_plan_from_adapters(adapters)

    if _stage_plan_has(stage_plan, ("synthesis", "pnr", "sta")) and _stage_plan_licensed(stage_plan) and capabilities.get("pdk"):
        profile = "proprietary_signoff_flow"
        backend = "proprietary" if any((stage.get("openness") == "proprietary") for stage in stage_plan.values()) else "adapter_native"
        confidence = "high" if pdk_class == "commercial_or_custom" else "medium"
        dominant_vendor = _dominant_vendor(stage_plan) or prop_vendor or "detected"
        rationale.append(
            f"Detected a complete licensed/native adapter chain dominated by {dominant_vendor}; prefer the user's installed toolchain when the local PDK scripts support it."
        )
        run_strategy = {
            "flow": backend,
            "vendor": dominant_vendor,
            "stage_plan": stage_plan,
            "requires": ["synthesis tool", "PnR tool", "STA tool", "license/configuration where applicable", "local PDK scripts or locally derived Tcl from LEF/Liberty/SDC evidence"],
            "agentic_contract": "Follow existing customer/foundry scripts first; generate missing Tcl only from local evidence.",
        }
        if pdk_class != "commercial_or_custom":
            alternatives.extend(_open_source_alternatives(openlane, orfs, pdk))
    elif _stage_plan_has(stage_plan, ("synthesis", "pnr", "sta")) and capabilities.get("pdk"):
        profile = "adapter_license_or_config_required"
        backend = "adapter_candidate"
        confidence = "medium"
        blockers.append("A complete implementation adapter chain is present, but one or more selected adapters lack visible license/configuration hints.")
        rationale.append("AgentIC should ask the user to configure/license the detected adapter chain before falling back.")
        run_strategy = {
            "flow": "adapter_chain",
            "stage_plan": stage_plan,
            "requires": ["license/configuration for selected adapters"],
        }
        alternatives.extend(_open_source_alternatives(openlane, orfs, pdk))
    elif prop_stack and prop_stack.get("complete_digital_backend") and capabilities.get("pdk"):
        profile = "proprietary_license_required"
        backend = "proprietary_candidate"
        confidence = "medium"
        blockers.append(f"{prop_vendor} backend tools are present, but no matching license environment variable is visible.")
        rationale.append("Commercial tools were detected, so AgentIC should ask the user to configure licensing before falling back.")
        run_strategy = {
            "flow": "proprietary",
            "vendor": prop_vendor,
            "capabilities": prop_stack.get("capabilities", {}),
            "requires": ["license environment"],
        }
        alternatives.extend(_open_source_alternatives(openlane, orfs, pdk))
    elif pdk_family == "asap7" and orfs.get("available"):
        profile = "orfs_research_node_flow"
        backend = "orfs"
        confidence = "high"
        rationale.append("ASAP7 is best matched to ORFS when its platform is available.")
        run_strategy = {
            "flow": "orfs",
            "root": orfs.get("path"),
            "platform_candidates": (pdk or {}).get("orfs", {}).get("platform_aliases", ["asap7"]),
            "config_contract": orfs.get("config_contract"),
        }
    elif pdk_family in {"sky130", "gf180mcu"} and openlane.get("available"):
        profile = "openlane_open_pdk_flow"
        backend = "openlane"
        confidence = "high"
        rationale.append("Legacy open-source PDK detected; OpenLane has the strongest turnkey PDK contract here.")
        run_strategy = {
            "flow": "openlane",
            "root": openlane.get("path"),
            "execution_modes": openlane.get("execution_modes"),
            "config_contract": openlane.get("config_contract"),
        }
    elif orfs.get("available") and _orfs_supports_pdk(orfs, pdk):
        profile = "orfs_openroad_flow"
        backend = "orfs"
        confidence = "medium"
        rationale.append("ORFS is installed and has a platform compatible with the selected PDK.")
        run_strategy = {
            "flow": "orfs",
            "root": orfs.get("path"),
            "platform_candidates": (pdk or {}).get("orfs", {}).get("platform_aliases", []),
            "config_contract": orfs.get("config_contract"),
        }
    elif _stage_plan_has(stage_plan, ("simulation", "synthesis")) or (capabilities.get("synthesis") and capabilities.get("simulation")):
        if prop_stack and prop_stack.get("available"):
            profile = "hybrid_user_toolchain_flow"
            backend = "hybrid"
            rationale.append(
                f"Use available {prop_vendor} stages where present, then fill missing stages with configured open-source alternatives after user approval."
            )
        else:
            profile = "rtl_to_synthesis_until_pnr_ready"
            backend = "synthesis_only"
            rationale.append("Simulation and synthesis are available, but no matching physical implementation flow is ready.")
        confidence = "medium"
        if not capabilities.get("pdk"):
            blockers.append("No indexed PDK root is available for physical implementation.")
        if not capabilities.get("pnr"):
            blockers.append("No native or Docker PnR flow is available.")
            setup_actions.append({
                "capability": "pnr",
                "preferred": "configure proprietary PnR if licensed; otherwise install an open-source OpenLane/ORFS/OpenROAD flow",
            })
        run_strategy = {
            "flow": backend,
            "requires": ["simulation", "synthesis"],
            "stage_plan": stage_plan,
            "proprietary_vendor": prop_vendor,
            "next_unlock": "Configure PDK_ROOT and any licensed proprietary tools first; if unavailable, install open-source alternatives.",
        }
    else:
        profile = "setup_required"
        backend = "none"
        if not capabilities.get("simulation"):
            blockers.append("Simulation tool missing.")
            setup_actions.append({"capability": "simulation", "preferred": "configure proprietary simulator if present; otherwise install iverilog/verilator"})
        if not capabilities.get("synthesis"):
            blockers.append("Synthesis tool missing.")
            setup_actions.append({"capability": "synthesis", "preferred": "configure proprietary synthesis if present; otherwise install yosys"})
        if not capabilities.get("pdk"):
            blockers.append("PDK missing.")
            setup_actions.append({"capability": "pdk", "preferred": "configure user's commercial/custom PDK root or an open PDK root"})
        rationale.append("The local environment does not yet satisfy the minimum execution contract.")

    if design_goal != "rtl_to_gds" and backend in {"openlane", "orfs", "proprietary"}:
        rationale.append(f"Design goal is {design_goal}; physical flow should be staged after RTL verification.")

    return {
        "profile": profile,
        "backend": backend,
        "confidence": confidence,
        "selected_pdk": pdk,
        "rationale": rationale,
        "blockers": blockers,
        "alternatives": alternatives,
        "setup_actions": setup_actions,
        "run_strategy": run_strategy,
        "adapter_matrix": adapters,
    }


def _stage_plan_from_adapters(adapters: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plan = {}
    for stage in ("simulation", "synthesis", "pnr", "sta", "physical_verification"):
        selected = (adapters.get(stage) or {}).get("selected")
        if selected:
            plan[stage] = selected
    return plan


def _stage_plan_has(plan: dict[str, dict[str, Any]], stages: tuple[str, ...]) -> bool:
    return all(bool(plan.get(stage, {}).get("available")) for stage in stages)


def _stage_plan_licensed(plan: dict[str, dict[str, Any]]) -> bool:
    return all(bool(stage.get("license_hint_present")) for stage in plan.values())


def _dominant_vendor(plan: dict[str, dict[str, Any]]) -> str | None:
    counts: dict[str, int] = {}
    for stage in plan.values():
        vendor = stage.get("vendor")
        if vendor:
            counts[vendor] = counts.get(vendor, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[0][0]


def _orfs_supports_pdk(orfs: dict[str, Any], pdk: dict[str, Any] | None) -> bool:
    if not pdk:
        return False
    platforms = set(orfs.get("platforms") or [])
    aliases = set((pdk.get("orfs") or {}).get("platform_aliases") or [])
    return bool(platforms & aliases)


def _open_source_alternatives(openlane: dict[str, Any], orfs: dict[str, Any], pdk: dict[str, Any] | None) -> list[dict[str, Any]]:
    alternatives: list[dict[str, Any]] = []
    if openlane.get("available"):
        alternatives.append({
            "backend": "openlane",
            "reason": "OpenLane is available as an open-source fallback.",
            "execution_modes": openlane.get("execution_modes", {}),
        })
    if orfs.get("available") and _orfs_supports_pdk(orfs, pdk):
        alternatives.append({
            "backend": "orfs",
            "reason": "ORFS is available for the selected PDK/platform.",
            "platforms": orfs.get("platforms", []),
        })
    if not alternatives:
        alternatives.append({
            "backend": "open_source_install",
            "reason": "No matching open-source physical flow is detected; ask user approval before installing/configuring one.",
        })
    return alternatives
