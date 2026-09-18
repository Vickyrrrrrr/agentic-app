from __future__ import annotations

import re
import hashlib
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentic_server.agentic_handoffs import (
    CoverageEvidence,
    DesignSpec,
    FailureAnalysis,
    FailureReproducer,
    FilePatchPlan,
    HandoffEnvelope,
    ModuleContract,
    RTLDelta,
    SignoffAudit,
    ToolAdapterPlan,
    VerificationPlan,
    payload_dict,
    seed_design_spec,
    seed_signoff_audit,
    seed_tool_adapter_plan,
    seed_verification_plan,
)
from agentic_server.agentic_validators import build_validation_context, validate_handoff_envelope


ROLE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "spec_architect": (),
    "flow_planner": ("spec_architect",),
    "verification_engineer": ("spec_architect",),
    "rtl_author": ("spec_architect", "flow_planner"),
    "debug_engineer": ("verification_engineer",),
    "signoff_critic": ("spec_architect", "flow_planner"),
}

PIPELINES: dict[str, tuple[str, ...]] = {
    "advisor": ("spec_architect",),
    "advisor_artifact": ("spec_architect",),
    "artifact_repair": ("debug_engineer", "signoff_critic"),
    "planning": ("spec_architect", "flow_planner", "verification_engineer", "signoff_critic"),
    "execution": ("spec_architect", "flow_planner", "rtl_author", "verification_engineer", "debug_engineer", "signoff_critic"),
}


@dataclass
class RoleContext:
    user_text: str
    workspace_root: str
    design_name: str
    context_contract: dict[str, Any]
    flow_decision: dict[str, Any]
    env: dict[str, Any]
    design_state: dict[str, Any]
    needs_spec_clarification: bool
    created_at: float = field(default_factory=time.time)

    @property
    def scope(self) -> str:
        return ((self.context_contract.get("permission_scope") or {}).get("name") or "unknown")

    @property
    def intent_digest(self) -> str:
        return self.context_contract.get("turn_digest") or "unknown"


@dataclass
class RoleResult:
    role: str
    envelopes: list[HandoffEnvelope]
    facts: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    validation: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


def run_role_pipeline(ctx: RoleContext) -> list[RoleResult]:
    roles = PIPELINES.get(ctx.scope, ("spec_architect",))
    validation_ctx = build_validation_context(
        env=ctx.env,
        design_state=ctx.design_state,
        context_contract=ctx.context_contract,
        flow_decision=ctx.flow_decision,
    )
    completed: set[str] = set()
    outputs: dict[str, list[HandoffEnvelope]] = {}
    results: list[RoleResult] = []
    for role in roles:
        deps = ROLE_DEPENDENCIES.get(role, ())
        missing = [dep for dep in deps if dep in roles and dep not in completed]
        if missing:
            results.append(RoleResult(
                role=role,
                envelopes=[],
                risks=[f"Skipped because dependencies are missing: {', '.join(missing)}"],
            ))
            continue
        result = _run_role(role, ctx, outputs)
        accepted_envelopes: list[HandoffEnvelope] = []
        for envelope in result.envelopes:
            envelope.validate()
            validation = validate_handoff_envelope(envelope, validation_ctx)
            validation_record = validation.to_record()
            result.validation.append(validation_record)
            result.evidence.append({
                "kind": "validation_result",
                "ref": f"{role}:{envelope.kind}:{validation.payload_digest}",
                "payload": validation_record,
            })
            if validation.accepted:
                accepted_envelopes.append(envelope)
            else:
                result.risks.extend([
                    f"Rejected {envelope.kind}: {issue.get('message')}"
                    for issue in validation_record.get("issues", [])
                    if issue.get("severity") == "error"
                ])
        result.envelopes = accepted_envelopes
        outputs[role] = accepted_envelopes
        completed.add(role)
        results.append(result)
    return results


