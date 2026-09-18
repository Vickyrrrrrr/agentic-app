from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from agentic_server.vlsi_capability_graph import bind_memory_requirement


INTENT_SCHEMA_VERSION = "agentic.design_intent.v1"
MANIFEST_NAME = "PROJECT_MANIFEST.json"
CANONICAL_DIRS = (
    "docs/plans",
    "docs/diagrams",
    "rtl",
    "tb",
    "dv",
    "constraints",
    "scripts",
    "sim/runs",
    "synth",
    "pnr",
    "sta",
    "signoff",
    "reports",
    "logs",
)
RTL_EXTENSIONS = {".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl"}
SOURCE_EXTENSIONS = RTL_EXTENSIONS | {".sdc", ".tcl", ".ys", ".mk", ".json", ".md", ".py", ".sh"}


class StrictIntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)


class TargetContract(StrictIntentModel):
    pdk: str | None = None
    node_nm: str | None = None
    deliverables: list[str] = Field(default_factory=list, max_length=32)
    flow_backend: str | None = None
    flow_profile: str | None = None


class ClockDomain(StrictIntentModel):
    name: str = "clk"
    period_ns: float | None = None
    source: str = "top"
    modules: list[str] = Field(default_factory=list, max_length=128)


class ResetDomain(StrictIntentModel):
    name: str = "rst_n"
    polarity: Literal["active_low", "active_high", "unspecified"] = "active_low"
    style: Literal["synchronous", "asynchronous", "synchronous_or_unspecified", "unspecified"] = "synchronous_or_unspecified"
    modules: list[str] = Field(default_factory=list, max_length=128)


class InterfacePort(StrictIntentModel):
    name: str
    direction: Literal["input", "output", "inout"]
    width: str = "1"
    role: str = "signal"
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_port_name(cls, value: str) -> str:
        if not hdl_identifier_like(value):
            raise ValueError("port name must be HDL identifier-like")
        return value


class InterfaceContract(StrictIntentModel):
    name: str
    protocol: str = "module_ports"
    ports: list[InterfacePort] = Field(default_factory=list, max_length=256)
    clock: str = "clk"
    reset: str = "rst_n"
    invariants: list[str] = Field(default_factory=list, max_length=128)
    unresolved: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_interface_name(cls, value: str) -> str:
        if not hdl_identifier_like(value):
            raise ValueError("interface name must be HDL identifier-like")
        return value


class ModuleIntent(StrictIntentModel):
    name: str
    kind: str
    owner_file: str
    verification_owner: str | None = None
    status: Literal["planned", "draft", "implemented", "verified", "blocked"] = "planned"
    create_policy: Literal["create_once_then_edit", "edit_existing_only", "generated_artifact"] = "create_once_then_edit"
    dialect: Literal["verilog_2001", "systemverilog", "vhdl"] = "verilog_2001"
    interfaces: list[str] = Field(default_factory=list, max_length=64)
    clock: str = "clk"
    reset: str = "rst_n"
    acceptance_tags: list[str] = Field(default_factory=list, max_length=64)
    reference_model_required: bool = False
    notes: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_module_name(cls, value: str) -> str:
        if not hdl_identifier_like(value) or value[0].isdigit():
            raise ValueError("module name must be a valid HDL identifier")
        return value

    @field_validator("owner_file", "verification_owner")
    @classmethod
    def validate_owned_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not safe_rel_path(value):
            raise ValueError("owned file path must be a safe relative workspace path")
        return value


class AcceptanceCriteria(StrictIntentModel):
    no_placeholder_rtl: bool = True
    compile_required: bool = True
    self_checking_tb_required: bool = True
    lint_required: bool = True
    synthesis_optional: bool = True
    reference_model_required_for_complex_blocks: bool = True
    minimum_quality_level: Literal["draft", "implementation", "industry"] = "implementation"


class ImplementationPolicy(StrictIntentModel):
    rtl_dialect: Literal["verilog_2001", "systemverilog", "vhdl"] = "verilog_2001"
    rtl_extensions: list[str] = Field(default_factory=lambda: [".v"])
    tb_extensions: list[str] = Field(default_factory=lambda: [".sv"])
    allow_systemverilog_rtl: bool = False
    require_edit_existing: bool = True
    forbid_duplicate_module_files: bool = True
    forbid_placeholder_rtl: bool = True
    forbid_root_source_writes: bool = True


class ToolchainStageContract(StrictIntentModel):
    stage: Literal["simulation", "lint", "formal", "synthesis", "pnr", "sta", "physical_verification", "power", "waveform", "utility"]
    adapter: str | None = None
    vendor: str | None = None
    openness: Literal["open_source", "proprietary", "unknown"] = "unknown"
    commands: list[str] = Field(default_factory=list, max_length=32)
    available: bool = False
    license_hint_present: bool | None = None
    evidence_ref: str | None = None


class ToolchainContract(StrictIntentModel):
    selected_backend: str | None = None
    selected_profile: str | None = None
    selected_pdk: str | None = None
    stages: dict[str, ToolchainStageContract] = Field(default_factory=dict)
    fallback_policy: str = "prefer_user_installed_proprietary_or_configured_stack_then_open_source_with_approval"
    unresolved: list[str] = Field(default_factory=list, max_length=64)


