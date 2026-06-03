import os
import re
import urllib.request
import urllib.parse
import glob as glob_mod
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


def read_file(path: str, workspace_root: str) -> str:
    full = os.path.normpath(os.path.join(workspace_root, path))
    if not full.startswith(os.path.normpath(workspace_root)):
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
    full = os.path.normpath(os.path.join(workspace_root, path))
    if not full.startswith(os.path.normpath(workspace_root)):
        return f"Error: path '{path}' is outside workspace"
    os.makedirs(os.path.dirname(full), exist_ok=True)
    try:
        with open(full, "w") as f:
            f.write(content)
        return f"File written: {path} ({len(content)} bytes)"
    except Exception as e:
        return f"Error writing file: {e}"


def edit_file(path: str, old_string: str, new_string: str, workspace_root: str) -> str:
    full = os.path.normpath(os.path.join(workspace_root, path))
    if not full.startswith(os.path.normpath(workspace_root)):
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
        new_content = content.replace(old_string, new_string, 1)
        with open(full, "w") as f:
            f.write(new_content)
        return f"File edited: {path}"
    except Exception as e:
        return f"Error editing file: {e}"


def bash_tool(command: str, workspace_root: str, timeout: int = 300, on_output=None) -> str:
    if on_output:
        result = run_bash_stream(command, workspace_root, timeout=timeout, on_line=on_output)
    else:
        result = run_bash(command, workspace_root, timeout=timeout)
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
    full = os.path.normpath(os.path.join(workspace_root, path))
    if not full.startswith(os.path.normpath(workspace_root)):
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
    full = os.path.join(workspace_root, pattern)
    try:
        results = glob_mod.glob(full, recursive=True)
        if not results:
            return f"No files matching: {pattern}"
        rels = [os.path.relpath(p, workspace_root) for p in sorted(results)]
        return "\n".join(rels)
    except Exception as e:
        return f"Error globbing: {e}"


def web_search(query: str, max_results: int = 5) -> str:
    if not _env_true("AGENTIC_ENABLE_WEB_SEARCH"):
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


def dispatch_tool(name: str, args: dict, workspace_root: str, on_output=None) -> str:
    if name == "read":
        return read_file(args["path"], workspace_root)
    elif name == "write":
        return write_file(args["path"], args["content"], workspace_root)
    elif name == "edit":
        return edit_file(args["path"], args["old_string"], args["new_string"], workspace_root)
    elif name == "bash":
        timeout = args.get("timeout", 300)
        return bash_tool(args["command"], workspace_root, timeout=timeout, on_output=on_output)
    elif name == "grep":
        path = args.get("path", ".")
        return grep_tool(args["pattern"], path, workspace_root)
    elif name == "glob":
        return glob_tool(args["pattern"], workspace_root)
    elif name == "web_search":
        return web_search(args["query"], args.get("max_results", 5))
    else:
        return f"Error: unknown tool '{name}'"