def run_single_role(role: str, ctx: RoleContext) -> RoleResult:
    """Execute exactly one kernel role on-demand through the full validation pipeline.

    This is the public API for the /opencode/kernel/role/{role} endpoint.
    The caller must pass a fully-resolved RoleContext (scope already derived
    from session state, not from caller input).

    The role runs with an empty upstream `outputs` dict — dependencies are not
    re-executed. If the role requires upstream data (e.g. rtl_author needs
    spec_architect output), the relevant stored design_state facts are used
    by the role handler via ctx.design_state.
    """
    validation_ctx = build_validation_context(
        env=ctx.env,
        design_state=ctx.design_state,
        context_contract=ctx.context_contract,
        flow_decision=ctx.flow_decision,
    )
    result = _run_role(role, ctx, {})
    accepted_envelopes: list[HandoffEnvelope] = []
    for envelope in result.envelopes:
        envelope.validate()
        validation = validate_handoff_envelope(envelope, validation_ctx)
        validation_record = validation.to_record()
        result.validation.append(validation_record)
        result.evidence.append({
            "kind": "validation_result",
            "ref": f"{role}:{envelope.kind}:{validation.payload_digest}",
            "payload": validation_record,
        })
        if validation.accepted:
            accepted_envelopes.append(envelope)
        else:
            result.risks.extend([
                f"Rejected {envelope.kind}: {issue.get('message')}"
                for issue in validation_record.get("issues", [])
                if issue.get("severity") == "error"
            ])
    result.envelopes = accepted_envelopes
    return result



def _run_role(role: str, ctx: RoleContext, outputs: dict[str, list[HandoffEnvelope]]) -> RoleResult:
    if role == "spec_architect":
        return _spec_architect(ctx)
    if role == "flow_planner":
        return _flow_planner(ctx, _latest_payload(outputs, "spec_architect", "DesignSpec"))
    if role == "rtl_author":
        return _rtl_author(ctx, _latest_payload(outputs, "spec_architect", "DesignSpec"))
    if role == "verification_engineer":
        return _verification_engineer(ctx, _latest_payload(outputs, "spec_architect", "DesignSpec"))
    if role == "debug_engineer":
        return _debug_engineer(ctx)
    if role == "signoff_critic":
        spec_payload = _latest_payload(outputs, "spec_architect", "DesignSpec")
        flow_payload = _latest_payload(outputs, "flow_planner", "ToolAdapterPlan")
        return _signoff_critic(ctx, spec_payload, flow_payload)
    return RoleResult(role=role, envelopes=[], risks=[f"Unknown role: {role}"])


def _spec_architect(ctx: RoleContext) -> RoleResult:
    spec = seed_design_spec(ctx.user_text, ctx.flow_decision, ctx.needs_spec_clarification)
    envelope = _envelope(
        ctx,
        source="principal",
        target="spec_architect",
        kind="DesignSpec",
        payload=payload_dict(spec),
        risks=spec.open_questions,
    )
    facts = [
        {"namespace": "spec", "key": "draft", "value": payload_dict(spec), "source": "spec_architect"},
        {"namespace": "spec", "key": "deliverables", "value": spec.deliverables, "source": "spec_architect"},
    ]
    if spec.module_contracts:
        facts.append({
            "namespace": "interfaces",
            "key": spec.module_contracts[0].module_name,
            "value": payload_dict(spec.module_contracts[0]),
            "source": "spec_architect",
        })
    return RoleResult(role="spec_architect", envelopes=[envelope], facts=facts, risks=spec.open_questions)