class MemoryRequirement(StrictIntentModel):
    name: str
    kind: Literal["sram", "rom", "register_file", "fifo", "cache", "scratchpad", "unknown"] = "unknown"
    owner_module: str | None = None
    width_bits: int | None = None
    depth_words: int | None = None
    capacity_bits: int | None = None
    ports: Literal["1rw", "1r1w", "2rw", "rom", "unspecified"] = "unspecified"
    implementation_preference: Literal["pdk_macro", "compiler_macro", "inferred_small", "unspecified"] = "unspecified"
    macro_binding: str | None = None
    evidence_ref: str | None = None
    unresolved: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_memory_name(cls, value: str) -> str:
        if not hdl_identifier_like(value) or value[0].isdigit():
            raise ValueError("memory requirement name must be HDL identifier-like")
        return value


class BusContract(StrictIntentModel):
    name: str
    protocol: Literal["axi4_lite", "axi4_stream", "axi4", "apb", "ahb", "wishbone", "native", "uart", "spi", "i2c", "unknown"] = "unknown"
    addr_width: int | None = None
    data_width: int | None = None
    clock: str = "clk"
    reset: str = "rst_n"
    endpoints: list[str] = Field(default_factory=list, max_length=128)
    address_map: dict[str, str] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_bus_name(cls, value: str) -> str:
        if not hdl_identifier_like(value):
            raise ValueError("bus name must be HDL identifier-like")
        return value


class RegisterField(StrictIntentModel):
    name: str
    bit_range: str = "0"
    access: Literal["ro", "rw", "wo", "w1c", "rc", "reserved"] = "rw"
    reset: str = "0"
    description: str = ""


class RegisterBlock(StrictIntentModel):
    name: str
    bus: str | None = None
    base_address: str | None = None
    registers: dict[str, list[RegisterField]] = Field(default_factory=dict)
    generated_docs: str | None = None
    generated_header: str | None = None
    unresolved: list[str] = Field(default_factory=list, max_length=64)


class TimingIntent(StrictIntentModel):
    clock: str = "clk"
    period_ns: float | None = None
    frequency_mhz: float | None = None
    source: str = "user_or_default"
    uncertainty_ns: float | None = None
    io_delay_ns: float | None = None


class PhysicalIntent(StrictIntentModel):
    utilization_pct: float | None = None
    die_area: str | None = None
    core_area: str | None = None
    voltage_domains: list[str] = Field(default_factory=list, max_length=32)
    pad_requirements: list[str] = Field(default_factory=list, max_length=128)
    macro_placement_policy: Literal["auto_from_macro_index", "user_floorplan", "not_applicable", "unspecified"] = "unspecified"
    signoff_required: bool = False
    unresolved: list[str] = Field(default_factory=list, max_length=64)


class CapabilityBinding(StrictIntentModel):
    requirement: str
    capability_kind: Literal["memory_macro", "pad_cell", "stdcell", "tool_adapter", "pdk_view", "flow_script"]
    capability_name: str
    pdk: str | None = None
    confidence: Literal["implementation_ready", "physical_timing_ready", "partial_collateral", "name_only", "selected_tool", "unknown"] = "unknown"
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: Literal["candidate", "selected", "rejected", "needs_user_config"] = "candidate"


