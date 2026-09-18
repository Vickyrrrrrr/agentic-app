from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentic_server.design_intent import DesignIntent, ModuleIntent


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

# Non-synthesizable constructs in design modules (allowed only in testbenches).
NON_SYNTH_RE = re.compile(r"(?m)^\s*(initial\b|#[0-9]|\$(?:display|finish|dumpfile|dumpvars|random|fopen|fwrite|fclose|readmem|writemem|stop)\b)")

# Behavioral memory stub markers — comments that admit the memory is a placeholder.
BEHAVIORAL_STUB_RE = re.compile(
    r"(?i)behavioral\s+(?:register\s+)?array|for\s+simulat\w+.*(?:reg|mem|memory)|"
    r"(?:reg|mem|memory).*stub|stub.*(?:reg|mem|memory)|not\s+a\s+real\s+sram|placeholder.*mem"
)


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

    # PDK/foundry-provided IP models are behavioral simulation models, not user-written RTL.
    # Skip ALL quality gates for them — the agent doesn't write these files.
    if _is_pdk_ip_path(normalized):
        return RTLQualityResult(
            accepted=True,
            path=normalized,
            quality_level="implementation",
            issues=[],
            metrics={"bytes": len(content or ""), "skipped": "pdk_ip"},
        )

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
        issues.append(_issue("multiple_modules_in_file", "Each Verilog file must contain exactly ONE module (filename = module name). Split into separate files.", normalized))

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
    issues.extend(_non_synthesizable_in_design_issues(content, path))
    issues.extend(_top_module_logic_issues(content, path))
    issues.extend(_inferred_latch_issues(content, path))
    issues.extend(_blocking_in_sequential_issues(content, path))
    issues.extend(_missing_reset_issues(content, path))
    issues.extend(_case_without_default_issues(content, path))
    issues.extend(_uninitialized_reg_issues(content, path))
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
        if module_type in {
            "module", "if", "for", "while", "case", "assign", "always", "begin", "end",
            "endcase", "endmodule", "else", "default", "posedge", "negedge", "integer",
            "wire", "reg", "logic", "input", "output", "inout", "parameter", "localparam",
            "generate", "endgenerate", "genvar", "function", "endfunction", "task", "endtask",
            "wait", "repeat", "forever", "disable", "force", "release", "deassign", "return",
            "break", "continue", "typedef", "enum", "struct", "union", "package", "endpackage",
            "import", "export", "class", "endclass", "covergroup", "endgroup", "property",
            "endproperty", "sequence", "endsequence", "clocking", "endclocking", "modport",
            "alias", "restrict", "assume", "assert", "cover", "cross", "wildcard",
        }:
            continue
        if instance_name in {"begin", "end", "if", "else", "for", "while", "case", "default"}:
            continue
        if module_type in allowed_external:
            continue
        if _looks_like_foundry_cell(module_type):
            severity = "warning"
        elif intent is None:
            severity = "warning"
        else:
            severity = "error"
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
    return _memory_implementation_issues(content, intent, path)


def _memory_implementation_issues(content: str, intent: DesignIntent | None, path: str) -> list[QualityIssue]:
    """Reject behavioral register-array memories in design files.

    In ASIC flows, `reg [W-1:0] mem [0:D-1]` infers D×W flip-flops instead of a
    single SRAM macro — catastrophic for area/power. Force the agent to query the
    PDK and instantiate a real macro (or explicitly request one via NEEDS_INPUT).
    """
    issues: list[QualityIssue] = []
    if _is_testbench_path(path) or _is_pdk_ip_path(path):
        return issues
    has_macro_binding = bool(getattr(intent, "capability_bindings", None))
    if has_macro_binding:
        return issues
    behavioral_stub = bool(BEHAVIORAL_STUB_RE.search(content or ""))
    for width_range, name, depth_range in MEMORY_DECL_RE.findall(content or ""):
        width = _range_width(width_range) if width_range else 1
        depth = _range_width(depth_range) if depth_range else 1
        bits = width * depth
        if bits > 1024 or behavioral_stub:
            issues.append(_issue(
                "behavioral_memory_in_design",
                f"Memory `{name}` ({bits} bits) is a behavioral register array, not a real SRAM macro. "
                f"For ASIC, query `query_pdk(find_memory, cell_type=\"sram\")` and instantiate a foundry/compiler "
                f"SRAM macro with synchronous read. If none is available, use NEEDS_INPUT to propose OpenRAM "
                f"generation. Do NOT use `reg ... mem[...]` for memories > 1024 bits in design modules.",
                path,
            ))
            break
    return issues


