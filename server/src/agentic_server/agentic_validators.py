from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from agentic_server.agentic_handoffs import SCHEMA_VERSION, HandoffEnvelope
from agentic_server.agentic_kernel import PermissionScope, validate_tool_call
from agentic_server.tool_adapters import load_adapters


VALIDATION_SCHEMA_VERSION = "agentic.validation.v1"


class StrictModel(BaseModel):
    model_config = {
        "extra": "forbid",
        "validate_assignment": True,
        "str_strip_whitespace": True,
    }


class ValidationIssue(StrictModel):
    code: str
    message: str
    path: str = ""
    severity: Literal["error", "warning"] = "error"


class ValidationResult(StrictModel):
    schema_version: str = VALIDATION_SCHEMA_VERSION
    accepted: bool
    schema_valid: bool
    semantic_valid: bool
    evidence_valid: bool
    permission_valid: bool = True
    role: str = ""
    kind: str = ""
    intent_digest: str = ""
    payload_digest: str = ""
    issues: list[ValidationIssue] = Field(default_factory=list)
    checked_at: float = Field(default_factory=time.time)

    def to_record(self) -> dict[str, Any]:
        return _model_dump(self)


class ClockResetContractModel(StrictModel):
    clock: str = Field(default="clk", max_length=128)
    reset: str = Field(default="rst_n", max_length=128)
    reset_polarity: Literal["active_low", "active_high", "unspecified"] = "active_low"
    reset_style: str = Field(default="synchronous_or_unspecified", max_length=128)
    notes: list[str] = Field(default_factory=list, max_length=16)


class PortSpecModel(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    direction: Literal["input", "output", "inout"]
    width: str = Field(default="1", max_length=64)
    role: str = Field(default="signal", max_length=96)
    description: str = Field(default="", max_length=512)

    @field_validator("name")
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        if not _hdl_identifier_like(value):
            raise ValueError("port name must be a simple HDL identifier-like string")
        return value


class ModuleContractModel(StrictModel):
    module_name: str = Field(min_length=1, max_length=128)
    block_type: str = Field(min_length=1, max_length=128)
    ports: list[PortSpecModel] = Field(default_factory=list, max_length=256)
    parameters: dict[str, Any] = Field(default_factory=dict)
    clock_reset: ClockResetContractModel = Field(default_factory=ClockResetContractModel)
    invariants: list[str] = Field(default_factory=list, max_length=64)
    unresolved: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("module_name")
    @classmethod
    def valid_module_name(cls, value: str) -> str:
        if not _hdl_identifier_like(value) or value[0].isdigit():
            raise ValueError("module_name must be a valid simple HDL identifier")
        return value


class DesignSpecModel(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    intent: str = Field(min_length=1, max_length=4000)
    target_pdk: str | None = None
    target_node_nm: str | None = None
    deliverables: list[str] = Field(default_factory=list, max_length=32)
    module_contracts: list[ModuleContractModel] = Field(default_factory=list, max_length=64)
    constraints: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list, max_length=64)
    open_questions: list[str] = Field(default_factory=list, max_length=64)
    confidence: str = "draft"

    @model_validator(mode="after")
    def concrete_or_blocked(self):
        if not self.module_contracts and self.confidence != "blocked_for_spec":
            raise ValueError("DesignSpec without module_contracts must be blocked_for_spec")
        return self


class VerificationPlanModel(StrictModel):
    strategy: str = Field(min_length=1, max_length=2000)
    testbench_language: str = Field(default="systemverilog_or_available", max_length=128)
    required_tests: list[str] = Field(default_factory=list, max_length=128)
    assertions: list[str] = Field(default_factory=list, max_length=128)
    coverage_goals: list[str] = Field(default_factory=list, max_length=128)
    pass_fail_contract: list[str] = Field(default_factory=list, max_length=64)


class FilePatchPlanModel(StrictModel):
    path: str = Field(min_length=1, max_length=512)
    action: Literal["create", "create_or_update", "update", "delete", "read_only"]
    stage: str = Field(min_length=1, max_length=96)
    purpose: str = Field(min_length=1, max_length=512)
    depends_on: list[str] = Field(default_factory=list, max_length=32)


class RTLDeltaModel(StrictModel):
    module_name: str = Field(min_length=1, max_length=128)
    module_contract: dict[str, Any]
    planned_files: list[FilePatchPlanModel] = Field(default_factory=list, max_length=128)
    synthesis_contract: list[str] = Field(default_factory=list, max_length=64)
    implementation_notes: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def has_hdl_file(self):
        if not any(item.path.endswith((".v", ".sv", ".vhd", ".vhdl")) for item in self.planned_files):
            raise ValueError("RTLDelta must include at least one HDL planned file")
        return self


class FailureReproducerModel(StrictModel):
    stage: str = Field(min_length=1, max_length=128)
    command_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)
    minimal_repro_steps: list[str] = Field(default_factory=list, max_length=64)
    expected_failure_signature: list[str] = Field(default_factory=list, max_length=64)