class DesignIntent(StrictIntentModel):
    schema_version: str = INTENT_SCHEMA_VERSION
    intent_id: str
    design_name: str
    project_root: str
    source_request_digest: str
    source_request_preview: str
    target: TargetContract = Field(default_factory=TargetContract)
    clocks: list[ClockDomain] = Field(default_factory=lambda: [ClockDomain()])
    resets: list[ResetDomain] = Field(default_factory=lambda: [ResetDomain()])
    interfaces: dict[str, InterfaceContract] = Field(default_factory=dict)
    modules: dict[str, ModuleIntent] = Field(default_factory=dict)
    acceptance: AcceptanceCriteria = Field(default_factory=AcceptanceCriteria)
    implementation_policy: ImplementationPolicy = Field(default_factory=ImplementationPolicy)
    toolchain: ToolchainContract = Field(default_factory=ToolchainContract)
    memories: dict[str, MemoryRequirement] = Field(default_factory=dict)
    buses: dict[str, BusContract] = Field(default_factory=dict)
    register_blocks: dict[str, RegisterBlock] = Field(default_factory=dict)
    timing: list[TimingIntent] = Field(default_factory=list)
    physical: PhysicalIntent = Field(default_factory=PhysicalIntent)
    capability_bindings: list[CapabilityBinding] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list, max_length=128)
    revision: int = 1
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @field_validator("project_root")
    @classmethod
    def validate_project_root(cls, value: str) -> str:
        if not safe_rel_path(value) or "/" in value.strip("/"):
            raise ValueError("project_root must be a single safe relative directory")
        return value.strip("/")

    @model_validator(mode="after")
    def module_keys_match(self):
        for key, module in self.modules.items():
            if key != module.name:
                raise ValueError(f"module map key `{key}` does not match module name `{module.name}`")
        return self

    def to_manifest(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["canonical_dirs"] = list(CANONICAL_DIRS)
        data["manifest_path"] = f"{self.project_root}/{MANIFEST_NAME}"
        return data


class WriteDecision(StrictIntentModel):
    allowed: bool
    reason: str
    action: Literal["create", "edit", "reject", "non_rtl"] = "non_rtl"
    module_name: str | None = None
    owner_file: str | None = None
    required_action: str | None = None
    severity: Literal["info", "warning", "error"] = "info"


def build_or_update_design_intent(
    *,
    workspace_root: str,
    design_name: str,
    user_text: str,
    flow_decision: dict[str, Any],
    role_results: list[Any],
    env: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
) -> DesignIntent:
    previous_intent = _load_previous(previous)
    spec = _latest_payload(role_results, "DesignSpec")
    reuse_previous = _reuse_previous_intent(user_text, design_name, spec, previous_intent)
    continuity_intent = previous_intent if reuse_previous else None
    project_root = _project_root(user_text, design_name, spec, continuity_intent)
    target = _target_contract(spec, flow_decision)
    modules = _module_intents(user_text, project_root, spec, continuity_intent)
    interfaces = _interfaces_from_modules(modules, spec)
    toolchain = _toolchain_contract(flow_decision, env)
    memories = _memory_requirements(user_text, spec, modules, continuity_intent)
    buses = _bus_contracts(user_text, spec, modules, continuity_intent)
    register_blocks = _register_blocks(user_text, buses, continuity_intent)
    timing = _timing_intents(user_text, spec, continuity_intent)
    physical = _physical_intent(user_text, spec, memories, continuity_intent)
    capability_bindings = _capability_bindings(env, memories, toolchain, continuity_intent)
    unresolved = _unresolved(spec, modules, memories, buses, toolchain)
    revision = (previous_intent.revision + 1) if reuse_previous and previous_intent else 1
    intent = DesignIntent(
        intent_id=_intent_id(design_name, project_root, user_text),
        design_name=design_name or "scratch",
        project_root=project_root,
        source_request_digest=hashlib.sha256((user_text or "").encode("utf-8")).hexdigest(),
        source_request_preview=(user_text or "")[:1000],
        target=target,
        clocks=[ClockDomain(modules=list(modules.keys()))],
        resets=[ResetDomain(modules=list(modules.keys()))],
        interfaces=interfaces,
        modules=modules,
        acceptance=AcceptanceCriteria(),
        implementation_policy=ImplementationPolicy(),
        toolchain=toolchain,
        memories=memories,
        buses=buses,
        register_blocks=register_blocks,
        timing=timing,
        physical=physical,
        capability_bindings=capability_bindings,
        unresolved=unresolved,
        revision=revision,
        created_at=previous_intent.created_at if reuse_previous and previous_intent else time.time(),
        updated_at=time.time(),
    )
    write_project_manifest(workspace_root, intent)
    return intent


def write_project_manifest(workspace_root: str, intent: DesignIntent) -> str:
    root = Path(workspace_root).resolve()
    for directory in CANONICAL_DIRS:
        (root / intent.project_root / directory).mkdir(parents=True, exist_ok=True)
    path = root / intent.project_root / MANIFEST_NAME
    path.write_text(json.dumps(intent.to_manifest(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path.relative_to(root).as_posix()


def load_project_manifest(workspace_root: str, project_root: str) -> DesignIntent | None:
    path = Path(workspace_root).resolve() / project_root / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return DesignIntent.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def intent_from_state_or_manifest(workspace_root: str, state: dict[str, Any]) -> DesignIntent | None:
    raw = state.get("design_intent")
    if isinstance(raw, dict):
        try:
            return DesignIntent.model_validate(raw)
        except Exception:
            pass
    for root in _candidate_project_roots(workspace_root):
        loaded = load_project_manifest(workspace_root, root)
        if loaded:
            return loaded
    return None


def evaluate_write_against_intent(
    *,
    workspace_root: str,
    design_name: str,
    path: str,
    content: str = "",
    old_string: str = "",
) -> WriteDecision:
    normalized = normalize_rel_path(path)
    if not normalized:
        return WriteDecision(allowed=False, reason="Write path is empty or unsafe.", action="reject", severity="error")
    suffix = Path(normalized).suffix.lower()
    if suffix not in SOURCE_EXTENSIONS:
        return WriteDecision(allowed=True, reason="Non-source artifact is outside RTL ownership policy.", action="non_rtl")
    if not _filename_ok(normalized):
        return WriteDecision(allowed=False, reason=f"Unsafe generated filename `{normalized}`.", action="reject", severity="error")
    state = _load_state(workspace_root, design_name)
    intent = intent_from_state_or_manifest(workspace_root, state)
    if not intent:
        if _is_rtl_source_path(normalized):
            return WriteDecision(
                allowed=False,
                reason="RTL writes require an approved DesignIntent and PROJECT_MANIFEST.json.",
                action="reject",
                required_action="create_or_approve_design_intent",
                severity="error",
            )
        return WriteDecision(allowed=True, reason="Source write is not RTL-owned and no manifest exists.", action="non_rtl")

    if _is_documentation_path(normalized):
        return WriteDecision(allowed=True, reason="Documentation write is allowed under the project documentation contract.", action="non_rtl")
    if _is_verification_path(normalized):
        return WriteDecision(
            allowed=True,
            reason="Verification/testbench source is allowed under the project verification contract.",
            action="non_rtl",
        )
    if intent.implementation_policy.forbid_root_source_writes and "/" not in normalized:
        return WriteDecision(allowed=False, reason="Source files must not be written at workspace root.", action="reject", severity="error")
    if _is_rtl_source_path(normalized):
        return _evaluate_rtl_write(intent, workspace_root, normalized, content, bool(old_string))
    return WriteDecision(allowed=True, reason="Non-RTL source write is allowed.", action="non_rtl")


def normalize_rel_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." or part.startswith(".") for part in parts):
        return ""
    return "/".join(parts)


def safe_rel_path(path: str) -> bool:
    return bool(normalize_rel_path(path))


def hdl_identifier_like(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in {"_", "$"} for ch in value)


def _evaluate_rtl_write(intent: DesignIntent, workspace_root: str, path: str, content: str, is_edit: bool) -> WriteDecision:
    suffix = Path(path).suffix.lower()
    if intent.implementation_policy.rtl_dialect == "verilog_2001" and suffix == ".sv" and not intent.implementation_policy.allow_systemverilog_rtl:
        return WriteDecision(
            allowed=False,
            reason="Synthesizable RTL policy is Verilog-2001; use `.v` for RTL and reserve `.sv` for testbench/assertions.",
            action="reject",
            required_action="write_verilog_2001_rtl",
            severity="error",
        )
    module_name = _module_name_for_path_or_content(path, content)
    owner = _owner_for_path(intent, path) or (intent.modules.get(module_name or "") if module_name else None)
    if not owner:
        return WriteDecision(
            allowed=False,
            reason=f"`{path}` is not owned by any module in PROJECT_MANIFEST.json.",
            action="reject",
            required_action="update_design_intent_module_ownership",
            severity="error",
        )
    if path != owner.owner_file:
        return WriteDecision(
            allowed=False,
            reason=f"Module `{owner.name}` is owned by `{owner.owner_file}`, not `{path}`.",
            action="reject",
            module_name=owner.name,
            owner_file=owner.owner_file,
            required_action="edit_owner_file",
            severity="error",
        )
    owner_full = Path(workspace_root).resolve() / owner.owner_file
    if owner_full.is_file() and intent.implementation_policy.require_edit_existing and not is_edit:
        return WriteDecision(
            allowed=False,
            reason=f"`{owner.owner_file}` already exists; patch it with old_string/new_string instead of rewriting or creating variants.",
            action="reject",
            module_name=owner.name,
            owner_file=owner.owner_file,
            required_action="surgical_edit_existing_owner",
            severity="error",
        )
    return WriteDecision(
        allowed=True,
        reason=f"Write targets the manifest owner for module `{owner.name}`.",
        action="edit" if owner_full.is_file() else "create",
        module_name=owner.name,
        owner_file=owner.owner_file,
    )


def _module_intents(user_text: str, project_root: str, spec: dict[str, Any], previous: DesignIntent | None) -> dict[str, ModuleIntent]:
    if previous and previous.modules:
        return previous.modules
    contracts = [item for item in spec.get("module_contracts") or [] if isinstance(item, dict)]
    if _looks_soc(user_text, spec):
        prefix = _safe_module_name(project_root)
        names = (
            f"{prefix}_top",
            f"{prefix}_control_core",
            f"{prefix}_dsp_tile",
            f"{prefix}_accelerator_tile",
            f"{prefix}_memory_subsystem",
            f"{prefix}_interconnect_fabric",
        )
        kinds = ("soc_top", "control_core", "dsp", "accelerator", "memory_controller", "interconnect")
        return {
            name: ModuleIntent(
                name=name,
                kind=kind,
                owner_file=f"{project_root}/rtl/{name}.v",
                verification_owner=f"{project_root}/tb/tb_{name}.sv",
                interfaces=[f"{name}_ports"],
                acceptance_tags=["non_placeholder", "reset_defined", "self_checking_tb"],
                reference_model_required=kind in {"dsp", "accelerator", "memory_controller", "interconnect"},
            )
            for name, kind in zip(names, kinds)
        }
    if contracts:
        modules: dict[str, ModuleIntent] = {}
        for item in contracts:
            name = _safe_module_name(str(item.get("module_name") or item.get("block_type") or _request_slug(user_text) or project_root))
            modules[name] = ModuleIntent(
                name=name,
                kind=str(item.get("block_type") or "unspecified_requested_block"),
                owner_file=f"{project_root}/rtl/{name}.v",
                verification_owner=f"{project_root}/tb/tb_{name}.sv",
                interfaces=[f"{name}_ports"],
                acceptance_tags=["non_placeholder", "reset_defined", "self_checking_tb"],
                reference_model_required=_complex_kind(str(item.get("block_type") or "")),
            )
        return modules
    name = _safe_module_name(_request_slug(user_text) or project_root or _digest_project_root(user_text, "", spec))
    return {
        name: ModuleIntent(
            name=name,
            kind="unspecified_requested_block",
            owner_file=f"{project_root}/rtl/{name}.v",
            verification_owner=f"{project_root}/tb/tb_{name}.sv",
            interfaces=[f"{name}_ports"],
            status="blocked",
            notes=["Generated from unresolved user intent; implementation must wait for spec approval."],
        )
    }


def _interfaces_from_modules(modules: dict[str, ModuleIntent], spec: dict[str, Any]) -> dict[str, InterfaceContract]:
    contracts_by_name: dict[str, dict[str, Any]] = {}
    for item in spec.get("module_contracts") or []:
        if isinstance(item, dict) and item.get("module_name"):
            contracts_by_name[_safe_module_name(str(item["module_name"]))] = item
    interfaces: dict[str, InterfaceContract] = {}
    for module in modules.values():
        contract = contracts_by_name.get(module.name) or {}
        ports = []
        for port in contract.get("ports") or _default_ports():
            if isinstance(port, dict):
                try:
                    ports.append(InterfacePort(
                        name=str(port.get("name") or "signal"),
                        direction=str(port.get("direction") or "input"),
                        width=str(port.get("width") or "1"),
                        role=str(port.get("role") or "signal"),
                        description=str(port.get("description") or ""),
                    ))
                except Exception:
                    continue
        interface_name = module.interfaces[0] if module.interfaces else f"{module.name}_ports"
        interfaces[interface_name] = InterfaceContract(
            name=interface_name,
            ports=ports or [InterfacePort(name="clk", direction="input", role="clock"), InterfacePort(name="rst_n", direction="input", role="reset")],
            unresolved=list(contract.get("unresolved") or []),
        )
    return interfaces


def _target_contract(spec: dict[str, Any], flow_decision: dict[str, Any]) -> TargetContract:
    selected = flow_decision.get("selected_pdk") or {}
    return TargetContract(
        pdk=spec.get("target_pdk") or selected.get("name"),
        node_nm=spec.get("target_node_nm") or (str(selected.get("node_nm")) if selected.get("node_nm") else None),
        deliverables=list(spec.get("deliverables") or ["rtl", "testbench", "simulation"]),
        flow_backend=flow_decision.get("backend"),
        flow_profile=flow_decision.get("profile"),
    )


def _latest_payload(role_results: list[Any], kind: str) -> dict[str, Any]:
    for result in reversed(role_results or []):
        for envelope in reversed(getattr(result, "envelopes", []) or []):
            if getattr(envelope, "kind", "") == kind:
                payload = getattr(envelope, "payload", {}) or {}
                return payload if isinstance(payload, dict) else {}
    return {}


def _load_previous(state: dict[str, Any] | None) -> DesignIntent | None:
    raw = (state or {}).get("design_intent")
    if isinstance(raw, dict):
        try:
            return DesignIntent.model_validate(raw)
        except Exception:
            return None
    return None


def _load_state(workspace_root: str, design_name: str) -> dict[str, Any]:
    path = Path(workspace_root).resolve() / ".agentic" / "design_state" / f"{_safe_design_name(design_name)}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _candidate_project_roots(workspace_root: str) -> list[str]:
    root = Path(workspace_root).resolve()
    candidates = []
    try:
        for item in root.iterdir():
            if item.is_dir() and not item.name.startswith(".") and (item / MANIFEST_NAME).is_file():
                candidates.append(item.name)
    except OSError:
        pass
    return sorted(candidates)


def _project_root(user_text: str, design_name: str, spec: dict[str, Any], previous: DesignIntent | None) -> str:
    if previous:
        return previous.project_root
    contracts = [item for item in spec.get("module_contracts") or [] if isinstance(item, dict)]
    if contracts:
        name = _slug(str(contracts[0].get("module_name") or contracts[0].get("block_type") or ""))
        if name:
            return name[:48]
    for candidate in (
        str(spec.get("title") or ""),
        user_text,
        "" if str(design_name).startswith("design_") else str(design_name or ""),
    ):
        name = _request_slug(candidate)
        if name:
            return name[:48]
    return _digest_project_root(user_text, design_name, spec)


def _reuse_previous_intent(user_text: str, design_name: str, spec: dict[str, Any], previous: DesignIntent | None) -> bool:
    if not previous:
        return False
    lowered = (user_text or "").lower()
    if re.search(r"\b(new|another|different|separate)\b", lowered):
        return False
    if re.search(r"\b(change|update|modify|fix|repair|add|remove|replace|rerun|continue|improve|debug)\b", lowered):
        return True
    candidate = _project_root_candidate(user_text, design_name, spec)
    return not candidate or candidate == previous.project_root


def _project_root_candidate(user_text: str, design_name: str, spec: dict[str, Any]) -> str:
    contracts = [item for item in spec.get("module_contracts") or [] if isinstance(item, dict)]
    if contracts:
        name = _slug(str(contracts[0].get("module_name") or contracts[0].get("block_type") or ""))
        if name:
            return name[:48]
    for candidate in (
        str(spec.get("title") or ""),
        user_text,
        "" if str(design_name).startswith("design_") else str(design_name or ""),
    ):
        name = _request_slug(candidate)
        if name:
            return name[:48]
    return ""


def _looks_soc(user_text: str, spec: dict[str, Any]) -> bool:
    text = f"{user_text} {spec.get('title') or ''}".lower()
    return bool(re.search(r"\b(best\s+chip|complex\s+chip|system\s+on\s+chip|soc|multi[- ]?core)\b", text))


def _complex_kind(kind: str) -> bool:
    return kind.lower() in {"cpu", "riscv", "risc-v", "dma", "fifo", "bridge", "controller", "accelerator", "dsp", "fft", "fir", "aes", "noc", "cache"}


def _toolchain_contract(flow_decision: dict[str, Any], env: dict[str, Any] | None) -> ToolchainContract:
    stages: dict[str, ToolchainStageContract] = {}
    stage_plan = ((flow_decision.get("run_strategy") or {}).get("stage_plan") or {})
    adapter_matrix = (env or {}).get("tool_adapters") or flow_decision.get("adapter_matrix") or {}
    for stage in ("simulation", "lint", "formal", "synthesis", "pnr", "sta", "physical_verification", "power"):
        selected = stage_plan.get(stage) or ((adapter_matrix.get(stage) or {}).get("selected") or {})
        if not selected:
            continue
        stages[stage] = ToolchainStageContract(
            stage=stage,  # type: ignore[arg-type]
            adapter=selected.get("adapter") or selected.get("name"),
            vendor=selected.get("vendor"),
            openness=selected.get("openness") or "unknown",
            commands=list(selected.get("commands") or [])[:32],
            available=bool(selected.get("available")),
            license_hint_present=selected.get("license_hint_present"),
            evidence_ref=f"capability_index.toolchains.stages.{stage}",
        )
    blockers = list(flow_decision.get("blockers") or [])
    return ToolchainContract(
        selected_backend=flow_decision.get("backend"),
        selected_profile=flow_decision.get("profile"),
        selected_pdk=((flow_decision.get("selected_pdk") or {}).get("name")),
        stages=stages,
        unresolved=blockers[:64],
    )


def _memory_requirements(
    user_text: str,
    spec: dict[str, Any],
    modules: dict[str, ModuleIntent],
    previous: DesignIntent | None,
) -> dict[str, MemoryRequirement]:
    if previous and previous.memories:
        return previous.memories
    text = f"{user_text} {json.dumps(spec, default=str)}".lower()
    requirements: dict[str, MemoryRequirement] = {}
    dims = _memory_dims_from_text(text)
    memory_words = {
        "ram": "sram",
        "scratchpad": "scratchpad",
        "rom": "rom",
        "bootrom": "rom",
        "fifo": "fifo",
        "cache": "cache",
        "register file": "register_file",
        "regfile": "register_file",
    }
    if "scratchpad" not in text:
        memory_words["sram"] = "sram"
    for token, kind in memory_words.items():
        if token == "ram" and re.search(r"(?i)scratchpad", text):
            continue
        if token in text:
            name = _safe_module_name(f"{kind}_0")
            requirements[name] = MemoryRequirement(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                owner_module=_memory_owner(modules),
                width_bits=dims.get("width_bits"),
                depth_words=dims.get("depth_words"),
                capacity_bits=dims.get("capacity_bits"),
                ports=_memory_ports_from_text(text, kind),
                implementation_preference="pdk_macro" if kind in {"sram", "rom", "cache", "scratchpad"} else "unspecified",
                unresolved=_memory_unresolved(kind, dims),
            )
    for module in modules.values():
        if module.kind in {"memory_controller", "cache"} and not requirements:
            requirements["memory_subsystem"] = MemoryRequirement(
                name="memory_subsystem",
                kind="cache" if module.kind == "cache" else "unknown",
                owner_module=module.name,
                implementation_preference="pdk_macro",
                unresolved=["Memory capacity, port semantics, and macro binding are not yet specified."],
            )
        if module.kind == "fifo" and "fifo_storage" not in requirements:
            requirements["fifo_storage"] = MemoryRequirement(
                name="fifo_storage",
                kind="fifo",
                owner_module=module.name,
                implementation_preference="inferred_small",
                unresolved=["FIFO depth/width should be confirmed or inferred from interface contract."],
            )
    return requirements


def _bus_contracts(
    user_text: str,
    spec: dict[str, Any],
    modules: dict[str, ModuleIntent],
    previous: DesignIntent | None,
) -> dict[str, BusContract]:
    if previous and previous.buses:
        return previous.buses
    text = f"{user_text} {json.dumps(spec, default=str)}".lower()
    protocols = (
        ("axi4_lite", ("axi4-lite", "axi lite", "axi-lite", "axi4 lite")),
        ("axi4_stream", ("axi-stream", "axis", "axi4-stream")),
        ("axi4", ("axi4", "axi")),
        ("apb", ("apb",)),
        ("ahb", ("ahb",)),
        ("wishbone", ("wishbone", "wb bus")),
        ("uart", ("uart",)),
        ("spi", ("spi",)),
        ("i2c", ("i2c", "iic")),
    )
    buses: dict[str, BusContract] = {}
    widths = _bus_widths_from_text(text)
    for protocol, aliases in protocols:
        if any(alias in text for alias in aliases):
            name = f"{protocol}_bus"
            buses[name] = BusContract(
                name=name,
                protocol=protocol,  # type: ignore[arg-type]
                addr_width=widths.get("addr_width") if protocol not in {"axi4_stream", "uart", "spi", "i2c"} else None,
                data_width=widths.get("data_width"),
                endpoints=list(modules.keys())[:128],
                unresolved=[] if protocol in {"uart", "spi", "i2c"} else ["Address map must be generated and approved before integration."],
            )
    if _looks_soc(user_text, spec) and not buses:
        buses["native_control_bus"] = BusContract(
            name="native_control_bus",
            protocol="native",
            addr_width=32,
            data_width=32,
            endpoints=list(modules.keys())[:128],
            unresolved=["No standard control bus was specified; use native until user selects APB/AHB/AXI/Wishbone."],
        )
    return buses


def _register_blocks(user_text: str, buses: dict[str, BusContract], previous: DesignIntent | None) -> dict[str, RegisterBlock]:
    if previous and previous.register_blocks:
        return previous.register_blocks
    lowered = (user_text or "").lower()
    needs_regs = any(term in lowered for term in ("register map", "csr", "control register", "status register", "memory mapped", "mmio", "peripheral"))
    if not needs_regs and not any(bus.protocol in {"apb", "axi4_lite", "ahb", "wishbone", "native"} for bus in buses.values()):
        return {}
    bus_name = next(iter(buses), None)
    return {
        "csr": RegisterBlock(
            name="csr",
            bus=bus_name,
            registers={
                "control": [
                    RegisterField(name="enable", bit_range="0", access="rw", reset="0", description="Block enable"),
                    RegisterField(name="soft_reset", bit_range="1", access="w1c", reset="0", description="Software-visible reset pulse"),
                ],
                "status": [
                    RegisterField(name="busy", bit_range="0", access="ro", reset="0", description="Operation in progress"),
                    RegisterField(name="error", bit_range="1", access="w1c", reset="0", description="Sticky error indicator"),
                ],
            },
            unresolved=["Base address and complete register map require approval before RTL generation."],
        )
    }


def _timing_intents(user_text: str, spec: dict[str, Any], previous: DesignIntent | None) -> list[TimingIntent]:
    if previous and previous.timing:
        return previous.timing
    text = f"{user_text} {json.dumps(spec.get('constraints') or {}, default=str)}".lower()
    mhz = re.search(r"(\d+(?:\.\d+)?)\s*mhz", text)
    ns = re.search(r"(\d+(?:\.\d+)?)\s*ns", text)
    if mhz:
        freq = float(mhz.group(1))
        return [TimingIntent(clock="clk", frequency_mhz=freq, period_ns=1000.0 / freq, source="user_request")]
    if ns:
        period = float(ns.group(1))
        return [TimingIntent(clock="clk", period_ns=period, frequency_mhz=1000.0 / period if period else None, source="user_request")]
    return [TimingIntent(clock="clk", source="default_unconfirmed")]


def _physical_intent(
    user_text: str,
    spec: dict[str, Any],
    memories: dict[str, MemoryRequirement],
    previous: DesignIntent | None,
) -> PhysicalIntent:
    if previous:
        return previous.physical
    text = f"{user_text} {json.dumps(spec, default=str)}".lower()
    util = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:util|utilization)", text)
    signoff_required = any(term in text for term in ("gds", "gdsii", "pnr", "place", "route", "drc", "lvs", "signoff", "tapeout"))
    pad_requirements = []
    if re.search(r"(?i)\b(pad\s*ring|gpio\s+pad|io\s+ring|i/o\s+ring|pad\s+cell|esd\s+pad)\b", text):
        pad_requirements.append("io_pad_ring")
    unresolved = []
    if signoff_required and not pad_requirements:
        unresolved.append("Pad ring / IO constraint policy is not specified.")
    if memories:
        unresolved.append("Macro placement must bind to discovered SRAM/ROM collateral before physical implementation.")
    return PhysicalIntent(
        utilization_pct=float(util.group(1)) if util else None,
        pad_requirements=pad_requirements,
        macro_placement_policy="auto_from_macro_index" if memories else "unspecified",
        signoff_required=signoff_required,
        unresolved=unresolved,
    )


def _capability_bindings(
    env: dict[str, Any] | None,
    memories: dict[str, MemoryRequirement],
    toolchain: ToolchainContract,
    previous: DesignIntent | None,
) -> list[CapabilityBinding]:
    if previous and previous.capability_bindings:
        return previous.capability_bindings
    bindings: list[CapabilityBinding] = []
    capability_index = (env or {}).get("capability_index") or {}
    for stage, contract in toolchain.stages.items():
        if contract.adapter:
            bindings.append(CapabilityBinding(
                requirement=f"toolchain.{stage}",
                capability_kind="tool_adapter",
                capability_name=contract.adapter,
                confidence="selected_tool",
                evidence={"stage": stage, "commands": contract.commands, "vendor": contract.vendor},
                status="selected" if contract.available and (contract.license_hint_present is not False) else "candidate",
            ))
    capability_graph = (env or {}).get("capability_graph") or {}
    selected_pdk = toolchain.selected_pdk
    for requirement in memories.values():
        binding = bind_memory_requirement(
            requirement.model_dump(mode="json"),
            capability_graph,
            target_pdk=selected_pdk,
            require_physical=True,
        ) if capability_graph else {}
        selected = binding.get("selected") if isinstance(binding, dict) else None
        if not selected:
            continue
        macro = selected.get("macro") or {}
        evidence_ref = f"capability_graph.memory_macro.{macro.get('pdk')}.{macro.get('name')}"
        requirement.macro_binding = str(macro.get("name") or "")
        requirement.evidence_ref = evidence_ref
        bindings.append(CapabilityBinding(
            requirement=f"memory.{requirement.name}",
            capability_kind="memory_macro",
            capability_name=macro.get("name") or "unknown",
            pdk=macro.get("pdk"),
            confidence=macro.get("confidence") or "unknown",
            evidence={
                "views": (selected.get("evidence") or {}).get("views") or {},
                "width_bits": macro.get("width_bits"),
                "depth_words": macro.get("depth_words"),
                "capacity_bits": macro.get("capacity_bits"),
                "fit": selected.get("fit") or {},
                "integration_contract": binding.get("integration_contract") or {},
                "blockers": binding.get("blockers") or [],
            },
            status="selected" if binding.get("status") == "selected" else "candidate",
        ))
    return bindings[:128]


def _unresolved(
    spec: dict[str, Any],
    modules: dict[str, ModuleIntent],
    memories: dict[str, MemoryRequirement],
    buses: dict[str, BusContract],
    toolchain: ToolchainContract,
) -> list[str]:
    out = list(spec.get("open_questions") or [])
    if not modules:
        out.append("No module ownership exists.")
    for memory in memories.values():
        out.extend(memory.unresolved)
        if memory.implementation_preference in {"pdk_macro", "compiler_macro"} and not memory.macro_binding:
            out.append(f"Memory `{memory.name}` requires macro binding before physical implementation.")
    for bus in buses.values():
        out.extend(bus.unresolved)
    out.extend(toolchain.unresolved)
    return _unique(out)


def _memory_dims_from_text(text: str) -> dict[str, int | None]:
    text = text or ""
    explicit = re.search(r"(\d+)\s*[x×]\s*(\d+)", text)
    if explicit:
        return {"depth_words": int(explicit.group(1)), "width_bits": int(explicit.group(2)), "capacity_bits": int(explicit.group(1)) * int(explicit.group(2))}
    capacity = re.search(r"(\d+)\s*(kb|kib|mb|mib|bits?|bytes?)", text)
    width = re.search(r"(\d+)\s*[- ]?bit", text)
    width_bits = int(width.group(1)) if width else None
    capacity_bits = None
    if capacity:
        value = int(capacity.group(1))
        unit = capacity.group(2)
        if unit in {"kb", "kib"}:
            capacity_bits = value * 1024 * 8
        elif unit in {"mb", "mib"}:
            capacity_bits = value * 1024 * 1024 * 8
        elif unit.startswith("byte"):
            capacity_bits = value * 8
        else:
            capacity_bits = value
    depth_words = (capacity_bits // width_bits) if capacity_bits and width_bits else None
    return {"depth_words": depth_words, "width_bits": width_bits, "capacity_bits": capacity_bits}


def _memory_ports_from_text(text: str, kind: str) -> Literal["1rw", "1r1w", "2rw", "rom", "unspecified"]:
    if kind == "rom" or "rom" in text:
        return "rom"
    if any(term in text for term in ("dual port", "2rw", "two port", "2 port")):
        return "2rw"
    if any(term in text for term in ("1r1w", "one read one write", "read and write port")):
        return "1r1w"
    if any(term in text for term in ("1rw", "single port", "one port")):
        return "1rw"
    return "unspecified"


def _memory_unresolved(kind: str, dims: dict[str, int | None]) -> list[str]:
    unresolved = []
    if kind in {"sram", "rom", "cache", "scratchpad"} and not dims.get("capacity_bits"):
        unresolved.append("Memory capacity is not fully specified.")
    if kind in {"sram", "rom", "cache", "scratchpad"} and not dims.get("width_bits"):
        unresolved.append("Memory data width is not fully specified.")
    return unresolved


def _memory_owner(modules: dict[str, ModuleIntent]) -> str | None:
    for module in modules.values():
        if module.kind in {"memory_controller", "cache", "fifo"}:
            return module.name
    for module in modules.values():
        return module.name
    return None


def _bus_widths_from_text(text: str) -> dict[str, int | None]:
    addr = re.search(r"(?:addr|address)\s*(?:width)?\s*(?:=|:)?\s*(\d+)", text)
    data = re.search(r"(?:data)\s*(?:width)?\s*(?:=|:)?\s*(\d+)", text)
    generic = re.search(r"(\d+)\s*[- ]?bit", text)
    return {
        "addr_width": int(addr.group(1)) if addr else 32 if any(term in text for term in ("axi", "apb", "ahb", "wishbone")) else None,
        "data_width": int(data.group(1)) if data else int(generic.group(1)) if generic else 32 if any(term in text for term in ("axi", "apb", "ahb", "wishbone")) else None,
    }


def _owner_for_path(intent: DesignIntent, path: str) -> ModuleIntent | None:
    for module in intent.modules.values():
        if module.owner_file == path:
            return module
    return None


def _module_name_for_path_or_content(path: str, content: str) -> str | None:
    match = re.search(r"(?m)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", content or "")
    if match:
        return match.group(1)
    stem = Path(path).stem
    return _safe_module_name(stem) if stem else None


def _is_documentation_path(path: str) -> bool:
    return "/docs/" in f"/{path}" or "/reports/" in f"/{path}" or path.endswith((".md", ".mermaid", ".mmd"))


def _is_verification_path(path: str) -> bool:
    wrapped = f"/{path.lower()}"
    return "/tb/" in wrapped or "/dv/" in wrapped or "/verification/" in wrapped or "/formal/" in wrapped


def _is_rtl_source_path(path: str) -> bool:
    lower = path.lower()
    if _is_verification_path(lower):
        return False
    return "/rtl/" in f"/{lower}" or Path(lower).suffix in RTL_EXTENSIONS


def _filename_ok(path: str) -> bool:
    name = Path(path).name
    if not name or any(ch in name for ch in "[]{}() \t\n\r"):
        return False
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _default_ports() -> list[dict[str, str]]:
    return [
        {"name": "clk", "direction": "input", "width": "1", "role": "clock"},
        {"name": "rst_n", "direction": "input", "width": "1", "role": "reset"},
    ]


def _safe_module_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_$]", "_", value or "").strip("_")
    if not cleaned:
        cleaned = "m_" + hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:8]
    if cleaned[0].isdigit():
        cleaned = f"m_{cleaned}"
    return cleaned


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").lower()).strip("_")


def _request_slug(value: str) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", value or "")
    stop = {
        "a", "an", "the", "on", "for", "with", "using", "please", "make",
        "create", "build", "design", "implement", "generate", "rtl", "gds",
        "gdsii", "chip", "core", "block", "module", "can", "you", "me",
    }
    kept = [word.lower() for word in words if word.lower() not in stop]
    return "_".join(kept[:8]).strip("_")


def _digest_project_root(user_text: str, design_name: str, spec: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps({
        "user_text": user_text,
        "design_name": design_name,
        "title": spec.get("title"),
    }, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
    return f"intent_{digest}"


def _intent_id(design_name: str, project_root: str, user_text: str) -> str:
    digest = hashlib.sha256(f"{design_name}:{project_root}:{user_text}".encode("utf-8")).hexdigest()[:12]
    return f"{project_root}_{digest}"


def _safe_design_name(design_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in (design_name or "scratch"))
    return cleaned.strip("._") or "scratch"


def _unique(items: list[Any]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out
