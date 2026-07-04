import os
import re
import urllib.request
import urllib.parse
import glob as glob_mod
import json
import hashlib
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser

from local_tools import run_bash, run_bash_stream
from artifact_kernel import evaluate_artifact_write
from design_intent import evaluate_write_against_intent, intent_from_state_or_manifest
from pdk_index import build_pdk_index, select_pdk
from rtl_quality import evaluate_rtl_quality
from vlsi_state import DesignStateStore
from vlsi_capability_graph import assess_design_readiness, bind_memory_requirement, compact_capability_graph


class _DDGResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._capture = False
        self._link = ""
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "a" and attrs_dict.get("class") == "result-link":
            self._capture = True
            self._link = attrs_dict.get("href", "")
    def handle_data(self, data):
        if self._capture:
            text = data.strip()
            if text:
                self.results.append(f"{text} — {self._link}")
            self._capture = False


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _looks_sensitive_web_query(query: str) -> bool:
    """Conservative guard against leaking local/proprietary details to web search."""
    lowered = (query or "").lower()
    sensitive_markers = (
        "/home/",
        "/users/",
        "/mnt/",
        "c:\\",
        "\\\\",
        "agentic-workspace",
        "pdk_root",
        "pdkpath",
        "pdk_home",
        "lm_license_file",
        "cds_lic_file",
        "snpslmd_license_file",
        "mgls_license_file",
        "license.dat",
    )
    if any(marker in lowered for marker in sensitive_markers):
        return True
    if re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", query or "", re.IGNORECASE):
        return True
    if re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", query or ""):
        return True
    if re.search(r"(?i)\b(?:token|api[_-]?key|secret|password)\s*[:=]", query or ""):
        return True
    if re.search(r"(?i)(?:^|\s)[./~][^\s]*(?:\.lib|\.lef|\.def|\.gds|\.tcl|\.sdc|\.v|\.sv|\.log|\.rpt)\b", query or ""):
        return True
    return False


ALLOWED_READ_EXTENSIONS = {
    ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".tcl", ".sdc", ".sby",
    ".cfg", ".json", ".md", ".txt", ".log", ".rpt", ".lib", ".lef", ".def",
    ".gds", ".tcl", ".py", ".c", ".cpp", ".h", ".sh", ".makefile", ".mk",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sdf", ".spi", ".lvs",
    ".drce", ".mag", ".magic", ".tcl", ".mermaid", ".mmd",
}


TEXT_WRITE_EXTENSIONS = {
    ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".tcl", ".sdc", ".sby",
    ".ys", ".cfg", ".json", ".md", ".txt", ".log", ".rpt", ".lib", ".lef",
    ".def", ".py", ".c", ".cpp", ".h", ".sh", ".mk", ".makefile", ".yml",
    ".yaml", ".toml", ".ini", ".csv", ".sp", ".spi", ".lvs", ".mermaid", ".mmd",
}

SURGICAL_REWRITE_LIMIT = 12000


def _safe_workspace_path(path: str, workspace_root: str) -> str | None:
    root = os.path.abspath(os.path.normpath(workspace_root))
    full = os.path.abspath(os.path.normpath(os.path.join(root, path)))
    try:
        if os.path.commonpath([root, full]) == root:
            if os.path.exists(full) or not os.path.exists(os.path.join(os.path.dirname(root), path)):
                return full
        
        # Fallback to parent workspace directory (one level up) to support general workspace files
        parent = os.path.dirname(root)
        full_parent = os.path.abspath(os.path.normpath(os.path.join(parent, path)))
        if os.path.commonpath([parent, full_parent]) == parent:
            return full_parent
    except ValueError:
        pass
    return None


def _is_text_source_path(path: str) -> bool:
    lower = path.lower()
    suffix = os.path.splitext(lower)[1]
    return suffix in TEXT_WRITE_EXTENSIONS or lower.endswith(("makefile", "dockerfile"))


def _normalize_text_content(path: str, content: str) -> str:
    if not _is_text_source_path(path):
        return content
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    normalized = "\n".join(lines).rstrip("\n") + "\n"
    return normalized


SKIPPED_DIRS = {"obj_dir", "build", "__pycache__", ".git", "node_modules"}
SKIPPED_EXTS = {".o", ".obj", ".exe", ".bin", ".so", ".dll", ".pyc", ".ko"}


def read_file(path: str, workspace_root: str) -> str:
    full = _safe_workspace_path(path, workspace_root)
    if not full:
        return f"Error: path '{path}' is outside workspace"
    if not os.path.isfile(full):
        return f"Error: file not found: {path}"
    parts = path.replace("\\", "/").split("/")
    for d in SKIPPED_DIRS:
        if d in parts:
            return f"Skipped: generated build artifact under {d}/ (use grep/offset if you need specific info)"
    ext = os.path.splitext(path)[1].lower()
    if ext in SKIPPED_EXTS:
        return f"Skipped: binary/object file ({ext})"
    if os.path.getsize(full) > 5 * 1024 * 1024:
        return f"Error: file is larger than 5MB"
    try:
        with open(full, "r", errors="replace") as f:
            content = f.read()
        return content
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(path: str, content: str, workspace_root: str) -> str:
    full = _safe_workspace_path(path, workspace_root)
    if not full:
        return f"Error: path '{path}' is outside workspace"
    os.makedirs(os.path.dirname(full), exist_ok=True)
    try:
        content = _normalize_text_content(path, content)
        with open(full, "w") as f:
            f.write(content)
        return f"File written: {path} ({len(content)} bytes)"
    except Exception as e:
        return f"Error writing file: {e}"


def edit_file(path: str, old_string: str, new_string: str, workspace_root: str) -> str:
    full = _safe_workspace_path(path, workspace_root)
    if not full:
        return f"Error: path '{path}' is outside workspace"
    if not os.path.isfile(full):
        return f"Error: file not found: {path}"
    try:
        with open(full, "r", errors="replace") as f:
            content = f.read()
        if old_string not in content:
            return f"Error: old_string not found in {path}"
        count = content.count(old_string)
        if count > 1:
            return f"Error: found {count} matches. Provide more context."
        new_content = _normalize_text_content(path, content.replace(old_string, new_string, 1))
        with open(full, "w") as f:
            f.write(new_content)
        return f"File edited: {path}"
    except Exception as e:
        return f"Error editing file: {e}"


