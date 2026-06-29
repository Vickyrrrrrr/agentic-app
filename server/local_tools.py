import copy
import os
import platform
import shlex
import shutil
import subprocess
import time

if hasattr(subprocess, "CREATE_NO_WINDOW"):
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW
elif getattr(os, "name", "") == "nt":
    _NO_WINDOW = 0x08000000
else:
    _NO_WINDOW = 0

import threading
from pathlib import Path

from capability_index import build_capability_index
from capability_manifest import load_capability_manifests, manifest_env_names
from flow_runtime import detect_flows, recommend_flow
from pdk_index import build_pdk_index
from tool_adapters import adapter_command_names, capability_matrix, license_env_names
from vlsi_capability_graph import build_capability_graph


EDA_TOOLS = (
    # Common open-source defaults. This list is only for convenience reporting;
    # the agent can run any local command through its bash tool.
    "docker",
    "yosys",
    "iverilog",
    "vvp",
    "verilator",
    "opensta",
    "openroad",
    "openlane",
    "openlane2",
    "volare",
    "gtkwave",
    "magic",
    "klayout",
    "netgen",
    "make",
    "python3",
    # Proprietary tools
    "vcs",
    "vlogan",
    "vhdlan",
    "dve",
    "verdi",
    "xrun",
    "irun",
    "xmvlog",
    "xmelab",
    "xmsim",
    "ncvlog",
    "ncelab",
    "ncsim",
    "vsim",
    "questasim",
    "dc_shell",
    "fm_shell",
    "lc_shell",
    "spyglass",
    "genus",
    "innovus",
    "icc2_shell",
    "tempus",
    "pt_shell",
    "quantus",
    "star_rcxt",
    "calibre",
    "pegasus",
    "assura",
)

CAPABILITY_TOOL_ENVS = {
    "simulation": ("AGENTIC_SIM_TOOLS", ("iverilog", "verilator", "vcs", "xrun", "irun", "vsim", "questasim")),
    "synthesis": ("AGENTIC_SYNTH_TOOLS", ("yosys", "dc_shell", "genus")),
    "pnr": ("AGENTIC_PNR_TOOLS", ("openroad", "innovus", "icc2_shell")),
    "sta": ("AGENTIC_STA_TOOLS", ("opensta", "openroad", "tempus", "pt_shell")),
    "physical_verification": ("AGENTIC_PHYSICAL_VERIFY_TOOLS", ("magic", "klayout", "netgen", "calibre")),
}

CAPABILITY_INSTALL_ENVS = {
    "simulation": "AGENTIC_SIM_INSTALL_COMMAND",
    "synthesis": "AGENTIC_SYNTH_INSTALL_COMMAND",
    "pnr": "AGENTIC_PNR_INSTALL_COMMAND",
    "sta": "AGENTIC_STA_INSTALL_COMMAND",
    "physical_verification": "AGENTIC_PHYSICAL_VERIFY_INSTALL_COMMAND",
    "basic": "AGENTIC_BASIC_INSTALL_COMMAND",
}

ENV_CACHE_DEFAULT_TTL_SECONDS = 180.0
_ENV_CACHE_LOCK = threading.Lock()
_ENV_CACHE: dict[str, object] = {
    "key": None,
    "expires_at": 0.0,
    "value": None,
}


def configured_eda_tools() -> tuple[str, ...]:
    extra = [
        item.strip()
        for item in os.environ.get("AGENTIC_EDA_TOOLS", "").split(",")
        if item.strip()
    ]
    return tuple(dict.fromkeys((*EDA_TOOLS, *adapter_command_names(), *extra)))


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


