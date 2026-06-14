from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


ADAPTER_SUMMARY_VERSION = "agentic.tool_adapter_summary.v1"

STAGES = (
    "simulation",
    "lint",
    "formal",
    "synthesis",
    "pnr",
    "sta",
    "physical_verification",
    "power",
    "waveform",
    "utility",
)


@dataclass(frozen=True)
class ParserContract:
    error: str = r"(?i)\b(error|fatal|failed)\b"
    warning: str = r"(?i)\b(warning|warn)\b"
    metrics: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolAdapter:
    name: str
    commands: tuple[str, ...]
    stages: tuple[str, ...]
    vendor: str = "unknown"
    openness: str = "unknown"
    license_env: tuple[str, ...] = ()
    parser: ParserContract = field(default_factory=ParserContract)
    priority: int = 50
    flow: str | None = None
    config_contract: dict[str, Any] = field(default_factory=dict)
    install_hint_env: str | None = None

    def to_dict(self, availability: dict[str, Any] | None = None) -> dict[str, Any]:
        data = asdict(self)
        data["parser"] = asdict(self.parser)
        data["availability"] = availability or {}
        return data


DEFAULT_ADAPTERS: tuple[ToolAdapter, ...] = (
    ToolAdapter(
        name="icarus",
        commands=("iverilog", "vvp"),
        stages=("simulation",),
        vendor="Icarus Verilog",
        openness="open_source",
        parser=ParserContract(error=r"^.*:\d+: error:.*$", warning=r"^.*:\d+: warning:.*$"),
        priority=40,
    ),
    ToolAdapter(
        name="verilator",
        commands=("verilator",),
        stages=("simulation", "lint"),
        vendor="Verilator",
        openness="open_source",
        parser=ParserContract(error=r"%Error.*$", warning=r"%Warning.*$"),
        priority=50,
    ),
    ToolAdapter(
        name="yosys",
        commands=("yosys",),
        stages=("synthesis",),
        vendor="YosysHQ",
        openness="open_source",
        parser=ParserContract(
            error=r"^ERROR:.*$",
            warning=r"^Warning:.*$",
            metrics={
                "cell_count": r"Number of cells:\s+(\d+)",
                "wire_count": r"Number of wires:\s+(\d+)",
                "memory_bits": r"Number of memory bits:\s+(\d+)",
            },
        ),
        priority=45,
    ),
    ToolAdapter(
        name="openroad",
        commands=("openroad",),
        stages=("pnr", "sta"),
        vendor="OpenROAD",
        openness="open_source",
        parser=ParserContract(error=r"^\[ERROR.*$", warning=r"^\[WARNING.*$"),
        priority=45,
        flow="orfs",
    ),
    ToolAdapter(
        name="opensta",
        commands=("opensta",),
        stages=("sta",),
        vendor="OpenROAD",
        openness="open_source",
        parser=ParserContract(
            error=r"^Error:.*$",
            warning=r"^Warning:.*$",
            metrics={"worst_slack_ns": r"worst slack\s+([-\d.]+)", "tns_ns": r"tns\s+([-\d.]+)"},
        ),
        priority=40,
    ),
    ToolAdapter(
        name="openlane",
        commands=("openlane", "openlane2", "volare"),
        stages=("pnr", "sta", "physical_verification"),
        vendor="OpenLane",
        openness="open_source",
        priority=55,
        flow="openlane",
        config_contract={
            "openlane2": "JSON/YAML/Tcl config with DESIGN_NAME, VERILOG_FILES, CLOCK_PORT/CLOCK_PERIOD, and PDK_ROOT supplied by CLI/env.",
            "openlane1": "design directory with config.json/config.tcl and src/ RTL; flow entry point is flow.tcl when using the legacy repository flow.",
        },
    ),
    ToolAdapter(
        name="magic",
        commands=("magic",),
        stages=("physical_verification",),
        vendor="Magic",
        openness="open_source",
        parser=ParserContract(error=r"^Error:.*$", warning=r"^Warning:.*$", metrics={"drc_violations": r"Total DRC errors found:\s+(\d+)"}),
    ),
    ToolAdapter(
        name="netgen",
        commands=("netgen",),
        stages=("physical_verification",),
        vendor="Netgen",
        openness="open_source",
        parser=ParserContract(error=r"^Netlists do not match", warning=r"^Warning:.*$"),
    ),
    ToolAdapter(
        name="klayout",
        commands=("klayout",),
        stages=("physical_verification", "utility"),
        vendor="KLayout",
        openness="open_source",
    ),
    ToolAdapter(
        name="xcelium",
        commands=("xrun", "irun"),
        stages=("simulation",),
        vendor="cadence",
        openness="proprietary",
        license_env=("CDS_LIC_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^xmvlog: \*E.*$", warning=r"^xmvlog: \*W.*$"),
        priority=90,
    ),
    ToolAdapter(
        name="vcs",
        commands=("vcs",),
        stages=("simulation",),
        vendor="synopsys",
        openness="proprietary",
        license_env=("SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^Error-\[.*$", warning=r"^Warning-\[.*$"),
        priority=90,
    ),
    ToolAdapter(
        name="questa",
        commands=("vsim", "questasim"),
        stages=("simulation",),
        vendor="siemens",
        openness="proprietary",
        license_env=("MGLS_LICENSE_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^\*\* Error:.*$", warning=r"^\*\* Warning:.*$"),
        priority=90,
    ),
    ToolAdapter(
        name="genus",
        commands=("genus",),
        stages=("synthesis",),
        vendor="cadence",
        openness="proprietary",
        license_env=("CDS_LIC_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^\*\*ERROR.*$", warning=r"^\*\*WARN.*$", metrics={"area_um2": r"Total area of.*?:\s+([\d.]+)", "cell_count": r"Total cell count.*?:\s+(\d+)"}),
        priority=90,
    ),
    ToolAdapter(
        name="innovus",
        commands=("innovus",),
        stages=("pnr", "sta"),
        vendor="cadence",
        openness="proprietary",
        license_env=("CDS_LIC_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^\*\*ERROR.*$", warning=r"^\*\*WARN.*$"),
        priority=90,
    ),
    ToolAdapter(
        name="design_compiler",
        commands=("dc_shell",),
        stages=("synthesis",),
        vendor="synopsys",
        openness="proprietary",
        license_env=("SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^Error:.*$", warning=r"^Warning:.*$"),
        priority=90,
    ),
    ToolAdapter(
        name="icc2",
        commands=("icc2_shell",),
        stages=("pnr",),
        vendor="synopsys",
        openness="proprietary",
        license_env=("SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^Error:.*$", warning=r"^Warning:.*$"),
        priority=90,
    ),
    ToolAdapter(
        name="primetime",
        commands=("pt_shell",),
        stages=("sta",),
        vendor="synopsys",
        openness="proprietary",
        license_env=("SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^Error:.*$", warning=r"^Warning:.*$"),
        priority=90,
    ),
    ToolAdapter(
        name="tempus",
        commands=("tempus",),
        stages=("sta",),
        vendor="cadence",
        openness="proprietary",
        license_env=("CDS_LIC_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^\*\*ERROR.*$", warning=r"^\*\*WARN.*$"),
        priority=90,
    ),
    ToolAdapter(
        name="calibre",
        commands=("calibre",),
        stages=("physical_verification",),
        vendor="siemens",
        openness="proprietary",
        license_env=("MGLS_LICENSE_FILE", "LM_LICENSE_FILE"),
        parser=ParserContract(error=r"^ERROR:.*$", warning=r"^WARNING:.*$", metrics={"drc_violations": r"TOTAL Result Count =\s+(\d+)"}),
        priority=95,
    ),
)


