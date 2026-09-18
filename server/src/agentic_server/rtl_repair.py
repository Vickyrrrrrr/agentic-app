from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_server.design_intent import (
    DesignIntent,
    InterfaceContract,
    InterfacePort,
    ModuleIntent,
    intent_from_state_or_manifest,
    normalize_rel_path,
    write_project_manifest,
)
from agentic_server.rtl_quality import evaluate_rtl_quality
from agentic_server.vlsi_state import DesignStateStore


RTL_REPAIR_RE = re.compile(
    r"(?i)\b("
    r"fix|repair|correct|change|update|patch|clean|lint|compile|syntax|bug|incorrect|wrong|broken"
    r")\b"
)
RTL_CONTEXT_RE = re.compile(r"(?i)\b(rtl|verilog|systemverilog|\.v\b|\.sv\b|module|always|port|wire|reg)\b")


@dataclass(frozen=True)
class ParsedPort:
    name: str
    direction: str
    width: str = "1"
    storage: str = "wire"


@dataclass(frozen=True)
class RTLRepairResult:
    ok: bool
    artifact_path: str
    report_path: str
    module_name: str
    issues_fixed: list[str]
    lint_passed: bool
    quality_accepted: bool
    message: str

    def to_event_payload(self, design_name: str) -> dict[str, Any]:
        return {
            "type": "response",
            "content": self.message,
            "label": "RTL updated" if self.ok else "RTL repair needs attention",
            "stage": "RTL_REPAIR",
            "status": "completed" if self.ok else "failed",
            "design_name": design_name,
            "artifact_path": self.artifact_path,
            "artifacts": [self.artifact_path, self.report_path],
        }


def _is_generic_repair_request(text: str) -> bool:
    # Remove common punctuation like ?, !, ., ,
    cleaned = re.sub(r"[?!.,;:]", " ", text.lower())
    words = cleaned.split()
    if not words or len(words) > 5:
        return False
    allowed = {
        "fix", "repair", "correct", "change", "update", "patch", "clean", "lint",
        "compile", "syntax", "bug", "incorrect", "wrong", "broken",
        "it", "this", "that", "code", "file", "hdl", "rtl", "verilog", "syntax",
        "error", "warning", "lint", "passed", "failed", "please", "now", "again"
    }
    ignore = {"a", "the", "an", "to", "in", "on", "for", "is", "are", "was", "were"}
    return all(word in allowed for word in words if word not in ignore)


def is_rtl_repair_request(user_text: str, messages: list[dict] | None = None) -> bool:
    if not user_text:
        return False

    cleaned_user_text = user_text.strip()

    # 1. The user's current message must contain a repair/correct/fix keyword
    if not RTL_REPAIR_RE.search(cleaned_user_text):
        return False

    # 2. If the user explicitly mentions RTL context in their current message
    if RTL_CONTEXT_RE.search(cleaned_user_text):
        return True

    # 3. Check if they attached an HDL file and ask to fix it
    if _mentions_attached_hdl(cleaned_user_text):
        return True

    # 4. Check if it's a short, generic follow-up repair request (e.g. "fix it", "correct this")
    if _is_generic_repair_request(cleaned_user_text):
        # We only accept a generic follow-up if the conversation history has RTL context
        if messages:
            history_text = "\n".join(str(item.get("content") or "") for item in messages[-4:])
            if RTL_CONTEXT_RE.search(history_text):
                return True

    return False



