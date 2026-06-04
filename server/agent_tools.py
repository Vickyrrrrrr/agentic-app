import os
import re
import urllib.request
import urllib.parse
import glob as glob_mod
import json
from html.parser import HTMLParser

from local_tools import run_bash, run_bash_stream


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
    ".drce", ".mag", ".magic", ".tcl",
}


TEXT_WRITE_EXTENSIONS = {
    ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".tcl", ".sdc", ".sby",
    ".ys", ".cfg", ".json", ".md", ".txt", ".log", ".rpt", ".lib", ".lef",
    ".def", ".py", ".c", ".cpp", ".h", ".sh", ".mk", ".makefile", ".yml",
    ".yaml", ".toml", ".ini", ".csv", ".sp", ".spi", ".lvs",
}


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


def read_file(path: str, workspace_root: str) -> str:
    full = _safe_workspace_path(path, workspace_root)
    if not full:
        return f"Error: path '{path}' is outside workspace"
    if not os.path.isfile(full):
        return f"Error: file not found: {path}"
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


def bash_tool(command: str, workspace_root: str, timeout: int = 300, on_output=None, cancel_checker=None) -> str:
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
    if not result["success"]:
        output = f"Exit code {result['code']}\n{output}"
    return output.strip() or "(no output)"


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


def query_pdk_tool(query_type: str, cell_type: str = "") -> str:
    pdk_root = os.environ.get("PDK_ROOT")
    if not pdk_root:
        common_paths = [
            os.path.expanduser("~/.volare"),
            "/usr/share/pdk",
            "/usr/local/share/pdk",
            "/opt/pdk",
            os.path.expanduser("~/pdk")
        ]
        for p in common_paths:
            if os.path.isdir(p):
                pdk_root = p
                break

    if not pdk_root or not os.path.isdir(pdk_root):
        return json.dumps({
            "status": "DEPENDENCY_MISSING",
            "missing_component": "PDK"
        })

    pdks = []
    try:
        for entry in os.listdir(pdk_root):
            if os.path.isdir(os.path.join(pdk_root, entry)) and not entry.startswith("."):
                pdks.append(entry)
    except Exception as e:
        return json.dumps({"error": f"Failed to read PDK_ROOT: {e}"})
    
    if not pdks:
        return json.dumps({"error": f"No PDKs found inside {pdk_root}"})
    
    active_pdk = pdks[0]
    env_pdk = os.environ.get("PDK", "")
    if env_pdk in pdks:
        active_pdk = env_pdk

    pdk_path = os.path.join(pdk_root, active_pdk)
    libs_dir = os.path.join(pdk_path, "libs.ref")
    
    if not os.path.isdir(libs_dir):
        return json.dumps({"error": f"libs.ref not found in {pdk_path}", "available_pdks": pdks})

    if query_type == "list_libraries":
        try:
            libraries = [d for d in os.listdir(libs_dir) if os.path.isdir(os.path.join(libs_dir, d))]
            return json.dumps({"pdk": active_pdk, "libraries": libraries})
        except Exception as e:
            return json.dumps({"error": str(e)})

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
        tech_dir = os.path.join(pdk_path, "libs.tech")
        return json.dumps({"pdk": active_pdk, "info": "Extract routing layers from .tech / .lef", "path": tech_dir})

    else:
        return json.dumps({"error": f"Unknown query_type: {query_type}"})


def dispatch_tool(name: str, args: dict, workspace_root: str, on_output=None, cancel_checker=None) -> str:
    if name == "read":
        return read_file(args["path"], workspace_root)
    elif name == "write":
        return write_file(args["path"], args["content"], workspace_root)
    elif name == "edit":
        return edit_file(args["path"], args["old_string"], args["new_string"], workspace_root)
    elif name == "bash":
        timeout = args.get("timeout", 300)
        return bash_tool(args["command"], workspace_root, timeout=timeout, on_output=on_output, cancel_checker=cancel_checker)
    elif name == "grep":
        path = args.get("path", ".")
        return grep_tool(args["pattern"], path, workspace_root)
    elif name == "glob":
        return glob_tool(args["pattern"], workspace_root)
    elif name == "web_search":
        return web_search(args["query"], args.get("max_results", 5))
    elif name == "query_pdk":
        return query_pdk_tool(args.get("query_type", ""), args.get("cell_type", ""))
    else:
        return f"Error: unknown tool '{name}'"