def load_adapters() -> dict[str, ToolAdapter]:
    adapters = {adapter.name: adapter for adapter in DEFAULT_ADAPTERS}
    for raw in _custom_adapter_payloads():
        for item in raw:
            adapter = _adapter_from_dict(item)
            adapters[adapter.name] = adapter
    return adapters


def adapter_command_names() -> tuple[str, ...]:
    commands: list[str] = ["docker", "make", "python3", "gtkwave"]
    for adapter in load_adapters().values():
        commands.extend(adapter.commands)
    extra = [item.strip() for item in os.environ.get("AGENTIC_EDA_TOOLS", "").split(",") if item.strip()]
    return tuple(dict.fromkeys([*commands, *extra]))


def adapter_regex_commands() -> tuple[str, ...]:
    return tuple(sorted(adapter_command_names(), key=len, reverse=True))


def adapter_parser(tool: str) -> ParserContract:
    normalized = (tool or "").strip().lower()
    for adapter in load_adapters().values():
        names = {adapter.name.lower(), *(command.lower() for command in adapter.commands)}
        if normalized in names:
            return adapter.parser
    return ParserContract(error=r"(?!x)x", warning=r"(?!x)x")


def adapter_catalog(tools: dict[str, bool] | None = None) -> dict[str, Any]:
    tools = tools or {}
    catalog: dict[str, Any] = {}
    for name, adapter in load_adapters().items():
        available_commands = [command for command in adapter.commands if tools.get(command) or shutil.which(command)]
        license_env = {env: bool(os.environ.get(env)) for env in adapter.license_env}
        catalog[name] = adapter.to_dict({
            "available": bool(available_commands),
            "available_commands": available_commands,
            "license_hint_present": any(license_env.values()) if adapter.license_env else True,
            "license_env": license_env,
        })
    return catalog