class CoverageEvidenceModel(StrictModel):
    stage: str = Field(min_length=1, max_length=128)
    covered: list[str] = Field(default_factory=list, max_length=128)
    missing: list[str] = Field(default_factory=list, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)


class AdapterStageModel(StrictModel):
    stage: str = Field(min_length=1, max_length=96)
    adapter: str = Field(min_length=1, max_length=128)
    vendor: str | None = None
    openness: str | None = None
    available: bool | None = None
    license_hint_present: bool | None = None
    commands: list[str] = Field(default_factory=list, max_length=32)
    required: bool = False


class ToolAdapterPlanModel(StrictModel):
    selected_backend: str | None
    selected_pdk: str | None
    required_capabilities: list[str] = Field(default_factory=list, max_length=32)
    adapter_sequence: list[AdapterStageModel] = Field(default_factory=list, max_length=64)
    blockers: list[str] = Field(default_factory=list, max_length=64)
    fallback_policy: str = Field(default="prefer_detected_user_toolchain_then_open_source_with_approval", max_length=512)


class FailureAnalysisModel(StrictModel):
    failing_stage: str = Field(min_length=1, max_length=128)
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)
    suspected_root_causes: list[str] = Field(default_factory=list, max_length=64)
    patch_plan: list[str] = Field(default_factory=list, max_length=64)
    recheck_scope: list[str] = Field(default_factory=list, max_length=64)


class SignoffAuditModel(StrictModel):
    required_evidence: list[str] = Field(default_factory=list, max_length=128)
    present_evidence: list[str] = Field(default_factory=list, max_length=128)
    missing_evidence: list[str] = Field(default_factory=list, max_length=128)
    spec_drift_checks: list[str] = Field(default_factory=list, max_length=128)
    residual_risk: list[str] = Field(default_factory=list, max_length=128)


class HandoffEnvelopeModel(StrictModel):
    source_role: str
    target_role: str
    kind: str
    payload: dict[str, Any]
    intent_digest: str
    scope: str
    evidence_refs: list[str] = Field(default_factory=list)
    open_risks: list[str] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    created_at: float = Field(default_factory=time.time)


PAYLOAD_MODELS: dict[str, type[StrictModel]] = {
    "DesignSpec": DesignSpecModel,
    "ModuleContract": ModuleContractModel,
    "RTLDelta": RTLDeltaModel,
    "FilePatchPlan": FilePatchPlanModel,
    "VerificationPlan": VerificationPlanModel,
    "FailureReproducer": FailureReproducerModel,
    "CoverageEvidence": CoverageEvidenceModel,
    "ToolAdapterPlan": ToolAdapterPlanModel,
    "FailureAnalysis": FailureAnalysisModel,
    "SignoffAudit": SignoffAuditModel,
}


@dataclass(frozen=True)
class ValidationContext:
    turn_digest: str
    scope: PermissionScope
    allowed_pdks: frozenset[str]
    selected_pdk: str | None
    allowed_adapters: frozenset[str]
    adapter_stages: dict[str, set[str]]
    evidence_ids: frozenset[str]
    evidence_kinds: frozenset[str]