def _flow_planner(ctx: RoleContext, spec_payload: dict[str, Any]) -> RoleResult:
    plan = seed_tool_adapter_plan(ctx.flow_decision)
    adapter_matrix = ctx.env.get("tool_adapters") or ctx.flow_decision.get("adapter_matrix") or {}
    if adapter_matrix:
        enriched_sequence = []
        for item in plan.adapter_sequence:
            stage = item.get("stage")
            selected = (adapter_matrix.get(stage) or {}).get("selected")
            if selected:
                merged = dict(item)
                merged.update({
                    "adapter": selected.get("adapter") or item.get("adapter"),
                    "vendor": selected.get("vendor"),
                    "openness": selected.get("openness"),
                    "available": selected.get("available"),
                    "license_hint_present": selected.get("license_hint_present"),
                    "commands": selected.get("commands", []),
                })
                enriched_sequence.append(merged)
            else:
                enriched_sequence.append(item)
        plan.adapter_sequence = enriched_sequence
    if spec_payload.get("open_questions"):
        plan.blockers = _unique([*plan.blockers, "Design spec still has open questions; do not execute physical flow."])
    payload = payload_dict(plan)
    envelope = _envelope(ctx, "spec_architect", "flow_planner", "ToolAdapterPlan", payload, risks=plan.blockers)
    return RoleResult(
        role="flow_planner",
        envelopes=[envelope],
        facts=[{"namespace": "flow", "key": "adapter_plan", "value": payload, "source": "flow_planner"}],
        evidence=[{"kind": "adapter_plan", "ref": "flow_planner", "payload": payload}],
        risks=plan.blockers,
    )


def _rtl_author(ctx: RoleContext, spec_payload: dict[str, Any]) -> RoleResult:
    contract = _first_module_contract(spec_payload, ctx)
    module_name = contract.get("module_name") or _fallback_module_name(ctx)
    project_root = _project_root(module_name)
    delta = RTLDelta(
        module_name=module_name,
        module_contract=contract,
        planned_files=[
            FilePatchPlan(f"{project_root}/rtl/{module_name}.sv", "create_or_update", "rtl", "synthesizable top-level implementation"),
            FilePatchPlan(f"{project_root}/reports/rtl_manifest.md", "create_or_update", "reporting", "RTL ownership and generated file manifest"),
        ],
        synthesis_contract=[
            "all sequential logic uses the approved clock/reset contract",
            "top-level ports match ModuleContract exactly",
            "no unsynthesizable delays/tasks in RTL",
        ],
        implementation_notes=[
            "RTL generation must read existing workspace files before overwriting matching modules.",
            "Any spec ambiguity must become NEEDS_INPUT before implementation.",
        ],
    )
    payload = payload_dict(delta)
    envelope = _envelope(ctx, "spec_architect", "rtl_author", "RTLDelta", payload, risks=contract.get("unresolved", []))
    return RoleResult(
        role="rtl_author",
        envelopes=[envelope],
        facts=[{"namespace": "rtl", "key": module_name, "value": payload, "source": "rtl_author"}],
        evidence=[{"kind": "rtl_plan", "ref": module_name, "payload": payload}],
        risks=contract.get("unresolved", []),
    )


def _verification_engineer(ctx: RoleContext, spec_payload: dict[str, Any]) -> RoleResult:
    spec = _design_spec_from_payload(spec_payload, ctx)
    plan = seed_verification_plan(spec)
    module_name = spec.module_contracts[0].module_name if spec.module_contracts else _fallback_module_name(ctx)
    project_root = _project_root(module_name)
    payload = payload_dict(plan)
    evidence = CoverageEvidence(
        stage="verification_planning",
        covered=["reset", "smoke", "boundary_conditions"],
        missing=["tool-run evidence", "coverage report", "waveform evidence"],
        evidence_refs=[],
    )
    envelopes = [
        _envelope(ctx, "spec_architect", "verification_engineer", "VerificationPlan", payload, risks=spec.open_questions),
        _envelope(ctx, "verification_engineer", "signoff_critic", "CoverageEvidence", payload_dict(evidence), risks=evidence.missing),
    ]
    return RoleResult(
        role="verification_engineer",
        envelopes=envelopes,
        facts=[
            {"namespace": "verification", "key": "plan", "value": payload, "source": "verification_engineer"},
            {"namespace": "verification", "key": "testbench_path", "value": f"{project_root}/tb/{module_name}_tb.sv", "source": "verification_engineer"},
        ],
        evidence=[{"kind": "verification_plan", "ref": module_name, "payload": payload}],
        risks=spec.open_questions,
    )


