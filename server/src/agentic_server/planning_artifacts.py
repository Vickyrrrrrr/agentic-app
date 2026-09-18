from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from agentic_server.design_intent import DesignIntent


CANONICAL_PROJECT_DIRS: tuple[tuple[str, str], ...] = (
    ("docs/plans", "Human-approved plans, scope decisions, and revision history."),
    ("docs/diagrams", "Renderable Mermaid architecture, flow, and verification diagrams."),
    ("rtl", "Synthesizable RTL only."),
    ("tb", "Self-checking simulation testbenches."),
    ("dv", "Assertions, formal plans, coverage models, and verification collateral."),
    ("constraints", "SDC and timing or physical constraints."),
    ("scripts", "Flow entry points and reproducible command wrappers."),
    ("sim/runs", "Generated simulation executables, logs, VCD/FST waveforms, and run manifests."),
    ("synth", "Synthesis scripts and reports."),
    ("pnr", "Place-and-route configuration and results."),
    ("sta", "Timing scripts, SPEF/SDF references, and timing reports."),
    ("signoff", "DRC/LVS/ERC/signoff collateral and evidence."),
    ("reports", "Summaries, manifests, and final evidence reports."),
    ("logs", "Long raw logs that should not pollute source folders."),
)


@dataclass(frozen=True)
class ApprovalPlanArtifact:
    project_root: str
    plan_path: str
    diagram_path: str
    structure_path: str
    content: str
    actions: list[dict[str, str]]

    @property
    def artifacts(self) -> list[str]:
        return [self.plan_path, self.diagram_path, self.structure_path]


def write_approval_plan_artifacts(
    *,
    workspace_root: str,
    design_name: str,
    user_text: str,
    flow_decision: dict[str, Any],
    role_results: list[Any],
    env: dict[str, Any],
    design_intent: DesignIntent | None = None,
) -> ApprovalPlanArtifact:
    spec = _latest_payload(role_results, "DesignSpec")
    flow = _latest_payload(role_results, "ToolAdapterPlan")
    verification = _latest_payload(role_results, "VerificationPlan")
    signoff = _latest_payload(role_results, "SignoffAudit")
    project_root = design_intent.project_root if design_intent else _project_root(user_text, design_name, spec)
    diagram = _architecture_mermaid(user_text, spec)
    plan_path = f"{project_root}/docs/plans/approval_plan.md"
    diagram_path = f"{project_root}/docs/diagrams/architecture.mermaid"
    structure_path = f"{project_root}/docs/PROJECT_STRUCTURE.md"
    content = _approval_markdown(
        user_text=user_text,
        design_name=design_name,
        project_root=project_root,
        design_intent=design_intent,
        spec=spec,
        flow=flow,
        verification=verification,
        signoff=signoff,
        flow_decision=flow_decision,
        diagram=diagram,
    )
    structure = _structure_markdown(project_root, user_text, flow_decision)
    root = Path(workspace_root).resolve()
    _write_text(root / plan_path, content)
    _write_text(root / diagram_path, diagram)
    _write_text(root / structure_path, structure)
    _write_text(root / project_root / ".agentic_project.json", json.dumps({
        "project_root": project_root,
        "design_workspace": design_name,
        "source_request": user_text[:1000],
        "created_at": time.time(),
        "canonical_dirs": [path for path, _ in CANONICAL_PROJECT_DIRS],
        "approval_artifacts": [plan_path, diagram_path, structure_path],
        "project_manifest": f"{project_root}/PROJECT_MANIFEST.json",
        "intent_id": design_intent.intent_id if design_intent else None,
        "flow_backend": flow_decision.get("backend"),
        "flow_profile": flow_decision.get("profile"),
    }, indent=2, sort_keys=True) + "\n")
    return ApprovalPlanArtifact(
        project_root=project_root,
        plan_path=plan_path,
        diagram_path=diagram_path,
        structure_path=structure_path,
        content=content,
        actions=[
            {
                "key": "approve_plan",
                "label": "Approve Plan",
                "prompt": "Approve this plan and begin execution.",
                "description": f"Start implementation inside {project_root}/ using the approved scope.",
            },
            {
                "key": "revise_plan",
                "label": "Revise Plan",
                "prompt": "Revise the plan: ",
                "description": "Change architecture, PDK, interface, tool flow, or deliverables before execution.",
            },
            {
                "key": "open_diagram",
                "label": "Open Diagram",
                "artifact": diagram_path,
                "description": "Open the saved Mermaid diagram in the workspace preview.",
            },
        ],
    )


