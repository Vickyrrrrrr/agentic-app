from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from design_intent import DesignIntent, ModuleIntent


PLACEHOLDER_RE = re.compile(
    r"(?i)\b(placeholder|todo|stub|dummy|example\s+logic|sample\s+logic|fake|not\s+implemented|demonstration|demo\s+logic)\b"
)
MODULE_RE = re.compile(r"(?ms)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b(.*?)endmodule")
PORT_RE = re.compile(r"\b(input|output|inout)\b\s+(?:wire|reg|logic)?\s*(?:\[[^\]]+\]\s*)?([A-Za-z_][A-Za-z0-9_$]*)")
ALWAYS_RE = re.compile(r"(?m)^\s*always\b|always_ff\b|always_comb\b")
RESET_RE = re.compile(r"(?i)\b(rst|reset|rst_n|reset_n)\b")
ASSIGN_RE = re.compile(r"(?m)^\s*assign\s+")
INTERNAL_DECL_RE = re.compile(r"(?m)^\s*(?:wire|reg|logic)\s+(?:\[[^\]]+\]\s*)?([^;]+);")
CONT_ASSIGN_RE = re.compile(r"(?m)^\s*assign\s+([A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?)\s*=")
PROC_ASSIGN_RE = re.compile(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?)\s*(?:<=|=)")
INSTANCE_RE = re.compile(r"(?ms)^\s*([A-Za-z_][A-Za-z0-9_$]*)(?:\s*#\s*\(.*?\))?\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(")
MEMORY_DECL_RE = re.compile(
    r"(?m)^\s*(?:reg|logic)\s*(\[[^\]]+\])?\s+([A-Za-z_][A-Za-z0-9_$]*)\s*(\[[^\]]+\])\s*;"
)
ASYNC_RESET_ALWAYS_RE = re.compile(r"(?is)always\s*@\s*\([^)]*(?:negedge|posedge)\s+(?:rst|reset)[A-Za-z0-9_$]*[^)]*\)")
SYNC_RESET_ALWAYS_RE = re.compile(r"(?is)always\s*@\s*\([^)]*posedge\s+(?!rst|reset)[^)]+\).*?\bif\s*\(\s*!?\s*(?:rst|reset)[A-Za-z0-9_$]*\s*\)")
SYSTEM_MODULES = {
    "and", "or", "xor", "xnor", "nand", "nor", "not", "buf", "bufif0", "bufif1",
    "pmos", "nmos", "cmos", "tran", "pullup", "pulldown",
}


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    path: str = ""


class RTLQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    path: str
    module_name: str | None = None
    owner_file: str | None = None
    quality_level: Literal["rejected", "draft", "implementation"] = "rejected"
    issues: list[QualityIssue] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def evaluate_rtl_quality(path: str, content: str, intent: DesignIntent | None = None) -> RTLQualityResult:
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    modules = _modules(content)
    module_name = modules[0][0] if modules else None
    owner = _owner(intent, normalized, module_name) if intent else None
    issues: list[QualityIssue] = []
    metrics = {
        "bytes": len(content or ""),
        "line_count": len((content or "").splitlines()),
        "module_count": len(modules),
        "assign_count": len(ASSIGN_RE.findall(content or "")),
        "always_count": len(ALWAYS_RE.findall(content or "")),
    }

    if Path(normalized).suffix.lower() not in {".v", ".sv", ".vh", ".svh"}:
        issues.append(_issue("rtl_bad_extension", "RTL quality gate only accepts Verilog/SystemVerilog source paths.", normalized))
    if len((content or "").strip()) < 120:
        issues.append(_issue("rtl_too_small", "RTL file is too small to be a meaningful implementation.", normalized))
    if PLACEHOLDER_RE.search(content or ""):
        issues.append(_issue("placeholder_rtl", "RTL contains placeholder/stub language.", normalized))
    if not modules:
        issues.append(_issue("missing_module", "RTL file does not contain a Verilog module.", normalized))
    if len(modules) > 1:
        issues.append(_issue("multiple_modules_in_owner_file", "Owner RTL file should contain one primary module.", normalized, "warning"))

    if intent and owner is None:
        issues.append(_issue("unowned_rtl", "RTL file is not owned by the active PROJECT_MANIFEST.", normalized))
    if owner is not None:
        if normalized != owner.owner_file:
            issues.append(_issue("wrong_owner_file", f"Module `{owner.name}` must be implemented in `{owner.owner_file}`.", normalized))
        if module_name and module_name != owner.name:
            issues.append(_issue("module_name_mismatch", f"Expected module `{owner.name}`, found `{module_name}`.", normalized))
        if owner.dialect == "verilog_2001" and normalized.endswith(".sv"):
            issues.append(_issue("rtl_dialect_mismatch", "Verilog-2001 RTL owner must use `.v`, not `.sv`.", normalized))
        if owner.kind == "unspecified_requested_block":
            issues.append(_issue(
                "insufficient_behavior_contract",
                "RTL implementation is blocked because the module intent has no concrete behavior/interface contract.",
                normalized,
            ))
        issues.extend(_interface_issues(content, owner, intent, normalized))
        if _sequential_expected(owner) and not ALWAYS_RE.search(content or ""):
            issues.append(_issue("missing_sequential_logic", f"Module `{owner.name}` requires sequential/reset behavior.", normalized))
        if _sequential_expected(owner) and not RESET_RE.search(content or ""):
            issues.append(_issue("missing_reset_contract", f"Module `{owner.name}` does not reference the reset contract.", normalized))

    if _looks_trivial(content, metrics, owner):
        issues.append(_issue("trivial_rtl", "RTL appears to be a trivial assign-only implementation for a nontrivial block.", normalized))
    issues.extend(_policy_issues(content, normalized, intent))

    accepted = not any(issue.severity == "error" for issue in issues)
    return RTLQualityResult(
        accepted=accepted,
        path=normalized,
        module_name=module_name,
        owner_file=owner.owner_file if owner else None,
        quality_level="implementation" if accepted else "rejected",
        issues=issues,
        metrics=metrics,
    )


