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
        if os.path.commonpath([root, full]) != root:
            return None
    except ValueError:
        return None
    return full


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
        return parse_module_tool(path, workspace_root)
    elif action == "schematic_json":
        return schematic_json_tool(path, workspace_root, module)
    else:
        return f"Error: unknown action '{action}' for workspace tool. Use read, search, list, lint, parse_module, or schematic_json."


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


def schematic_json_tool(path: str, workspace_root: str, module: str = "") -> str:
    """Render a real RTL/gate-level schematic by running Yosys `write_json`.

    Returns a JSON string with either:
      { "available": true,  "module": "...", "yosys_json": {...}, "cells": n, "ports": n }
      { "available": false, "reason": "yosys not found on PATH" }
    Cached by file content hash under <workspace>/.agentic/schematic_cache/.
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

    script_parts = [f"read_verilog {' '.join('-I' + d for d in inc_dirs)} {full}"]
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