def build_validation_context(
    *,
    env: dict[str, Any],
    design_state: dict[str, Any],
    context_contract: dict[str, Any],
    flow_decision: dict[str, Any],
) -> ValidationContext:
    scope_payload = context_contract.get("permission_scope") or {}
    scope = PermissionScope(
        name=str(scope_payload.get("name") or "unknown"),
        reason=str(scope_payload.get("reason") or "validation context"),
        allowed_tools=tuple(scope_payload.get("allowed_tools") or ()),
        write_roots=tuple(scope_payload.get("write_roots") or ()),
        write_extensions=tuple(scope_payload.get("write_extensions") or ()),
        allow_bash=bool(scope_payload.get("allow_bash")),
        allow_eda=bool(scope_payload.get("allow_eda")),
        allow_install=bool(scope_payload.get("allow_install")),
        allow_destructive=bool(scope_payload.get("allow_destructive")),
        allow_web=bool(scope_payload.get("allow_web")),
        allowed_bash=str(scope_payload.get("allowed_bash") or "none"),
    )

    pdk_names = _pdk_names(env.get("pdk_index") or {})
    selected = _selected_pdk_name(flow_decision)
    if selected:
        pdk_names.add(selected)

    adapter_names = set(load_adapters().keys())
    adapter_stages: dict[str, set[str]] = {}
    for stage, record in ((env.get("tool_adapters") or flow_decision.get("adapter_matrix") or {}) or {}).items():
        selected_record = record.get("selected") if isinstance(record, dict) else None
        if isinstance(selected_record, dict):
            name = selected_record.get("adapter")
            if name:
                adapter_names.add(str(name))
                adapter_stages.setdefault(str(stage), set()).add(str(name))
        for item in (record.get("candidates") if isinstance(record, dict) else None) or []:
            if isinstance(item, dict) and item.get("adapter"):
                adapter_names.add(str(item["adapter"]))
                adapter_stages.setdefault(str(stage), set()).add(str(item["adapter"]))
    adapter_names.add("local_environment")

    nodes = ((design_state.get("evidence_graph") or {}).get("nodes") or {})
    evidence_ids = set(nodes.keys())
    evidence_kinds = {str(node.get("kind")) for node in nodes.values() if isinstance(node, dict) and node.get("kind")}

    return ValidationContext(
        turn_digest=str(context_contract.get("turn_digest") or ""),
        scope=scope,
        allowed_pdks=frozenset(pdk_names),
        selected_pdk=selected,
        allowed_adapters=frozenset(adapter_names),
        adapter_stages=adapter_stages,
        evidence_ids=frozenset(evidence_ids),
        evidence_kinds=frozenset(evidence_kinds),
    )


def validate_handoff_envelope(envelope: HandoffEnvelope | dict[str, Any], ctx: ValidationContext) -> ValidationResult:
    record = envelope.to_record() if isinstance(envelope, HandoffEnvelope) else envelope
    issues: list[ValidationIssue] = []
    role = str(record.get("source_role") or "")
    kind = str(record.get("kind") or "")
    intent_digest = str(record.get("intent_digest") or "")
    payload_digest = _digest(record.get("payload") or {})

    envelope_model: HandoffEnvelopeModel | None = None
    payload_model: StrictModel | None = None
    try:
        envelope_model = HandoffEnvelopeModel.model_validate(record)
        payload_cls = PAYLOAD_MODELS.get(envelope_model.kind)
        if payload_cls is None:
            issues.append(_issue("unknown_payload_kind", f"Unsupported handoff payload kind `{envelope_model.kind}`.", "kind"))
        else:
            payload_model = payload_cls.model_validate(envelope_model.payload)
    except ValidationError as exc:
        issues.extend(_schema_issues(exc))

    schema_valid = not any(issue.severity == "error" and issue.code.startswith("schema_") for issue in issues)
    if envelope_model is not None:
        issues.extend(_semantic_issues(envelope_model, payload_model, ctx))
        issues.extend(_evidence_issues(envelope_model, payload_model, ctx))
        issues.extend(_permission_issues(envelope_model, payload_model, ctx))

    semantic_valid = not any(issue.severity == "error" and issue.code.startswith("semantic_") for issue in issues)
    evidence_valid = not any(issue.severity == "error" and issue.code.startswith("evidence_") for issue in issues)
    permission_valid = not any(issue.severity == "error" and issue.code.startswith("permission_") for issue in issues)
    accepted = schema_valid and semantic_valid and evidence_valid and permission_valid and not any(issue.severity == "error" for issue in issues)
    return ValidationResult(
        accepted=accepted,
        schema_valid=schema_valid,
        semantic_valid=semantic_valid,
        evidence_valid=evidence_valid,
        permission_valid=permission_valid,
        role=role,
        kind=kind,
        intent_digest=intent_digest,
        payload_digest=payload_digest,
        issues=issues,
    )


def validation_schema_catalog() -> dict[str, Any]:
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "envelope": _schema(HandoffEnvelopeModel),
        "payloads": {name: _schema(model) for name, model in PAYLOAD_MODELS.items()},
        "result": _schema(ValidationResult),
        "policy": {
            "extra_fields": "forbid",
            "intent_digest": "must match current context contract",
            "tool_adapters": "must exist in dynamic adapter registry or current environment matrix",
            "pdk": "must match detected/selected PDK when local PDK evidence exists",
            "evidence_refs": "must resolve to the current evidence graph when present",
            "execution_files": "must satisfy the current permission scope write contract",
        },
    }


