from __future__ import annotations

import re
import hashlib
import time
from dataclasses import MISSING, asdict, dataclass, field, is_dataclass
from typing import Any


SCHEMA_VERSION = "agentic.handoff.v1"


@dataclass
class ClockResetContract:
    clock: str = "clk"
    reset: str = "rst_n"
    reset_polarity: str = "active_low"
    reset_style: str = "synchronous_or_unspecified"
    notes: list[str] = field(default_factory=list)


@dataclass
class PortSpec:
    name: str
    direction: str
    width: str = "1"
    role: str = "signal"
    description: str = ""


@dataclass
class ModuleContract:
    module_name: str
    block_type: str
    ports: list[PortSpec] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    clock_reset: ClockResetContract = field(default_factory=ClockResetContract)
    invariants: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


@dataclass
class DesignSpec:
    title: str
    intent: str
    target_pdk: str | None = None
    target_node_nm: str | None = None
    deliverables: list[str] = field(default_factory=list)
    module_contracts: list[ModuleContract] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    confidence: str = "draft"


@dataclass
class VerificationPlan:
    strategy: str
    testbench_language: str = "systemverilog_or_available"
    required_tests: list[str] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)
    coverage_goals: list[str] = field(default_factory=list)
    pass_fail_contract: list[str] = field(default_factory=list)


@dataclass
class FilePatchPlan:
    path: str
    action: str
    stage: str
    purpose: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class RTLDelta:
    module_name: str
    module_contract: dict[str, Any]
    planned_files: list[FilePatchPlan] = field(default_factory=list)
    synthesis_contract: list[str] = field(default_factory=list)
    implementation_notes: list[str] = field(default_factory=list)


@dataclass
class FailureReproducer:
    stage: str
    command_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    minimal_repro_steps: list[str] = field(default_factory=list)
    expected_failure_signature: list[str] = field(default_factory=list)


@dataclass
class CoverageEvidence:
    stage: str
    covered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class FlowLockContract:
    """Enterprise Flow Lock contract: locks company-approved Makefiles/TCL flows for multi-engineer teams."""
    flow_name: str
    signed_off_by: str
    master_script_path: str
    is_locked: bool = True
    allowed_parameters: list[str] = field(default_factory=list)
    prohibited_actions: list[str] = field(default_factory=list)
    team_leads: list[str] = field(default_factory=list)
    group_project_id: str = "default_group"




@dataclass
class ToolAdapterPlan:
    selected_backend: str | None
    selected_pdk: str | None
    required_capabilities: list[str] = field(default_factory=list)
    adapter_sequence: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    flow_lock: FlowLockContract | None = None
    fallback_policy: str = "strictly_use_user_signed_off_flow_and_makefiles"



@dataclass
class FailureAnalysis:
    failing_stage: str
    evidence_refs: list[str] = field(default_factory=list)
    suspected_root_causes: list[str] = field(default_factory=list)
    patch_plan: list[str] = field(default_factory=list)
    recheck_scope: list[str] = field(default_factory=list)


@dataclass
class SignoffAudit:
    required_evidence: list[str] = field(default_factory=list)
    present_evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    spec_drift_checks: list[str] = field(default_factory=list)
    residual_risk: list[str] = field(default_factory=list)