def _latest_payload(role_results: list[Any], kind: str) -> dict[str, Any]:
    for result in reversed(role_results or []):
        for envelope in reversed(getattr(result, "envelopes", []) or []):
            if getattr(envelope, "kind", "") == kind:
                payload = getattr(envelope, "payload", {}) or {}
                return payload if isinstance(payload, dict) else {}
    return {}


def _project_root(user_text: str, design_name: str, spec: dict[str, Any]) -> str:
    module = ""
    contracts = spec.get("module_contracts") or []
    if contracts and isinstance(contracts[0], dict):
        module = str(contracts[0].get("module_name") or "")
    title = str(spec.get("title") or "")
    for candidate in (module, title, design_name):
        slug = _slug(candidate)
        if slug and not slug.startswith("design_"):
            return slug
    return _request_slug(user_text)[:48] or _digest_project_root(user_text, design_name, spec)


def _approval_markdown(
    *,
    user_text: str,
    design_name: str,
    project_root: str,
    spec: dict[str, Any],
    flow: dict[str, Any],
    verification: dict[str, Any],
    signoff: dict[str, Any],
    flow_decision: dict[str, Any],
    diagram: str,
    design_intent: DesignIntent | None = None,
) -> str:
    selected_pdk = spec.get("target_pdk") or flow.get("selected_pdk") or (flow_decision.get("selected_pdk") or {}).get("name") or "not selected"
    backend = flow.get("selected_backend") or flow_decision.get("backend") or "not selected"
    profile = flow_decision.get("profile") or "environment dependent"
    open_questions = spec.get("open_questions") or []
    deliverables = spec.get("deliverables") or ["rtl", "testbench", "simulation"]
    blocker_text = "\n".join(f"- {item}" for item in _unique([*open_questions, *(flow.get("blockers") or [])])) or "- None blocking the planning step."
    module_rows = _intent_module_table(design_intent)
    return (
        "# AgentIC Approval Plan\n\n"
        f"**Workspace:** `{design_name}`  \n"
        f"**Project root:** `{project_root}/`  \n"
        f"**User request:** {user_text.strip()[:1000]}\n\n"
        "## 1. Design Specification\n\n"
        f"- **Title:** {spec.get('title') or 'Draft chip project'}\n"
        f"- **Target PDK/node:** `{selected_pdk}`\n"
        f"- **Deliverables:** {', '.join(str(item) for item in deliverables)}\n"
        f"- **Spec confidence:** `{spec.get('confidence') or 'draft'}`\n\n"
        "## 2. Architecture Diagram\n\n"
        "The diagram is saved as a real workspace artifact so the preview panel can render it.\n\n"
        "```mermaid\n"
        f"{diagram.strip()}\n"
        "```\n\n"
        "## 3. Directory And File Plan\n\n"
        "| Directory | Purpose |\n"
        "| --- | --- |\n"
        + "".join(f"| `{project_root}/{path}/` | {purpose} |\n" for path, purpose in CANONICAL_PROJECT_DIRS)
        + "\n## 3.1 Module Ownership\n\n"
        + module_rows
        + "\n## 3.2 Intent Contracts And Capability Bindings\n\n"
        + _intent_contract_sections(design_intent)
        + "\n## 4. Verification Strategy\n\n"
        f"- **Strategy:** {verification.get('strategy') or 'self-checking simulation plus assertions where available'}\n"
        + "".join(f"- {item}\n" for item in (verification.get("required_tests") or [])[:8])
        + "\n## 5. Tool Flow\n\n"
        f"- **Selected backend:** `{backend}`\n"
        f"- **Flow profile:** `{profile}`\n"
        f"- **Fallback policy:** {flow.get('fallback_policy') or 'prefer detected user toolchain, then approved open-source fallback'}\n"
        + _adapter_table(flow)
        + "\n## 6. Approval Gate\n\n"
        f"{blocker_text}\n\n"
        "Approve this plan to begin execution.\n"
        "If anything above is wrong, revise the architecture, PDK/tool flow, interface, or deliverables before approval.\n\n"
        "## Saved Artifacts\n\n"
        f"- `{project_root}/docs/plans/approval_plan.md`\n"
        f"- `{project_root}/docs/diagrams/architecture.mermaid`\n"
        f"- `{project_root}/docs/PROJECT_STRUCTURE.md`\n"
        f"- `{project_root}/PROJECT_MANIFEST.json`\n"
    )