def _semantic_issues(
    envelope: HandoffEnvelopeModel,
    payload: StrictModel | None,
    ctx: ValidationContext,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if envelope.schema_version != SCHEMA_VERSION:
        issues.append(_issue("semantic_schema_version", "Handoff schema version does not match the kernel.", "schema_version"))
    if ctx.turn_digest and envelope.intent_digest != ctx.turn_digest:
        issues.append(_issue("semantic_intent_drift", "Handoff intent digest does not match this user turn.", "intent_digest"))
    if envelope.scope != ctx.scope.name:
        issues.append(_issue("semantic_scope_drift", "Handoff scope does not match the active permission scope.", "scope"))
    if envelope.kind not in PAYLOAD_MODELS:
        issues.append(_issue("semantic_unknown_kind", f"No validator exists for `{envelope.kind}`.", "kind"))

    if isinstance(payload, DesignSpecModel):
        if payload.target_pdk and ctx.allowed_pdks and payload.target_pdk not in ctx.allowed_pdks:
            issues.append(_issue(
                "semantic_unknown_pdk",
                f"DesignSpec target_pdk `{payload.target_pdk}` is not in detected or selected PDK evidence.",
                "payload.target_pdk",
            ))
        if ctx.scope.name == "execution" and payload.open_questions:
            issues.append(_issue(
                "semantic_unresolved_execution_spec",
                "Execution scope cannot proceed with unresolved DesignSpec open_questions.",
                "payload.open_questions",
            ))
        if "gdsii" in payload.deliverables and payload.confidence == "blocked_for_spec":
            issues.append(_issue(
                "semantic_gds_blocked_spec",
                "GDSII deliverable cannot be actionable while the spec is blocked.",
                "payload.deliverables",
            ))
        for idx, contract in enumerate(payload.module_contracts):
            issues.extend(_module_contract_issues(contract, f"payload.module_contracts[{idx}]"))

    if isinstance(payload, ModuleContractModel):
        issues.extend(_module_contract_issues(payload, "payload"))

    if isinstance(payload, ToolAdapterPlanModel):
        if payload.selected_backend and not _backend_allowed(payload.selected_backend, ctx):
            issues.append(_issue(
                "semantic_unknown_backend",
                f"Selected backend `{payload.selected_backend}` is neither a dynamic adapter nor a supported flow backend.",
                "payload.selected_backend",
            ))
        if payload.selected_pdk and ctx.allowed_pdks and payload.selected_pdk not in ctx.allowed_pdks:
            issues.append(_issue(
                "semantic_unknown_plan_pdk",
                f"ToolAdapterPlan selected_pdk `{payload.selected_pdk}` is not in detected or selected PDK evidence.",
                "payload.selected_pdk",
            ))
        for idx, stage in enumerate(payload.adapter_sequence):
            issues.extend(_adapter_stage_issues(stage, ctx, f"payload.adapter_sequence[{idx}]"))

    if isinstance(payload, RTLDeltaModel):
        contract_name = payload.module_contract.get("module_name")
        if contract_name and payload.module_name != contract_name:
            issues.append(_issue(
                "semantic_module_contract_mismatch",
                "RTLDelta module_name does not match its ModuleContract.",
                "payload.module_name",
            ))
        for idx, plan in enumerate(payload.planned_files):
            decision = validate_tool_call(ctx.scope, "write", {"path": plan.path})
            if not decision.allowed:
                issues.append(_issue(
                    "semantic_write_scope",
                    f"Planned RTL file `{plan.path}` is outside the active write contract: {decision.reason}",
                    f"payload.planned_files[{idx}].path",
                ))

    if isinstance(payload, SignoffAuditModel):
        impossible = set(payload.present_evidence) & set(payload.missing_evidence)
        for item in sorted(impossible):
            issues.append(_issue(
                "semantic_conflicting_signoff_evidence",
                f"Signoff evidence `{item}` cannot be both present and missing.",
                "payload.present_evidence",
            ))
        if "tool_checkpoint" in payload.present_evidence and "checkpoint" not in ctx.evidence_kinds:
            issues.append(_issue(
                "semantic_unbacked_tool_checkpoint",
                "SignoffAudit claims tool_checkpoint is present, but the evidence graph has no checkpoint node.",
                "payload.present_evidence",
            ))
        signoff_claims = {"verification_pass", "synthesis_report", "sta_report", "drc_lvs_if_available"}
        if set(payload.present_evidence) & signoff_claims and not ctx.evidence_ids:
            issues.append(_issue(
                "semantic_signoff_claim_without_graph",
                "SignoffAudit claims implementation evidence, but the evidence graph is empty.",
                "payload.present_evidence",
            ))
    return issues


def _evidence_issues(
    envelope: HandoffEnvelopeModel,
    payload: StrictModel | None,
    ctx: ValidationContext,
) -> list[ValidationIssue]:
    refs = list(envelope.evidence_refs)
    if isinstance(payload, (FailureReproducerModel, CoverageEvidenceModel, FailureAnalysisModel)):
        refs.extend(payload.evidence_refs)
    issues: list[ValidationIssue] = []
    for idx, ref in enumerate(refs):
        if ref and ref not in ctx.evidence_ids:
            issues.append(_issue(
                "evidence_missing_ref",
                f"Evidence ref `{ref}` does not resolve in the current design evidence graph.",
                f"evidence_refs[{idx}]",
            ))
    if isinstance(payload, FailureAnalysisModel) and payload.failing_stage != "none" and not payload.evidence_refs:
        issues.append(_issue(
            "evidence_failure_without_ref",
            "FailureAnalysis needs evidence_refs unless failing_stage is none.",
            "payload.evidence_refs",
        ))
    return issues


def _permission_issues(
    envelope: HandoffEnvelopeModel,
    payload: StrictModel | None,
    ctx: ValidationContext,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if isinstance(payload, RTLDeltaModel) and ctx.scope.name != "execution":
        issues.append(_issue(
            "permission_rtl_delta_outside_execution",
            "RTLDelta is only accepted in execution scope.",
            "kind",
        ))
    if isinstance(payload, ToolAdapterPlanModel) and ctx.scope.name in {"advisor", "advisor_artifact"}:
        required_physical = [
            stage for stage in payload.adapter_sequence
            if stage.required and stage.stage in {"synthesis", "pnr", "sta", "physical_verification"}
        ]
        if required_physical:
            issues.append(_issue(
                "permission_physical_plan_in_advisor",
                "Advisor scope cannot require physical execution stages.",
                "payload.adapter_sequence",
            ))
    return issues


def _adapter_stage_issues(stage: AdapterStageModel, ctx: ValidationContext, path: str) -> list[ValidationIssue]:
    adapter = stage.adapter
    if adapter in ctx.allowed_adapters:
        return []
    detected_prefix = f"detected_{stage.stage}"
    if adapter == detected_prefix and not stage.available:
        return []
    return [_issue(
        "semantic_unknown_adapter",
        f"Adapter `{adapter}` is not registered in dynamic tool adapters or current environment matrix.",
        f"{path}.adapter",
    )]


def _backend_allowed(backend: str, ctx: ValidationContext) -> bool:
    if backend in ctx.allowed_adapters:
        return True
    return backend in {
        "none",
        "adapter_native",
        "adapter_candidate",
        "proprietary",
        "proprietary_candidate",
        "hybrid",
        "synthesis_only",
        "openlane",
        "orfs",
    }


def _module_contract_issues(contract: ModuleContractModel, path: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    names = [port.name for port in contract.ports]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    for name in duplicates:
        issues.append(_issue("semantic_duplicate_port", f"Duplicate port `{name}` in module contract.", f"{path}.ports"))
    if not _hdl_identifier_like(contract.module_name) or contract.module_name[0].isdigit():
        issues.append(_issue("semantic_bad_module_name", "Module name must be a synthesizable identifier-like name.", f"{path}.module_name"))
    return issues


def _hdl_identifier_like(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in {"_", "$"} for ch in value)


def _pdk_names(pdk_index: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for pdk in pdk_index.get("pdks") or []:
        if not isinstance(pdk, dict):
            continue
        for key in ("name", "family", "class", "profile"):
            value = pdk.get(key)
            if value:
                names.add(str(value))
        for alias in pdk.get("aliases") or []:
            names.add(str(alias))
    return names


def _selected_pdk_name(flow_decision: dict[str, Any]) -> str | None:
    selected = flow_decision.get("selected_pdk")
    if isinstance(selected, dict):
        return str(selected.get("name") or "") or None
    if selected:
        return str(selected)
    return None


def _schema_issues(exc: ValidationError) -> list[ValidationIssue]:
    issues = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err.get("loc", ()))
        issues.append(_issue("schema_validation_error", str(err.get("msg") or "schema validation failed"), loc))
    return issues


def _issue(code: str, message: str, path: str = "", severity: Literal["error", "warning"] = "error") -> ValidationIssue:
    return ValidationIssue(code=code, message=message, path=path, severity=severity)


def _digest(payload: Any) -> str:
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:16]


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    if hasattr(model, "model_json_schema"):
        return model.model_json_schema()
    return model.schema()


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()