def _is_testbench_path(path: str) -> bool:
    lower = str(path or "").lower()
    wrapped = f"/{lower}"
    return (
        any(section in wrapped for section in ("/tb/", "/dv/", "/verification/", "/formal/", "/testbench/", "/sim/"))
        or lower.startswith("tb_")
        or "_tb." in lower
    )


def _is_pdk_ip_path(path: str) -> bool:
    """Exempt PDK/foundry-provided IP models from design-only quality gates.

    These are behavioral simulation models (with initial/$system tasks) provided
    by the foundry — not user-written synthesizable RTL.
    """
    lower = str(path or "").lower()
    filename = lower.rsplit("/", 1)[-1]
    if any(filename.startswith(prefix) for prefix in ("sky130_", "gf180", "asap7", "saed", "nangate", "tsmc", "gf180mcu")):
        return True
    return any(section in f"/{lower}" for section in ("/ip/", "/lib/", "/libs.ref/", "/libs.tech/"))


def _non_synthesizable_in_design_issues(content: str, path: str) -> list[QualityIssue]:
    """Reject non-synthesizable constructs (initial, #delay, $system tasks) in design modules."""
    issues: list[QualityIssue] = []
    if _is_testbench_path(path) or _is_pdk_ip_path(path):
        return issues
    lines = (content or "").splitlines()
    for index, line in enumerate(lines, start=1):
        stripped = re.sub(r"//.*$", "", line).strip()
        if not stripped:
            continue
        match = NON_SYNTH_RE.match(line)
        if not match:
            continue
        construct = match.group(1).strip()
        issues.append(_issue(
            "non_synthesizable_in_design",
            f"Non-synthesizable construct `{construct}` (line {index}) in a design module. "
            f"`initial`, `#delay`, and `$`-system tasks are only allowed in testbenches. "
            f"For design modules, use synchronous reset and continuous/procedural assignments only.",
            path,
        ))
        break
    return issues