def _modules(content: str) -> list[tuple[str, str]]:
    return [(match.group(1), match.group(2)) for match in MODULE_RE.finditer(content or "")]


def _owner(intent: DesignIntent | None, path: str, module_name: str | None) -> ModuleIntent | None:
    if not intent:
        return None
    for module in intent.modules.values():
        if module.owner_file == path:
            return module
    if module_name:
        return intent.modules.get(module_name)
    return None


def _interface_issues(content: str, owner: ModuleIntent, intent: DesignIntent, path: str) -> list[QualityIssue]:
    declared = {match.group(2) for match in PORT_RE.finditer(content or "")}
    issues: list[QualityIssue] = []
    expected_ports = set()
    for interface_name in owner.interfaces:
        interface = intent.interfaces.get(interface_name)
        if not interface:
            continue
        expected_ports.update(port.name for port in interface.ports)
    for required in sorted(expected_ports):
        if required not in declared and required not in {"rst_n"}:
            issues.append(_issue("missing_contract_port", f"Expected port `{required}` from interface contract.", path))
    return issues


def _sequential_expected(owner: ModuleIntent | None) -> bool:
    if not owner:
        return False
    kind = owner.kind.lower()
    return kind not in {"combinational", "pure_comb", "decoder"} and owner.kind != "architecture_diagram"


def _looks_trivial(content: str, metrics: dict[str, Any], owner: ModuleIntent | None) -> bool:
    if not owner or not _sequential_expected(owner):
        return False
    if metrics.get("always_count", 0) == 0 and metrics.get("assign_count", 0) <= 2:
        return True
    compact = re.sub(r"\s+", " ", content or "").lower()
    trivial_patterns = (
        "assign result = instr + 1",
        "assign signal_out = signal_in + 1",
        "assign data_out = addr + data_in",
        "assign result = start ? 32'd12345",
        "ports <= ports + 1",
        "simple incrementer logic",
    )
    return any(pattern in compact for pattern in trivial_patterns)


def _policy_issues(content: str, path: str, intent: DesignIntent | None) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    modules = {name for name, _body in _modules(content)}
    ports = _ports(content)
    internals = _internal_signals(content)
    shadowed = sorted(ports & internals)
    for name in shadowed[:12]:
        issues.append(_issue("port_shadowing", f"Port `{name}` is redeclared as an internal signal.", path))
    issues.extend(_driver_issues(content, ports, path))
    issues.extend(_missing_submodule_issues(content, modules, intent, path))
    issues.extend(_reset_style_issues(content, path))
    issues.extend(_large_inferred_memory_issues(content, intent, path))
    return issues


def _ports(content: str) -> set[str]:
    return {match.group(2) for match in PORT_RE.finditer(content or "")}


def _internal_signals(content: str) -> set[str]:
    names: set[str] = set()
    for match in INTERNAL_DECL_RE.finditer(content or ""):
        for piece in match.group(1).split(","):
            name = re.sub(r"\s*=.*$", "", piece).strip()
            name = re.sub(r"\[[^\]]+\]", "", name).strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
                names.add(name)
    return names


