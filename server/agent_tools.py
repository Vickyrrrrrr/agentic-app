import os
import re
import subprocess
import glob as glob_mod


def run_bash(command: str, workspace_root: str = ".", timeout: int = 300) -> dict:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=workspace_root
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "Command timed out", "code": -1}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e), "code": -1}


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


def bash_tool(command: str, workspace_root: str, timeout: int = 300) -> str:
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


def dispatch_tool(name: str, args: dict, workspace_root: str) -> str:
    if name == "read":
        return read_file(args["path"], workspace_root)
    elif name == "write":
        return write_file(args["path"], args["content"], workspace_root)
    elif name == "edit":
        return edit_file(args["path"], args["old_string"], args["new_string"], workspace_root)
    elif name == "bash":
        timeout = args.get("timeout", 300)
        return bash_tool(args["command"], workspace_root, timeout=timeout)
    elif name == "grep":
        path = args.get("path", ".")
        return grep_tool(args["pattern"], path, workspace_root)
    elif name == "glob":
        return glob_tool(args["pattern"], workspace_root)
    else:
        return f"Error: unknown tool '{name}'"