def _debug_engineer(ctx: RoleContext) -> RoleResult:
    failures = _failing_evidence(ctx.design_state)
    if not failures:
        repro = FailureReproducer(
            stage="none",
            minimal_repro_steps=[],
            expected_failure_signature=[],
        )
        return RoleResult(
            role="debug_engineer",
            envelopes=[_envelope(ctx, "verification_engineer", "debug_engineer", "FailureReproducer", payload_dict(repro))],
        )
    latest = failures[0]
    payload = latest.get("payload") or {}
    analysis = FailureAnalysis(
        failing_stage=payload.get("stage") or latest.get("ref") or "unknown",
        evidence_refs=[latest.get("id")],
        suspected_root_causes=_extract_failure_signatures(payload),
        patch_plan=[
            "read exact failing source/log snippet",
            "patch the minimal owning file only",
            "rerun the failing stage before advancing",
        ],
        recheck_scope=[payload.get("stage") or "failing_stage"],
    )
    return RoleResult(
        role="debug_engineer",
        envelopes=[_envelope(ctx, "verification_engineer", "debug_engineer", "FailureAnalysis", payload_dict(analysis), risks=analysis.suspected_root_causes)],
        facts=[{"namespace": "debug", "key": "latest_failure", "value": payload_dict(analysis), "source": "debug_engineer"}],
        evidence=[{"kind": "failure_analysis", "ref": analysis.failing_stage, "payload": payload_dict(analysis)}],
        risks=analysis.suspected_root_causes,
    )


def _signoff_critic(ctx: RoleContext, spec_payload: dict[str, Any], flow_payload: dict[str, Any]) -> RoleResult:
    spec = _design_spec_from_payload(spec_payload, ctx)
    audit = seed_signoff_audit(spec)
    evidence_nodes = list(((ctx.design_state.get("evidence_graph") or {}).get("nodes") or {}).values())
    present_kinds = {node.get("kind") for node in evidence_nodes}
    present = []
    if "handoff" in present_kinds:
        present.append("approved_spec" if ctx.scope == "execution" else "draft_spec")
    if "verification_plan" in present_kinds:
        present.append("verification_plan")
    if "checkpoint" in present_kinds:
        present.append("tool_checkpoint")
    audit.present_evidence = _unique([*audit.present_evidence, *present])
    audit.missing_evidence = [item for item in audit.required_evidence if item not in audit.present_evidence]
    if flow_payload.get("blockers"):
        audit.residual_risk = _unique([*audit.residual_risk, *flow_payload.get("blockers", [])])
    payload = payload_dict(audit)
    return RoleResult(
        role="signoff_critic",
        envelopes=[_envelope(ctx, "signoff_critic", "principal", "SignoffAudit", payload, risks=audit.residual_risk)],
        facts=[{"namespace": "signoff", "key": "latest_audit", "value": payload, "source": "signoff_critic"}],
        evidence=[{"kind": "signoff_audit", "ref": ctx.intent_digest, "payload": payload}],
        risks=audit.residual_risk,
    )


def persist_role_results(store, results: list[RoleResult]) -> dict[str, int]:
    counts = {"handoffs": 0, "facts": 0, "evidence": 0, "validation": 0}
    for result in results:
        for envelope in result.envelopes:
            record = envelope.to_record()
            store.record_handoff(envelope.source_role, envelope.target_role, record)
            store.record_evidence("handoff", envelope.kind, record)
            counts["handoffs"] += 1
        for fact in result.facts:
            store.upsert_design_fact(
                str(fact.get("namespace") or result.role),
                str(fact.get("key") or "latest"),
                fact.get("value"),
                source=str(fact.get("source") or result.role),
            )
            counts["facts"] += 1
        for evidence in result.evidence:
            store.record_evidence(
                str(evidence.get("kind") or result.role),
                str(evidence.get("ref") or result.role),
                evidence.get("payload") if isinstance(evidence.get("payload"), dict) else {"value": evidence.get("payload")},
                links=evidence.get("links") if isinstance(evidence.get("links"), list) else None,
            )
            counts["evidence"] += 1
            if evidence.get("kind") == "validation_result":
                counts["validation"] += 1
    return counts