def _intent_module_table(design_intent: DesignIntent | None) -> str:
    if not design_intent or not design_intent.modules:
        return "Module ownership will be finalized before execution.\n"
    rows = [
        "| Module | Kind | RTL Owner | Test Owner | Status |",
        "| --- | --- | --- | --- | --- |",
    ]
    for module in design_intent.modules.values():
        rows.append(
            f"| `{module.name}` | {module.kind} | `{module.owner_file}` | "
            f"`{module.verification_owner or ''}` | `{module.status}` |"
        )
    return "\n".join(rows) + "\n"


def _intent_contract_sections(design_intent: DesignIntent | None) -> str:
    if not design_intent:
        return "Typed intent contracts will be materialized before execution.\n"
    sections = []
    if design_intent.memories:
        rows = [
            "| Memory | Kind | Width | Depth | Preference | Candidate Macro |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for memory in design_intent.memories.values():
            candidate = _binding_for(design_intent, f"memory.{memory.name}", "memory_macro")
            rows.append(
                f"| `{memory.name}` | `{memory.kind}` | {memory.width_bits or 'TBD'} | "
                f"{memory.depth_words or 'TBD'} | `{memory.implementation_preference}` | "
                f"`{candidate.get('capability_name') if candidate else 'unbound'}` |"
            )
        sections.append("### Memory Requirements\n\n" + "\n".join(rows) + "\n")
    if design_intent.buses:
        rows = [
            "| Bus | Protocol | Address Width | Data Width | Endpoints |",
            "| --- | --- | --- | --- | --- |",
        ]
        for bus in design_intent.buses.values():
            rows.append(
                f"| `{bus.name}` | `{bus.protocol}` | {bus.addr_width or 'N/A'} | "
                f"{bus.data_width or 'N/A'} | {', '.join(f'`{item}`' for item in bus.endpoints[:6]) or 'TBD'} |"
            )
        sections.append("### Bus Contracts\n\n" + "\n".join(rows) + "\n")
    if design_intent.timing:
        rows = [
            "| Clock | Period ns | Frequency MHz | Source |",
            "| --- | --- | --- | --- |",
        ]
        for timing in design_intent.timing:
            rows.append(f"| `{timing.clock}` | {timing.period_ns or 'TBD'} | {timing.frequency_mhz or 'TBD'} | `{timing.source}` |")
        sections.append("### Timing Intent\n\n" + "\n".join(rows) + "\n")
    if design_intent.toolchain.stages:
        rows = [
            "| Stage | Adapter | Vendor | Available | License Hint |",
            "| --- | --- | --- | --- | --- |",
        ]
        for stage, contract in design_intent.toolchain.stages.items():
            rows.append(
                f"| `{stage}` | `{contract.adapter or 'auto'}` | `{contract.vendor or 'detected'}` | "
                f"{contract.available} | {contract.license_hint_present} |"
            )
        sections.append("### Toolchain Contract\n\n" + "\n".join(rows) + "\n")
    if design_intent.physical.signoff_required or design_intent.physical.pad_requirements or design_intent.physical.macro_placement_policy != "unspecified":
        sections.append(
            "### Physical Intent\n\n"
            f"- Signoff required: `{design_intent.physical.signoff_required}`\n"
            f"- Macro placement policy: `{design_intent.physical.macro_placement_policy}`\n"
            f"- Pad requirements: {', '.join(f'`{item}`' for item in design_intent.physical.pad_requirements) or '`not specified`'}\n"
        )
    unresolved = design_intent.unresolved[:12]
    if unresolved:
        sections.append("### Open Contract Items\n\n" + "".join(f"- {item}\n" for item in unresolved))
    return "\n".join(sections) if sections else "No memory, bus, timing, or physical contracts were inferred from this request yet.\n"


def _binding_for(design_intent: DesignIntent, requirement: str, kind: str) -> dict[str, Any] | None:
    for binding in design_intent.capability_bindings:
        if binding.requirement == requirement and binding.capability_kind == kind:
            return binding.model_dump(mode="json")
    return None


def _adapter_table(flow: dict[str, Any]) -> str:
    rows = []
    for item in flow.get("adapter_sequence") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            f"| {item.get('stage') or 'stage'} | {item.get('adapter') or 'auto'} | "
            f"{item.get('vendor') or 'detected'} | {item.get('available')} | {item.get('required')} |"
        )
    if not rows:
        return "\n"
    return "\n| Stage | Adapter | Vendor | Available | Required |\n| --- | --- | --- | --- | --- |\n" + "\n".join(rows) + "\n"