def grep_tool(pattern: str, path: str, workspace_root: str) -> str:
    full = _safe_workspace_path(path, workspace_root)
    if not full:
        return f"Error: path '{path}' is outside workspace"
    if not os.path.exists(full):
        return f"Error: path not found: {path}"
    try:
        matches = []
        for root, dirs, files in os.walk(full):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    with open(fp, "r", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if re.search(pattern, line):
                                rel = os.path.relpath(fp, workspace_root)
                                matches.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                except Exception:
                    pass
        if not matches:
            return f"No matches for pattern: {pattern}"
        return "\n".join(matches[:200])
    except Exception as e:
        return f"Error searching: {e}"


def glob_tool(pattern: str, workspace_root: str) -> str:
    if any(part == ".." for part in pattern.replace("\\", "/").split("/")):
        return f"Error: pattern '{pattern}' is outside workspace"
    full = os.path.join(workspace_root, pattern)
    root = os.path.abspath(os.path.normpath(workspace_root))
    try:
        results = []
        for result in glob_mod.glob(full, recursive=True):
            normalized = os.path.abspath(os.path.normpath(result))
            try:
                if os.path.commonpath([root, normalized]) == root:
                    results.append(normalized)
            except ValueError:
                continue
        if not results:
            return f"No files matching: {pattern}"
        rels = [os.path.relpath(p, workspace_root) for p in sorted(results)]
        return "\n".join(rels)
    except Exception as e:
        return f"Error globbing: {e}"


def workspace_tool(action: str, workspace_root: str, path: str = ".", pattern: str = "", module: str = "") -> str:
    if action == "read":
        return read_file(path, workspace_root)
    elif action == "search":
        return grep_tool(pattern, path, workspace_root)
    elif action == "list":
        return glob_tool(pattern, workspace_root)
    elif action == "lint":
        return lint_file_tool(path, workspace_root)
    elif action == "parse_module":
        return parse_module_tool(path, workspace_root, module)
    elif action == "parse_log":
        return parse_log_tool(path, workspace_root)
    elif action == "layout_inspect":
        return layout_inspect_tool(path, workspace_root)
    elif action == "timing_analysis":
        return timing_analysis_tool(path, workspace_root)
    elif action == "schematic_json":
        return schematic_json_tool(path, workspace_root, module)
    else:
        return f"Error: unknown action '{action}' for workspace tool. Use read, search, list, lint, parse_module, parse_log, layout_inspect, timing_analysis, or schematic_json."


def parse_module_tool(path: str, workspace_root: str) -> str:
    full = _safe_workspace_path(path, workspace_root)
    if not full or not os.path.isfile(full):
        return json.dumps({"error": "File not found"})
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return json.dumps({"error": str(e)})

    # 1. Parse Ports
    ports = []
    # Regex matching input/output/inout wire/reg/logic [width] name
    port_re = re.compile(
        r'(input|output|inout)\s+(wire|reg|logic)?\s*(\[[^\]]+\])?\s*(\w+)',
        re.MULTILINE
    )
    for match in port_re.finditer(content):
        direction, net_type, width, name = match.groups()
        ports.append({
            "name": name,
            "direction": direction,
            "width": width.strip() if width else "1"
        })

    # 2. Parse Instantiations
    instantiations = []
    # Match: module_name instance_name (.port(wire))
    inst_re = re.compile(
        r'(\w+)\s+(\w+)\s*\((?:\s*\.\w+\s*\(\s*\w+\s*\)\s*,?)*\s*\);',
        re.MULTILINE
    )
    for match in inst_re.finditer(content):
        mod_type, inst_name = match.groups()
        if mod_type not in ("module", "input", "output", "wire", "reg", "assign", "always", "initial", "generate", "endgenerate"):
            instantiations.append({
                "module": mod_type,
                "instance": inst_name
            })

    # 3. Parse Register Map (address offsets)
    registers = []
    # Search for case/if assignments mapping addresses to registers (e.g. 6'h0: reg_ctrl <= ...)
    addr_map_re = re.compile(
        r"(\d+)'h([0-9a-fA-F]+)\s*:\s*(\w+)\s*<=",
        re.MULTILINE
    )
    seen_regs = set()
    for match in addr_map_re.finditer(content):
        bit_width, hex_val, reg_name = match.groups()
        offset = int(hex_val, 16) * 4 # 32-bit word alignment
        if reg_name not in seen_regs:
            seen_regs.add(reg_name)
            registers.append({
                "name": reg_name,
                "address": f"0x{offset:02X}",
                "access": "Read/Write",
                "width": "32 bits"
            })
            
    # Fallback to general registers if address decode cases aren't found
    if not registers:
        reg_decl_re = re.compile(
            r'reg\s*(?:\[([^\]]+)\])?\s*(\w+);',
            re.MULTILINE
        )
        idx = 0
        for match in reg_decl_re.finditer(content):
            width, name = match.groups()
            if "clk" not in name and "rst" not in name and name not in seen_regs:
                seen_regs.add(name)
                registers.append({
                    "name": name,
                    "address": f"0x{idx*4:02X}",
                    "access": "Read/Write",
                    "width": width.strip() if width else "1 bit"
                })
                idx += 1

    return json.dumps({
        "ports": ports,
        "instantiations": instantiations,
        "registers": registers
    })


def parse_log_tool(path: str, workspace_root: str) -> str:
    """Parse any EDA log file into a structured diagnosis.

    Auto-detects the tool (Yosys, OpenROAD, Magic, KLayout, iverilog, verilator, etc.)
    and extracts: pass/fail, cell count, warnings, errors, file references.
    Returns a JSON string with the structured diagnosis.
    """
    full = _safe_workspace_path(path, workspace_root)
    if not full or not os.path.isfile(full):
        return json.dumps({"error": f"Log file not found: {path}"})

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(2 * 1024 * 1024)  # Read up to 2MB
    except Exception as e:
        return json.dumps({"error": str(e)})

    # Detect tool from the first ~50 lines — supports both OSS and proprietary
    head_lower = "\n".join(text.splitlines()[:50]).lower()
    tool = "generic"
    stage_from_tool = None

    # Proprietary tools (check first — they have distinctive headers)
    if "design compiler" in head_lower or "dc_shell" in head_lower:
        tool, stage_from_tool = "dc_shell", "synthesis"
    elif "genus" in head_lower:
        tool, stage_from_tool = "genus", "synthesis"
    elif "innovus" in head_lower:
        tool, stage_from_tool = "innovus", "synthesis"
    elif "ic compiler" in head_lower or "icc2" in head_lower:
        tool, stage_from_tool = "icc2", "synthesis"
    elif "primetime" in head_lower or "pt_shell" in head_lower:
        tool, stage_from_tool = "pt_shell", "sta"
    elif "tempus" in head_lower:
        tool, stage_from_tool = "tempus", "sta"
    elif "calibre" in head_lower:
        tool, stage_from_tool = "calibre", "drc"
    elif "vcs" in head_lower and "synopsys" in head_lower:
        tool, stage_from_tool = "vcs", "lint"
    elif "xcelium" in head_lower or "xrun" in head_lower or "xmvlog" in head_lower:
        tool, stage_from_tool = "xcelium", "lint"
    elif "questa" in head_lower or "vsim" in head_lower:
        tool, stage_from_tool = "questa", "lint"
    # OSS tools
    elif "yosys" in head_lower:
        tool, stage_from_tool = "yosys", "synthesis"
    elif "openroad" in head_lower:
        tool, stage_from_tool = "openroad", "synthesis"
    elif "opensta" in head_lower or "sta " in head_lower:
        tool, stage_from_tool = "opensta", "sta"
    elif "magic" in head_lower:
        tool, stage_from_tool = "magic", "drc"
    elif "klayout" in head_lower:
        tool, stage_from_tool = "klayout", "drc"
    elif "iverilog" in head_lower:
        tool, stage_from_tool = "iverilog", "lint"
    elif "verilator" in head_lower:
        tool, stage_from_tool = "verilator", "lint"
    elif "netgen" in head_lower:
        tool, stage_from_tool = "netgen", "lvs"

    # Stage: prefer tool-based detection, fall back to filename, then default
    if stage_from_tool:
        stage = stage_from_tool
    else:
        stage = "synthesis"
        lower_path = path.lower()
        if "sta" in lower_path or "timing" in lower_path:
            stage = "sta"
        elif "drc" in lower_path:
            stage = "drc"
        elif "lvs" in lower_path:
            stage = "lvs"
        elif "lint" in lower_path:
            stage = "lint"
        elif "synth" in lower_path:
            stage = "synthesis"
        elif "sim" in lower_path or "test" in lower_path:
            stage = "lint"

    from report_parsers import parse_report
    parsed = parse_report(stage=stage, tool=tool, text=text)

    result = {
        "path": path,
        "tool": tool,
        "stage": stage,
        "line_count": len(text.splitlines()),
        "summary": parsed.get("summary", {}),
        "metrics": parsed.get("metrics", {}),
        "diagnostics": parsed.get("diagnostics", [])[:50],
        "compact_for_agent": _compact_log_summary(path, tool, stage, parsed),
    }
    return json.dumps(result, indent=2)


def _compact_log_summary(path: str, tool: str, stage: str, parsed: dict) -> str:
    """Generate a 2-3 line compact summary suitable for sending to the LLM."""
    summary = parsed.get("summary", {})
    metrics = parsed.get("metrics", {})
    error_count = summary.get("error_count", 0)
    warn_count = summary.get("warning_count", 0)
    cell_count = metrics.get("cell_count")
    wns = metrics.get("wns_ns")

    parts = [f"{tool} {stage}: {path}"]
    if cell_count is not None:
        parts.append(f"{cell_count} cells")
    area = metrics.get("area_um2")
    if area is not None:
        parts.append(f"area={area}")
    if wns is not None:
        parts.append(f"WNS={wns}ns")
    parts.append(f"{warn_count} warnings, {error_count} errors")
    if error_count == 0 and warn_count == 0:
        parts.append("CLEAN")
    elif error_count > 0:
        parts.append("FAILED")
    else:
        parts.append("PASSED with warnings")

    # Add first 3 error/warning messages
    diags = parsed.get("diagnostics", [])[:3]
    for d in diags:
        msg = d.get("message", "")[:100]
        parts.append(f"  - [{d.get('severity', '?')}] {msg}")

    return "\n".join(parts)


def timing_analysis_tool(path: str, workspace_root: str) -> str:
    """Specialized STA report parser - extracts critical paths, not raw log.

    Parses STA summary/max/min reports and returns:
    - WNS/TNS (worst/total negative slack)
    - Top violating paths with startpoint, endpoint, slack, path group
    - Which RTL modules are on the critical path (from instance names)
    - Suggested fixes based on the violation type

    This is the timing closure tool - the agent gets structured data,
    not a 5000-line raw STA log.
    """
    full = _safe_workspace_path(path, workspace_root)
    if not full or not os.path.isfile(full):
        return json.dumps({"error": f"STA report not found: {path}"})

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(2 * 1024 * 1024)  # 2MB limit
    except Exception as e:
        return json.dumps({"error": str(e)})

    from report_parsers import parse_sta_report
    parsed = parse_sta_report(text, tool=_sta_tool_hint(path, text))

    metrics = parsed.get("metrics", {})
    diagnostics = parsed.get("diagnostics", [])
    wns = metrics.get("wns_ns")
    tns = metrics.get("tns_ns")

    # Extract top 10 violating paths
    violating_paths = []
    for d in diagnostics[:10]:
        if d.get("severity") == "error" or (d.get("slack_ns") is not None and d["slack_ns"] < 0):
            startpoint = d.get("startpoint", "")
            endpoint = d.get("endpoint", "")
            slack = d.get("slack_ns")
            path_group = d.get("path_group", "")

            # Extract module name from instance path (e.g., "soc_top/u_alu/result_reg[7]" -> "alu")
            module_hint = _extract_module_from_path(startpoint) or _extract_module_from_path(endpoint)

            violating_paths.append({
                "startpoint": startpoint,
                "endpoint": endpoint,
                "slack_ns": slack,
                "path_group": path_group,
                "module_hint": module_hint,
            })

    # Generate fix suggestions based on violation characteristics
    suggestions = []
    if wns is not None and wns < 0:
        suggestions.append(f"WNS = {wns}ns. Need to reduce path delay by {abs(wns)}ns.")
        if abs(wns) > 5:
            suggestions.append("Large violation - consider architectural changes: pipelining, parallelism, or retiming.")
        elif abs(wns) > 1:
            suggestions.append("Medium violation - try gate sizing, buffering, or logic restructuring.")
        else:
            suggestions.append("Small violation - try wire sizing, buffer insertion, or minor logic changes.")

    for p in violating_paths[:3]:
        if p["module_hint"]:
            suggestions.append(f"Critical path through {p['module_hint']} module (slack={p['slack_ns']}ns). Review RTL in that module for pipeline opportunities.")

    # Compact summary for the agent
    compact_parts = [f"STA: {path}"]
    if wns is not None:
        compact_parts.append(f"WNS={wns}ns TNS={tns}ns")
    compact_parts.append(f"{len(violating_paths)} violating paths")
    if violating_paths:
        p = violating_paths[0]
        compact_parts.append(f"Worst: {p['startpoint']} -> {p['endpoint']} ({p['slack_ns']}ns)")
        if p["module_hint"]:
            compact_parts.append(f"Module: {p['module_hint']}")
    for s in suggestions[:3]:
        compact_parts.append(f"Fix: {s}")

    result = {
        "path": path,
        "wns_ns": wns,
        "tns_ns": tns,
        "violating_path_count": len(violating_paths),
        "violating_paths": violating_paths,
        "suggestions": suggestions,
        "compact_for_agent": "\n".join(compact_parts),
    }
    return json.dumps(result, indent=2)


def _sta_tool_hint(path: str, text: str) -> str:
    lower = (text[:500] or "").lower()
    if "primetime" in lower or "pt_shell" in lower:
        return "pt_shell"
    if "tempus" in lower:
        return "tempus"
    if "opensta" in lower:
        return "opensta"
    return "generic"


def _extract_module_from_path(instance_path: str) -> str:
    """Extract a module hint from an instance path.

    e.g., "soc_top/u_alu/result_reg[7]" -> "alu"
         "top/inst_datapath/multiplier/pipeline_reg" -> "datapath"
    """
    if not instance_path:
        return ""
    parts = instance_path.replace("\\", "/").split("/")
    # Skip the first part (top module) and look at instance names
    for part in parts[1:4]:
        # Remove common prefixes (u_, inst_, i_) and suffixes (_reg, _inst)
        name = part
        for prefix in ("u_", "inst_", "i_", "u"):
            if name.lower().startswith(prefix) and len(name) > len(prefix):
                name = name[len(prefix):]
                break
        for suffix in ("_reg", "_inst", "_u"):
            if name.lower().endswith(suffix):
                name = name[:-len(suffix)]
                break
        # Remove bit-select brackets
        name = name.split("[")[0]
        if name and len(name) > 2:
            return name
    return ""


def layout_inspect_tool(path: str, workspace_root: str) -> str:
    """Inspect a GDS layout - the agent visual inspection API.

    Returns structured data: cell hierarchy, layers, polygon counts, bounding box.
    The agent queries the layout programmatically instead of looking at pixels.

    Returns JSON with top_cell, cell_count, polygon_count, layers, hierarchy,
    and a compact_for_agent summary string.
    """
    full = _safe_workspace_path(path, workspace_root)
    if not full or not os.path.isfile(full):
        return json.dumps({"available": False, "reason": f"File not found: {path}"})

    file_size = os.path.getsize(full)
    if file_size > 500 * 1024 * 1024:  # 500MB limit
        return json.dumps({"available": False, "reason": f"GDS file too large ({file_size // 1024 // 1024}MB). Max 500MB."})

    try:
        with open(full, "rb") as f:
            data = f.read()
    except Exception as e:
        return json.dumps({"available": False, "reason": str(e)})

    # Parse GDS binary - minimal parser inline (no external deps)
    try:
        result = _parse_gds_summary(data)
        return json.dumps(result, indent=2)
    except Exception as e:
        return json.dumps({"available": False, "reason": f"GDS parse error: {e}"})


def _parse_gds_summary(data: bytes) -> dict:
    # Parse GDS binary and return a structured summary for the agent.
    import struct

    view = memoryview(data)
    cells = {}
    top_cell = None
    total_polys = 0
    offset = 0

    current_cell = None
    current_layer = 0
    current_datatype = 0
    current_xy = []
    element_type = None
    sname = ""
    mag = 1.0
    angle = 0.0
    strans = 0
    cols = 0
    rows = 0
    srefs = []
    arefs = []
    layers_seen = set()

    def read_real8(offset):
        # GDS 8-byte real: excess-64 base-16
        first = data[offset]
        if first == 0:
            return 0.0
        sign = -1 if (first & 0x80) else 1
        exp = (first & 0x7f) - 64
        mantissa = 0.0
        for i in range(1, 8):
            b = data[offset + i]
            hi = (b >> 4) & 0x0f
            lo = b & 0x0f
            mantissa += hi * (16 ** (-(2 * i - 1)))
            mantissa += lo * (16 ** (-(2 * i)))
        return sign * mantissa * (16 ** exp)

    while offset + 4 <= len(data):
        rec_len = struct.unpack_from(">H", data, offset)[0]
        tag = struct.unpack_from(">H", data, offset + 2)[0]
        rec_type = (tag >> 8) & 0xff
        data_type = tag & 0xff

        if rec_len == 0 or rec_len < 4 or offset + rec_len > len(data):
            break

        ds = offset + 4
        dl = rec_len - 4

        if rec_type == 0x03:  # UNITS
            if dl >= 16:
                pass  # units not needed for summary
        elif rec_type == 0x05:  # BGNSTR
            current_cell = {"name": "", "polygons": 0, "srefs": 0, "arefs": 0, "layers": set()}
            srefs = []
            arefs = []
        elif rec_type == 0x06:  # STRNAME
            name = data[ds:ds + dl].rstrip(b"\x00").decode("ascii", errors="replace").strip()
            if current_cell:
                current_cell["name"] = name
                if not top_cell:
                    top_cell = name
        elif rec_type == 0x07:  # ENDSTR
            if current_cell:
                current_cell["srefs"] = len(srefs)
                current_cell["arefs"] = len(arefs)
                cells[current_cell["name"]] = current_cell
                for sr in srefs:
                    pass  # track references
                for ar in arefs:
                    pass
            current_cell = None
        elif rec_type == 0x08:  # BOUNDARY
            element_type = "boundary"
            current_xy = []
        elif rec_type == 0x09:  # PATH
            element_type = "path"
            current_xy = []
        elif rec_type == 0x0a:  # SREF
            element_type = "sref"
            sname = ""
            mag = 1.0
            angle = 0.0
        elif rec_type == 0x0b:  # AREF
            element_type = "aref"
            sname = ""
            cols = 0
            rows = 0
        elif rec_type == 0x0d:  # LAYER
            if data_type == 1 and ds + 2 <= len(data):
                current_layer = struct.unpack_from(">h", data, ds)[0]
            elif data_type == 2 and ds + 4 <= len(data):
                current_layer = struct.unpack_from(">i", data, ds)[0]
            layers_seen.add(current_layer)
        elif rec_type == 0x0e:  # DATATYPE
            if data_type == 1 and ds + 2 <= len(data):
                current_datatype = struct.unpack_from(">h", data, ds)[0]
        elif rec_type == 0x12:  # SNAME
            sname = data[ds:ds + dl].rstrip(b"\x00").decode("ascii", errors="replace").strip()
        elif rec_type == 0x17:  # MAG
            mag = read_real8(ds)
        elif rec_type == 0x18:  # ANGLE
            angle = read_real8(ds)
        elif rec_type == 0x13:  # COLROW
            cols = struct.unpack_from(">H", data, ds)[0]
            rows = struct.unpack_from(">H", data, ds + 2)[0]
        elif rec_type == 0x10:  # XY
            current_xy = []
            if data_type == 2:
                for i in range(0, dl, 8):
                    if ds + i + 8 <= len(data):
                        current_xy.append(struct.unpack_from(">i", data, ds + i)[0])
                        current_xy.append(struct.unpack_from(">i", data, ds + i + 4)[0])
        elif rec_type == 0x11:  # ENDEL
            if element_type in ("boundary", "path", "box") and current_cell:
                current_cell["polygons"] += 1
                current_cell["layers"].add(current_layer)
                total_polys += 1
            elif element_type == "sref":
                srefs.append(sname)
            elif element_type == "aref":
                arefs.append(sname)
            element_type = None
        elif rec_type == 0x04:  # ENDLIB
            break

        offset += rec_len

    # Find top cell (not referenced by others)
    referenced = set()
    for cell in cells.values():
        # We didn't store sref names per cell, but we counted them
        pass
    if top_cell and cells:
        unreferenced = [name for name in cells.keys() if name not in referenced]
        if unreferenced:
            top_cell = unreferenced[-1]

    # Build hierarchy (first 50 cells)
    hierarchy = []
    for name, cell in list(cells.items())[:50]:
        hierarchy.append({
            "name": name,
            "polygons": cell["polygons"],
            "srefs": cell["srefs"],
            "arefs": cell["arefs"],
            "layers": sorted(cell["layers"]),
        })

    layers_sorted = sorted(layers_seen)

    # Compact summary for the agent
    biggest = sorted(cells.values(), key=lambda x: x["polygons"], reverse=True)[:5]
    biggest_str = ", ".join(f'{c["name"]}({c["polygons"]}p)' for c in biggest)
    compact = (
        f"GDS layout: {total_polys} polygons, {len(cells)} cells, {len(layers_sorted)} layers. "
        f"Top cell: {top_cell}. Layers: {', '.join(str(l) for l in layers_sorted[:20])}. "
        f"Largest cells: {biggest_str}."
    )

    return {
        "available": True,
        "top_cell": top_cell,
        "cell_count": len(cells),
        "polygon_count": total_polys,
        "layers": layers_sorted,
        "hierarchy": hierarchy,
        "compact_for_agent": compact,
    }

def schematic_json_tool(path: str, workspace_root: str, module: str = "") -> str:
    """Render a real RTL/gate-level schematic by running Yosys write_json.

    Returns JSON with available, module, yosys_json, cells, ports.
    Cached by file content hash under workspace/.agentic/schematic_cache/.
    """
    full = _safe_workspace_path(path, workspace_root)
    if not full or not os.path.isfile(full):
        return json.dumps({"available": False, "reason": f"File not found: {path}"})

    yosys_bin = shutil.which("yosys")
    if not yosys_bin:
        return json.dumps({"available": False, "reason": "yosys not found on PATH"})

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return json.dumps({"available": False, "reason": str(e)})

    # Auto-detect top module from the file if not provided.
    if not module:
        m = re.search(r"module\s+(\w+)\s*[#(]", content)
        if not m:
            return json.dumps({"available": False, "reason": "No module declaration found in file"})
        module = m.group(1)

    cache_dir = os.path.join(workspace_root, ".agentic", "schematic_cache")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        pass
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
    cache_key = f"{module}_{digest}"
    cache_path = os.path.join(cache_dir, f"{cache_key}.json")

    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as cf:
                return cf.read()
        except Exception:
            pass

    # Build include dirs like lint_file_tool does.
    inc_dirs = [workspace_root]
    for sub in ["rtl", "simulation", "verification"]:
        subdir = os.path.join(workspace_root, sub)
        if os.path.isdir(subdir):
            inc_dirs.append(subdir)

    file_dir = os.path.dirname(full)
    verilog_files = [full]
    try:
        for name in os.listdir(file_dir):
            if name.endswith((".v", ".sv")) and os.path.join(file_dir, name) != full:
                verilog_files.append(os.path.join(file_dir, name))
    except Exception:
        pass

    script_parts = [f"read_verilog {' '.join('-I' + d for d in inc_dirs)} {' '.join(verilog_files)}"]
    script_parts.append(f"hierarchy -check -top {module}")
    script_parts.append("prep -top %s" % module)
    script_parts.append(f"write_json {cache_path}")
    yosys_script = "; ".join(script_parts)

    try:
        proc = subprocess.run(
            [yosys_bin, "-q", "-p", yosys_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"available": False, "reason": "yosys timed out (design too large or complex)"})
    except Exception as e:
        return json.dumps({"available": False, "reason": f"yosys failed to run: {e}"})

    if proc.returncode != 0 or not os.path.isfile(cache_path):
        err = (proc.stderr or proc.stdout or "").strip()
        return json.dumps({"available": False, "reason": "yosys synthesis failed", "log": err[-2000:]})

    try:
        with open(cache_path, "r", encoding="utf-8") as cf:
            yosys_json = json.load(cf)
    except Exception as e:
        return json.dumps({"available": False, "reason": f"failed to parse yosys json: {e}"})

    modules = yosys_json.get("modules", {}) if isinstance(yosys_json, dict) else {}
    mod_data = modules.get(module, {})
    ports = len(mod_data.get("ports", {}))
    cells = len(mod_data.get("cells", {}))

    result = json.dumps({
        "available": True,
        "module": module,
        "yosys_json": yosys_json,
        "ports": ports,
        "cells": cells,
    })

    try:
        with open(cache_path, "w", encoding="utf-8") as cf:
            cf.write(result)
    except Exception:
        pass
    return result


def lint_file_tool(path: str, workspace_root: str) -> str:
    full = _safe_workspace_path(path, workspace_root)
    if not full or not os.path.isfile(full):
        return json.dumps({"errors": [{"severity": "error", "line": 1, "message": "File not found"}]})
    
    vlogan_tool = shutil.which("vlogan")      # Synopsys
    xmvlog_tool = shutil.which("xmvlog")      # Cadence
    vlog_tool = shutil.which("vlog")          # Siemens Questa
    verilator_tool = shutil.which("verilator") # OSS Verilator
    iverilog_tool = shutil.which("iverilog")   # OSS Icarus Verilog
    
    errors = []
    
    if vlogan_tool:
        try:
            proc = subprocess.run(
                [vlogan_tool, "-sverilog", "-q", full],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            output = proc.stdout + "\n" + proc.stderr
            current_error = None
            for line in output.splitlines():
                line_str = line.strip()
                if line_str.startswith("Error-[") or line_str.startswith("Warning-["):
                    severity = "error" if "Error" in line_str else "warning"
                    current_error = {"severity": severity, "message": line_str}
                elif current_error and (line_str.startswith('"') or "," in line_str):
                    m = re.search(r'"?([^",]+)"?,\s*(\d+)', line_str)
                    if m:
                        current_error["line"] = int(m.group(2))
                        errors.append(current_error)
                        current_error = None
        except Exception:
            pass
            
    elif xmvlog_tool:
        try:
            proc = subprocess.run(
                [xmvlog_tool, "-sv", "-messages", full],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            output = proc.stdout + "\n" + proc.stderr
            for line in output.splitlines():
                m = re.match(r"^xmvlog:\s*\*([EW]),[A-Z0-9_]+\s+\(([^,]+),(\d+)(?:\|(\d+))?\):\s*(.*)$", line.strip())
                if m:
                    severity = "error" if m.group(1) == "E" else "warning"
                    errors.append({
                        "severity": severity,
                        "line": int(m.group(3)),
                        "message": m.group(5)
                    })
        except Exception:
            pass
            
    elif vlog_tool:
        try:
            proc = subprocess.run(
                [vlog_tool, "-sv", "-lint", full],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            output = proc.stdout + "\n" + proc.stderr
            for line in output.splitlines():
                m = re.match(r"^\*\*\s+(Error|Warning):\s+([^(]+)\((\d+)\):\s*(.*)$", line.strip())
                if m:
                    errors.append({
                        "severity": m.group(1).lower(),
                        "line": int(m.group(3)),
                        "message": m.group(4)
                    })
        except Exception:
            pass
            
    elif verilator_tool:
        try:
            cmd = [verilator_tool, "--lint-only"]
            cmd.extend(["-I" + workspace_root])
            cmd.extend(["-y", workspace_root])
            for sub in ["rtl", "simulation", "verification"]:
                subdir = os.path.join(workspace_root, sub)
                if os.path.isdir(subdir):
                    cmd.extend(["-I" + subdir])
                    cmd.extend(["-y", subdir])
            cmd.append(full)
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            output = proc.stdout + "\n" + proc.stderr
            for line in output.splitlines():
                m = re.match(r"^(%Error|%Warning-[A-Z0-9_]+):\s+([^:]+):(\d+):(?:\d+:)?\s*(.*)$", line.strip())
                if m:
                    severity = "error" if "Error" in m.group(1) else "warning"
                    errors.append({
                        "severity": severity,
                        "line": int(m.group(3)),
                        "message": f"[{m.group(1).replace('%', '')}] {m.group(4)}"
                    })
        except Exception:
            pass
            
    elif iverilog_tool:
        try:
            cmd = [iverilog_tool, "-o", "/dev/null", "-t", "null"]
            cmd.extend(["-I", workspace_root])
            cmd.extend(["-y", workspace_root])
            for sub in ["rtl", "simulation", "verification"]:
                subdir = os.path.join(workspace_root, sub)
                if os.path.isdir(subdir):
                    cmd.extend(["-I", subdir])
                    cmd.extend(["-y", subdir])
            cmd.append(full)
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            output = proc.stdout + "\n" + proc.stderr
            for line in output.splitlines():
                m = re.match(r"^([^:]+):(\d+):\s*(error|warning):\s*(.*)$", line.strip(), re.IGNORECASE)
                if m:
                    errors.append({
                        "severity": m.group(3).lower(),
                        "line": int(m.group(2)),
                        "message": m.group(4)
                    })
        except Exception:
            pass
            
    return json.dumps({"errors": errors})


def write_tool_combined(path: str, workspace_root: str, design_name: str = "scratch", content: str = "", old_string: str = "", new_string: str = "") -> str:
    state = DesignStateStore(workspace_root, design_name).load()
    artifact_decision = evaluate_artifact_write(
        workspace_root=workspace_root,
        design_name=design_name,
        path=path,
        state=state,
        content=content or new_string or "",
        old_string=old_string or "",
    )
    if not artifact_decision.allowed:
        return "Error: " + json.dumps({
            "status": "ARTIFACT_OWNERSHIP_VIOLATION",
            "reason": artifact_decision.reason,
            "path": artifact_decision.path,
            "project_root": artifact_decision.project_root,
            "stage": artifact_decision.stage,
            "kind": artifact_decision.kind,
            "logical_key": artifact_decision.logical_key,
            "canonical_path": artifact_decision.canonical_path,
            "required_action": artifact_decision.required_action,
        }, indent=2)
    decision = evaluate_write_against_intent(
        workspace_root=workspace_root,
        design_name=design_name,
        path=path,
        content=content or new_string or "",
        old_string=old_string or "",
    )
    if not decision.allowed:
        return "Error: " + json.dumps({
            "status": "INTENT_OWNERSHIP_VIOLATION",
            "reason": decision.reason,
            "module_name": decision.module_name,
            "owner_file": decision.owner_file,
            "required_action": decision.required_action,
        }, indent=2)
    preflight = _rtl_quality_preflight(path, workspace_root, design_name, content, old_string, new_string)
    if preflight:
        return preflight
    if old_string:
        return edit_file(path, old_string, new_string, workspace_root)
    full = _safe_workspace_path(path, workspace_root)
    if full and os.path.isfile(full) and _is_text_source_path(path):
        try:
            existing = open(full, "r", errors="replace").read()
        except OSError:
            existing = ""
        if len(existing) > SURGICAL_REWRITE_LIMIT and len(content or "") > SURGICAL_REWRITE_LIMIT:
            return (
                "Error: whole-file rewrite rejected for a large existing source file. "
                "Use old_string/new_string with enough surrounding context for a surgical edit."
            )
    return write_file(path, content, workspace_root)


def bash_tool(command: str, workspace_root: str, design_name: str, timeout: int = 300, eda_tool: str = "", stage: str = "", log_file: str = "", on_output=None, cancel_checker=None) -> str:
    if on_output:
        result = run_bash_stream(command, workspace_root, timeout=timeout, on_line=on_output, cancel_checker=cancel_checker)
    else:
        result = run_bash(command, workspace_root, timeout=timeout, cancel_checker=cancel_checker)

    output = ""
    if result["stdout"]:
        output += result["stdout"]
    if result["stderr"]:
        if output:
            output += "\n"
        output += result["stderr"]

    exit_code = result["code"]
    raw_log = output.strip() or "(no output)"
    try:
        DesignStateStore(workspace_root, design_name).record_command(
            command,
            tool=eda_tool or "shell",
            stage=stage or "execution",
            exit_code=exit_code,
        )
    except Exception:
        pass

    # If this isn't flagged as an EDA run, just return standard bash output
    if not eda_tool:
        if exit_code != 0:
            return f"Exit code {exit_code}\n{raw_log}"
        return raw_log

    # Auto-Checkpoint logic
    from checkpoint_engine import CheckpointEngine
    engine = CheckpointEngine(design_name, workspace_root)

    full_log_file = os.path.join(workspace_root, log_file) if log_file else ""
    checkpoint_result = engine.evaluate(
        tool=eda_tool,
        stage=stage or "execution",
        exit_code=exit_code,
        stdout_log=raw_log,
        log_file=full_log_file
    )
    checkpoint_result["verdict"]["artifact_hashes"] = _artifact_hashes_from_command(command, workspace_root)

    verdict = checkpoint_result["verdict"]
    truncated_log = checkpoint_result["truncated_log"]
    try:
        DesignStateStore(workspace_root, design_name).record_checkpoint(
            eda_tool,
            stage or "execution",
            verdict,
        )
    except Exception:
        pass

    return json.dumps({
        "bash_exit_code": exit_code,
        "checkpoint_verdict": verdict,
        "log_snippet": truncated_log
    }, indent=2)


def web_search(query: str, max_results: int = 5) -> str:
    web_setting = os.environ.get("AGENTIC_ENABLE_WEB_SEARCH", "true").strip().lower()
    if web_setting in {"0", "false", "no", "off"}:
        return (
            "Web search is disabled for IP safety. Use local files first. "
            "If public web research is needed, ask the user to enable AGENTIC_ENABLE_WEB_SEARCH=true."
        )
    if _looks_sensitive_web_query(query) and not _env_true("AGENTIC_ALLOW_SENSITIVE_WEB_SEARCH"):
        return (
            "Web search blocked because the query appears to contain local, license, or proprietary details. "
            "Ask the user for approval or remove sensitive identifiers before searching public sources."
        )
    url = f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote_plus(query)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AgentIC/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        parser = _DDGResultParser()
        parser.feed(html)
        results = parser.results[:max_results]
        return "\n".join(results) if results else "(no results)"
    except Exception as e:
        return f"Web search error: {e}"


def query_pdk_tool(query_type: str, cell_type: str = "", workspace_root: str = "", design_name: str = "scratch") -> str:
    if query_type in {"capability_summary", "find_memory", "readiness", "manifest_status", "tool_adapters"}:
        from local_tools import detect_environment
        env = detect_environment()
        graph = env.get("capability_graph") or {}
        manifests = env.get("capability_manifests") or {}
        if query_type == "tool_adapters":
            from tool_adapters import normalized_adapter_summary
            return json.dumps({
                "status": "OK",
                "tool_adapters": normalized_adapter_summary(env.get("tools") or {}),
                "wsl_tools": env.get("wsl_tools") or {},
                "wsl_capabilities": env.get("wsl_capabilities") or {},
                "wsl": env.get("wsl") or {},
                "recommended_flow": env.get("recommended_flow") or {},
                "capability_graph": compact_capability_graph(graph),
            }, indent=2)
        if query_type == "capability_summary":
            return json.dumps({
                "status": "OK",
                "capability_graph": compact_capability_graph(graph),
                "recommended_flow": env.get("recommended_flow") or {},
                "wsl_tools": env.get("wsl_tools") or {},
                "wsl_capabilities": env.get("wsl_capabilities") or {},
                "wsl": env.get("wsl") or {},
                "missing": env.get("missing") or [],
                "manifest_status": {
                    "files": manifests.get("files") or [],
                    "validation_error_count": len(manifests.get("validation_errors") or []),
                },
            }, indent=2)
        if query_type == "manifest_status":
            return json.dumps({
                "status": "OK",
                "manifest_status": manifests,
            }, indent=2)
        if query_type == "find_memory":
            requirement = _memory_query_from_text(cell_type)
            target_pdk = os.environ.get("PDK", "").strip() or None
            return json.dumps({
                "status": "OK",
                "query": cell_type,
                "requirement": requirement,
                "binding": bind_memory_requirement(requirement, graph, target_pdk=target_pdk),
            }, indent=2)
        if query_type == "readiness":
            state = DesignStateStore(workspace_root, design_name).load() if workspace_root else {}
            intent = state.get("design_intent") if isinstance(state, dict) else None
            return json.dumps({
                "status": "OK",
                "readiness": assess_design_readiness(intent, graph),
            }, indent=2)

    index = build_pdk_index()
    active = select_pdk(index, os.environ.get("PDK", ""))
    if not active:
        return json.dumps({
            "status": "DEPENDENCY_MISSING",
            "missing_component": "PDK"
        })
    pdks = [pdk["name"] for pdk in index.get("pdks", [])]
    active_pdk = active["name"]
    pdk_path = active["path"]
    libs_dir = os.path.join(pdk_path, "libs.ref")

    if not os.path.isdir(libs_dir):
        return json.dumps({"error": f"libs.ref not found in {pdk_path}", "available_pdks": pdks})

    if query_type == "list_libraries":
        libraries = [lib["name"] for lib in active.get("libraries", [])]
        return json.dumps({
            "pdk": active_pdk,
            "libraries": libraries,
            "readiness": active.get("readiness", {}),
            "openlane": active.get("openlane", {}),
            "orfs": active.get("orfs", {}),
        })

    elif query_type == "find_cell":
        if not cell_type:
            return json.dumps({"error": "cell_type is required for find_cell"})

        cell_type_lower = cell_type.lower()
        found_cells = []

        try:
            libraries = [d for d in os.listdir(libs_dir) if os.path.isdir(os.path.join(libs_dir, d))]
            for lib in libraries:
                lib_path = os.path.join(libs_dir, lib)
                lef_dir = os.path.join(lib_path, "lef")
                if os.path.isdir(lef_dir):
                    for lef_file in glob_mod.glob(os.path.join(lef_dir, "*.lef")):
                        try:
                            with open(lef_file, "r", errors="ignore") as f:
                                for line in f:
                                    if line.startswith("MACRO"):
                                        macro_name = line.split()[1]
                                        if cell_type_lower in macro_name.lower():
                                            found_cells.append({"library": lib, "cell": macro_name})
                                            if len(found_cells) >= 15:
                                                break
                        except Exception:
                            pass
                if len(found_cells) >= 15:
                    break
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps({"pdk": active_pdk, "query": cell_type, "results": found_cells})

    elif query_type == "get_layers":
        return json.dumps({
            "pdk": active_pdk,
            "layers": active.get("tech", {}).get("sample_routing_layers", []),
            "tech": active.get("tech", {}),
            "path": os.path.join(pdk_path, "libs.tech"),
        })

    else:
        return json.dumps({"error": f"Unknown query_type: {query_type}"})


def eda_capability_tool(scope: str = "summary", workspace_root: str = "", design_name: str = "scratch") -> str:
    from local_tools import detect_environment
    from tool_adapters import normalized_adapter_summary

    env = detect_environment()
    adapters = normalized_adapter_summary(env.get("tools") or {})
    graph = env.get("capability_graph") or {}
    conflicts = _eda_environment_conflicts(env, adapters)
    compact = _compact_eda_agent_context(env, adapters, conflicts)

    if scope in {"agent_context", "compact"}:
        return json.dumps(compact, indent=2)
    if scope == "conflicts":
        return json.dumps(conflicts, indent=2)
    if scope == "tools":
        return json.dumps({
            "status": "OK",
            "agent_context": compact,
            "tool_adapters": _compact_adapter_summary(adapters),
            "conflicts": conflicts,
            "wsl": _compact_wsl_for_agent(env),
            "recommended_flow": env.get("recommended_flow") or {},
            "capability_graph": compact_capability_graph(graph),
        }, indent=2)
    if scope == "readiness":
        state = DesignStateStore(workspace_root, design_name).load() if workspace_root else {}
        intent = state.get("design_intent") if isinstance(state, dict) else None
        return json.dumps({
            "status": "OK",
            "agent_context": compact,
            "readiness": assess_design_readiness(intent, graph),
        }, indent=2)
    if scope == "manifests":
        return json.dumps({
            "status": "OK",
            "agent_context": compact,
            "manifest_status": env.get("capability_manifests") or {},
        }, indent=2)
    return json.dumps({
        "status": "OK",
        "agent_context": compact,
        "capability_graph": compact_capability_graph(graph),
        "recommended_flow": env.get("recommended_flow") or {},
        "conflicts": conflicts,
    }, indent=2)


def _compact_eda_agent_context(env: dict, adapters: dict, conflicts: dict) -> dict:
    stages = adapters.get("stages") or {}
    required = adapters.get("required_digital_stages") or ["simulation", "synthesis", "pnr", "sta", "physical_verification"]
    stage_context = {}
    for stage in required:
        record = stages.get(stage) or {}
        selected = record.get("selected") or {}
        stage_context[stage] = {
            "status": record.get("status"),
            "selected_adapter": selected.get("adapter"),
            "vendor": selected.get("vendor"),
            "openness": selected.get("openness"),
            "commands": selected.get("available_commands") or selected.get("commands") or [],
            "license_hint_present": selected.get("license_hint_present"),
            "blockers": record.get("blockers", [])[:3],
        }
    ready = [stage for stage, record in stage_context.items() if record.get("status") == "ready"]
    return {
        "schema_version": "agentic.eda_agent_context.v1",
        "capability_tier": env.get("capability_tier"),
        "readiness": adapters.get("readiness") or {},
        "vendor_posture": (adapters.get("vendor_profile") or {}).get("posture"),
        "selected_chain": adapters.get("selected_chain") or {},
        "ready_required_stages": ready,
        "missing_required_stages": [stage for stage in required if stage not in ready],
        "stage_context": stage_context,
        "pdk": {
            "available": bool((env.get("capabilities") or {}).get("pdk")),
            "count": len(((env.get("pdk_index") or {}).get("pdks") or [])),
            "existing_dirs_count": len(env.get("existing_pdk_dirs") or []),
        },
        "flow_decision": {
            "profile": (env.get("recommended_flow") or {}).get("profile"),
            "backend": (env.get("recommended_flow") or {}).get("backend"),
            "confidence": (env.get("recommended_flow") or {}).get("confidence"),
            "blockers": ((env.get("recommended_flow") or {}).get("blockers") or [])[:6],
        },
        "conflict_counts": conflicts.get("counts") or {},
        "next_actions": _eda_next_actions(stage_context, env, conflicts),
    }


def _compact_adapter_summary(adapters: dict) -> dict:
    stages = {}
    for stage, record in (adapters.get("stages") or {}).items():
        candidates = record.get("candidates") or []
        stages[stage] = {
            "status": record.get("status"),
            "selected": record.get("selected"),
            "ready_candidates": [
                {
                    "adapter": item.get("adapter"),
                    "vendor": item.get("vendor"),
                    "openness": item.get("openness"),
                    "available_commands": item.get("available_commands"),
                    "license_hint_present": item.get("license_hint_present"),
                }
                for item in candidates
                if item.get("available")
            ][:8],
            "blockers": record.get("blockers", [])[:5],
        }
    return {
        "schema_version": adapters.get("schema_version"),
        "readiness": adapters.get("readiness"),
        "vendor_profile": adapters.get("vendor_profile"),
        "selected_chain": adapters.get("selected_chain"),
        "stages": stages,
        "blocking": (adapters.get("blocking") or [])[:12],
        "extension_contract": adapters.get("extension_contract"),
    }


def _eda_environment_conflicts(env: dict, adapters: dict) -> dict:
    commands = sorted({
        command
        for record in (adapters.get("stages") or {}).values()
        for candidate in (record.get("candidates") or [])
        for command in (candidate.get("commands") or [])
    })
    command_paths = {}
    shadowed = []
    for command in commands:
        matches = _path_matches_for_command(command)
        if matches:
            command_paths[command] = matches[:8]
        if len(matches) > 1:
            shadowed.append({
                "command": command,
                "selected": matches[0],
                "shadowed": matches[1:8],
                "risk": "PATH order chooses the first binary; later EDA binaries are hidden unless invoked by absolute path.",
            })

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    normalized = [os.path.abspath(os.path.expanduser(os.path.expandvars(item))) for item in path_entries if item]
    seen = set()
    duplicates = []
    for item in normalized:
        key = os.path.normcase(item)
        if key in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(key)
    missing_dirs = [item for item in normalized if item and not os.path.isdir(item)]

    license_env = env.get("license_env") or {}
    unresolved_license_stages = []
    for stage, record in (adapters.get("stages") or {}).items():
        if record.get("status") == "license_unresolved":
            unresolved_license_stages.append({
                "stage": stage,
                "selected_adapter": ((record.get("selected") or {}).get("adapter")),
                "missing_license_env": [
                    name for blocker in (record.get("blockers") or [])
                    for name in (blocker.get("missing_license_env") or [])
                ],
            })

    pdk_env = {
        name: bool(os.environ.get(name, "").strip())
        for name in ("PDK", "PDK_ROOT", "PDKPATH", "PDK_HOME", "AGENTIC_PDK_SEARCH_PATHS")
    }
    existing_pdk_dirs = env.get("existing_pdk_dirs") or []
    pdk_conflicts = []
    if len(existing_pdk_dirs) > 1 and not os.environ.get("PDK", "").strip():
        pdk_conflicts.append({
            "reason": "multiple_pdk_roots_without_selected_pdk",
            "count": len(existing_pdk_dirs),
            "message": "Multiple PDK directories are visible but PDK is not selected; ask the user or use design intent before choosing.",
        })

    return {
        "schema_version": "agentic.eda_environment_conflicts.v1",
        "counts": {
            "shadowed_commands": len(shadowed),
            "duplicate_path_entries": len(duplicates),
            "missing_path_entries": len(missing_dirs),
            "unresolved_license_stages": len(unresolved_license_stages),
            "pdk_conflicts": len(pdk_conflicts),
        },
        "shadowed_commands": shadowed[:24],
        "path_hygiene": {
            "duplicate_entries": duplicates[:16],
            "missing_entries": missing_dirs[:16],
        },
        "license": {
            "present_env": sorted([name for name, present in license_env.items() if present]),
            "unresolved_stages": unresolved_license_stages[:12],
        },
        "pdk": {
            "env_present": pdk_env,
            "existing_dirs_count": len(existing_pdk_dirs),
            "conflicts": pdk_conflicts,
        },
        "command_paths": command_paths,
    }


def _path_matches_for_command(command: str) -> list[str]:
    if not command:
        return []
    matches = []
    seen = set()
    path_exts = [""]
    if os.name == "nt":
        path_exts = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(directory)))
        for ext in path_exts:
            candidate = os.path.join(expanded, command + ext)
            key = os.path.normcase(candidate)
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                matches.append(candidate)
    return matches


def _compact_wsl_for_agent(env: dict) -> dict:
    wsl = env.get("wsl") or {}
    inventory = []
    for item in (wsl.get("tool_inventory") or [])[:6]:
        tools = item.get("tools") or {}
        inventory.append({
            "distro": item.get("distro"),
            "can_execute": bool(item.get("can_execute")),
            "available_tools": sorted([name for name, present in tools.items() if present])[:40],
            "license_env": sorted([name for name, present in (item.get("license_env") or {}).items() if present])[:12],
            "error": item.get("error"),
        })
    return {
        "available": bool(wsl.get("available")),
        "distros": (wsl.get("distros") or [])[:10],
        "capabilities": env.get("wsl_capabilities") or {},
        "inventory": inventory,
    }


def _eda_next_actions(stage_context: dict, env: dict, conflicts: dict) -> list[str]:
    actions = []
    if conflicts.get("counts", {}).get("shadowed_commands"):
        actions.append("Resolve PATH shadowing for selected EDA commands or invoke the intended binary by absolute path.")
    if conflicts.get("counts", {}).get("unresolved_license_stages"):
        actions.append("Configure the missing license environment for installed proprietary tools before running those stages.")
    if not ((env.get("capabilities") or {}).get("pdk")):
        actions.append("Configure PDK_ROOT, PDKPATH, PDK_HOME, AGENTIC_PDK_SEARCH_PATHS, or a capability manifest before implementation.")
    for stage, record in stage_context.items():
        if record.get("status") != "ready":
            actions.append(f"Stage `{stage}` is not ready: inspect its blocker and choose install/configure/fallback.")
    return actions[:8]


def ledger_tool(action: str, workspace_root: str, design_name: str, **kwargs) -> str:
    store = DesignStateStore(workspace_root, design_name)
    if action == "get_state":
        return json.dumps(store.summary(max_events=int(kwargs.get("max_events") or 12)), indent=2, default=str)
    if action == "record_fact":
        namespace = str(kwargs.get("namespace") or "").strip()
        key = str(kwargs.get("key") or "").strip()
        if not namespace or not key:
            return "Error: ledger.record_fact requires namespace and key"
        state = store.upsert_design_fact(namespace, key, kwargs.get("value"), source=str(kwargs.get("source") or "agent"))
        return json.dumps({"ok": True, "namespace": namespace, "key": key, "updated_at": state.get("updated_at")}, indent=2)
    if action == "record_handoff":
        source_role = str(kwargs.get("source_role") or "").strip()
        target_role = str(kwargs.get("target_role") or "").strip()
        payload = kwargs.get("payload") or {}
        if not source_role or not target_role or not isinstance(payload, dict):
            return "Error: ledger.record_handoff requires source_role, target_role, and object payload"
        state = store.record_handoff(source_role, target_role, payload)
        return json.dumps({"ok": True, "handoff_count": len(state.get("handoffs", []))}, indent=2)
    if action == "record_evidence":
        kind = str(kwargs.get("kind") or "").strip()
        ref = str(kwargs.get("ref") or "").strip()
        payload = kwargs.get("payload") or {}
        links = kwargs.get("links") or []
        if not kind or not ref or not isinstance(payload, dict):
            return "Error: ledger.record_evidence requires kind, ref, and object payload"
        if not isinstance(links, list):
            links = []
        state = store.record_evidence(kind, ref, payload, links=links)
        graph = state.get("evidence_graph") or {}
        return json.dumps({"ok": True, "evidence_count": len(graph.get("nodes", {}))}, indent=2)
    return f"Error: unknown ledger action '{action}'"


def git_clone(url: str, workspace_root: str, target_dir: str | None = None, branch: str = "main", token: str = "") -> str:
    import subprocess, shutil
    url = url.strip()
    if not url.startswith("http") and not url.startswith("git@"):
        return "Error: provide a valid GitHub URL (https://github.com/... or git@github.com:...)"
    if token:
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        authed = f"{parsed.scheme}://{token}@{parsed.netloc}{parsed.path}"
        url = authed
    name = target_dir or url.rstrip("/").split("/")[-1].replace(".git", "")
    dest = os.path.join(workspace_root, name)
    if os.path.exists(dest):
        return f"Error: '{name}' already exists in workspace"
    try:
        cmd = ["git", "clone", "--branch", branch, "--depth", "1", url, dest]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return f"Error: git clone failed:\n{result.stderr[:2000]}"
        return f"Cloned '{name}' into workspace. {len(os.listdir(dest))} items."
    except subprocess.TimeoutExpired:
        return "Error: git clone timed out after 120s"
    except Exception as e:
        return f"Error: {e}"


def dispatch_tool(name: str, args: dict, workspace_root: str, design_name: str = "scratch", on_output=None, cancel_checker=None) -> str:
    if name == "workspace":
        return workspace_tool(args.get("action", ""), workspace_root, args.get("path", "."), args.get("pattern", ""), args.get("module", ""))
    elif name == "design_state":
        max_events = int(args.get("max_events") or 20)
        return json.dumps(DesignStateStore(workspace_root, design_name).summary(max_events=max_events), indent=2, default=str)
    elif name == "eda_capability":
        return eda_capability_tool(str(args.get("scope") or "agent_context"), workspace_root, design_name)
    elif name == "layout_inspect":
        return layout_inspect_tool(args.get("path", ""), workspace_root)
    elif name == "timing_inspect":
        return timing_analysis_tool(args.get("path", ""), workspace_root)
    elif name == "drc_inspect":
        return parse_log_tool(args.get("path", ""), workspace_root)
    elif name == "run_flow":
        timeout = args.get("timeout", 600)
        return bash_tool(
            args["command"],
            workspace_root,
            design_name,
            timeout=timeout,
            eda_tool=args.get("eda_tool", ""),
            stage=args.get("stage", ""),
            log_file=args.get("log_file", ""),
            on_output=on_output,
            cancel_checker=cancel_checker,
        )
    elif name == "write":
        path = args.get("path", "")
        result = write_tool_combined(path, workspace_root, design_name, args.get("content", ""), args.get("old_string", ""), args.get("new_string", ""))
        if result.startswith("File written") or result.startswith("File edited"):
            try:
                store = DesignStateStore(workspace_root, design_name)
                state = store.record_file(
                    path,
                    action="edit" if args.get("old_string") else "write",
                )
                if _is_rtl_path(path):
                    full_path = _safe_workspace_path(path, workspace_root)
                    if full_path and os.path.isfile(full_path):
                        content = open(full_path, "r", errors="replace").read()
                        intent = intent_from_state_or_manifest(workspace_root, state)
                        quality = evaluate_rtl_quality(path, content, intent)
                        store.record_evidence("rtl_quality", path, quality.to_record())
                        if not quality.accepted:
                            return "Error: " + json.dumps({
                                "status": "RTL_QUALITY_REJECTED",
                                "path": path,
                                "issues": [issue.model_dump(mode="json") for issue in quality.issues],
                                "metrics": quality.metrics,
                            }, indent=2)
            except Exception:
                pass
        return result
    elif name == "bash":
        timeout = args.get("timeout", 300)
        return bash_tool(args["command"], workspace_root, design_name, timeout=timeout, eda_tool=args.get("eda_tool", ""), stage=args.get("stage", ""), log_file=args.get("log_file", ""), on_output=on_output, cancel_checker=cancel_checker)
    elif name == "report":
        from checkpoint_engine import CheckpointEngine
        engine = CheckpointEngine(design_name, workspace_root)
        report = engine.signoff_report()
        try:
            report["design_state"] = DesignStateStore(workspace_root, design_name).summary()
        except Exception:
            pass
        return json.dumps(report, indent=2)
    elif name == "web_search":
        return web_search(args["query"], args.get("max_results", 5))
    elif name == "query_pdk":
        return query_pdk_tool(args.get("query_type", ""), args.get("cell_type", ""), workspace_root, design_name)
    elif name == "ledger":
        ledger_args = dict(args)
        action = ledger_args.pop("action", "")
        return ledger_tool(action, workspace_root, design_name, **ledger_args)
    elif name == "git_clone":
        return git_clone(args.get("url", ""), workspace_root, args.get("target_dir"), args.get("branch", "main"), args.get("token", ""))
    else:
        return f"Error: unknown tool '{name}'"


def _is_rtl_path(path: str) -> bool:
    lower = str(path or "").lower()
    wrapped = f"/{lower}"
    if any(section in wrapped for section in ("/tb/", "/dv/", "/verification/", "/formal/")):
        return False
    return "/rtl/" in wrapped or lower.endswith((".v", ".sv", ".vh", ".svh"))


def _rtl_quality_preflight(path: str, workspace_root: str, design_name: str, content: str, old_string: str, new_string: str) -> str:
    if not _is_rtl_path(path):
        return ""
    final_content = content or ""
    if old_string:
        full_path = _safe_workspace_path(path, workspace_root)
        if not full_path or not os.path.isfile(full_path):
            return ""
        try:
            existing = open(full_path, "r", errors="replace").read()
        except OSError:
            return ""
        if old_string not in existing:
            return ""
        final_content = existing.replace(old_string, new_string, 1)
    if not final_content:
        return ""
    try:
        state = DesignStateStore(workspace_root, design_name).load()
        intent = intent_from_state_or_manifest(workspace_root, state)
        quality = evaluate_rtl_quality(path, final_content, intent)
        if not quality.accepted:
            try:
                DesignStateStore(workspace_root, design_name).record_evidence("rtl_quality_rejected", path, quality.to_record())
            except Exception:
                pass
            return "Error: " + json.dumps({
                "status": "RTL_QUALITY_REJECTED",
                "path": path,
                "issues": [issue.model_dump(mode="json") for issue in quality.issues],
                "metrics": quality.metrics,
            }, indent=2)
        syntax_error = _rtl_syntax_preflight(path, final_content)
        if syntax_error:
            return syntax_error
    except Exception:
        return ""
    return ""


def _rtl_syntax_preflight(path: str, content: str) -> str:
    tool = shutil.which("verilator")
    if not tool:
        return ""
    suffix = os.path.splitext(path)[1] or ".v"
    try:
        with tempfile.TemporaryDirectory(prefix="agentic_rtl_lint_") as tmp:
            tmp_path = os.path.join(tmp, "candidate" + suffix)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(_normalize_text_content(path, content))
            proc = subprocess.run(
                [tool, "--lint-only", tmp_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            if proc.returncode == 0:
                return ""
            return "Error: " + json.dumps({
                "status": "RTL_SYNTAX_REJECTED",
                "path": path,
                "tool": "verilator",
                "exit_code": proc.returncode,
                "log_snippet": (proc.stdout + "\n" + proc.stderr).strip()[:3000],
            }, indent=2)
    except Exception:
        return ""


def _artifact_hashes_from_command(command: str, workspace_root: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for token in re.findall(r"(?<!['\"])([A-Za-z0-9_./-]+\.(?:v|sv|vh|svh|vhd|vhdl|sdc|tcl|ys|json|md))(?!['\"])", command or ""):
        full = _safe_workspace_path(token, workspace_root)
        if not full or not os.path.isfile(full):
            continue
        try:
            with open(full, "rb") as fh:
                hashes[token] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            continue
    return hashes


def _memory_query_from_text(text: str) -> dict[str, object]:
    lowered = (text or "").lower()
    dims = _memory_dims_from_text(lowered)
    kind = "rom" if "rom" in lowered else "fifo" if "fifo" in lowered else "cache" if "cache" in lowered else "sram"
    ports = "rom" if kind == "rom" else "2rw" if re.search(r"\b(2rw|dual\s*port|two\s*port)\b", lowered) else "1r1w" if "1r1w" in lowered else "1rw" if re.search(r"\b(1rw|single\s*port|one\s*port)\b", lowered) else "unspecified"
    return {
        "name": "queried_memory",
        "kind": kind,
        "width_bits": dims.get("width_bits"),
        "depth_words": dims.get("depth_words"),
        "capacity_bits": dims.get("capacity_bits"),
        "ports": ports,
        "implementation_preference": "pdk_macro",
    }


def _memory_dims_from_text(text: str) -> dict[str, int | None]:
    explicit = re.search(r"(\d+)\s*[x×]\s*(\d+)", text or "")
    if explicit:
        depth = int(explicit.group(1))
        width = int(explicit.group(2))
        return {"depth_words": depth, "width_bits": width, "capacity_bits": depth * width}
    capacity = re.search(r"(\d+)\s*(kb|kib|mb|mib|bits?|bytes?)", text or "")
    width = re.search(r"(\d+)\s*[- ]?bit", text or "")
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
    depth_words = capacity_bits // width_bits if capacity_bits and width_bits else None
    return {"depth_words": depth_words, "width_bits": width_bits, "capacity_bits": capacity_bits}