def capability_matrix(tools: dict[str, bool] | None = None) -> dict[str, Any]:
    catalog = adapter_catalog(tools)
    matrix: dict[str, Any] = {}
    for stage in STAGES:
        candidates = []
        for name, adapter in catalog.items():
            if stage not in adapter.get("stages", []):
                continue
            availability = adapter.get("availability") or {}
            candidates.append({
                "adapter": name,
                "vendor": adapter.get("vendor"),
                "openness": adapter.get("openness"),
                "available": bool(availability.get("available")),
                "license_hint_present": bool(availability.get("license_hint_present")),
                "priority": int(adapter.get("priority") or 0),
                "commands": adapter.get("commands", []),
            })
        candidates.sort(key=lambda item: (item["available"], item["license_hint_present"], item["priority"]), reverse=True)
        matrix[stage] = {
            "available": any(item["available"] for item in candidates),
            "candidates": candidates,
            "selected": next((item for item in candidates if item["available"] and item["license_hint_present"]), None)
                or next((item for item in candidates if item["available"]), None),
        }
    return matrix


def normalized_adapter_summary(tools: dict[str, bool] | None = None) -> dict[str, Any]:
    """Return the VLSI toolchain contract used by AgentIC and external harnesses.

    This is deliberately stage/capability based instead of flow-name based. A
    customer can bring OpenROAD, Cadence, Synopsys, Siemens, or site-local
    adapters through AGENTIC_TOOL_ADAPTERS_* and the agent receives the same
    schema every time.
    """

    catalog = adapter_catalog(tools)
    matrix = capability_matrix(tools)
    stages = {
        stage: _normalized_stage_record(stage, record, catalog)
        for stage, record in matrix.items()
    }
    selected_chain = {
        stage: record["selected"]["adapter"]
        for stage, record in stages.items()
        if record.get("selected")
    }
    vendor_profile = _vendor_profile(stages)
    blocking = [
        blocker
        for record in stages.values()
        for blocker in record.get("blockers", [])
    ]
    required = ("simulation", "synthesis", "pnr", "sta", "physical_verification")
    required_ready = [
        stage for stage in required
        if (stages.get(stage) or {}).get("status") == "ready"
    ]

    return {
        "schema_version": ADAPTER_SUMMARY_VERSION,
        "generated_at": time.time(),
        "stage_order": list(STAGES),
        "required_digital_stages": list(required),
        "readiness": {
            "status": "ready" if len(required_ready) == len(required) else "partial",
            "ready_required_stage_count": len(required_ready),
            "required_stage_count": len(required),
            "missing_required_stages": [stage for stage in required if stage not in required_ready],
        },
        "vendor_profile": vendor_profile,
        "selected_chain": selected_chain,
        "stages": stages,
        "catalog": {
            name: _public_adapter_record(name, adapter)
            for name, adapter in catalog.items()
        },
        "blocking": blocking[:32],
        "extension_contract": {
            "custom_adapter_env": "AGENTIC_TOOL_ADAPTERS_JSON",
            "custom_adapter_file_env": "AGENTIC_TOOL_ADAPTERS_FILE",
            "required_adapter_fields": ["name", "commands", "stages"],
            "optional_adapter_fields": [
                "vendor",
                "openness",
                "license_env",
                "parser",
                "priority",
                "flow",
                "config_contract",
            ],
        },
    }