@dataclass
class HandoffEnvelope:
    source_role: str
    target_role: str
    kind: str
    payload: dict[str, Any]
    intent_digest: str
    scope: str
    evidence_refs: list[str] = field(default_factory=list)
    open_risks: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    created_at: float = field(default_factory=time.time)

    def validate(self) -> None:
        required = {
            "source_role": self.source_role,
            "target_role": self.target_role,
            "kind": self.kind,
            "intent_digest": self.intent_digest,
            "scope": self.scope,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"handoff missing required fields: {', '.join(missing)}")
        if not isinstance(self.payload, dict):
            raise TypeError("handoff payload must be a dictionary")

    def to_record(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


def payload_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    raise TypeError(f"unsupported handoff payload type: {type(value)!r}")


def seed_design_spec(user_text: str, flow_decision: dict[str, Any], needs_spec_clarification: bool) -> DesignSpec:
    lowered = (user_text or "").lower()
    block_type = _detect_block_type(lowered)
    module_name = _module_name(block_type, lowered)
    selected_pdk = flow_decision.get("selected_pdk") or {}
    deliverables = _detect_deliverables(lowered)
    open_questions = []
    if needs_spec_clarification:
        open_questions.extend([
            "Exact block type and behavioral contract are not fully specified.",
            "Interface ports, widths, clock/reset, and acceptance tests need confirmation.",
        ])
    if not selected_pdk:
        open_questions.append("Target PDK/node is not selected from local evidence.")
    if "gds" in lowered or "harden" in lowered:
        deliverables.extend(["synthesis", "pnr", "gdsii", "signoff_reports"])

    contract = ModuleContract(
        module_name=module_name,
        block_type=block_type,
        ports=_seed_ports(block_type),
        invariants=_seed_invariants(block_type),
        unresolved=open_questions[:],
    )
    return DesignSpec(
        title=_title_from_text(user_text, block_type),
        intent=(user_text or "").strip()[:1000],
        target_pdk=selected_pdk.get("name"),
        target_node_nm=str(selected_pdk.get("node_nm")) if selected_pdk.get("node_nm") else None,
        deliverables=_unique(deliverables or ["rtl", "testbench", "simulation"]),
        module_contracts=[contract],
        constraints=_seed_constraints(lowered),
        assumptions=[] if not needs_spec_clarification else ["No implementation may start until the user approves a concrete spec/plan."],
        open_questions=_unique(open_questions),
        confidence="blocked_for_spec" if needs_spec_clarification else "draft",
    )


def seed_verification_plan(spec: DesignSpec) -> VerificationPlan:
    block_type = spec.module_contracts[0].block_type if spec.module_contracts else "block"
    tests = [
        "reset behavior",
        "basic transaction smoke test",
        "back-to-back operation",
        "invalid/stress input handling",
    ]
    if block_type in {"fifo", "dma", "bridge", "controller"}:
        tests.extend(["full/empty or backpressure behavior", "ordering and data integrity"])
    return VerificationPlan(
        strategy="self-checking simulation plus assertions where supported",
        required_tests=_unique(tests),
        assertions=["no X-propagation after reset", "outputs obey interface-valid contract"],
        coverage_goals=["toggle major control states", "exercise boundary conditions"],
        pass_fail_contract=["compile/elaboration must pass", "all self-checks must pass", "no uncontrolled fatal warnings"],
    )


def seed_tool_adapter_plan(flow_decision: dict[str, Any]) -> ToolAdapterPlan:
    selected = flow_decision.get("selected_pdk") or {}
    backend = flow_decision.get("backend")
    run_strategy = flow_decision.get("run_strategy") or {}
    stage_plan = run_strategy.get("stage_plan") or {}
    stages = [{"stage": "discovery", "adapter": "local_environment", "required": True}]
    for stage in ("simulation", "synthesis", "pnr", "sta", "physical_verification"):
        selected_adapter = stage_plan.get(stage) or {}
        adapter_name = selected_adapter.get("adapter") or selected_adapter.get("name")
        stages.append({
            "stage": stage,
            "adapter": adapter_name or f"detected_{stage}",
            "vendor": selected_adapter.get("vendor"),
            "openness": selected_adapter.get("openness"),
            "available": bool(selected_adapter.get("available")) if selected_adapter else False,
            "license_hint_present": selected_adapter.get("license_hint_present"),
            "required": stage in {"simulation", "synthesis"} or stage in str(flow_decision.get("profile", "")),
        })
    if not stage_plan and backend:
        stages.append({"stage": "implementation", "adapter": backend, "required": backend not in {"none", "synthesis_only"}})
    return ToolAdapterPlan(
        selected_backend=backend,
        selected_pdk=selected.get("name"),
        required_capabilities=["compile", "simulate", "parse_logs", "record_evidence"],
        adapter_sequence=stages,
        blockers=list(flow_decision.get("blockers") or [])[:8],
    )


def seed_signoff_audit(spec: DesignSpec) -> SignoffAudit:
    required = ["approved_spec", "rtl_manifest", "verification_pass"]
    if any(item in spec.deliverables for item in ("synthesis", "pnr", "gdsii", "signoff_reports")):
        required.extend(["synthesis_report", "sta_report", "drc_lvs_if_available", "artifact_manifest"])
    return SignoffAudit(
        required_evidence=required,
        missing_evidence=required[:],
        spec_drift_checks=[
            "generated files implement approved module/interface only",
            "constraints match approved clock/reset assumptions",
            "debug fixes do not silently relax user intent",
        ],
        residual_risk=["No signoff claim is valid until required evidence nodes are present."],
    )


def schema_catalog() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "payloads": {
            "DesignSpec": _schema_for_dataclass(DesignSpec),
            "ModuleContract": _schema_for_dataclass(ModuleContract),
            "RTLDelta": _schema_for_dataclass(RTLDelta),
            "FilePatchPlan": _schema_for_dataclass(FilePatchPlan),
            "VerificationPlan": _schema_for_dataclass(VerificationPlan),
            "FailureReproducer": _schema_for_dataclass(FailureReproducer),
            "CoverageEvidence": _schema_for_dataclass(CoverageEvidence),
            "ToolAdapterPlan": _schema_for_dataclass(ToolAdapterPlan),
            "FailureAnalysis": _schema_for_dataclass(FailureAnalysis),
            "SignoffAudit": _schema_for_dataclass(SignoffAudit),
        },
    }