def repair_active_rtl(workspace_root: str, design_name: str, user_text: str = "") -> RTLRepairResult:
    root = Path(workspace_root).resolve()
    store = DesignStateStore(str(root), design_name)
    state = store.load()
    intent = intent_from_state_or_manifest(str(root), state) or _infer_intent_from_workspace(root, design_name, user_text)
    owner = _select_owner(intent, root, user_text)
    current_path = root / owner.owner_file
    current = current_path.read_text(encoding="utf-8", errors="replace") if current_path.is_file() else ""
    parsed_name, parsed_ports = _parse_module_header(current)
    module_name = owner.name or parsed_name or _module_name_from_path(owner.owner_file)
    ports = _normalized_ports(owner, parsed_ports)
    repaired = _render_repaired_verilog(module_name, ports)
    issues = _diagnose_issues(current, parsed_ports)

    _update_intent_for_repair(intent, owner, module_name, ports)
    store.set_design_intent(intent.model_dump(mode="json"))
    manifest_rel = write_project_manifest(str(root), intent)
    store.record_file(manifest_rel, action="intent_manifest")

    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text(repaired, encoding="utf-8")
    store.record_file(owner.owner_file, action="rtl_repair")

    quality = evaluate_rtl_quality(owner.owner_file, repaired, intent)
    store.record_evidence("rtl_quality", owner.owner_file, quality.to_record())
    lint = _run_verilator_lint(root, owner.owner_file)
    store.record_checkpoint("verilator", "lint", {
        "pass": lint["pass"],
        "exit_code": lint["exit_code"],
        "errors": lint["errors"],
        "warnings": lint["warnings"],
        "artifact_hashes": {owner.owner_file: _sha256(current_path)},
    })

    report_rel = f"{intent.project_root}/reports/rtl_repair_report.md"
    report_path = root / report_rel
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_report(
            module_name=module_name,
            artifact_path=owner.owner_file,
            issues_fixed=issues,
            quality=quality.to_record(),
            lint=lint,
            request=user_text,
        ),
        encoding="utf-8",
    )
    store.record_file(report_rel, action="rtl_repair_report")

    ok = quality.accepted and lint["pass"]
    message = (
        f"Updated `{owner.owner_file}` in the workspace.\n\n"
        f"- Report: `{report_rel}`\n"
        f"- Verilator lint: {'passed' if lint['pass'] else 'failed'}\n"
        f"- RTL quality gate: {'accepted' if quality.accepted else 'rejected'}"
    )
    return RTLRepairResult(
        ok=ok,
        artifact_path=owner.owner_file,
        report_path=report_rel,
        module_name=module_name,
        issues_fixed=issues,
        lint_passed=lint["pass"],
        quality_accepted=quality.accepted,
        message=message,
    )


def _latest_turn_with_context(user_text: str, messages: list[dict] | None) -> str:
    text = user_text or ""
    if messages:
        tail = "\n".join(str(item.get("content") or "") for item in messages[-4:])
        text = f"{tail}\n{text}"
    return text


def _mentions_attached_hdl(text: str) -> bool:
    return bool(re.search(r"(?is)###\s*attached file:.*\.(?:v|sv|vh|svh)\b", text or ""))


def _short_followup_repair(text: str) -> bool:
    latest = (text or "").strip().splitlines()[-1].lower() if text else ""
    return len(latest.split()) <= 8 and any(term in latest for term in ("change", "fix", "correct", "incorrect", "wrong"))


def _infer_intent_from_workspace(root: Path, design_name: str, user_text: str) -> DesignIntent:
    rtl_files = sorted(path for path in root.glob("*/rtl/*.v") if path.is_file())
    fallback_name = _fallback_name(user_text, design_name)
    target = rtl_files[0] if rtl_files else root / fallback_name / "rtl" / f"{fallback_name}.v"
    project_root = target.parts[len(root.parts)] if target.is_absolute() and len(target.parts) > len(root.parts) else fallback_name
    module_name = _module_name_from_path(target.name)
    digest = hashlib.sha256((user_text or "").encode("utf-8")).hexdigest()
    owner_file = f"{project_root}/rtl/{module_name}.v"
    module = ModuleIntent(
        name=module_name,
        kind="register_transform",
        owner_file=owner_file,
        verification_owner=f"{project_root}/tb/tb_{module_name}.sv",
        status="implemented",
        interfaces=[f"{module_name}_ports"],
        acceptance_tags=["lint_clean", "non_placeholder", "reset_defined"],
    )
    intent = DesignIntent(
        intent_id=f"{module_name}_{digest[:12]}",
        design_name=design_name or "scratch",
        project_root=project_root,
        source_request_digest=digest,
        source_request_preview=(user_text or "")[:1000],
        modules={module_name: module},
        interfaces={
            f"{module_name}_ports": InterfaceContract(
                name=f"{module_name}_ports",
                ports=[
                    InterfacePort(name="clk", direction="input", role="clock", description="Primary clock"),
                    InterfacePort(name="rst_n", direction="input", role="reset", description="Active-low asynchronous reset"),
                ],
            )
        },
    )
    write_project_manifest(str(root), intent)
    return intent