def _environment_cache_ttl() -> float:
    raw = os.environ.get("AGENTIC_ENV_CACHE_TTL_SECONDS", str(ENV_CACHE_DEFAULT_TTL_SECONDS)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return ENV_CACHE_DEFAULT_TTL_SECONDS


def _environment_cache_key() -> tuple[tuple[str, str], ...]:
    watched = {
        "PATH",
        "PDK",
        "PDK_ROOT",
        "PDKPATH",
        "PDK_HOME",
        "AGENTIC_PDK_SEARCH_PATHS",
        "AGENTIC_FLOW_SEARCH_PATHS",
        "AGENTIC_TOOL_SEARCH_PATHS",
        "AGENTIC_OPENLANE_ROOT",
        "AGENTIC_ORFS_ROOT",
        "AGENTIC_EDA_TOOLS",
        "AGENTIC_WSL_TOOL_SCAN",
        "AGENTIC_WSL_PROBE_TIMEOUT_SECONDS",
        *manifest_env_names(),
        *CAPABILITY_INSTALL_ENVS.values(),
        *(env_name for env_name, _defaults in CAPABILITY_TOOL_ENVS.values()),
        "LM_LICENSE_FILE",
        "CDS_LIC_FILE",
        "SNPSLMD_LICENSE_FILE",
        "MGLS_LICENSE_FILE",
        *license_env_names(),
    }
    return tuple(sorted((name, os.environ.get(name, "")) for name in watched if name))


def clear_environment_cache() -> None:
    with _ENV_CACHE_LOCK:
        _ENV_CACHE["key"] = None
        _ENV_CACHE["expires_at"] = 0.0
        _ENV_CACHE["value"] = None


def _license_env_status() -> dict:
    keys = ("LM_LICENSE_FILE", "CDS_LIC_FILE", "SNPSLMD_LICENSE_FILE", "MGLS_LICENSE_FILE", *license_env_names())
    return {key: bool(os.environ.get(key)) for key in keys}


def _decode_wsl_output(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.count(b"\x00") > max(2, len(raw) // 8):
        for encoding in ("utf-16le", "utf-16"):
            try:
                return raw.decode(encoding, errors="ignore")
            except Exception:
                pass
    return raw.decode("utf-8", errors="replace")


def _wsl_probe_timeout() -> float:
    raw = os.environ.get("AGENTIC_WSL_PROBE_TIMEOUT_SECONDS", "8").strip()
    try:
        return max(1.0, min(30.0, float(raw)))
    except ValueError:
        return 8.0


def _wsl_tool_scan_enabled() -> bool:
    return os.environ.get("AGENTIC_WSL_TOOL_SCAN", "1").strip().lower() not in {"0", "false", "no", "off"}


def _wsl_status() -> dict:
    if platform.system().lower() != "windows":
        return {"available": False, "required": False, "distros": [], "tool_inventory": []}
    if not shutil.which("wsl"):
        return {"available": False, "required": False, "distros": [], "tool_inventory": []}
    try:
        result = subprocess.run(["wsl", "-l", "-q"], capture_output=True,
            creationflags=_NO_WINDOW, timeout=8)
        stdout = _decode_wsl_output(result.stdout)
        distros = [
            line.strip("*\x00\r\n ")
            for line in stdout.splitlines()
            if line.strip("*\x00\r\n ")
        ]
        return {
            "available": result.returncode == 0,
            "required": False,
            "distros": distros,
            "tool_inventory": _wsl_tool_inventory(distros) if result.returncode == 0 and _wsl_tool_scan_enabled() else [],
        }
    except Exception:
        return {"available": True, "required": False, "distros": [], "tool_inventory": []}


def _wsl_tool_inventory(distros: list[str]) -> list[dict]:
    inventory = []
    tool_names = configured_eda_tools()
    license_names = ("LM_LICENSE_FILE", "CDS_LIC_FILE", "SNPSLMD_LICENSE_FILE", "MGLS_LICENSE_FILE", *license_env_names())
    tool_loop = "\n".join(
        [
            f"candidate={shlex.quote(tool)}; path=$(command -v \"$candidate\" 2>/dev/null || true); "
            "if [ -n \"$path\" ]; then printf 'TOOL\\t%s\\t%s\\n' \"$candidate\" \"$path\"; fi"
            for tool in tool_names
        ]
    )
    license_loop = "\n".join(
        [
            f"value=$(printenv {shlex.quote(name)} 2>/dev/null || true); "
            f"if [ -n \"$value\" ]; then printf 'LICENSE\\t%s\\t1\\n' {shlex.quote(name)}; fi"
            for name in license_names
        ]
    )
    script = "\n".join(["set +e", tool_loop, license_loop])
    for distro in distros:
        entry = {
            "distro": distro,
            "can_execute": False,
            "tools": {},
            "paths": {},
            "license_env": {},
        }
        try:
            result = subprocess.run(
                ["wsl", "-d", distro, "--", "sh", "-lc", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW,
                timeout=_wsl_probe_timeout(),
            )
            entry["can_execute"] = result.returncode == 0
            if result.returncode != 0:
                entry["error"] = (result.stderr or result.stdout or "").strip()[:500]
            for line in (result.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 3 and parts[0] == "TOOL":
                    entry["tools"][parts[1]] = True
                    entry["paths"][parts[1]] = parts[2]
                elif len(parts) >= 3 and parts[0] == "LICENSE":
                    entry["license_env"][parts[1]] = True
        except Exception as exc:
            entry["error"] = str(exc)
        inventory.append(entry)
    return inventory


def _wsl_tool_presence(inventory: list[dict]) -> dict[str, bool]:
    tools: dict[str, bool] = {}
    for distro in inventory:
        for name, available in (distro.get("tools") or {}).items():
            tools[name] = bool(tools.get(name) or available)
    return tools


def _wsl_capabilities(wsl_tools: dict[str, bool]) -> dict[str, bool]:
    return {
        "simulation": any(wsl_tools.get(tool) for tool in configured_tools_for("simulation")),
        "synthesis": any(wsl_tools.get(tool) for tool in configured_tools_for("synthesis")),
        "pnr": any(wsl_tools.get(tool) for tool in configured_tools_for("pnr")),
        "sta": any(wsl_tools.get(tool) for tool in configured_tools_for("sta")),
        "waveform": bool(wsl_tools.get("gtkwave")),
        "physical_verification": any(wsl_tools.get(tool) for tool in configured_tools_for("physical_verification")),
    }


def _subprocess_cwd(workspace_root: str) -> str | None:
    if os.name == "nt" and str(workspace_root or "").startswith("\\\\"):
        # cmd.exe cannot use UNC paths as its current directory. WSL commands
        # should cd inside their own shell; this cwd only needs to launch wsl.exe.
        return os.path.expanduser("~")
    if workspace_root and os.path.isdir(workspace_root):
        return workspace_root
    return None


def run_bash_stream(command: str, workspace_root: str, timeout: int = 300, on_line=None, cancel_checker=None) -> dict:
    """Run a command, streaming each output line via on_line callback. Returns same dict as run_bash."""
    try:
        kwargs = {}
        if os.name != "nt":
            kwargs["preexec_fn"] = os.setsid

        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=_NO_WINDOW,
            cwd=_subprocess_cwd(workspace_root),
            **kwargs
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
        elapsed = 0.0

        def kill_process():
            if os.name != "nt":
                import signal
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    process.kill()
            else:
                process.kill()

        while thread.is_alive() and elapsed < timeout:
            if cancel_checker and cancel_checker():
                kill_process()
                thread.join(timeout=2)
                full_output = "\n".join(output_lines)
                return {
                    "success": False,
                    "stdout": full_output,
                    "stderr": "Command cancelled",
                    "code": -1,
                }
            thread.join(timeout=0.2)
            elapsed += 0.2

        if thread.is_alive():
            kill_process()
            thread.join(timeout=2)

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


def run_bash(command: str, workspace_root: str, timeout: int = 300, cancel_checker=None) -> dict:
    if cancel_checker:
        return run_bash_stream(command, workspace_root, timeout=timeout, cancel_checker=cancel_checker)
    try:
        kwargs = {}
        if os.name != "nt":
            kwargs["preexec_fn"] = os.setsid

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            creationflags=_NO_WINDOW,
            timeout=timeout,
            cwd=_subprocess_cwd(workspace_root),
            **kwargs
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
        "vcs": ["vcs", "-platform"],
        "xrun": ["xrun", "-version"],
        "irun": ["irun", "-version"],
        "vsim": ["vsim", "-version"],
        "questasim": ["questasim", "-version"],
        "dc_shell": ["dc_shell", "-version"],
        "genus": ["genus", "-version"],
        "innovus": ["innovus", "-version"],
        "icc2_shell": ["icc2_shell", "-version"],
        "tempus": ["tempus", "-version"],
        "pt_shell": ["pt_shell", "-version"],
        "calibre": ["calibre", "-version"],
    }.get(tool)
    if not version_args:
        return None
    try:
        result = subprocess.run(version_args, capture_output=True, text=True,
            creationflags=_NO_WINDOW, timeout=5)
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
            creationflags=_NO_WINDOW,
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


def detect_environment(force_refresh: bool = False) -> dict:
    cache_ttl = _environment_cache_ttl()
    cache_key = _environment_cache_key()
    now = time.monotonic()
    if not force_refresh and cache_ttl > 0:
        with _ENV_CACHE_LOCK:
            cached_value = _ENV_CACHE.get("value")
            if (
                cached_value is not None
                and _ENV_CACHE.get("key") == cache_key
                and float(_ENV_CACHE.get("expires_at") or 0.0) > now
            ):
                return copy.deepcopy(cached_value)

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
    pdk_index = build_pdk_index()
    capability_manifests = load_capability_manifests()
    exposed_pdk_index = _augment_pdk_index_with_manifests(pdk_index, capability_manifests)
    wsl = _wsl_status()
    wsl_tools = _wsl_tool_presence(wsl.get("tool_inventory") or [])
    wsl_capabilities = _wsl_capabilities(wsl_tools)
    has_sim = any(tools.get(tool) for tool in configured_tools_for("simulation"))
    has_synth = any(tools.get(tool) for tool in configured_tools_for("synthesis"))
    has_pnr_native = any(tools.get(tool) for tool in configured_tools_for("pnr"))
    has_pnr_docker = bool(tools.get("docker") and _has_openlane_image(images))
    has_physical_verify = any(tools.get(tool) for tool in configured_tools_for("physical_verification"))

    capabilities = {
        "simulation": has_sim or wsl_capabilities["simulation"],
        "synthesis": has_synth or wsl_capabilities["synthesis"],
        "pnr": has_pnr_native or has_pnr_docker or wsl_capabilities["pnr"],
        "sta": any(tools.get(tool) for tool in configured_tools_for("sta")) or wsl_capabilities["sta"],
        "waveform": bool(tools.get("gtkwave")) or wsl_capabilities["waveform"],
        "physical_verification": has_physical_verify or wsl_capabilities["physical_verification"],
        "pdk": bool(pdk_dirs or (capability_manifests.get("pdks") or [])),
        "docker": bool(tools.get("docker")),
        "wsl": wsl["available"],
    }

    flows = detect_flows(tools, images)
    adapter_matrix = capability_matrix(tools)
    capability_index = build_capability_index(
        pdk_index=pdk_index,
        tools=tools,
        flows=flows,
        adapter_matrix=adapter_matrix,
    )
    capability_graph = build_capability_graph({
        "capability_index": capability_index,
        "flows": flows,
        "tool_adapters": adapter_matrix,
        "capability_manifests": capability_manifests,
    })
    for capability in ("simulation", "synthesis", "pnr", "sta", "physical_verification"):
        capabilities[capability] = bool(capabilities.get(capability) or (adapter_matrix.get(capability) or {}).get("available"))

    missing = []
    for capability in ("simulation", "synthesis", "pnr", "sta", "physical_verification"):
        if not (adapter_matrix.get(capability) or {}).get("available") and not capabilities.get(capability):
            missing.append({
                "capability": capability,
                "tools": [
                    command
                    for candidate in (adapter_matrix.get(capability) or {}).get("candidates", [])
                    for command in candidate.get("commands", [])
                ] or list(configured_tools_for(capability)),
            })
    if not capabilities["pdk"]:
        missing.append({"capability": "pdk", "tools": ["PDK_ROOT", "PDKPATH", "AGENTIC_PDK_SEARCH_PATHS"]})

    if capabilities["pnr"] and capabilities["synthesis"] and capabilities["simulation"] and capabilities["pdk"]:
        tier = "rtl_to_gds"
    elif capabilities["synthesis"] and capabilities["simulation"]:
        tier = "rtl_to_synth"
    elif capabilities["simulation"]:
        tier = "rtl_simulation"
    else:
        tier = "setup_required"

    env = {
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
            "$AGENTIC_CAPABILITY_MANIFESTS",
        ],
        "existing_pdk_dirs": pdk_dirs,
        "pdk_index": exposed_pdk_index,
        "capability_index": capability_index,
        "capability_manifests": capability_manifests,
        "capability_graph": capability_graph,
        "flows": flows,
        "tool_adapters": adapter_matrix,
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
        "wsl_tools": wsl_tools,
        "wsl_capabilities": wsl_capabilities,
        "capabilities": capabilities,
        "missing": missing,
        "capability_tier": tier,
    }
    env["recommended_flow"] = recommend_flow(env, requested_pdk=os.environ.get("PDK", ""))
    if cache_ttl > 0:
        with _ENV_CACHE_LOCK:
            _ENV_CACHE["key"] = cache_key
            _ENV_CACHE["expires_at"] = time.monotonic() + cache_ttl
            _ENV_CACHE["value"] = copy.deepcopy(env)
    return env


def _augment_pdk_index_with_manifests(pdk_index: dict, manifests: dict) -> dict:
    augmented = copy.deepcopy(pdk_index)
    entries = augmented.setdefault("pdks", [])
    existing = {str(item.get("name") or "").lower() for item in entries if isinstance(item, dict)}
    for pdk in manifests.get("pdks") or []:
        name = str(pdk.get("name") or "").strip()
        if not name or name.lower() in existing:
            continue
        entries.append({
            "name": name,
            "path": pdk.get("path"),
            "family": pdk.get("family"),
            "node_nm": pdk.get("node_nm"),
            "class": pdk.get("class") or "commercial_or_custom",
            "preferred_open_flow": None,
            "openlane": {"has_pdk_config": False, "config_tcl": None, "active_scl": pdk.get("active_scl")},
            "orfs": {"platform_aliases": [name]},
            "libraries": [],
            "tech": pdk.get("physical_decks") or {},
            "readiness": pdk.get("readiness") or {},
            "source": "capability_manifest",
        })
        existing.add(name.lower())
    augmented["count"] = len(entries)
    return augmented