def _driver_issues(content: str, ports: set[str], path: str) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    continuous: dict[str, int] = {}
    procedural: dict[str, int] = {}
    for match in CONT_ASSIGN_RE.finditer(content or ""):
        lhs = _base_signal(match.group(1))
        continuous[lhs] = continuous.get(lhs, 0) + 1
    for block in _always_blocks(content):
        assigned_in_block: set[str] = set()
        for match in PROC_ASSIGN_RE.finditer(block):
            lhs = _base_signal(match.group(1))
            assigned_in_block.add(lhs)
        for lhs in assigned_in_block:
            procedural[lhs] = procedural.get(lhs, 0) + 1
    for signal in sorted(set(continuous) | set(procedural)):
        total = continuous.get(signal, 0) + procedural.get(signal, 0)
        if continuous.get(signal, 0) and procedural.get(signal, 0):
            issues.append(_issue("mixed_continuous_procedural_driver", f"Signal `{signal}` is driven by both assign and always logic.", path))
        elif signal in ports and total > 1:
            issues.append(_issue("multiple_output_drivers", f"Output/top-level signal `{signal}` has {total} driver sites.", path))
    return issues


def _always_blocks(content: str) -> list[str]:
    starts = [match.start() for match in re.finditer(r"(?m)^\s*always(?:_[a-z]+)?\b", content or "")]
    blocks: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(content or "")
        blocks.append((content or "")[start:end])
    return blocks


def _base_signal(value: str) -> str:
    return re.sub(r"\[.*$", "", value or "").strip()


def _missing_submodule_issues(content: str, modules: set[str], intent: DesignIntent | None, path: str) -> list[QualityIssue]:
    allowed_external = _allowed_external_modules(intent)
    issues: list[QualityIssue] = []
    for match in INSTANCE_RE.finditer(content or ""):
        module_type, instance_name = match.group(1), match.group(2)
        if module_type in SYSTEM_MODULES or module_type in modules:
            continue
        if module_type in {"module", "if", "for", "while", "case", "assign", "always"}:
            continue
        if instance_name in {"begin", "end"}:
            continue
        if module_type in allowed_external:
            continue
        severity = "warning" if _looks_like_foundry_cell(module_type) else "error"
        issues.append(_issue(
            "missing_submodule_definition",
            f"Instance `{instance_name}` references `{module_type}`, but no local definition or approved macro binding is present.",
            path,
            severity,
        ))
    return issues[:16]


def _allowed_external_modules(intent: DesignIntent | None) -> set[str]:
    allowed: set[str] = set()
    if not intent:
        return allowed
    for binding in getattr(intent, "capability_bindings", []) or []:
        try:
            name = getattr(binding, "capability_name", "") or ""
            if name:
                allowed.add(name)
        except Exception:
            continue
    return allowed


def _looks_like_foundry_cell(module_type: str) -> bool:
    lowered = module_type.lower()
    return lowered.startswith(("sky130_", "gf180", "tsmc", "saed", "nangate", "asap7")) or "__" in lowered


def _reset_style_issues(content: str, path: str) -> list[QualityIssue]:
    has_async = False
    has_sync = False
    for block in _always_blocks(content):
        sensitivity = re.search(r"(?is)always\s*@\s*\(([^)]*)\)", block)
        if not sensitivity:
            continue
        sensitivity_text = sensitivity.group(1).lower()
        has_reset_check = bool(re.search(r"(?i)\bif\s*\(\s*!?\s*(?:rst|reset)[A-Za-z0-9_$]*\s*\)", block))
        if re.search(r"(?i)(?:posedge|negedge)\s+(?:rst|reset)[A-Za-z0-9_$]*", sensitivity_text):
            has_async = True
        elif has_reset_check:
            has_sync = True
    if has_async and has_sync:
        return [_issue("mixed_reset_styles", "RTL mixes asynchronous-reset and synchronous-reset always blocks in one file.", path)]
    return []


def _large_inferred_memory_issues(content: str, intent: DesignIntent | None, path: str) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    has_macro_binding = bool(getattr(intent, "capability_bindings", None))
    for width_range, name, depth_range in MEMORY_DECL_RE.findall(content or ""):
        width = _range_width(width_range)
        depth = _range_width(depth_range)
        bits = width * depth
        if bits > 8192 and not has_macro_binding:
            issues.append(_issue(
                "large_inferred_memory_without_macro",
                f"Memory `{name}` infers about {bits} bits of flops/registers; bind to a PDK/compiler macro or justify small-memory inference.",
                path,
            ))
    return issues


def _range_width(value: str) -> int:
    match = re.match(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", value or "")
    if not match:
        return 1
    return abs(int(match.group(1)) - int(match.group(2))) + 1


def _issue(code: str, message: str, path: str, severity: Literal["error", "warning"] = "error") -> QualityIssue:
    return QualityIssue(code=code, message=message, path=path, severity=severity)