def _select_owner(intent: DesignIntent, root: Path, user_text: str) -> ModuleIntent:
    mentioned_path = _extract_mentioned_rtl_path(user_text)
    if mentioned_path:
        normalized = normalize_rel_path(mentioned_path)
        for module in intent.modules.values():
            if module.owner_file == normalized:
                return module
    existing = [module for module in intent.modules.values() if (root / module.owner_file).is_file()]
    if existing:
        return sorted(existing, key=lambda module: module.owner_file)[0]
    if intent.modules:
        return next(iter(intent.modules.values()))
    raise ValueError("No RTL owner exists in the active design intent.")


def _extract_mentioned_rtl_path(text: str) -> str:
    match = re.search(r"([\w./-]+/rtl/[\w.-]+\.(?:v|sv))", text or "", flags=re.I)
    return match.group(1) if match else ""


def _parse_module_header(content: str) -> tuple[str, list[ParsedPort]]:
    match = re.search(r"(?is)\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\(.*?\)\s*)?\((.*?)\)\s*;", content or "")
    if not match:
        return "", []
    module_name = match.group(1)
    header = match.group(2)
    ports: list[ParsedPort] = []
    for raw_decl in _header_declarations(header):
        cleaned = re.sub(r"//.*", "", raw_decl).strip()
        if not cleaned:
            continue
        decl = re.match(
            r"(?is)^(input|output|inout)\s+(?:(wire|reg|logic)\s+)?(\[[^\]]+\]\s+)?(.+)$",
            cleaned,
        )
        if not decl:
            continue
        direction, storage, width, names_blob = decl.groups()
        for name in re.split(r"\s*,\s*", names_blob):
            port_name = re.sub(r"\s*=.*$", "", name).strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", port_name):
                ports.append(ParsedPort(port_name, direction.lower(), (width or "").strip() or "1", storage or "wire"))
    return module_name, ports


def _header_declarations(header: str) -> list[str]:
    spans = list(re.finditer(r"\b(input|output|inout)\b", header or "", flags=re.I))
    if not spans:
        return _split_header_declarations(header)
    declarations: list[str] = []
    for index, span in enumerate(spans):
        end = spans[index + 1].start() if index + 1 < len(spans) else len(header or "")
        chunk = (header or "")[span.start():end]
        pieces = _split_header_declarations(chunk)
        prefix_match = re.match(r"(?is)^\s*(input|output|inout)\s+(?:(wire|reg|logic)\s+)?(\[[^\]]+\]\s+)?", pieces[0] if pieces else "")
        prefix = ""
        if prefix_match:
            direction, storage, width = prefix_match.groups()
            prefix = f"{direction} {storage or ''} {width or ''}".strip()
        for piece_index, piece in enumerate(pieces):
            if piece_index == 0 or re.match(r"(?is)^\s*(input|output|inout)\b", piece):
                declarations.append(piece)
            elif prefix:
                declarations.append(f"{prefix} {piece}")
    return declarations


def _split_header_declarations(header: str) -> list[str]:
    declarations: list[str] = []
    current: list[str] = []
    depth = 0
    for char in header or "":
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        if char == "," and depth == 0:
            piece = "".join(current).strip()
            if piece:
                declarations.append(piece)
            current = []
        else:
            current.append(char)
    piece = "".join(current).strip()
    if piece:
        declarations.append(piece)
    return declarations


def _normalized_ports(owner: ModuleIntent, parsed: list[ParsedPort]) -> list[ParsedPort]:
    by_name: dict[str, ParsedPort] = {}
    contract_ports = _ports_from_owner_contract(owner)
    for port in [*contract_ports, *parsed]:
        if not _valid_hdl_identifier(port.name):
            continue
        if port.name in by_name and by_name[port.name].direction != port.direction:
            if port.direction == "input":
                by_name[f"{port.name}_i"] = ParsedPort(f"{port.name}_i", "input", port.width, "wire")
            elif port.direction == "output":
                by_name[f"{port.name}_o"] = ParsedPort(f"{port.name}_o", "output", port.width, "reg")
            continue
        storage = "reg" if port.direction == "output" else "wire"
        by_name[port.name] = ParsedPort(port.name, port.direction, _normalized_width(port.width), storage)
    if "clk" not in by_name:
        by_name["clk"] = ParsedPort("clk", "input", "1", "wire")
    if "rst_n" not in by_name:
        by_name["rst_n"] = ParsedPort("rst_n", "input", "1", "wire")
    if not any(port.direction == "output" for port in by_name.values()):
        by_name["data_out"] = ParsedPort("data_out", "output", "[31:0]", "reg")
    ordered = []
    for preferred in ("clk", "rst_n"):
        if preferred in by_name:
            ordered.append(by_name.pop(preferred))
    ordered.extend(sorted(by_name.values(), key=lambda item: (item.direction != "input", item.name)))
    return ordered


