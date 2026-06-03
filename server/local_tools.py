import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path


EDA_TOOLS = (
    # Common open-source defaults. This list is only for convenience reporting;
    # the agent can run any local command through its bash tool.
    "docker",
    "yosys",
    "iverilog",
    "verilator",
    "opensta",
    "openroad",
    "gtkwave",
    "magic",
    "klayout",
    "netgen",
    "make",
    "python3",
)

CAPABILITY_TOOL_ENVS = {
    "simulation": ("AGENTIC_SIM_TOOLS", ("iverilog", "verilator")),
    "synthesis": ("AGENTIC_SYNTH_TOOLS", ("yosys",)),
    "pnr": ("AGENTIC_PNR_TOOLS", ("openroad",)),
    "sta": ("AGENTIC_STA_TOOLS", ("opensta", "openroad")),
    "physical_verification": ("AGENTIC_PHYSICAL_VERIFY_TOOLS", ("magic", "klayout", "netgen")),
}

CAPABILITY_INSTALL_ENVS = {
    "simulation": "AGENTIC_SIM_INSTALL_COMMAND",
    "synthesis": "AGENTIC_SYNTH_INSTALL_COMMAND",
    "pnr": "AGENTIC_PNR_INSTALL_COMMAND",
    "sta": "AGENTIC_STA_INSTALL_COMMAND",
    "physical_verification": "AGENTIC_PHYSICAL_VERIFY_INSTALL_COMMAND",
    "basic": "AGENTIC_BASIC_INSTALL_COMMAND",
}


def configured_eda_tools() -> tuple[str, ...]:
    extra = [
        item.strip()
        for item in os.environ.get("AGENTIC_EDA_TOOLS", "").split(",")
        if item.strip()
    ]
    return tuple(dict.fromkeys((*EDA_TOOLS, *extra)))


def install_command_for(capability: str) -> str:
    env_name = CAPABILITY_INSTALL_ENVS.get(capability, "")
    return os.environ.get(env_name, "").strip() if env_name else ""


def configured_tools_for(capability: str) -> tuple[str, ...]:
    env_name, defaults = CAPABILITY_TOOL_ENVS[capability]
    extra = [
        item.strip()
        for item in os.environ.get(env_name, "").split(",")
        if item.strip()
    ]
    return tuple(dict.fromkeys((*defaults, *extra)))


def _license_env_status() -> dict:
    keys = ("LM_LICENSE_FILE", "CDS_LIC_FILE", "SNPSLMD_LICENSE_FILE", "MGLS_LICENSE_FILE")
    return {key: bool(os.environ.get(key)) for key in keys}


def _wsl_status() -> dict:
    if platform.system().lower() != "windows":
        return {"available": False, "required": False, "distros": []}
    if not shutil.which("wsl"):
        return {"available": False, "required": False, "distros": []}
    try:
        result = subprocess.run(["wsl", "-l", "-q"], capture_output=True, text=True, timeout=8)
        distros = [
            line.strip("\x00\r\n ")
            for line in result.stdout.splitlines()
            if line.strip("\x00\r\n ")
        ]
        return {"available": result.returncode == 0, "required": False, "distros": distros}
    except Exception:
        return {"available": True, "required": False, "distros": []}


def run_bash_stream(command: str, workspace_root: str, timeout: int = 300, on_line=None) -> dict:
    """Run a command, streaming each output line via on_line callback. Returns same dict as run_bash."""
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=workspace_root,
        )

        output_lines = []
        def reader():
            try:
                for line in process.stdout:
                    stripped = line.rstrip("\n\r")
                    output_lines.append(stripped)
                    if on_line:
                        on_line(stripped)
            except ValueError:
                pass

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            process.kill()
            thread.join(timeout=5)

        process.wait(timeout=5)
        full_output = "\n".join(output_lines)
        return {
            "success": process.returncode == 0,
            "stdout": full_output,
            "stderr": "",
            "code": process.returncode,
        }
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e), "code": -1}


def run_bash(command: str, workspace_root: str, timeout: int = 300) -> dict:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workspace_root,
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