def _structure_markdown(project_root: str, user_text: str, flow_decision: dict[str, Any]) -> str:
    return (
        "# Project Structure Contract\n\n"
        f"Project root: `{project_root}/`\n\n"
        "AgentIC must keep generated chip work inside this project root. Source files, generated run outputs, reports, and diagrams must not be mixed at the workspace root.\n\n"
        "| Directory | Contract |\n"
        "| --- | --- |\n"
        + "".join(f"| `{path}/` | {purpose} |\n" for path, purpose in CANONICAL_PROJECT_DIRS)
        + "\n## Flow Context\n\n"
        f"- Backend: `{flow_decision.get('backend') or 'none'}`\n"
        f"- Profile: `{flow_decision.get('profile') or 'unknown'}`\n"
        f"- Request digest source: {user_text[:240]}\n"
    )


def _architecture_mermaid(user_text: str, spec: dict[str, Any]) -> str:
    contracts = [item for item in spec.get("module_contracts") or [] if isinstance(item, dict)]
    block_type = (contracts[0].get("block_type") if contracts else "") or ""
    lowered = f"{user_text} {block_type}".lower()
    if re.search(r"\bbest\s+chip\b|\bcomplex\b|\bsoc\b|\b130nm\b", lowered):
        return _normalize_mermaid("""
flowchart TD
    SYS["130nm Reference SoC"]
    CPU["Control CPU Cluster"]
    DSP["DSP / Signal Processing Tile"]
    ACC["Domain Accelerator Tile"]
    MEM["SRAM + Memory Controller"]
    FAB["Shared Interconnect Fabric"]
    PER["Peripheral and Debug Subsystem"]
    PWR["Clock Reset Power Manager"]
    IO["Pad Ring / External IO"]

    SYS --> CPU
    SYS --> DSP
    SYS --> ACC
    CPU --> FAB
    DSP --> FAB
    ACC --> FAB
    FAB --> MEM
    FAB --> PER
    PER --> IO
    PWR --> CPU
    PWR --> DSP
    PWR --> ACC
    PWR --> MEM
""")
    if contracts:
        contract = contracts[0]
        fallback_module = _request_slug(user_text) or _digest_project_root(user_text, design_name, spec)
        module_name = str(contract.get("module_name") or fallback_module)
        module = _safe_id(module_name)
        lines = ["flowchart TD", f'    {module}["{module_name}"]']
        for port in contract.get("ports") or []:
            if not isinstance(port, dict):
                continue
            port_id = _safe_id(str(port.get("name") or "port"))
            label = str(port.get("name") or port_id).replace('"', '\\"')
            direction = port.get("direction")
            lines.append(f'    {port_id}["{label}"]')
            lines.append(f"    {port_id} --> {module}" if direction == "input" else f"    {module} --> {port_id}")
        return _normalize_mermaid("\n".join(lines))
    return _normalize_mermaid("""
flowchart TD
    SPEC["Approved Specification"]
    RTL["RTL Implementation"]
    DV["Verification"]
    FLOW["Synthesis / Implementation Flow"]
    SIGNOFF["Evidence-Based Signoff"]
    SPEC --> RTL
    RTL --> DV
    DV --> FLOW
    FLOW --> SIGNOFF
""")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n", encoding="utf-8")


def _normalize_mermaid(text: str) -> str:
    lines = []
    for raw in (text or "").strip().splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        lines.append(line)
    if not lines:
        return "flowchart TD\n    A[Specification] --> B[Implementation]\n"
    if not re.match(r"^\s*(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram)", lines[0]):
        lines.insert(0, "flowchart TD")
    return "\n".join(lines) + "\n"


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value or "node").strip("_")
    if not cleaned:
        cleaned = "node"
    if cleaned[0].isdigit():
        cleaned = f"N_{cleaned}"
    return cleaned


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").lower()).strip("_")[:64]


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
    import hashlib
    import json

    digest = hashlib.sha256(json.dumps({
        "user_text": user_text,
        "design_name": design_name,
        "title": spec.get("title"),
    }, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
    return f"intent_{digest}"


def _unique(items: list[Any]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out