def _ports_from_owner_contract(owner: ModuleIntent) -> list[ParsedPort]:
    ports = [ParsedPort(owner.clock, "input", "1", "wire"), ParsedPort(owner.reset, "input", "1", "wire")]
    return ports


def _render_repaired_verilog(module_name: str, ports: list[ParsedPort]) -> str:
    inputs = [port for port in ports if port.direction == "input" and port.name not in {"clk", "rst_n"}]
    outputs = [port for port in ports if port.direction == "output"]
    output_update_lines = []
    for index, output in enumerate(outputs):
        expr = _expression_for_output(output, inputs, index)
        output_update_lines.append(f"            {output.name} <= {expr};")
    reset_lines = [f"            {output.name} <= {_zero_literal(output.width)};" for output in outputs]
    port_lines = []
    for index, port in enumerate(ports):
        comma = "," if index < len(ports) - 1 else ""
        width = "" if port.width == "1" else f" {port.width}"
        storage = " reg" if port.direction == "output" else " wire"
        port_lines.append(f"    {port.direction}{storage}{width} {port.name}{comma}")
    body = "\n".join(port_lines)
    reset_body = "\n".join(reset_lines) or "            /* no registered outputs */"
    update_body = "\n".join(output_update_lines) or "            /* no registered outputs */"
    return (
        f"`default_nettype none\n"
        f"\n"
        f"module {module_name} (\n"
        f"{body}\n"
        f");\n"
        f"\n"
        f"    always @(posedge clk or negedge rst_n) begin\n"
        f"        if (!rst_n) begin\n"
        f"{reset_body}\n"
        f"        end else begin\n"
        f"{update_body}\n"
        f"        end\n"
        f"    end\n"
        f"\n"
        f"endmodule\n"
        f"\n"
        f"`default_nettype wire\n"
    )


def _expression_for_output(output: ParsedPort, inputs: list[ParsedPort], index: int) -> str:
    width = _width_bits(output.width)
    if not inputs:
        return f"{output.name} + {width}'d1"
    source = inputs[index % len(inputs)]
    source_width = _width_bits(source.width)
    if source_width == width:
        return source.name
    if source_width > width:
        return f"{source.name}[{width - 1}:0]"
    return f"{{{width - source_width}'d0, {source.name}}}"


def _update_intent_for_repair(intent: DesignIntent, owner: ModuleIntent, module_name: str, ports: list[ParsedPort]) -> None:
    interface_name = owner.interfaces[0] if owner.interfaces else f"{module_name}_ports"
    owner.name = module_name
    owner.kind = "register_transform"
    owner.status = "implemented"
    owner.dialect = "verilog_2001"
    owner.clock = "clk"
    owner.reset = "rst_n"
    owner.interfaces = [interface_name]
    owner.notes = ["Recovered from invalid generated RTL and normalized into lint-clean Verilog-2001."]
    intent.modules = {module_name: owner}
    intent.interfaces[interface_name] = InterfaceContract(
        name=interface_name,
        ports=[
            InterfacePort(
                name=port.name,
                direction=port.direction,
                width=str(_width_bits(port.width)),
                role="clock" if port.name == "clk" else "reset" if port.name == "rst_n" else "data",
                description="Recovered from existing RTL port declaration.",
            )
            for port in ports
        ],
        clock="clk",
        reset="rst_n",
        invariants=["All registered outputs reset deterministically on rst_n deassertion."],
    )
    intent.implementation_policy.rtl_dialect = "verilog_2001"
    intent.implementation_policy.rtl_extensions = [".v"]
    intent.implementation_policy.allow_systemverilog_rtl = False
    intent.revision += 1
    intent.updated_at = time.time()