def _normalized_stage_record(stage: str, record: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    candidates = [_public_candidate(candidate, catalog) for candidate in record.get("candidates", [])]
    selected = _public_candidate(record.get("selected") or {}, catalog) if record.get("selected") else None
    status = _stage_status(candidates, selected)
    blockers = _stage_blockers(stage, candidates, selected, status)
    return {
        "stage": stage,
        "status": status,
        "available": status == "ready",
        "selected": selected if status == "ready" else selected,
        "candidates": candidates,
        "blockers": blockers,
        "decision_rule": "highest priority available adapter with a present license hint; custom adapters can override priority.",
    }


def _public_candidate(candidate: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    name = str(candidate.get("adapter") or "").strip()
    adapter = catalog.get(name) or {}
    availability = adapter.get("availability") or {}
    parser = adapter.get("parser") or {}
    return {
        "adapter": name,
        "vendor": candidate.get("vendor"),
        "openness": candidate.get("openness"),
        "available": bool(candidate.get("available")),
        "license_hint_present": bool(candidate.get("license_hint_present")),
        "priority": int(candidate.get("priority") or 0),
        "commands": list(candidate.get("commands") or []),
        "available_commands": list(availability.get("available_commands") or []),
        "license_env": dict(availability.get("license_env") or {}),
        "flow": adapter.get("flow"),
        "config_contract": dict(adapter.get("config_contract") or {}),
        "parser_contract": {
            "error": parser.get("error"),
            "warning": parser.get("warning"),
            "metrics": dict(parser.get("metrics") or {}),
        },
    }


def _public_adapter_record(name: str, adapter: dict[str, Any]) -> dict[str, Any]:
    availability = adapter.get("availability") or {}
    return {
        "name": name,
        "vendor": adapter.get("vendor"),
        "openness": adapter.get("openness"),
        "stages": list(adapter.get("stages") or []),
        "commands": list(adapter.get("commands") or []),
        "available": bool(availability.get("available")),
        "available_commands": list(availability.get("available_commands") or []),
        "license_hint_present": bool(availability.get("license_hint_present")),
        "license_env": dict(availability.get("license_env") or {}),
        "priority": int(adapter.get("priority") or 0),
        "flow": adapter.get("flow"),
        "config_contract": dict(adapter.get("config_contract") or {}),
    }


def _stage_status(candidates: list[dict[str, Any]], selected: dict[str, Any] | None) -> str:
    if selected and selected.get("available") and selected.get("license_hint_present"):
        return "ready"
    if selected and selected.get("available") and not selected.get("license_hint_present"):
        return "license_unresolved"
    if any(candidate.get("available") for candidate in candidates):
        return "license_unresolved"
    if candidates:
        return "tool_missing"
    return "unsupported"


def _stage_blockers(
    stage: str,
    candidates: list[dict[str, Any]],
    selected: dict[str, Any] | None,
    status: str,
) -> list[dict[str, Any]]:
    if status == "ready":
        return []
    if status == "license_unresolved":
        adapter = selected or next((candidate for candidate in candidates if candidate.get("available")), None) or {}
        missing_license_env = [
            name for name, present in (adapter.get("license_env") or {}).items()
            if not present
        ]
        return [{
            "stage": stage,
            "reason": "tool_available_but_license_unconfirmed",
            "adapter": adapter.get("adapter"),
            "missing_license_env": missing_license_env,
            "message": "A candidate tool is installed, but the expected license environment was not detected.",
        }]
    if status == "tool_missing":
        requested_commands = sorted({
            command
            for candidate in candidates
            for command in candidate.get("commands", [])
        })
        return [{
            "stage": stage,
            "reason": "no_candidate_command_found",
            "commands": requested_commands[:24],
            "message": "No registered adapter command for this stage was found on PATH or in detected tools.",
        }]
    return [{
        "stage": stage,
        "reason": "no_adapter_registered",
        "message": "No adapter is registered for this stage. Add a custom adapter manifest if this is a private flow.",
    }]


def _vendor_profile(stages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    selected = [
        record["selected"]
        for record in stages.values()
        if record.get("status") == "ready" and record.get("selected")
    ]
    openness_counts: dict[str, int] = {}
    vendor_counts: dict[str, int] = {}
    for candidate in selected:
        openness = str(candidate.get("openness") or "unknown")
        vendor = str(candidate.get("vendor") or "unknown")
        openness_counts[openness] = openness_counts.get(openness, 0) + 1
        vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
    if openness_counts.get("proprietary") and openness_counts.get("open_source"):
        posture = "mixed"
    elif openness_counts.get("proprietary"):
        posture = "proprietary"
    elif openness_counts.get("open_source"):
        posture = "open_source"
    else:
        posture = "unknown"
    return {
        "posture": posture,
        "openness_counts": openness_counts,
        "vendor_counts": vendor_counts,
        "selected_stage_count": len(selected),
    }


def select_stage_adapter(stage: str, tools: dict[str, bool] | None = None, prefer_commercial: bool = True) -> dict[str, Any] | None:
    candidates = list((capability_matrix(tools).get(stage) or {}).get("candidates") or [])
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            item["available"],
            item["license_hint_present"],
            item["openness"] == "proprietary" if prefer_commercial else item["openness"] != "proprietary",
            item["priority"],
        ),
        reverse=True,
    )
    return candidates[0]


def license_env_names() -> tuple[str, ...]:
    names: list[str] = []
    for adapter in load_adapters().values():
        names.extend(adapter.license_env)
    return tuple(dict.fromkeys(names))


def _custom_adapter_payloads() -> list[list[dict[str, Any]]]:
    payloads = []
    inline = os.environ.get("AGENTIC_TOOL_ADAPTERS_JSON", "").strip()
    if inline:
        try:
            parsed = json.loads(inline)
            if isinstance(parsed, dict):
                parsed = parsed.get("adapters", [])
            if isinstance(parsed, list):
                payloads.append(parsed)
        except json.JSONDecodeError:
            pass
    path = os.environ.get("AGENTIC_TOOL_ADAPTERS_FILE", "").strip()
    if path:
        expanded = Path(os.path.expandvars(os.path.expanduser(path)))
        if expanded.is_file():
            try:
                parsed = json.loads(expanded.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    parsed = parsed.get("adapters", [])
                if isinstance(parsed, list):
                    payloads.append(parsed)
            except (OSError, json.JSONDecodeError):
                pass
    return payloads


def _adapter_from_dict(data: dict[str, Any]) -> ToolAdapter:
    parser = data.get("parser") or {}
    return ToolAdapter(
        name=str(data["name"]).strip(),
        commands=tuple(str(item).strip() for item in data.get("commands", []) if str(item).strip()),
        stages=tuple(stage for stage in data.get("stages", []) if stage in STAGES),
        vendor=str(data.get("vendor") or "custom"),
        openness=str(data.get("openness") or "custom"),
        license_env=tuple(str(item).strip() for item in data.get("license_env", []) if str(item).strip()),
        parser=ParserContract(
            error=str(parser.get("error") or r"(?i)\b(error|fatal|failed)\b"),
            warning=str(parser.get("warning") or r"(?i)\b(warning|warn)\b"),
            metrics=dict(parser.get("metrics") or {}),
        ),
        priority=int(data.get("priority") or 70),
        flow=data.get("flow"),
        config_contract=dict(data.get("config_contract") or {}),
        install_hint_env=data.get("install_hint_env"),
    )