def _top_module_logic_issues(content: str, path: str) -> list[QualityIssue]:
    """Reject logic (always blocks, non-trivial assigns) in top-level structural modules.

    A top module (chip_top, *_top.v) must be purely structural — instantiations and wiring only.
    Logic at the top means the hierarchy is wrong; move it into a leaf module.
    """
    issues: list[QualityIssue] = []
    lower_path = str(path or "").lower()
    is_top = (
        lower_path.endswith("_top.v")
        or lower_path.endswith("_top.sv")
        or "chip_top" in lower_path
        or "/top/" in f"/{lower_path}"
    )
    if not is_top:
        return issues
    always_count = len(ALWAYS_RE.findall(content or ""))
    # Count non-trivial assigns (exclude simple signal renaming: assign a = b;)
    non_trivial_assigns = 0
    for match in CONT_ASSIGN_RE.finditer(content or ""):
        rhs_start = match.end()
        rhs = (content or "")[rhs_start:rhs_start + 80].strip().rstrip(";")
        # Trivial if it's just a signal name (renaming/buffering)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*(\[[^\]]*\])?$", rhs):
            non_trivial_assigns += 1
    if always_count > 0 or non_trivial_assigns > 2:
        issues.append(_issue(
            "logic_in_top_module",
            f"Top-level module contains {always_count} always block(s) and {non_trivial_assigns} non-trivial assign(s). "
            f"The top module must be STRUCTURAL ONLY (instantiations + wiring). Move all logic into leaf modules. "
            f"Only simple signal renaming assigns are allowed at the top level.",
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


# ---------------------------------------------------------------------------
# Fabrication-ready quality gates
# ---------------------------------------------------------------------------

# Regex for combinational always blocks (potential latch sources)
COMB_ALWAYS_RE = re.compile(r"(?m)^\s*always\s*@\s*\(\s*\*\s*\)|always_comb\b")
IF_WITHOUT_ELSE_RE = re.compile(r"(?ms)(\bif\s*\([^)]+\)\s*(?:begin\b)?(?(1)(?:.*?end|.*?)|(?:[^\n]*?)))(?=\s*(?:else|end|if|case|always|assign|$))", re.IGNORECASE)
CASE_WITHOUT_DEFAULT_RE = re.compile(r"(?ms)\bcase\s*\([^)]+\)\s*(?:.*?)(?:endcase\b)", re.IGNORECASE)
SEQUENTIAL_ALWAYS_RE = re.compile(r"(?mis)^\s*always(?:_ff)?\s*@\s*\(\s*(?:posedge|negedge)\s+clk", re.IGNORECASE)
BLOCKING_IN_SEQ_RE = re.compile(r"(?m)^\s*[A-Za-z_]\w*\s*(?:\[[^\]]+\])?\s*=\s*[^=]")
CASE_DEFAULT_RE = re.compile(r"(?mi)^\s*default\s*:")
REG_DECL_RE = re.compile(r"(?m)^\s*(?:reg|logic)\s+(?:\[[^\]]+\]\s*)?(\w+)")
RESET_BRANCH_RE = re.compile(r"(?i)\bif\s*\(\s*!?\s*(?:rst|reset)[A-Za-z0-9_$]*\s*\)")


def _inferred_latch_issues(content: str, path: str) -> list[QualityIssue]:
    """Detect potential inferred latches: if without else in combinational blocks."""
    if _is_testbench_path(path) or _is_pdk_ip_path(path):
        return []
    issues: list[QualityIssue] = []
    # Find combinational always blocks
    for match in COMB_ALWAYS_RE.finditer(content or ""):
        # Get the block content (from always to the next always/endmodule)
        start = match.start()
        end_match = re.search(r"(?m)^\s*(?:always|endmodule|assign|function|task)\b", content[start + 1:])
        block = content[start:end_match.start() + start + 1] if end_match else content[start:start + 2000]
        # Check for if without else
        if_lines = re.findall(r"(?m)^\s*if\s*\(", block)
        else_lines = re.findall(r"(?m)^\s*else\b", block)
        if len(if_lines) > len(else_lines):
            line_num = content[:start].count("\n") + 1
            issues.append(_issue(
                "inferred_latch",
                f"Combinational always block at line {line_num} has if without else — potential inferred latch. "
                f"Every if must have an else in combinational logic to prevent latches.",
                path,
            ))
            break  # one per file is enough
    return issues


def _blocking_in_sequential_issues(content: str, path: str) -> list[QualityIssue]:
    """Detect blocking assignments in sequential always blocks — fabrication blocker."""
    if _is_testbench_path(path) or _is_pdk_ip_path(path):
        return []
    issues: list[QualityIssue] = []
    for match in SEQUENTIAL_ALWAYS_RE.finditer(content or ""):
        start = match.start()
        end_match = re.search(r"(?m)^\s*(?:always|endmodule|assign|function|task)\b", content[start + 1:])
        block = content[start:end_match.start() + start + 1] if end_match else content[start:start + 2000]
        # Find blocking assignments (signal = value, but not <= or == or != or <=)
        for line_num, line in enumerate(block.splitlines(), 1):
            stripped = re.sub(r"//.*$", "", line).strip()
            if not stripped or stripped.startswith("//"):
                continue
            # Skip if it's actually non-blocking (<=) or comparison (==, !=, <=, >=)
            if "<=" in stripped or "==" in stripped or "!=" in stripped or ">=" in stripped:
                continue
            # Check for blocking assignment: signal = value
            if re.match(r"^[A-Za-z_]\w*\s*(?:\[[^\]]+\])?\s*=\s*[^=]", stripped):
                abs_line = content[:start].count("\n") + line_num
                issues.append(_issue(
                    "blocking_in_sequential",
                    f"Blocking assignment (`=`) at line {abs_line} in a sequential always block. "
                    f"Use non-blocking (`<=`) in sequential logic to prevent race conditions.",
                    path,
                ))
                break  # one per block
    return issues[:5]  # cap at 5


def _missing_reset_issues(content: str, path: str) -> list[QualityIssue]:
    """Detect sequential always blocks without reset — every register must be resettable."""
    if _is_testbench_path(path) or _is_pdk_ip_path(path):
        return []
    issues: list[QualityIssue] = []
    has_reset = bool(RESET_RE.search(content or ""))
    for match in SEQUENTIAL_ALWAYS_RE.finditer(content or ""):
        start = match.start()
        end_match = re.search(r"(?m)^\s*(?:always|endmodule|assign|function|task)\b", content[start + 1:])
        block = content[start:end_match.start() + start + 1] if end_match else content[start:start + 2000]
        # Check if the block has a reset branch
        if not RESET_BRANCH_RE.search(block) and has_reset:
            line_num = content[:start].count("\n") + 1
            issues.append(_issue(
                "missing_reset_in_sequential",
                f"Sequential always block at line {line_num} has no reset branch. "
                f"Every register must be reset: add `if (!rst_n)` branch.",
                path,
                "warning",
            ))
            break
    return issues


def _case_without_default_issues(content: str, path: str) -> list[QualityIssue]:
    """Detect case statements without default — inferred latch risk."""
    if _is_testbench_path(path) or _is_pdk_ip_path(path):
        return []
    issues: list[QualityIssue] = []
    for match in CASE_WITHOUT_DEFAULT_RE.finditer(content or ""):
        block = match.group(0)
        if not CASE_DEFAULT_RE.search(block):
            line_num = content[:match.start()].count("\n") + 1
            issues.append(_issue(
                "case_without_default",
                f"case statement at line {line_num} has no default branch. "
                f"Every case must have a default to prevent inferred latches.",
                path,
                "warning",
            ))
            break
    return issues


def _uninitialized_reg_issues(content: str, path: str) -> list[QualityIssue]:
    """Detect reg declarations in sequential blocks that are never assigned in reset."""
    if _is_testbench_path(path) or _is_pdk_ip_path(path):
        return []
    issues: list[QualityIssue] = []
    # Find all reg/logic declared at module level
    regs = set()
    for match in REG_DECL_RE.finditer(content or ""):
        name = match.group(1)
        if name and not name.startswith("clk") and not name.startswith("rst"):
            regs.add(name)
    # Find regs assigned in reset branches
    for match in SEQUENTIAL_ALWAYS_RE.finditer(content or ""):
        start = match.start()
        end_match = re.search(r"(?m)^\s*(?:always|endmodule|assign|function|task)\b", content[start + 1:])
        block = content[start:end_match.start() + start + 1] if end_match else content[start:start + 2000]
        # Find reset branch
        reset_match = RESET_BRANCH_RE.search(block)
        if reset_match:
            reset_block = block[reset_match.start():]
            # Remove regs that ARE assigned in reset
            for reg in list(regs):
                if re.search(rf"\b{re.escape(reg)}\s*(?:\[[^\]]+\])?\s*<=", reset_block):
                    regs.discard(reg)
    # Remaining regs are potentially uninitialized in reset
    if regs:
        issues.append(_issue(
            "uninitialized_register",
            f"Registers may be uninitialized in reset: {', '.join(sorted(list(regs)[:5]))}. "
            f"Every register must have a reset value.",
            path,
            "warning",
        ))
    return issues