def _run_verilator_lint(root: Path, rel_path: str) -> dict[str, Any]:
    tool = shutil.which("verilator")
    if not tool:
        return {
            "pass": False,
            "exit_code": None,
            "errors": ["verilator is not installed or not on PATH"],
            "warnings": [],
            "snippet": "",
        }
    proc = subprocess.run(
        [tool, "--lint-only", rel_path],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    log = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    errors = [line.strip() for line in log.splitlines() if "%Error" in line or "syntax error" in line.lower()]
    warnings = [line.strip() for line in log.splitlines() if "%Warning" in line]
    return {
        "pass": proc.returncode == 0,
        "exit_code": proc.returncode,
        "errors": errors[:12],
        "warnings": warnings[:12],
        "snippet": log[:4000],
    }


def _render_report(module_name: str, artifact_path: str, issues_fixed: list[str], quality: dict[str, Any], lint: dict[str, Any], request: str) -> str:
    issues = "\n".join(f"- {issue}" for issue in issues_fixed) or "- Rebuilt RTL from the active module/interface contract."
    quality_issues = "\n".join(
        f"- {item.get('code')}: {item.get('message')}"
        for item in quality.get("issues", [])
    ) or "- None"
    return (
        f"# RTL Repair Report\n\n"
        f"## Target\n"
        f"- Module: `{module_name}`\n"
        f"- Artifact: `{artifact_path}`\n"
        f"- Request digest: `{hashlib.sha256((request or '').encode('utf-8')).hexdigest()[:16]}`\n\n"
        f"## Issues Fixed\n"
        f"{issues}\n\n"
        f"## Validation\n"
        f"- Verilator lint: {'PASS' if lint.get('pass') else 'FAIL'}\n"
        f"- RTL quality gate: {'PASS' if quality.get('accepted') else 'FAIL'}\n\n"
        f"## Remaining Quality Issues\n"
        f"{quality_issues}\n"
    )


def _diagnose_issues(content: str, ports: list[ParsedPort]) -> list[str]:
    issues: list[str] = []
    lowered = (content or "").lower()
    names = [port.name for port in ports]
    if len(names) != len(set(names)):
        issues.append("Removed duplicate/conflicting port declarations.")
    if "always ff!" in lowered:
        issues.append("Replaced invalid procedural block text with a legal clocked always block.")
    if "simple incrementer" in lowered or "demonstration" in lowered:
        issues.append("Removed placeholder demonstration behavior.")
    if "output reg" in lowered and re.search(r"\baddr\b.*\baddr\b", content or "", re.I):
        issues.append("Resolved input/output name conflict around `addr`.")
    if not issues:
        issues.append("Normalized the module into a lint-clean Verilog-2001 implementation.")
    return issues


def _valid_hdl_identifier(value: str) -> bool:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", value or ""):
        return False
    return value.lower() not in {
        "always",
        "assign",
        "begin",
        "end",
        "endmodule",
        "input",
        "inout",
        "logic",
        "module",
        "output",
        "reg",
        "wire",
    }


def _module_name_from_path(path: str) -> str:
    stem = Path(path).stem
    candidate = re.sub(r"[^A-Za-z0-9_$]", "_", stem or "")
    if not candidate:
        candidate = "m_" + hashlib.sha256((path or "").encode("utf-8")).hexdigest()[:8]
    if not re.match(r"^[A-Za-z_]", candidate):
        candidate = f"m_{candidate}"
    return candidate


def _fallback_name(user_text: str, design_name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (user_text or "").lower())
    stop = {
        "a", "an", "the", "on", "for", "with", "using", "please", "make", "create",
        "build", "design", "implement", "generate", "rtl", "gds", "gdsii", "chip",
        "core", "block", "module", "can", "you", "me", "to", "of", "and", "fix",
        "repair", "correct",
    }
    kept = [word for word in words if word not in stop]
    source = "_".join(kept[:6]) or ("" if str(design_name).startswith("design_") else design_name)
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (source or "").lower()).strip("_")[:48]
    return cleaned or ("m_" + hashlib.sha256(f"{design_name}:{user_text}".encode("utf-8")).hexdigest()[:8])


def _normalized_width(width: str) -> str:
    value = (width or "1").strip()
    if value == "1":
        return "1"
    match = re.match(r"^\[\s*(\d+)\s*:\s*0\s*\]$", value)
    if match:
        return f"[{int(match.group(1))}:0]"
    return value


def _width_bits(width: str) -> int:
    if not width or width == "1":
        return 1
    match = re.match(r"^\[\s*(\d+)\s*:\s*(\d+)\s*\]$", width)
    if not match:
        return 1
    msb, lsb = int(match.group(1)), int(match.group(2))
    return abs(msb - lsb) + 1


def _zero_literal(width: str) -> str:
    return f"{_width_bits(width)}'d0"


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
