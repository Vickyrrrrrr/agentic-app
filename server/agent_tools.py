import os
import re
import urllib.request
import urllib.parse
import glob as glob_mod
import json
import hashlib
import shlex
import shutil
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from typing import Any

from local_tools import detect_environment, run_bash, run_bash_stream
from app_capabilities import build_app_capability_contract
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
    elif action == "rtl_repair_diagnose":
        return rtl_repair_diagnose_tool(path, workspace_root)
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
        return f"Error: unknown action '{action}' for workspace tool. Use read, search, list, lint, rtl_repair_diagnose, parse_module, parse_log, layout_inspect, timing_analysis, or schematic_json."


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

    # Add tool instruction nudge to prevent LLM guesswork/hallucinations
    parts.append("\nNote to Agent: You can parse the complete detailed log file, timing metrics, and full warnings/errors diagnostics list by running the tool:")
    parts.append(f'workspace(action="parse_log", path="{path}")')

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
    import math
    import struct

    cells = {}
    top_cell = None
    total_polys = 0
    offset = 0
    user_unit_in_db_units = 0.001
    database_unit_m = 1e-9

    current_cell = None
    current_layer = 0
    current_datatype = 0
    current_xy = []
    element_type = None
    sname = ""
    mag = 1.0
    angle = 0.0
    reflected = False
    cols = 0
    rows = 0
    srefs = []
    arefs = []
    layers_seen = set()

    def empty_bbox():
        return {"minX": None, "minY": None, "maxX": None, "maxY": None}

    def expand_bbox(bb, x, y):
        bb["minX"] = x if bb["minX"] is None else min(bb["minX"], x)
        bb["minY"] = y if bb["minY"] is None else min(bb["minY"], y)
        bb["maxX"] = x if bb["maxX"] is None else max(bb["maxX"], x)
        bb["maxY"] = y if bb["maxY"] is None else max(bb["maxY"], y)

    def merge_bbox(dst, src):
        if not src or any(src.get(k) is None for k in ("minX", "minY", "maxX", "maxY")):
            return
        expand_bbox(dst, src["minX"], src["minY"])
        expand_bbox(dst, src["maxX"], src["maxY"])

    def bbox_to_um(bb):
        if not bb or any(bb.get(k) is None for k in ("minX", "minY", "maxX", "maxY")):
            return None
        um_per_unit_local = database_unit_m * 1e6
        return {
            "minX_um": round(bb["minX"] * um_per_unit_local, 4),
            "minY_um": round(bb["minY"] * um_per_unit_local, 4),
            "maxX_um": round(bb["maxX"] * um_per_unit_local, 4),
            "maxY_um": round(bb["maxY"] * um_per_unit_local, 4),
        }

    def transform_point(x, y, *, origin=(0, 0), mag_value=1.0, angle_value=0.0, reflected_value=False):
        if reflected_value:
            y = -y
        theta = math.radians(angle_value or 0.0)
        sx = x * (mag_value or 1.0)
        sy = y * (mag_value or 1.0)
        tx = sx * math.cos(theta) - sy * math.sin(theta)
        ty = sx * math.sin(theta) + sy * math.cos(theta)
        return origin[0] + tx, origin[1] + ty

    def transform_bbox(bb, *, origin=(0, 0), mag_value=1.0, angle_value=0.0, reflected_value=False):
        if not bb or any(bb.get(k) is None for k in ("minX", "minY", "maxX", "maxY")):
            return None
        points = (
            (bb["minX"], bb["minY"]),
            (bb["minX"], bb["maxY"]),
            (bb["maxX"], bb["minY"]),
            (bb["maxX"], bb["maxY"]),
        )
        out = empty_bbox()
        for x, y in points:
            tx, ty = transform_point(
                x,
                y,
                origin=origin,
                mag_value=mag_value,
                angle_value=angle_value,
                reflected_value=reflected_value,
            )
            expand_bbox(out, tx, ty)
        return out

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
                # GDS UNITS: first real is user-unit size in database units;
                # second real is one database unit in meters. XY coordinates
                # are stored in database units, so bbox conversion uses the
                # second value.
                try:
                    user_unit_in_db_units = read_real8(ds)
                    database_unit_m = read_real8(ds + 8)
                except Exception:
                    pass
        elif rec_type == 0x05:  # BGNSTR
            current_cell = {
                "name": "",
                "polygons": 0,
                "srefs": 0,
                "arefs": 0,
                "layers": set(),
                "layer_counts": {},
                "bbox": empty_bbox(),
                "refs": [],
            }
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
                current_cell["sref_names"] = [ref["name"] for ref in srefs + arefs]
                current_cell["refs"] = list(srefs + arefs)
                cells[current_cell["name"]] = current_cell
            current_cell = None
            srefs = []
            arefs = []
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
            reflected = False
        elif rec_type == 0x0b:  # AREF
            element_type = "aref"
            sname = ""
            cols = 0
            rows = 0
            mag = 1.0
            angle = 0.0
            reflected = False
        elif rec_type == 0x0d:  # LAYER
            if data_type == 2 and ds + 2 <= len(data):
                current_layer = struct.unpack_from(">h", data, ds)[0]
            elif data_type == 3 and ds + 4 <= len(data):
                current_layer = struct.unpack_from(">i", data, ds)[0]
            layers_seen.add(current_layer)
        elif rec_type == 0x0e:  # DATATYPE
            if data_type == 2 and ds + 2 <= len(data):
                current_datatype = struct.unpack_from(">h", data, ds)[0]
            elif data_type == 3 and ds + 4 <= len(data):
                current_datatype = struct.unpack_from(">i", data, ds)[0]
        elif rec_type == 0x12:  # SNAME
            sname = data[ds:ds + dl].rstrip(b"\x00").decode("ascii", errors="replace").strip()
        elif rec_type == 0x1a:  # STRANS
            if dl >= 2:
                strans = struct.unpack_from(">H", data, ds)[0]
                reflected = bool(strans & 0x8000)
        elif rec_type == 0x1b:  # MAG
            mag = read_real8(ds)
        elif rec_type == 0x1c:  # ANGLE
            angle = read_real8(ds)
        elif rec_type == 0x13:  # COLROW
            cols = struct.unpack_from(">H", data, ds)[0]
            rows = struct.unpack_from(">H", data, ds + 2)[0]
        elif rec_type == 0x10:  # XY
            current_xy = []
            if data_type == 3:
                for i in range(0, dl, 8):
                    if ds + i + 8 <= len(data):
                        current_xy.append(struct.unpack_from(">i", data, ds + i)[0])
                        current_xy.append(struct.unpack_from(">i", data, ds + i + 4)[0])
            elif data_type == 2:
                for i in range(0, dl, 4):
                    if ds + i + 4 <= len(data):
                        current_xy.append(struct.unpack_from(">h", data, ds + i)[0])
                        current_xy.append(struct.unpack_from(">h", data, ds + i + 2)[0])
        elif rec_type == 0x11:  # ENDEL
            if element_type in ("boundary", "path", "box") and current_cell:
                current_cell["polygons"] += 1
                current_cell["layers"].add(current_layer)
                current_cell["layer_counts"][current_layer] = current_cell["layer_counts"].get(current_layer, 0) + 1
                total_polys += 1
                # Update cell bounding box from polygon XY points
                bb = current_cell["bbox"]
                for pi in range(0, len(current_xy) - 1, 2):
                    px, py = current_xy[pi], current_xy[pi + 1]
                    expand_bbox(bb, px, py)
            elif element_type == "sref":
                origin = (current_xy[0], current_xy[1]) if len(current_xy) >= 2 else (0, 0)
                srefs.append({
                    "type": "sref",
                    "name": sname,
                    "origin": origin,
                    "mag": mag,
                    "angle": angle,
                    "reflected": reflected,
                })
            elif element_type == "aref":
                origin = (current_xy[0], current_xy[1]) if len(current_xy) >= 2 else (0, 0)
                col_vec = (0, 0)
                row_vec = (0, 0)
                if len(current_xy) >= 6 and cols and rows:
                    col_div = max(cols - 1, 1)
                    row_div = max(rows - 1, 1)
                    col_vec = ((current_xy[2] - origin[0]) / col_div, (current_xy[3] - origin[1]) / col_div) if cols > 1 else (0, 0)
                    row_vec = ((current_xy[4] - origin[0]) / row_div, (current_xy[5] - origin[1]) / row_div) if rows > 1 else (0, 0)
                arefs.append({
                    "type": "aref",
                    "name": sname,
                    "origin": origin,
                    "cols": cols or 1,
                    "rows": rows or 1,
                    "col_vec": col_vec,
                    "row_vec": row_vec,
                    "mag": mag,
                    "angle": angle,
                    "reflected": reflected,
                })
            element_type = None
        elif rec_type == 0x04:  # ENDLIB
            break

        offset += rec_len

    # Find top cell: the cell NOT referenced by any other cell (= layout top).
    # Build referenced set from stored sref_names.
    referenced: set = set()
    for cell in cells.values():
        for sref_name in cell.get("sref_names", []):
            referenced.add(sref_name)

    unreferenced = [name for name in cells.keys() if name not in referenced]
    score_cache = {}

    def hierarchical_score(cell_name, visiting_score=None):
        if visiting_score is None:
            visiting_score = set()
        if cell_name in score_cache:
            return score_cache[cell_name]
        if cell_name in visiting_score:
            return 0
        cell = cells.get(cell_name)
        if not cell:
            return 0
        visiting_score.add(cell_name)
        score = int(cell.get("polygons") or 0)
        for ref in cell.get("refs", []):
            multiplier = 1
            if ref.get("type") == "aref":
                multiplier = max(int(ref.get("cols") or 1), 1) * max(int(ref.get("rows") or 1), 1)
            score += hierarchical_score(ref.get("name"), visiting_score) * multiplier
        visiting_score.remove(cell_name)
        score_cache[cell_name] = score
        return score

    if unreferenced:
        # Prefer the unreferenced cell with the largest hierarchy, not just the
        # most direct polygons. Top cells often contain mostly references.
        top_cell = max(unreferenced, key=lambda n: (hierarchical_score(n), cells[n].get("polygons", 0), n))
    elif cells:
        top_cell = max(cells.keys(), key=lambda n: (hierarchical_score(n), cells[n].get("polygons", 0), n))

    bbox_cache = {}
    visiting = set()

    def hierarchical_bbox(cell_name):
        if cell_name in bbox_cache:
            return bbox_cache[cell_name]
        cell = cells.get(cell_name)
        if not cell or cell_name in visiting:
            return None
        visiting.add(cell_name)
        out = empty_bbox()
        merge_bbox(out, cell.get("bbox"))
        for ref in cell.get("refs", []):
            child_bbox = hierarchical_bbox(ref.get("name"))
            if not child_bbox:
                continue
            if ref.get("type") == "aref":
                cols_local = max(int(ref.get("cols") or 1), 1)
                rows_local = max(int(ref.get("rows") or 1), 1)
                ox, oy = ref.get("origin", (0, 0))
                cvx, cvy = ref.get("col_vec", (0, 0))
                rvx, rvy = ref.get("row_vec", (0, 0))
                for col in range(cols_local):
                    for row in range(rows_local):
                        origin = (ox + col * cvx + row * rvx, oy + col * cvy + row * rvy)
                        merge_bbox(out, transform_bbox(
                            child_bbox,
                            origin=origin,
                            mag_value=ref.get("mag") or 1.0,
                            angle_value=ref.get("angle") or 0.0,
                            reflected_value=bool(ref.get("reflected")),
                        ))
            else:
                merge_bbox(out, transform_bbox(
                    child_bbox,
                    origin=ref.get("origin", (0, 0)),
                    mag_value=ref.get("mag") or 1.0,
                    angle_value=ref.get("angle") or 0.0,
                    reflected_value=bool(ref.get("reflected")),
                ))
        visiting.remove(cell_name)
        bbox_cache[cell_name] = None if out["minX"] is None else out
        return bbox_cache[cell_name]

    top_bbox = hierarchical_bbox(top_cell) if top_cell else None
    bbox_um = bbox_to_um(top_bbox)

    # Build per-layer polygon counts
    layer_counts: dict = {}
    for cell in cells.values():
        for layer, count in cell.get("layer_counts", {}).items():
            layer_counts[layer] = layer_counts.get(layer, 0) + count

    # Build hierarchy (first 50 cells)
    hierarchy = []
    for name, cell in list(cells.items())[:50]:
        cell_bbox_um = bbox_to_um(hierarchical_bbox(name))
        hierarchy.append({
            "name": name,
            "polygons": cell["polygons"],
            "srefs": cell["srefs"],
            "arefs": cell["arefs"],
            "layers": sorted(cell["layers"]),
            "layer_polygon_counts": dict(sorted(cell.get("layer_counts", {}).items())),
            "bbox_um": cell_bbox_um,
            "is_top": name == top_cell,
        })

    layers_sorted = sorted(layers_seen)

    # Compact summary for the agent
    biggest = sorted(cells.values(), key=lambda x: x["polygons"], reverse=True)[:5]
    biggest_str = ", ".join(f'{c["name"]}({c["polygons"]}p)' for c in biggest)
    layer_note = ", ".join(f"{l}:{layer_counts.get(l,0)}p" for l in layers_sorted[:12])
    um_per_unit = database_unit_m * 1e6
    compact = (
        f"GDS layout: {total_polys} polygons, {len(cells)} cells, {len(layers_sorted)} layers. "
        f"Top cell: {top_cell}. GDS database unit: {database_unit_m:.2e} m = {um_per_unit:.6f} µm/unit. "
        f"Layers (layer:poly_count): {layer_note}. Largest cells: {biggest_str}."
    )
    if bbox_um:
        compact += (f" Top-cell bbox: ({bbox_um['minX_um']},{bbox_um['minY_um']}) to "
                    f"({bbox_um['maxX_um']},{bbox_um['maxY_um']}) µm.")

    return {
        "available": True,
        "top_cell": top_cell,
        "cell_count": len(cells),
        "polygon_count": total_polys,
        "layers": layers_sorted,
        "layer_polygon_counts": layer_counts,
        "hierarchy": hierarchy,
        "bbox_um": bbox_um,
        "bbox_source": "hierarchical_sref_aref_bbox",
        "user_unit_in_db_units": user_unit_in_db_units,
        "database_unit_m": database_unit_m,
        "user_unit_m": database_unit_m,
        "um_per_unit": um_per_unit,
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

    cache_dir = os.path.join(workspace_root, ".agentic", "cache", "schematics")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        pass

    full_json_cell_limit = int(os.environ.get("AGENTIC_SCHEMATIC_FULL_JSON_CELL_LIMIT", "2500"))
    full_json_net_limit = int(os.environ.get("AGENTIC_SCHEMATIC_FULL_JSON_NET_LIMIT", "10000"))
    cache_version = f"schematic_json_v4_cells{full_json_cell_limit}_nets{full_json_net_limit}"
    digest = hashlib.sha256((cache_version + "\n" + content).encode("utf-8", errors="replace")).hexdigest()[:16]
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
                other_file_path = os.path.join(file_dir, name)
                try:
                    with open(other_file_path, "r", encoding="utf-8", errors="replace") as of:
                        other_content = of.read()
                    if re.search(r"\bmodule\s+" + re.escape(module) + r"\b", other_content):
                        continue
                except Exception:
                    pass
                verilog_files.append(other_file_path)
    except Exception:
        pass


    # Detect gate-level netlists: .pnl.v / .gl.v / .synth.v / .mapped.v, or
    # files containing PDK standard-cell instantiations (sky130_fd_sc*, gf180mcu*, etc.)
    # For these, use `hierarchy -nocheck` so PDK cells are treated as black boxes.
    gate_level_extensions = (".pnl.v", ".gl.v", ".synth.v", ".mapped.v", ".pnl.sv", ".gl.sv")
    is_gate_level = (
        any(full.endswith(ext) for ext in gate_level_extensions)
        or bool(re.search(r"\bsky130_fd_sc_\w+\b|\bgf180mcu_fd_sc_\w+\b|\basap7sc\w+\b", content[:4000]))
    )
    hierarchy_flag = "-nocheck" if is_gate_level else "-check"

    def yosys_quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def rel_to_workspace(value: str) -> str:
        try:
            return os.path.relpath(value, workspace_root)
        except Exception:
            return value

    read_opts = [f"-I{rel_to_workspace(d)}" for d in inc_dirs]
    read_files = [yosys_quote(rel_to_workspace(f)) for f in verilog_files]
    script_parts = [f"read_verilog {' '.join(read_opts + read_files)}"]
    script_parts.append(f"hierarchy {hierarchy_flag} -top {module}")
    script_parts.append("prep -top %s" % module)
    script_parts.append(f"write_json {yosys_quote(rel_to_workspace(cache_path))}")
    yosys_script = "; ".join(script_parts)

    try:
        proc = subprocess.run(
            [yosys_bin, "-q", "-p", yosys_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            cwd=workspace_root,
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
    ports_data = mod_data.get("ports", {})
    cells_data = mod_data.get("cells", {})
    ports = len(ports_data)
    cells = len(cells_data)

    # Build compact netlist_summary for agent consumption.
    # Top-level port list with direction and bit-width.
    top_ports = []
    for pname, pinfo in ports_data.items():
        if isinstance(pinfo, dict):
            bits = pinfo.get("bits", [])
            top_ports.append({
                "name": pname,
                "direction": pinfo.get("direction", "input"),
                "width": len(bits) if isinstance(bits, list) else 1,
            })

    # Compact named-net preview. The full bit-level connectivity remains in
    # yosys_json; this summary gives the agent a cheap reality check.
    netnames_data = mod_data.get("netnames", {})
    named_nets = []
    if isinstance(netnames_data, dict):
        for nname, ninfo in list(netnames_data.items())[:80]:
            if not isinstance(ninfo, dict):
                continue
            if str(ninfo.get("hide_name", "0")) == "1":
                continue
            bits = ninfo.get("bits", [])
            named_nets.append({
                "name": nname,
                "width": len(bits) if isinstance(bits, list) else 1,
                "bits": bits[:16] if isinstance(bits, list) else [],
                "truncated": isinstance(bits, list) and len(bits) > 16,
            })

    # Cell type histogram.
    cell_type_hist: dict = {}
    for cinfo in cells_data.values():
        if isinstance(cinfo, dict):
            ct = cinfo.get("type", "unknown")
            cell_type_hist[ct] = cell_type_hist.get(ct, 0) + 1

    # Per-cell connection table (capped at 80 cells to stay token-friendly).
    cell_connections = []
    for cname, cinfo in list(cells_data.items())[:80]:
        if not isinstance(cinfo, dict):
            continue
        conn = cinfo.get("connections", {})
        dirs = cinfo.get("port_directions", {})
        pins = []
        for pn, bits in (conn.items() if isinstance(conn, dict) else []):
            pins.append({
                "pin": pn,
                "direction": dirs.get(pn, "input") if isinstance(dirs, dict) else "input",
                "width": len(bits) if isinstance(bits, list) else 1,
            })
        cell_connections.append({
            "instance": cname,
            "type": cinfo.get("type", "unknown"),
            "pins": pins,
        })

    netlist_summary = {
        "top_ports": top_ports,
        "named_net_count": len(netnames_data) if isinstance(netnames_data, dict) else 0,
        "named_nets": named_nets,
        "cell_type_histogram": cell_type_hist,
        "total_cells": cells,
        "cell_connections": cell_connections,
        "note": "cell_connections capped at 80; full netlist in yosys_json" if cells > 80 else None,
    }

    # Complex-chip guardrail: do not ship huge Yosys JSON into the browser/model.
    # The full JSON remains cached on disk for future trace/subgraph APIs.
    named_net_count = len(netnames_data) if isinstance(netnames_data, dict) else 0
    large_design = cells > full_json_cell_limit or named_net_count > full_json_net_limit
    netlist_health = _schematic_health(mod_data)
    netlist_summary["health"] = netlist_health
    netlist_summary["complexity"] = {
        "large_design": large_design,
        "full_json_cell_limit": full_json_cell_limit,
        "full_json_net_limit": full_json_net_limit,
        "recommended_mode": "summary_trace_subgraph" if large_design else "interactive_full_graph",
    }

    if large_design:
        result = json.dumps({
            "available": True,
            "large_design": True,
            "module": module,
            "ports": ports,
            "cells": cells,
            "netlist_summary": netlist_summary,
            "cache_ref": os.path.relpath(cache_path, workspace_root),
            "reason": (
                "Large schematic: full Yosys JSON kept in local cache; returning compact "
                "health/index summary to avoid browser/model overload."
            ),
        })
        try:
            with open(cache_path, "w", encoding="utf-8") as cf:
                cf.write(result)
        except Exception:
            pass
        return result

    result = json.dumps({
        "available": True,
        "large_design": False,
        "module": module,
        "yosys_json": yosys_json,
        "ports": ports,
        "cells": cells,
        "netlist_summary": netlist_summary,
    })

    try:
        with open(cache_path, "w", encoding="utf-8") as cf:
            cf.write(result)
    except Exception:
        pass
    return result


def _schematic_health(mod_data: dict) -> dict:
    ports_data = mod_data.get("ports", {}) if isinstance(mod_data, dict) else {}
    cells_data = mod_data.get("cells", {}) if isinstance(mod_data, dict) else {}
    netnames_data = mod_data.get("netnames", {}) if isinstance(mod_data, dict) else {}

    drivers: dict[str, list[str]] = {}
    loads: dict[str, list[str]] = {}
    constants: dict[str, int] = {}

    def bit_key(bit) -> str:
        return str(bit)

    def is_const(bit) -> bool:
        return isinstance(bit, str) and bit.lower() in {"0", "1", "x", "z"}

    def add(mapping: dict[str, list[str]], bit, endpoint: str):
        key = bit_key(bit)
        mapping.setdefault(key, []).append(endpoint)

    for pname, pinfo in ports_data.items():
        if not isinstance(pinfo, dict):
            continue
        direction = str(pinfo.get("direction", "input"))
        bits = pinfo.get("bits", [])
        if not isinstance(bits, list):
            continue
        for bit in bits:
            if is_const(bit):
                constants[str(bit).lower()] = constants.get(str(bit).lower(), 0) + 1
                continue
            if direction == "input":
                add(drivers, bit, f"port:{pname}")
            elif direction == "output":
                add(loads, bit, f"port:{pname}")
            else:
                add(drivers, bit, f"port:{pname}")
                add(loads, bit, f"port:{pname}")

    for cname, cinfo in cells_data.items():
        if not isinstance(cinfo, dict):
            continue
        dirs = cinfo.get("port_directions", {})
        conns = cinfo.get("connections", {})
        if not isinstance(dirs, dict) or not isinstance(conns, dict):
            continue
        for pin, bits in conns.items():
            if not isinstance(bits, list):
                continue
            direction = str(dirs.get(pin, "input"))
            for bit in bits:
                if is_const(bit):
                    constants[str(bit).lower()] = constants.get(str(bit).lower(), 0) + 1
                    if direction in {"input", "inout"}:
                        add(loads, bit, f"{cname}.{pin}")
                    continue
                if direction in {"input", "inout"}:
                    add(loads, bit, f"{cname}.{pin}")
                if direction in {"output", "inout"}:
                    add(drivers, bit, f"{cname}.{pin}")

    all_bits = set(drivers) | set(loads)
    undriven = []
    unloaded = []
    multidriven = []
    high_fanout = []
    for bit in sorted(all_bits, key=lambda v: (len(v), v)):
        driver_count = len(drivers.get(bit, []))
        load_count = len(loads.get(bit, []))
        if driver_count == 0 and load_count > 0:
            undriven.append({"bit": bit, "loads": loads.get(bit, [])[:8], "load_count": load_count})
        if load_count == 0 and driver_count > 0:
            unloaded.append({"bit": bit, "drivers": drivers.get(bit, [])[:8], "driver_count": driver_count})
        if driver_count > 1:
            multidriven.append({"bit": bit, "drivers": drivers.get(bit, [])[:8], "driver_count": driver_count})
        if load_count >= 32:
            high_fanout.append({"bit": bit, "fanout": load_count, "driver": (drivers.get(bit, []) or ["unknown"])[0]})

    high_fanout.sort(key=lambda item: item["fanout"], reverse=True)
    return {
        "bit_count": len(all_bits),
        "named_net_count": len(netnames_data) if isinstance(netnames_data, dict) else 0,
        "constant_bit_uses": dict(sorted(constants.items())),
        "undriven_count": len(undriven),
        "unloaded_count": len(unloaded),
        "multidriven_count": len(multidriven),
        "high_fanout_count": len(high_fanout),
        "undriven_preview": undriven[:20],
        "unloaded_preview": unloaded[:20],
        "multidriven_preview": multidriven[:20],
        "high_fanout_preview": high_fanout[:20],
    }


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


def rtl_repair_diagnose_tool(path: str, workspace_root: str) -> str:
    """Run RTL lint and return compact, categorized repair guidance."""
    full = _safe_workspace_path(path, workspace_root)
    if not full or not os.path.isfile(full):
        return json.dumps({
            "status": "failed",
            "stage": "lint",
            "path": path,
            "diagnostics": [{
                "severity": "error",
                "category": "file_not_found",
                "line": 1,
                "message": f"File not found: {path}",
                "hint": "Check the RTL path and run the diagnosis on an existing .v/.sv file.",
                "next_action": "inspect_path",
            }],
        }, indent=2)

    try:
        available_linter = next((tool for tool in ("vlogan", "xmvlog", "vlog", "verilator", "iverilog") if shutil.which(tool)), None)
        if available_linter:
            lint = json.loads(lint_file_tool(path, workspace_root))
            linter_engine = available_linter
        else:
            lint = {"errors": _builtin_rtl_lint(full, path)}
            linter_engine = "agentic_builtin_static"
    except Exception as exc:
        return json.dumps({
            "status": "failed",
            "stage": "lint",
            "path": path,
            "diagnostics": [{
                "severity": "error",
                "category": "lint_runner_error",
                "line": None,
                "message": f"Could not run or parse lint output: {exc}",
                "hint": "Inspect local lint tool availability and rerun the diagnosis.",
                "next_action": "inspect_toolchain",
            }],
        }, indent=2)

    errors = lint.get("errors") if isinstance(lint, dict) else []
    diagnostics = []
    for item in (errors or [])[:12]:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "")
        line = _safe_int(item.get("line"))
        category, hint, next_action = _categorize_rtl_lint_message(message)
        diagnostics.append({
            "severity": item.get("severity") or "error",
            "category": category,
            "file": path,
            "line": line,
            "message": message,
            "nearby_code": _nearby_code(full, line),
            "hint": hint,
            "next_action": next_action,
        })

    status = "clean" if not diagnostics else "failed"
    primary = diagnostics[0] if diagnostics else None
    return json.dumps({
        "status": status,
        "stage": "lint",
        "path": path,
        "linter_engine": linter_engine,
        "diagnostic_count": len(diagnostics),
        "primary_category": primary.get("category") if primary else None,
        "primary_hint": primary.get("hint") if primary else "RTL lint is clean.",
        "repair_policy": "Use surgical edits for the reported line/context. Rerun rtl_repair_diagnose after each edit. Escalate to block rewrite only if the same category persists after two surgical attempts.",
        "diagnostics": diagnostics,
    }, indent=2)


def _categorize_rtl_lint_message(message: str) -> tuple[str, str, str]:
    text = (message or "").lower()
    rules = [
        (
            "undeclared_signal",
            (r"not declared", r"undeclared", r"can't find definition", r"cannot find.*definition", r"unknown identifier", r"identifier .* undefined", r"used before declaration", r"not visible in this scope", r"not found"),
            "Declare the signal in the correct scope or fix a typo in the signal/port name.",
            "surgical_edit_declaration_or_name",
        ),
        (
            "width_mismatch",
            (r"width", r"expects .* bits", r"generate[s]? .* bits", r"truncat", r"extend", r"size mismatch", r"port size"),
            "Match bit widths by fixing declarations, adding explicit slices, or using explicit zero/sign extension.",
            "surgical_edit_width",
        ),
        (
            "syntax_error",
            (r"syntax error", r"unexpected", r"parse error", r"unexpected token", r"near unexpected", r"unexpected end"),
            "Fix the local Verilog syntax near the reported line: punctuation, begin/end balance, declarations, or statement form.",
            "surgical_edit_syntax",
        ),
        (
            "inferred_latch",
            (r"latch", r"not assigned in all", r"incomplete assignment"),
            "Add default assignments at the start of the combinational block, or add missing else/default branches.",
            "surgical_edit_comb_defaults",
        ),
        (
            "blocking_in_sequential",
            (r"blocking", r"blocked assignment", r"use nonblocking", r"non-blocking"),
            "Use non-blocking <= assignments for registers inside clocked sequential blocks.",
            "surgical_edit_assignment",
        ),
        (
            "multiple_driver",
            (r"multiple driver", r"multidriven", r"driven from multiple", r"also assigned", r"conflicting drivers"),
            "Ensure each net/register has one driver; remove duplicate assign/always drivers or split signals.",
            "inspect_drivers",
        ),
        (
            "port_mismatch",
            (r"port .*not", r"pin .*not", r"too few port", r"too many port", r"missing port", r"does not exist in module"),
            "Check the instance port map against the child module declaration and fix names or connections.",
            "surgical_edit_instance_ports",
        ),
        (
            "missing_module",
            (r"module .*not found", r"can't resolve module", r"unknown module", r"cannot find module", r"cell .*not found"),
            "Add the missing RTL file to the file list/include path, or correct the instantiated module name.",
            "inspect_filelist_or_module_name",
        ),
        (
            "non_synthesizable_construct",
            (r"delay", r"initial", r"\$display", r"\$finish", r"\$random", r"unsupported.*synth"),
            "Move simulation-only constructs to the testbench or replace them with synthesizable logic.",
            "surgical_edit_synthesizability",
        ),
        (
            "reset_issue",
            (r"reset", r"uninitialized", r"not initialized", r"no reset"),
            "Add or fix the reset branch so every sequential register has a deterministic reset value.",
            "surgical_edit_reset",
        ),
    ]
    for category, patterns, hint, next_action in rules:
        if any(re.search(pattern, text) for pattern in patterns):
            return category, hint, next_action
    return (
        "unknown",
        "Inspect the reported line and nearby code. Preserve intended behavior and make the smallest fix that satisfies the lint tool.",
        "inspect_line",
    )


def _builtin_rtl_lint(full_path: str, rel_path: str) -> list[dict[str, Any]]:
    """Small dependency-free RTL checker used when no external linter exists."""
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as exc:
        return [{"severity": "error", "line": 1, "message": f"Could not read RTL file: {exc}"}]

    errors: list[dict[str, Any]] = []
    stripped = _strip_sv_comments_preserve_lines(content)
    lines = stripped.splitlines()

    modules = list(re.finditer(r"(?m)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", stripped))
    endmodules = list(re.finditer(r"(?m)^\s*endmodule\b", stripped))
    if not modules:
        errors.append({"severity": "error", "line": 1, "message": "No Verilog module declaration found."})
    if len(modules) != len(endmodules):
        line = _offset_to_line(stripped, modules[-1].start() if modules else 0)
        errors.append({
            "severity": "error",
            "line": line,
            "message": f"module/endmodule mismatch: found {len(modules)} module and {len(endmodules)} endmodule token(s).",
        })
    if len(modules) > 1:
        errors.append({
            "severity": "warning",
            "line": _offset_to_line(stripped, modules[1].start()),
            "message": "Multiple modules in one file; AgentIC RTL policy expects one module per file.",
        })

    block_pairs = (
        ("begin", "end"),
        ("case", "endcase"),
        ("function", "endfunction"),
        ("task", "endtask"),
        ("generate", "endgenerate"),
    )
    for open_token, close_token in block_pairs:
        opens = list(re.finditer(rf"\b{open_token}\b", stripped))
        closes = list(re.finditer(rf"\b{close_token}\b", stripped))
        if len(opens) != len(closes):
            line = _offset_to_line(stripped, (opens[-1].start() if opens else closes[-1].start() if closes else 0))
            errors.append({
                "severity": "error",
                "line": line,
                "message": f"{open_token}/{close_token} mismatch: found {len(opens)} {open_token} and {len(closes)} {close_token}.",
            })

    for char_open, char_close, name in (("(", ")", "parenthesis"), ("[", "]", "bracket")):
        balance = 0
        first_bad_line = None
        for idx, line in enumerate(lines, start=1):
            balance += line.count(char_open)
            balance -= line.count(char_close)
            if balance < 0 and first_bad_line is None:
                first_bad_line = idx
        if balance != 0:
            errors.append({
                "severity": "error",
                "line": first_bad_line or len(lines) or 1,
                "message": f"Unbalanced {name} tokens: `{char_open}` and `{char_close}` counts do not match.",
            })

    try:
        quality = evaluate_rtl_quality(rel_path, content, None)
        for issue in quality.issues:
            errors.append({
                "severity": issue.severity,
                "line": _line_from_message(issue.message),
                "message": f"{issue.code}: {issue.message}",
            })
    except Exception:
        pass

    errors.extend(_builtin_undeclared_signal_checks(stripped))
    return _dedupe_builtin_errors(errors)[:24]


def _strip_sv_comments_preserve_lines(content: str) -> str:
    text = re.sub(r"//.*", "", content or "")

    def repl(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")

    return re.sub(r"/\*.*?\*/", repl, text, flags=re.S)


def _builtin_undeclared_signal_checks(content: str) -> list[dict[str, Any]]:
    declared: set[str] = set()
    module_names: set[str] = set()
    instance_names: set[str] = set()
    errors: list[dict[str, Any]] = []

    for match in re.finditer(r"(?m)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", content or ""):
        module_names.add(match.group(1))
        declared.add(match.group(1))
    for match in re.finditer(r"\b(?:input|output|inout|wire|reg|logic|integer|genvar|parameter|localparam)\b\s*(?:signed\s*)?(?:\[[^\]]+\]\s*)?([^;,)]+)", content or ""):
        for piece in re.split(r",", match.group(1)):
            name = re.sub(r"=.*$", "", piece).strip()
            name = re.sub(r"\[[^\]]+\]", "", name).strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
                declared.add(name)
    for match in re.finditer(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(?:#\s*\([^;]*?\)\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*\(", content or ""):
        module_type, instance_name = match.group(1), match.group(2)
        if module_type not in _SV_KEYWORDS:
            instance_names.add(instance_name)
            declared.add(instance_name)

    expressions: list[tuple[int, str]] = []
    for idx, line in enumerate((content or "").splitlines(), start=1):
        clean = line.strip()
        if not clean:
            continue
        assign = re.search(r"(?:assign\s+)?[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?\s*(?:<=|=)\s*(.+?);?\s*$", clean)
        if assign:
            expressions.append((idx, assign.group(1)))
            lhs = re.match(r"(?:assign\s+)?([A-Za-z_][A-Za-z0-9_$]*)", clean)
            if lhs:
                declared.add(lhs.group(1))
            continue
        condition = re.search(r"\b(?:if|case)\s*\((.*?)\)", clean)
        if condition:
            expressions.append((idx, condition.group(1)))

    seen: set[tuple[int, str]] = set()
    for line, expr in expressions:
        for ident in re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", expr):
            if ident in declared or ident in module_names or ident in instance_names or ident in _SV_KEYWORDS:
                continue
            if ident.startswith("$"):
                continue
            key = (line, ident)
            if key in seen:
                continue
            seen.add(key)
            errors.append({
                "severity": "error",
                "line": line,
                "message": f"Identifier `{ident}` is used before declaration or is not visible in this scope.",
            })
    return errors[:12]


def _offset_to_line(content: str, offset: int) -> int:
    return (content or "")[:max(0, offset)].count("\n") + 1


def _line_from_message(message: str) -> int | None:
    match = re.search(r"\bline\s+(\d+)\b", message or "", re.I)
    return int(match.group(1)) if match else None


def _dedupe_builtin_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any, Any]] = set()
    result: list[dict[str, Any]] = []
    for item in errors:
        key = (item.get("severity"), item.get("line"), item.get("message"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


_SV_KEYWORDS = {
    "always", "always_comb", "always_ff", "assign", "begin", "case", "casex", "casez", "default",
    "else", "end", "endcase", "endfunction", "endmodule", "endtask", "for", "function", "generate",
    "if", "inout", "input", "integer", "localparam", "logic", "module", "negedge", "or", "output",
    "parameter", "posedge", "reg", "signed", "task", "wire", "while",
}


def _nearby_code(full_path: str, line: int | None, radius: int = 3) -> list[dict[str, Any]]:
    if not line or line < 1:
        return []
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    return [{"line": idx, "text": lines[idx - 1]} for idx in range(start, end + 1)]


def _safe_int(value) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


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
    auto_repair_diagnosis = None
    if not verdict.get("pass"):
        auto_repair_diagnosis = _auto_rtl_repair_diagnosis_for_failure(command, raw_log, workspace_root)
    try:
        DesignStateStore(workspace_root, design_name).record_checkpoint(
            eda_tool,
            stage or "execution",
            verdict,
        )
    except Exception:
        pass

    response = {
        "bash_exit_code": exit_code,
        "checkpoint_verdict": verdict,
        "log_snippet": truncated_log
    }
    if auto_repair_diagnosis:
        response["auto_repair_diagnosis"] = auto_repair_diagnosis
    return json.dumps(response, indent=2)


def flow_command_for_target(
    command: str,
    workspace_root: str,
    target: str = "native",
    *,
    wsl_distro: str = "",
    docker_image: str = "",
    docker_args: str = "",
    linux_workdir: str = "",
) -> tuple[bool, str, str]:
    """Wrap an EDA command for one of AgentIC's three execution targets."""
    target = (target or "native").strip().lower()
    if target == "native":
        return True, command, ""
    if target == "wsl":
        distro_args = f"-d {shlex.quote(wsl_distro)} " if wsl_distro else ""
        workdir = linux_workdir.strip()
        inner = f"cd {shlex.quote(workdir)} && {command}" if workdir else command
        return True, f"wsl {distro_args}-- bash -lc {shlex.quote(inner)}", ""
    if target == "docker":
        if not docker_image.strip():
            return False, command, "docker target requires docker_image"
        root = os.path.abspath(os.path.expanduser(os.path.expandvars(workspace_root or ".")))
        volume = f"{root}:/workspace"
        extra = docker_args.strip()
        extra_part = f"{extra} " if extra else ""
        wrapped = (
            f"docker run --rm {extra_part}"
            f"-v {shlex.quote(volume)} -w /workspace "
            f"{shlex.quote(docker_image.strip())} bash -lc {shlex.quote(command)}"
        )
        return True, wrapped, ""
    return False, command, f"unknown execution target '{target}'. Use native, wsl, or docker."


def _auto_rtl_repair_diagnosis_for_failure(command: str, raw_log: str, workspace_root: str) -> dict[str, Any] | None:
    """Attach RTL repair guidance to failed checkpointed EDA runs when possible."""
    rtl_path = _extract_rtl_path_from_text(f"{raw_log}\n{command}", workspace_root)
    if not rtl_path:
        return None
    try:
        diagnosis = json.loads(rtl_repair_diagnose_tool(rtl_path, workspace_root))
    except Exception:
        return None
    if not isinstance(diagnosis, dict):
        return None
    return {
        "trigger": "failed_checkpoint",
        "path": rtl_path,
        "status": diagnosis.get("status"),
        "linter_engine": diagnosis.get("linter_engine"),
        "primary_category": diagnosis.get("primary_category"),
        "primary_hint": diagnosis.get("primary_hint"),
        "repair_policy": diagnosis.get("repair_policy"),
        "diagnostics": (diagnosis.get("diagnostics") or [])[:5],
    }


def _extract_rtl_path_from_text(text: str, workspace_root: str) -> str | None:
    candidates: list[str] = []
    for match in re.finditer(r"(?<![A-Za-z0-9_$./-])([A-Za-z0-9_./~$ -]+?\.(?:sv|v|vh|svh))(?=$|[\s:),;])", text or "", re.I):
        raw = match.group(1).strip().strip("'\"")
        if not raw:
            continue
        # Skip obvious generated/system paths that are not user RTL.
        lower = raw.lower().replace("\\", "/")
        if any(part in lower for part in ("/tb/", "/testbench/", "/sim/", "/simulation/", "/.agentic/")):
            continue
        candidates.append(raw)
    candidates.sort(key=lambda item: ("/rtl/" not in "/" + item.lower().replace("\\", "/"), len(item)))
    for candidate in candidates[:20]:
        full = _safe_workspace_path(candidate, workspace_root)
        if full and os.path.isfile(full):
            return os.path.relpath(full, workspace_root).replace("\\", "/")
    return None


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
        "execution_targets": _execution_targets_for_agent(env),
        "flow_decision": {
            "profile": (env.get("recommended_flow") or {}).get("profile"),
            "backend": (env.get("recommended_flow") or {}).get("backend"),
            "confidence": (env.get("recommended_flow") or {}).get("confidence"),
            "blockers": ((env.get("recommended_flow") or {}).get("blockers") or [])[:6],
        },
        "conflict_counts": conflicts.get("counts") or {},
        "next_actions": _eda_next_actions(stage_context, env, conflicts),
    }


def _execution_targets_for_agent(env: dict) -> dict:
    wsl = env.get("wsl") or {}
    docker_images = env.get("docker_images") or []
    return {
        "options": ["native", "wsl", "docker"],
        "default": "native",
        "native": {
            "available": True,
            "label": "Native",
            "description": "Run commands on the current OS/PATH.",
        },
        "wsl": {
            "available": bool(wsl.get("available")),
            "label": "WSL",
            "distros": (wsl.get("distros") or [])[:8],
            "capabilities": env.get("wsl_capabilities") or {},
        },
        "docker": {
            "available": bool((env.get("tools") or {}).get("docker")),
            "label": "Docker",
            "image_count": len(docker_images),
            "images": docker_images[:12],
        },
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


def design_contract_tool(action: str, workspace_root: str, design_name: str = "scratch") -> str:
    action = (action or "get").strip().lower()
    store = DesignStateStore(workspace_root, design_name)
    if action == "infer":
        contract = _infer_design_contract(workspace_root, design_name)
        store.set_design_contract(contract)
        try:
            store.record_evidence("design_contract", contract.get("top_module") or "unknown", contract)
        except Exception:
            pass
        return json.dumps(contract, indent=2, default=str)
    if action == "validate":
        state = store.load()
        contract = state.get("design_contract") if isinstance(state.get("design_contract"), dict) else None
        if not contract:
            contract = _infer_design_contract(workspace_root, design_name)
        contract = _validate_design_contract(contract, workspace_root, design_name)
        store.set_design_contract(contract)
        try:
            store.record_evidence("design_contract_validation", contract.get("top_module") or "unknown", {
                "status": (contract.get("status") or {}).get("contract"),
                "issues": contract.get("validation_issues") or [],
                "next_actions": contract.get("next_actions") or [],
            })
        except Exception:
            pass
        return json.dumps(contract, indent=2, default=str)
    if action == "get":
        state = store.load()
        contract = state.get("design_contract") if isinstance(state.get("design_contract"), dict) else None
        if not contract:
            return json.dumps({
                "schema_version": "agentic.design_contract.v1",
                "status": {"contract": "missing"},
                "message": "No active design contract is stored yet. Run agentic_design_contract(action='infer') first.",
                "next_actions": ["Run design_contract infer", "Then run design_contract validate"],
            }, indent=2)
        return json.dumps(_compact_design_contract(contract), indent=2, default=str)
    return f"Error: unknown design_contract action '{action}'. Use infer, validate, or get."


def _infer_design_contract(workspace_root: str, design_name: str) -> dict[str, Any]:
    rtl_files = _find_rtl_files(workspace_root)
    modules: dict[str, dict[str, Any]] = {}
    referenced: set[str] = set()
    for rel_path in rtl_files:
        full = os.path.join(workspace_root, rel_path)
        try:
            content = open(full, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for module in _parse_rtl_modules_for_contract(content, rel_path):
            modules[module["name"]] = module
            referenced.update(inst.get("module") for inst in module.get("instances", []) if inst.get("module"))

    top_module = _select_top_module(modules, referenced)
    top = modules.get(top_module or "") if top_module else None
    sdc_files = _find_sdc_files(workspace_root)
    clock_ports = _clock_ports(top.get("ports", []) if top else [])
    reset_ports = _reset_ports(top.get("ports", []) if top else [])
    sdc_clock_defined, sdc_clock_names = _sdc_clock_status(workspace_root, sdc_files, clock_ports)
    stage_status = _contract_stage_status(workspace_root, design_name)
    validation_issues = []
    if not top_module:
        validation_issues.append(_contract_issue("top_missing", "error", "No top module candidate could be inferred from RTL."))

    contract = {
        "schema_version": "agentic.design_contract.v1",
        "generated_at": time.time(),
        "design_name": design_name,
        "top_module": top_module,
        "top_file": top.get("file") if top else None,
        "clock_ports": clock_ports,
        "reset_ports": reset_ports,
        "io_ports": [p["name"] for p in (top.get("ports", []) if top else [])],
        "submodules": sorted({inst["module"] for inst in (top.get("instances", []) if top else []) if inst.get("module")}),
        "rtl_files": rtl_files[:200],
        "module_count": len(modules),
        "modules": {name: _compact_contract_module(module) for name, module in sorted(modules.items())},
        "constraints": {
            "sdc_files": sdc_files[:50],
            "clock_defined": sdc_clock_defined,
            "clock_names": sdc_clock_names,
        },
        "policy": {
            "top_structural_only": True,
            "one_module_per_file": True,
            "constraints_required": True,
            "no_behavioral_large_memory": True,
        },
        "status": {
            "contract": "draft" if validation_issues else "inferred",
            "top_valid": bool(top_module),
            "sdc_clock": "present" if sdc_clock_defined else "missing",
            **stage_status,
        },
        "validation_issues": validation_issues,
        "next_actions": [],
    }
    contract["next_actions"] = _contract_next_actions(contract)
    contract["compact_for_agent"] = _contract_compact_text(contract)
    return contract


def _validate_design_contract(contract: dict[str, Any], workspace_root: str, design_name: str) -> dict[str, Any]:
    inferred = _infer_design_contract(workspace_root, design_name)
    top_module = contract.get("top_module") or inferred.get("top_module")
    top_file = contract.get("top_file") or inferred.get("top_file")
    merged = dict(inferred)
    if top_module and top_module in (inferred.get("modules") or {}):
        merged["top_module"] = top_module
        merged["top_file"] = top_file or (inferred.get("modules") or {}).get(top_module, {}).get("file")

    issues = list(merged.get("validation_issues") or [])
    modules = merged.get("modules") or {}
    top = modules.get(merged.get("top_module") or "")
    if not top:
        issues.append(_contract_issue("top_missing", "error", "Active top module does not exist in scanned RTL."))
    else:
        if top.get("always_count", 0) > 0 or top.get("nontrivial_assign_count", 0) > 2:
            issues.append(_contract_issue(
                "top_not_structural",
                "error",
                f"Top module `{merged.get('top_module')}` has {top.get('always_count')} always block(s) and {top.get('nontrivial_assign_count')} non-trivial assign(s).",
                top.get("file"),
            ))
        missing_children = [
            name for name in (merged.get("submodules") or [])
            if name not in modules and not _contract_external_module_ok(name)
        ]
        for child in missing_children[:12]:
            issues.append(_contract_issue("missing_submodule", "warning", f"Top instantiates `{child}`, but no local RTL module was found. It may be an external macro/IP.", top.get("file")))
        if not merged.get("clock_ports"):
            issues.append(_contract_issue("clock_missing", "warning", "No obvious top clock port was detected."))
        if not merged.get("reset_ports"):
            issues.append(_contract_issue("reset_missing", "warning", "No obvious top reset port was detected."))
        if not (merged.get("constraints") or {}).get("clock_defined"):
            issues.append(_contract_issue("sdc_clock_missing", "warning", "No SDC create_clock matching the top clock was found."))
        full = os.path.join(workspace_root, top.get("file") or "")
        if os.path.isfile(full):
            try:
                quality = evaluate_rtl_quality(top.get("file") or "", open(full, "r", encoding="utf-8", errors="replace").read(), None)
                for issue in quality.issues:
                    if issue.code in {"logic_in_top_module", "multiple_modules_in_file", "non_synthesizable_in_design", "behavioral_memory_in_design"}:
                        issues.append(_contract_issue(issue.code, issue.severity, issue.message, top.get("file")))
            except Exception:
                pass

    deduped = _dedupe_contract_issues(issues)
    merged["validation_issues"] = deduped
    errors = [item for item in deduped if item.get("severity") == "error"]
    warnings = [item for item in deduped if item.get("severity") == "warning"]
    merged["status"]["contract"] = "invalid" if errors else "valid_with_warnings" if warnings else "valid"
    merged["status"]["top_valid"] = not any(item.get("code") in {"top_missing", "top_not_structural"} and item.get("severity") == "error" for item in deduped)
    merged["next_actions"] = _contract_next_actions(merged)
    merged["compact_for_agent"] = _contract_compact_text(merged)
    merged["validated_at"] = time.time()
    return merged


def _find_rtl_files(workspace_root: str) -> list[str]:
    results: list[str] = []
    exclude = {".git", ".agentic", "node_modules", "out", "dist", "build", ".venv", "venv", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [d for d in dirnames if d not in exclude]
        rel_dir = os.path.relpath(dirpath, workspace_root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        if any(part in f"/{rel_dir.lower()}/" for part in ("/sim/", "/tb/", "/testbench/", "/verification/")):
            continue
        for filename in filenames:
            if filename.lower().endswith((".v", ".sv")):
                rel = os.path.join(rel_dir, filename).replace("\\", "/").lstrip("/")
                results.append(rel)
    results.sort(key=lambda p: ("/rtl/" not in f"/{p.lower()}", p))
    return results[:300]


def _find_sdc_files(workspace_root: str) -> list[str]:
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [d for d in dirnames if d not in {".git", ".agentic", "node_modules", "out", "dist", "build", ".venv", "venv"}]
        for filename in filenames:
            if filename.lower().endswith(".sdc"):
                results.append(os.path.relpath(os.path.join(dirpath, filename), workspace_root).replace("\\", "/"))
    return sorted(results)[:100]


def _parse_rtl_modules_for_contract(content: str, rel_path: str) -> list[dict[str, Any]]:
    text = _strip_sv_comments_preserve_lines(content or "")
    modules = []
    for match in re.finditer(r"(?ms)\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b(.*?)\bendmodule\b", text):
        name = match.group(1)
        body = match.group(2)
        header = body.split(";", 1)[0] if ";" in body else body[:1000]
        ports = _contract_ports(header + "\n" + body)
        instances = _contract_instances(body)
        always_count = len(re.findall(r"(?m)^\s*always(?:_[a-z]+)?\b", body))
        modules.append({
            "name": name,
            "file": rel_path,
            "ports": ports,
            "instances": instances,
            "always_count": always_count,
            "assign_count": len(re.findall(r"(?m)^\s*assign\s+", body)),
            "nontrivial_assign_count": _contract_nontrivial_assign_count(body),
            "line": _offset_to_line(text, match.start()),
        })
    return modules


def _contract_ports(text: str) -> list[dict[str, Any]]:
    ports: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r"\b(input|output|inout)\b\s+(?:wire|reg|logic)?\s*(?:signed\s*)?(?P<width>\[[^\]]+\])?\s*(?P<name>[A-Za-z_][A-Za-z0-9_$]*)", re.I)
    for match in pattern.finditer(text or ""):
        direction = match.group(1).lower()
        width = (match.group("width") or "1").strip()
        name = match.group("name")
        if name.lower() not in {"input", "output", "inout", "wire", "reg", "logic"}:
            ports[name] = {"name": name, "direction": direction, "width": width, "role": _port_role(name)}
    return list(ports.values())[:256]


def _contract_instances(body: str) -> list[dict[str, str]]:
    instances = []
    skip = _SV_KEYWORDS | {"assign", "always", "initial"}
    for match in re.finditer(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\([^;]*?\)\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*\(", body or ""):
        module_type, instance_name = match.group(1), match.group(2)
        if module_type in skip or instance_name in skip:
            continue
        instances.append({"module": module_type, "instance": instance_name})
    return instances[:256]


def _contract_nontrivial_assign_count(body: str) -> int:
    count = 0
    for match in re.finditer(r"(?m)^\s*assign\s+[^=]+=\s*([^;]+);", body or ""):
        rhs = match.group(1).strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?$", rhs):
            count += 1
    return count


def _select_top_module(modules: dict[str, dict[str, Any]], referenced: set[str]) -> str | None:
    if not modules:
        return None
    candidates = [name for name in modules if name not in referenced] or list(modules)
    def score(name: str) -> tuple[int, str]:
        module = modules[name]
        lower_name = name.lower()
        lower_file = (module.get("file") or "").lower()
        value = 0
        if lower_name in {"chip_top", "soc_top", "top"}:
            value += 100
        if lower_name.endswith("_top") or lower_name.endswith("top"):
            value += 60
        if "/top/" in f"/{lower_file}" or lower_file.endswith("_top.v") or lower_file.endswith("_top.sv"):
            value += 40
        value += min(len(module.get("instances") or []), 20)
        value += min(len(module.get("ports") or []), 20)
        return (value, name)
    return max(candidates, key=score)


def _clock_ports(ports: list[dict[str, Any]]) -> list[str]:
    return [p["name"] for p in ports if p.get("direction") == "input" and re.search(r"(?i)(^clk$|clock|clk_?|_clk)", p.get("name") or "")][:16]


def _reset_ports(ports: list[dict[str, Any]]) -> list[str]:
    return [p["name"] for p in ports if p.get("direction") == "input" and re.search(r"(?i)(rst|reset)", p.get("name") or "")][:16]


def _port_role(name: str) -> str:
    lower = (name or "").lower()
    if re.search(r"(^clk$|clock|clk_?|_clk)", lower):
        return "clock"
    if "rst" in lower or "reset" in lower:
        return "reset"
    if "valid" in lower or "ready" in lower:
        return "handshake"
    if "addr" in lower:
        return "address"
    if "data" in lower:
        return "data"
    return "signal"


def _sdc_clock_status(workspace_root: str, sdc_files: list[str], clock_ports: list[str]) -> tuple[bool, list[str]]:
    clocks: list[str] = []
    for rel in sdc_files[:20]:
        try:
            text = open(os.path.join(workspace_root, rel), "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for match in re.finditer(r"(?i)create_clock\b[^\n;]*", text):
            line = match.group(0)
            clocks.extend(re.findall(r"\b(?:get_ports|get_pins)\s+([A-Za-z_][A-Za-z0-9_$]*)", line))
            for port in clock_ports:
                if re.search(rf"\b{re.escape(port)}\b", line):
                    clocks.append(port)
    unique = sorted(set(clocks))
    if not clock_ports:
        return bool(unique), unique
    return bool(set(clock_ports) & set(unique)), unique


def _contract_stage_status(workspace_root: str, design_name: str) -> dict[str, str]:
    state = DesignStateStore(workspace_root, design_name).load()
    statuses = {}
    stage_status = state.get("stage_status") or {}
    for stage in ("rtl", "lint", "simulation", "synthesis", "sta", "drc", "lvs", "signoff"):
        item = stage_status.get(stage) or {}
        statuses[stage] = item.get("status") or "missing"
    for checkpoint in state.get("checkpoints") or []:
        stage = str(checkpoint.get("stage") or "").lower()
        if not stage:
            continue
        mapped = "simulation" if "sim" in stage else "synthesis" if "synth" in stage else "sta" if "sta" in stage or "timing" in stage else "drc" if "drc" in stage else "lvs" if "lvs" in stage else "lint" if "lint" in stage else stage
        if mapped in statuses:
            statuses[mapped] = "passed" if checkpoint.get("pass") else "failed"
    return statuses


def _compact_contract_module(module: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": module.get("file"),
        "port_count": len(module.get("ports") or []),
        "ports": (module.get("ports") or [])[:40],
        "instances": (module.get("instances") or [])[:80],
        "always_count": module.get("always_count", 0),
        "assign_count": module.get("assign_count", 0),
        "nontrivial_assign_count": module.get("nontrivial_assign_count", 0),
        "line": module.get("line"),
    }


def _contract_issue(code: str, severity: str, message: str, path: str | None = None) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, "path": path}


def _dedupe_contract_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for issue in issues:
        key = (issue.get("code"), issue.get("severity"), issue.get("message"), issue.get("path"))
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result[:80]


def _contract_external_module_ok(name: str) -> bool:
    lowered = (name or "").lower()
    return lowered.startswith(("sky130_", "gf180", "asap7", "tsmc", "saed", "nangate")) or "__" in lowered


def _contract_next_actions(contract: dict[str, Any]) -> list[str]:
    actions = []
    status = contract.get("status") or {}
    if not contract.get("top_module"):
        actions.append("Select or create a top module, then rerun design_contract infer.")
    if status.get("top_valid") is False:
        actions.append("Fix top module structure: keep top as ports, wires, instances, and simple wiring only.")
    if status.get("sdc_clock") == "missing":
        actions.append("Create or update SDC with create_clock for the detected top clock.")
    if status.get("lint") in {"missing", "failed"} and contract.get("top_file"):
        actions.append(f"Run rtl_repair_diagnose on {contract.get('top_file')}.")
    if status.get("simulation") == "missing":
        actions.append("Run or create a self-checking simulation checkpoint.")
    if status.get("sta") in {"missing", "failed"}:
        actions.append("Run STA or inspect timing evidence before timing claims.")
    return actions[:8]


def _contract_compact_text(contract: dict[str, Any]) -> str:
    status = contract.get("status") or {}
    constraints = contract.get("constraints") or {}
    return (
        f"Design contract: top={contract.get('top_module') or 'unknown'} "
        f"file={contract.get('top_file') or 'unknown'} clocks={','.join(contract.get('clock_ports') or []) or 'none'} "
        f"resets={','.join(contract.get('reset_ports') or []) or 'none'} "
        f"submodules={len(contract.get('submodules') or [])} sdc_clock={constraints.get('clock_defined')} "
        f"contract_status={status.get('contract')} lint={status.get('lint')} sim={status.get('simulation')} "
        f"sta={status.get('sta')} drc={status.get('drc')} lvs={status.get('lvs')}."
    )


def _compact_design_contract(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": contract.get("schema_version"),
        "design_name": contract.get("design_name"),
        "top_module": contract.get("top_module"),
        "top_file": contract.get("top_file"),
        "clock_ports": contract.get("clock_ports") or [],
        "reset_ports": contract.get("reset_ports") or [],
        "io_port_count": len(contract.get("io_ports") or []),
        "submodules": (contract.get("submodules") or [])[:80],
        "rtl_file_count": len(contract.get("rtl_files") or []),
        "constraints": contract.get("constraints") or {},
        "policy": contract.get("policy") or {},
        "status": contract.get("status") or {},
        "validation_issues": (contract.get("validation_issues") or [])[:20],
        "next_actions": contract.get("next_actions") or [],
        "compact_for_agent": contract.get("compact_for_agent") or _contract_compact_text(contract),
    }


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
    elif name == "design_contract":
        return design_contract_tool(str(args.get("action") or "get"), workspace_root, design_name)
    elif name == "app_capability":
        return json.dumps(build_app_capability_contract(workspace_root, detect_environment()), indent=2, default=str)
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
        ok, command, error = flow_command_for_target(
            args["command"],
            workspace_root,
            args.get("target", "native"),
            wsl_distro=args.get("wsl_distro", ""),
            docker_image=args.get("docker_image", ""),
            docker_args=args.get("docker_args", ""),
            linux_workdir=args.get("linux_workdir", ""),
        )
        if not ok:
            return "Error: " + error
        return bash_tool(
            command,
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
    elif name == "ipython":
        code = args.get("code", "")
        from ipyt_harness import get_rlm_harness
        harness = get_rlm_harness(workspace_root=workspace_root)
        result = harness.execute_code_sync(code)
        out = []
        if result.get("stdout"):
            out.append(f"STDOUT:\n{result['stdout']}")
        if result.get("stderr"):
            out.append(f"STDERR:\n{result['stderr']}")
        if not out:
            out.append("Cell executed with no output.")
        out.append(f"\n[IPython Kernel Status: {'SUCCESS' if result.get('success') else 'FAILED'}, Active Scope: {', '.join(result.get('active_variables', [])[:10])}]")
        return "\n".join(out)
    elif name == "container":
        action = args.get("action", "status")
        from collab_container import get_container_manager
        mgr = get_container_manager(workspace_root=workspace_root)
        if action == "configure":
            cfg = mgr.configure_environment(design_name=design_name, docker_image=args.get("docker_image", "efabless/openlane:latest"))
            return f"Configured container environment for '{design_name}': {json.dumps(cfg.to_dict(), indent=2)}"
        elif action == "start":
            res = mgr.start_container(design_name=design_name)
            return json.dumps(res, indent=2)
        elif action == "stop":
            res = mgr.stop_container(design_name=design_name)
            return json.dumps(res, indent=2)
        elif action == "exec":
            cmd = args.get("command", "pwd")
            res = mgr.run_command_in_container(design_name=design_name, command=cmd)
            return json.dumps(res, indent=2)
        elif action == "status":
            return json.dumps(mgr.get_container_status(design_name=design_name), indent=2)
        else:
            return f"Error: unknown container action '{action}'"
    elif name == "chip_pr":
        action = args.get("action", "list")
        from chip_pr_engine import get_chip_pr_manager
        pr_mgr = get_chip_pr_manager(workspace_root=workspace_root)
        if action == "create":
            pr = pr_mgr.create_pull_request(
                title=args.get("title", "RTL Improvement"),
                description=args.get("description", ""),
                author_id="agent_local",
                author_name="AgentIC Assistant",
                design_name=design_name,
                affected_files=args.get("affected_files", []),
                patch_diff=args.get("patch_diff", ""),
            )
            return f"Created Chip PR '{pr.pr_id}': {json.dumps(pr.to_dict(), indent=2)}"
        elif action == "list":
            prs = pr_mgr.list_pull_requests(design_name=design_name)
            return json.dumps({"prs": prs}, indent=2)
        elif action == "get":
            pr_id = args.get("pr_id", "")
            pr = pr_mgr.get_pull_request(pr_id)
            return json.dumps(pr or {"error": "PR not found"}, indent=2)
        elif action == "approve":
            pr_id = args.get("pr_id", "")
            role = args.get("role", "RTL Lead")
            res = pr_mgr.approve_pull_request(pr_id=pr_id, approver_id="lead_user", approver_name="Chip Reviewer", role=role, comment=args.get("comment", "Approved."))
            return json.dumps(res, indent=2)
        elif action == "merge":
            pr_id = args.get("pr_id", "")
            res = pr_mgr.merge_pull_request(pr_id=pr_id)
            return json.dumps(res, indent=2)
        else:
            return f"Error: unknown chip_pr action '{action}'"
    elif name == "chip_space":
        action = args.get("action", "list_spaces")
        from chip_space_engine import get_chip_space_manager
        sp_mgr = get_chip_space_manager(workspace_root=workspace_root)
        if action == "create_space":
            space = sp_mgr.create_space(
                space_name=args.get("space_name", "Chip Design Space"),
                target_pdk=args.get("target_pdk", "sky130"),
                server_url=args.get("server_url", "https://api.buildstack.live"),
            )
            return f"Created Collaborative Chip Space '{space.space_name}'! Space ID: {space.space_id}, Invite Code: {space.invite_code}\nConfig: {json.dumps(space.to_dict(), indent=2)}"
        elif action == "join_space":
            identifier = args.get("invite_code") or args.get("space_id") or ""
            res = sp_mgr.join_space(identifier=identifier, engineer_id="eng_local", name="Team Member", role=args.get("role", "RTL Design"))
            return json.dumps(res, indent=2)
        elif action == "list_spaces":
            return json.dumps({"spaces": sp_mgr.list_spaces()}, indent=2)
        elif action == "get_config":
            sid = args.get("space_id") or args.get("invite_code") or ""
            return json.dumps(sp_mgr.get_space_config(sid) or {"error": "Space not found"}, indent=2)
        else:
            return f"Error: unknown chip_space action '{action}'"
    elif name == "chip_build":
        target = args.get("target", "freepdk45demo")
        clock_period_ns = float(args.get("clock_period_ns", 10.0))
        from silicon_compiler_adapter import get_sc_adapter
        sc_adapter = get_sc_adapter(workspace_root=workspace_root)
        res = sc_adapter.run_flow(design_name=design_name, target=target, clock_period_ns=clock_period_ns)
        return json.dumps(res, indent=2)
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