def _envelope(ctx: RoleContext, source: str, target: str, kind: str, payload: dict[str, Any], risks: list[str] | None = None) -> HandoffEnvelope:
    return HandoffEnvelope(
        source_role=source,
        target_role=target,
        kind=kind,
        payload=payload,
        intent_digest=ctx.intent_digest,
        scope=ctx.scope,
        evidence_refs=_evidence_refs(ctx.design_state),
        open_risks=_unique(risks or []),
    )


def _latest_payload(outputs: dict[str, list[HandoffEnvelope]], role: str, kind: str) -> dict[str, Any]:
    for envelope in reversed(outputs.get(role, [])):
        if envelope.kind == kind:
            return envelope.payload
    return {}


def _first_module_contract(spec_payload: dict[str, Any], ctx: RoleContext) -> dict[str, Any]:
    contracts = spec_payload.get("module_contracts") or []
    if contracts and isinstance(contracts[0], dict):
        return contracts[0]
    module_name = _fallback_module_name(ctx)
    return {
        "module_name": module_name,
        "block_type": "unspecified_requested_block",
        "ports": [],
        "unresolved": ["No module contract was available."],
    }


def _design_spec_from_payload(payload: dict[str, Any], ctx: RoleContext) -> DesignSpec:
    if not payload:
        return seed_design_spec(ctx.user_text, ctx.flow_decision, ctx.needs_spec_clarification)
    contracts = []
    for item in payload.get("module_contracts") or []:
        if isinstance(item, ModuleContract):
            contracts.append(item)
        elif isinstance(item, dict):
            contracts.append(ModuleContract(
                module_name=item.get("module_name") or _fallback_module_name(ctx),
                block_type=item.get("block_type") or "unspecified_requested_block",
                ports=item.get("ports") or [],
                parameters=item.get("parameters") or {},
                invariants=item.get("invariants") or [],
                unresolved=item.get("unresolved") or [],
            ))
    return DesignSpec(
        title=payload.get("title") or "Untitled design",
        intent=payload.get("intent") or ctx.user_text,
        target_pdk=payload.get("target_pdk"),
        target_node_nm=payload.get("target_node_nm"),
        deliverables=payload.get("deliverables") or [],
        module_contracts=contracts,
        constraints=payload.get("constraints") or {},
        assumptions=payload.get("assumptions") or [],
        open_questions=payload.get("open_questions") or [],
        confidence=payload.get("confidence") or "draft",
    )


def _project_root(module_name: str) -> str:
    return _slug(module_name)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (value or "").lower()).strip("_")[:64]
    return cleaned or ("m_" + hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:8])


def _fallback_module_name(ctx: RoleContext) -> str:
    words = re.findall(r"[a-z0-9]+", (ctx.user_text or "").lower())
    stop = {
        "a", "an", "the", "on", "for", "with", "using", "please", "make", "create",
        "build", "design", "implement", "generate", "rtl", "gds", "gdsii", "chip",
        "core", "block", "module", "can", "you", "me", "to", "of", "and",
    }
    kept = [word for word in words if word not in stop]
    if kept:
        return _slug("_".join(kept[:6]))
    return _slug(ctx.design_name or ctx.user_text)


def _evidence_refs(state: dict[str, Any]) -> list[str]:
    nodes = list(((state.get("evidence_graph") or {}).get("nodes") or {}).values())
    nodes.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return [node.get("id") for node in nodes[:8] if node.get("id")]


def _failing_evidence(state: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = list(((state.get("evidence_graph") or {}).get("nodes") or {}).values())
    failures = []
    for node in nodes:
        payload = node.get("payload") or {}
        if payload.get("pass") is False or payload.get("exit_code") not in (None, 0):
            failures.append(node)
    failures.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return failures


def _extract_failure_signatures(payload: dict[str, Any]) -> list[str]:
    signatures = []
    for key in ("errors", "warnings"):
        values = payload.get(key) or []
        if isinstance(values, list):
            signatures.extend(str(item)[:240] for item in values[:5])
    return _unique(signatures or ["Failure evidence exists but no parser signature was extracted."])


def _unique(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