def _version_for(tool: str) -> str | None:
    path = shutil.which(tool)
    if not path:
        return None
    version_args = {
        "iverilog": ["iverilog", "-V"],
        "verilator": ["verilator", "--version"],
        "yosys": ["yosys", "-V"],
        "openroad": ["openroad", "-version"],
        "opensta": ["opensta", "-version"],
        "gtkwave": ["gtkwave", "--version"],
        "magic": ["magic", "--version"],
        "klayout": ["klayout", "-v"],
        # Netgen treats arbitrary argv as Tcl in console mode; `-batch` prints
        # the version banner and exits without opening tkcon.
        "netgen": ["netgen", "-batch"],
        "docker": ["docker", "--version"],
        "make": ["make", "--version"],
        "python3": ["python3", "--version"],
    }.get(tool)
    if not version_args:
        return None
    try:
        result = subprocess.run(version_args, capture_output=True, text=True, timeout=5)
        text = (result.stdout or result.stderr).strip().splitlines()
        return text[0] if text else None
    except Exception:
        return None


def _docker_images() -> list[str]:
    if not shutil.which("docker"):
        return []
    try:
        result = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def _pdk_dirs() -> list[str]:
    roots = []
    for env_name in ("PDK_ROOT", "PDKPATH", "PDK_HOME"):
        value = os.environ.get(env_name, "")
        if value:
            roots.extend(value.split(os.pathsep))
    roots.extend(
        path.strip()
        for path in os.environ.get("AGENTIC_PDK_SEARCH_PATHS", "").split(os.pathsep)
        if path.strip()
    )
    found: list[str] = []
    for root in roots:
        expanded = os.path.expanduser(os.path.expandvars(root))
        if not expanded or not os.path.isdir(expanded):
            continue
        children = [str(path) for path in Path(expanded).iterdir() if path.is_dir()]
        found.extend(children or [expanded])
    return sorted(set(found))


def _has_openlane_image(images: list[str]) -> bool:
    return any(("openlane" in image.lower() or "openroad" in image.lower()) for image in images)


def detect_environment() -> dict:
    tools = {}
    versions = {}
    paths = {}
    for name in configured_eda_tools():
        tool_path = shutil.which(name)
        tools[name] = tool_path is not None
        if tool_path:
            paths[name] = tool_path
            versions[name] = _version_for(name)

    images = _docker_images()
    pdk_dirs = _pdk_dirs()
    wsl = _wsl_status()
    has_sim = any(tools.get(tool) for tool in configured_tools_for("simulation"))
    has_synth = any(tools.get(tool) for tool in configured_tools_for("synthesis"))
    has_pnr_native = any(tools.get(tool) for tool in configured_tools_for("pnr"))
    has_pnr_docker = bool(tools.get("docker") and _has_openlane_image(images))
    has_physical_verify = any(tools.get(tool) for tool in configured_tools_for("physical_verification"))

    capabilities = {
        "simulation": has_sim,
        "synthesis": has_synth,
        "pnr": has_pnr_native or has_pnr_docker,
        "sta": any(tools.get(tool) for tool in configured_tools_for("sta")),
        "waveform": bool(tools.get("gtkwave")),
        "physical_verification": has_physical_verify,
        "pdk": bool(pdk_dirs),
        "docker": bool(tools.get("docker")),
        "wsl": wsl["available"],
    }

    missing = []
    if not has_sim:
        missing.append({"capability": "simulation", "tools": list(configured_tools_for("simulation"))})
    if not has_synth:
        missing.append({"capability": "synthesis", "tools": list(configured_tools_for("synthesis"))})
    if not capabilities["pnr"]:
        missing.append({"capability": "pnr", "tools": [*configured_tools_for("pnr"), "dockerized flow"]})
    if not capabilities["pdk"]:
        missing.append({"capability": "pdk", "tools": ["PDK_ROOT", "PDKPATH", "AGENTIC_PDK_SEARCH_PATHS"]})

    if capabilities["pnr"] and has_synth and has_sim and capabilities["pdk"]:
        tier = "rtl_to_gds"
    elif has_synth and has_sim:
        tier = "rtl_to_synth"
    elif has_sim:
        tier = "rtl_simulation"
    else:
        tier = "setup_required"

    return {
        "platform": platform.system().lower(),
        "tools": tools,
        "versions": versions,
        "paths": paths,
        "docker_images": images,
        "pdk_search_paths": [
            "$PDK_ROOT",
            "$PDKPATH",
            "$PDK_HOME",
            "$AGENTIC_PDK_SEARCH_PATHS",
        ],
        "existing_pdk_dirs": pdk_dirs,
        "adapters": {
            capability: {
                "env": env_name,
                "tools": list(configured_tools_for(capability)),
                "available": [tool for tool in configured_tools_for(capability) if tools.get(tool)],
            }
            for capability, (env_name, _defaults) in CAPABILITY_TOOL_ENVS.items()
        },
        "license_env": _license_env_status(),
        "wsl": wsl,
        "capabilities": capabilities,
        "missing": missing,
        "capability_tier": tier,
    }