def _detect_block_type(lowered: str) -> str:
    ordered = [
        "uart", "spi", "i2c", "axi", "wishbone", "gpio", "timer", "pwm", "fifo",
        "dma", "riscv", "risc-v", "cpu", "soc", "accelerator", "fir", "fft",
        "aes", "sha", "noc", "controller", "bridge", "arbiter", "alu", "sram", "cache",
    ]
    for term in ordered:
        if term in lowered:
            return "riscv" if term == "risc-v" else term
    if "diagram" in lowered:
        return "architecture_diagram"
    return "unspecified_requested_block"


def _module_name(block_type: str, lowered: str) -> str:
    if block_type == "architecture_diagram":
        return "architecture_diagram"
    if block_type != "unspecified_requested_block":
        return re.sub(r"[^a-z0-9_]+", "_", block_type.lower()).strip("_")
    width = re.search(r"\b(\d+)\s*[- ]?bit\b", lowered)
    if width:
        return f"block_{width.group(1)}bit"
    return _request_module_name(lowered)


def _request_module_name(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text or "")
    stop = {
        "a", "an", "the", "on", "for", "with", "using", "please", "make", "create",
        "build", "design", "implement", "generate", "rtl", "gds", "gdsii", "chip",
        "core", "block", "module", "can", "you", "me", "to", "of", "and",
    }
    kept = [word for word in words if word not in stop]
    if kept:
        return re.sub(r"[^a-z0-9_]+", "_", "_".join(kept[:6])).strip("_")[:64]
    return "m_" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]


def _seed_ports(block_type: str) -> list[PortSpec]:
    base = [
        PortSpec("clk", "input", role="clock", description="primary clock"),
        PortSpec("rst_n", "input", role="reset", description="active-low reset unless approved otherwise"),
    ]
    if block_type == "uart":
        return base + [
            PortSpec("rx_i", "input", role="serial_rx"),
            PortSpec("tx_o", "output", role="serial_tx"),
            PortSpec("data_i", "input", "8", "payload"),
            PortSpec("data_o", "output", "8", "payload"),
            PortSpec("valid_i", "input", role="handshake"),
            PortSpec("ready_o", "output", role="handshake"),
        ]
    if block_type in {"fifo", "dma", "bridge", "controller"}:
        return base + [
            PortSpec("valid_i", "input", role="handshake"),
            PortSpec("ready_o", "output", role="handshake"),
            PortSpec("data_i", "input", "parameterized", "payload"),
            PortSpec("data_o", "output", "parameterized", "payload"),
        ]
    return base


def _seed_invariants(block_type: str) -> list[str]:
    common = ["deterministic reset state", "no combinational feedback across top-level outputs"]
    if block_type in {"fifo", "dma", "bridge", "controller"}:
        common.append("valid/ready handshakes must not lose accepted data")
    return common


def _detect_deliverables(lowered: str) -> list[str]:
    deliverables = []
    if any(term in lowered for term in ("rtl", "verilog", "systemverilog", "code")):
        deliverables.append("rtl")
    if any(term in lowered for term in ("testbench", "simulate", "simulation", "verify")):
        deliverables.extend(["testbench", "simulation"])
    if "synth" in lowered:
        deliverables.append("synthesis")
    if any(term in lowered for term in ("diagram", "mermaid", "flowchart")):
        deliverables.append("diagram")
    return _unique(deliverables)


def _seed_constraints(lowered: str) -> dict[str, Any]:
    clock = re.search(r"\b(\d+(?:\.\d+)?)\s*(mhz|ghz|ns)\b", lowered)
    return {"clock_target": clock.group(0) if clock else None}


def _title_from_text(user_text: str, block_type: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", user_text or "")[:10]
    if words:
        return " ".join(words)
    return block_type.replace("_", " ").title()


def _schema_for_dataclass(cls: type) -> dict[str, Any]:
    fields = {}
    for name, field_def in getattr(cls, "__dataclass_fields__", {}).items():
        fields[name] = {
            "type": str(field_def.type),
            "required": field_def.default is MISSING and field_def.default_factory is MISSING,
        }
    return {"type": "object", "fields": fields}


def _unique(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
