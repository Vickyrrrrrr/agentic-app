import copy
import os
import platform
import shlex
import shutil
import subprocess
import time



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


def _subprocess_cwd(workspace_root: str) -> str | None:
    if workspace_root and os.path.isdir(workspace_root):
        return workspace_root
    return None


# Background promotion threshold: if a process runs longer than this, it is
# registered in the BACKGROUND_JOBS registry and the calling agent is unblocked.
_BG_PROMOTE_THRESHOLD_S = 3.0


def _register_background_job(
    job_id: str,
    title: str,
    command: str,
    pid: int,
    log_path: str,
    session_id: str = "",
) -> None:
    """Thread-safe persistent registration of a shell process into SQLite JobQueue."""
    try:
        from job_queue import get_job_queue
        jq = get_job_queue()
        jq.submit(
            title=title or command[:60],
            command=command[:200],
            session_id=session_id,
            pid=pid,
        )
    except Exception as e:
        import logging
        logging.warning("Error registering background job in SQLite JobQueue: %s", e)


def _update_background_job(job_id: str, success: bool, exit_code: int = 0) -> None:
    """Mark a persistent background job as completed or failed in SQLite JobQueue."""
    try:
        from job_queue import get_job_queue
        jq = get_job_queue()
        status = "completed" if success else "failed"
        jq.update_status(job_id=job_id, status=status, exit_code=exit_code)
    except Exception as e:
        import logging
        logging.warning("Error updating background job in SQLite JobQueue: %s", e)




def run_bash_stream(
    command: str,
    workspace_root: str,
    timeout: int = 300,
    on_line=None,
    cancel_checker=None,
    promote_callback=None,
    job_title: str = "",
) -> dict:
    """Run a command, streaming each output line via on_line callback.

    If the process exceeds _BG_PROMOTE_THRESHOLD_S seconds, it is automatically
    promoted to a background job. `promote_callback(job_id)` is called at that
    point so the caller (chat agent) can unblock the LLM turn immediately.
    The process continues running and BACKGROUND_JOBS is updated on completion.
    """
    import uuid as _uuid
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

        # Prepare log file in .agentic/job-logs/
        job_id = str(_uuid.uuid4())[:8]
        log_dir = os.path.join(workspace_root or os.path.expanduser("~"), ".agentic", "job-logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{job_id}.log")

        output_lines = []
        promoted = threading.Event()
        completion_event = threading.Event()
        return_code_holder = [None]

        def reader():
            try:
                with open(log_path, "w", encoding="utf-8") as log_f:
                    for line in process.stdout:
                        stripped = line.rstrip("\n\r")
                        output_lines.append(stripped)
                        log_f.write(line)
                        log_f.flush()
                        if on_line and not promoted.is_set():
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
                if promoted.is_set():
                    _update_background_job(job_id, success=False)
                return {
                    "success": False,
                    "stdout": full_output,
                    "stderr": "Command cancelled",
                    "code": -1,
                }

            # Auto-promote after threshold
            if not promoted.is_set() and elapsed >= _BG_PROMOTE_THRESHOLD_S:
                promoted.set()
                title = job_title or command.split()[0] if command else "shell"
                _register_background_job(
                    job_id=job_id,
                    title=title,
                    command=command,
                    pid=process.pid,
                    log_path=log_path,
                )
                if promote_callback:
                    promote_callback(job_id)
                # Unblock caller — background watcher thread will update on completion
                def _bg_watcher():
                    thread.join()
                    try:
                        process.wait(timeout=5)
                    except Exception:
                        pass
                    _update_background_job(job_id, success=(process.returncode == 0))
                threading.Thread(target=_bg_watcher, daemon=True).start()
                return {
                    "success": True,
                    "stdout": "\n".join(output_lines),
                    "stderr": "",
                    "code": 0,
                    "background": True,
                    "job_id": job_id,
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


def run_bash(
    command: str,
    workspace_root: str,
    timeout: int = 300,
    cancel_checker=None,
    promote_callback=None,
    job_title: str = "",
) -> dict:
    """Run a shell command, routing through run_bash_stream for 15s auto-promotion."""
    return run_bash_stream(
        command,
        workspace_root,
        timeout=timeout,
        cancel_checker=cancel_checker,
        promote_callback=promote_callback,
        job_title=job_title,
    )



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
        "pdk": bool(pdk_dirs or (capability_manifests.get("pdks") or [])),
        "docker": bool(tools.get("docker")),
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
