from __future__ import annotations

import os
import re
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


PDK_ROOT_ENVS = ("PDK_ROOT", "PDKPATH", "PDK_HOME", "AGENTIC_PDK_SEARCH_PATHS")

COMMON_PDK_ROOTS = (
    "~/.volare",
    "~/pdks",
    "~/pdk",
    "/usr/share/pdk",
    "/usr/local/share/pdk",
    "/opt/pdk",
    "/opt/pdks",
)

KNOWN_PDKS = {
    "sky130": {"family": "sky130", "node_nm": 130, "class": "open_source", "preferred_open_flow": "openlane"},
    "sky130a": {"family": "sky130", "node_nm": 130, "class": "open_source", "preferred_open_flow": "openlane"},
    "gf180": {"family": "gf180mcu", "node_nm": 180, "class": "open_source", "preferred_open_flow": "openlane"},
    "gf180mcu": {"family": "gf180mcu", "node_nm": 180, "class": "open_source", "preferred_open_flow": "openlane"},
    "asap7": {"family": "asap7", "node_nm": 7, "class": "open_source_research", "preferred_open_flow": "orfs"},
    "nangate45": {"family": "nangate45", "node_nm": 45, "class": "open_source_reference", "preferred_open_flow": "orfs"},
}


def _split_env_paths(value: str) -> list[str]:
    paths: list[str] = []
    for chunk in value.split(os.pathsep):
        stripped = chunk.strip()
        if stripped:
            paths.append(stripped)
    return paths


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def discover_pdk_roots() -> list[str]:
    roots: list[Path] = []
    for env_name in PDK_ROOT_ENVS:
        for raw in _split_env_paths(os.environ.get(env_name, "")):
            candidate = _expand(raw)
            if candidate.is_dir():
                roots.append(candidate)

    for raw in COMMON_PDK_ROOTS:
        candidate = _expand(raw)
        if candidate.is_dir():
            roots.append(candidate)

    # When the backend runs on Windows native, also probe WSL distros for PDKs
    # so the capability graph / PDK panel works without running the backend in WSL.
    for raw in discover_wsl_pdk_roots():
        try:
            candidate = Path(raw)
            if candidate.is_dir():
                roots.append(candidate)
        except Exception:
            pass

    seen: set[str] = set()
    result: list[str] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _wsl_distros() -> list[str]:
    if platform.system().lower() != "windows":
        return []
    if not shutil.which("wsl"):
        return []
    try:
        result = subprocess.run(["wsl", "-l", "-q"], capture_output=True, timeout=8)
        raw = result.stdout.decode("utf-8", errors="replace")
        if raw.count("\x00") > 4:
            try:
                raw = result.stdout.decode("utf-16-le", errors="replace")
            except Exception:
                pass
        distros = [line.strip("*\x00\r\n ") for line in raw.splitlines() if line.strip("*\x00\r\n ")]
        return distros
    except Exception:
        return []


def _linux_to_wsl_unc(linux_path: str, distro: str) -> str:
    p = linux_path.strip().rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    win_path = p.replace("/", "\\")
    return "\\\\wsl.localhost\\" + distro + "\\" + win_path


def _probe_wsl_pdk_roots(distro: str) -> list[str]:
    script = (
        'for v in PDK_ROOT PDKPATH PDK_HOME AGENTIC_PDK_SEARCH_PATHS; do '
        'val=$(printenv "$v" 2>/dev/null || true); '
        '[ -n "$val" ] && printf "ENV\\t%s\\t%s\\n" "$v" "$val"; '
        'done; '
        'for p in "$HOME/.volare" "$HOME/pdks" "$HOME/pdk" "/usr/share/pdk" "/usr/local/share/pdk" "/opt/pdk" "/opt/pdks"; do '
        '[ -d "$p" ] && printf "PATH\\t%s\\n" "$p"; '
        'done'
    )
    try:
        result = subprocess.run(
            ["wsl", "-d", distro, "--", "bash", "-lc", script],
            capture_output=True,
            text=True,
            timeout=12,
        )
    except Exception:
        return []
    roots: list[str] = []
    seen: set[str] = set()
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "ENV":
            for chunk in parts[2].split(":"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                unc = _linux_to_wsl_unc(chunk, distro)
                if unc not in seen:
                    seen.add(unc)
                    roots.append(unc)
        elif len(parts) == 2 and parts[0] == "PATH":
            unc = _linux_to_wsl_unc(parts[1], distro)
            if unc not in seen:
                seen.add(unc)
                roots.append(unc)
    return roots


def discover_wsl_pdk_roots() -> list[str]:
    roots: list[str] = []
    for distro in _wsl_distros():
        roots.extend(_probe_wsl_pdk_roots(distro))
    return roots



def _pdk_identity(name: str) -> dict[str, Any]:
    lowered = name.lower()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    for key, data in KNOWN_PDKS.items():
        if lowered.startswith(key) or compact.startswith(key):
            return dict(data)
    node_match = re.search(r"(\d{1,3})\s*nm", lowered)
    return {
        "family": lowered,
        "node_nm": int(node_match.group(1)) if node_match else None,
        "class": "commercial_or_custom",
        "preferred_open_flow": None,
    }


def _iter_candidate_pdk_dirs(root: Path) -> list[Path]:
    candidates: list[Path] = []
    if (root / "libs.tech").is_dir() or (root / "libs.ref").is_dir():
        candidates.append(root)
    try:
        for child in root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                if (child / "libs.tech").is_dir() or (child / "libs.ref").is_dir():
                    candidates.append(child)
    except OSError:
        pass
    try:
        root_depth = len(root.parts)
        for current, dirs, _files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"tmp", "runs", "node_modules"}]
            if depth > 4:
                dirs[:] = []
                continue
            if (current_path / "libs.tech").is_dir() or (current_path / "libs.ref").is_dir():
                candidates.append(current_path)
                dirs[:] = []
    except OSError:
        pass
    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return deduped


def _safe_listdir(path: Path) -> list[Path]:
    try:
        return sorted([item for item in path.iterdir() if not item.name.startswith(".")], key=lambda p: p.name)
    except OSError:
        return []


def _find_files(root: Path, patterns: tuple[str, ...], limit: int = 80) -> list[str]:
    found: list[str] = []
    if not root.is_dir():
        return found
    for pattern in patterns:
        try:
            for path in root.rglob(pattern):
                if path.is_file():
                    found.append(str(path))
                    if len(found) >= limit:
                        return found
        except OSError:
            continue
    return found


def _extract_lef_layers(lef_files: list[str], limit: int = 24) -> list[str]:
    layers: list[str] = []
    seen: set[str] = set()
    for file_name in lef_files[:8]:
        try:
            with open(file_name, "r", errors="ignore") as fh:
                for line in fh:
                    match = re.match(r"\s*LAYER\s+([A-Za-z0-9_]+)", line)
                    if not match:
                        continue
                    layer = match.group(1)
                    if layer not in seen:
                        seen.add(layer)
                        layers.append(layer)
                        if len(layers) >= limit:
                            return layers
        except OSError:
            continue
    return layers


def _count_lef_macros(lef_files: list[str], max_files: int = 60) -> int:
    count = 0
    for file_name in lef_files[:max_files]:
        try:
            with open(file_name, "r", errors="ignore") as fh:
                for line in fh:
                    if line.lstrip().startswith("MACRO "):
                        count += 1
        except OSError:
            continue
    return count


def _standard_cell_libraries(pdk_path: Path) -> list[dict[str, Any]]:
    libs_root = pdk_path / "libs.ref"
    libraries: list[dict[str, Any]] = []
    for lib_dir in _safe_listdir(libs_root):
        if not lib_dir.is_dir():
            continue
        lef_files = _find_files(lib_dir / "lef", ("*.lef",), limit=120)
        lib_files = _find_files(lib_dir / "lib", ("*.lib",), limit=120)
        gds_files = _find_files(lib_dir / "gds", ("*.gds", "*.gds.gz"), limit=40)
        if not (lef_files or lib_files or gds_files):
            continue
        libraries.append({
            "name": lib_dir.name,
            "lef_count": len(lef_files),
            "liberty_count": len(lib_files),
            "gds_count": len(gds_files),
            "macro_count_sample": _count_lef_macros(lef_files),
            "has_openlane_config": (pdk_path / "libs.tech" / lib_dir.name / "openlane" / "config.tcl").is_file(),
        })
    return libraries


def index_pdk_dir(path: str | Path) -> dict[str, Any]:
    pdk_path = Path(path).resolve()
    identity = _pdk_identity(pdk_path.name)
    tech_root = pdk_path / "libs.tech"
    openlane_config = tech_root / "openlane" / "config.tcl"
    tech_lefs = _find_files(tech_root, ("*.tlef", "*.tech.lef", "*.lef"), limit=60)
    drc_decks = _find_files(tech_root, ("*drc*.lydrc", "*drc*.tcl", "*.drc", "*.drcrules"), limit=40)
    lvs_decks = _find_files(tech_root, ("*lvs*.tcl", "*.lvs", "*setup.tcl"), limit=40)
    libraries = _standard_cell_libraries(pdk_path)
    active_scl = os.environ.get("STD_CELL_LIBRARY", "").strip()
    if not active_scl and libraries:
        preferred = [lib for lib in libraries if lib["has_openlane_config"]]
        active_scl = (preferred or libraries)[0]["name"]

    return {
        "name": pdk_path.name,
        "path": str(pdk_path),
        "family": identity["family"],
        "node_nm": identity["node_nm"],
        "class": identity["class"],
        "preferred_open_flow": identity["preferred_open_flow"],
        "openlane": {
            "has_pdk_config": openlane_config.is_file(),
            "config_tcl": str(openlane_config) if openlane_config.is_file() else None,
            "active_scl": active_scl or None,
        },
        "orfs": {
            "platform_aliases": _orfs_platform_aliases(identity["family"], pdk_path.name),
        },
        "libraries": libraries[:80],
        "tech": {
            "tech_lef_count": len(tech_lefs),
            "sample_routing_layers": _extract_lef_layers(tech_lefs),
            "drc_deck_count": len(drc_decks),
            "lvs_deck_count": len(lvs_decks),
        },
        "readiness": _readiness(identity, bool(openlane_config.is_file()), libraries, tech_lefs, drc_decks, lvs_decks),
    }


def _orfs_platform_aliases(family: str, pdk_name: str) -> list[str]:
    aliases = {
        "sky130": ["sky130hd", "sky130hs"],
        "gf180mcu": ["gf180"],
        "asap7": ["asap7"],
        "nangate45": ["nangate45"],
    }.get(family, [])
    if pdk_name not in aliases:
        aliases.append(pdk_name)
    return aliases


def _readiness(
    identity: dict[str, Any],
    has_openlane_config: bool,
    libraries: list[dict[str, Any]],
    tech_lefs: list[str],
    drc_decks: list[str],
    lvs_decks: list[str],
) -> dict[str, Any]:
    has_liberty = any(lib["liberty_count"] for lib in libraries)
    has_lef = any(lib["lef_count"] for lib in libraries) or bool(tech_lefs)
    can_synthesize = has_liberty
    can_harden = has_liberty and has_lef and (has_openlane_config or identity["preferred_open_flow"] == "orfs")
    signoff_ready = can_harden and bool(drc_decks or lvs_decks)
    if signoff_ready:
        tier = "layout_signoff_candidate"
    elif can_harden:
        tier = "layout_generation_candidate"
    elif can_synthesize:
        tier = "synthesis_only"
    else:
        tier = "indexed_metadata_only"
    return {
        "tier": tier,
        "can_synthesize": can_synthesize,
        "can_harden": can_harden,
        "signoff_decks_present": bool(drc_decks or lvs_decks),
    }


def build_pdk_index() -> dict[str, Any]:
    roots = discover_pdk_roots()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root_name in roots:
        root = Path(root_name)
        for pdk_dir in _iter_candidate_pdk_dirs(root):
            key = str(pdk_dir.resolve())
            if key in seen:
                continue
            seen.add(key)
            entries.append(index_pdk_dir(pdk_dir))
    return {
        "roots": roots,
        "pdks": entries,
        "active_pdk": os.environ.get("PDK", "").strip() or (entries[0]["name"] if entries else None),
    }


def select_pdk(index: dict[str, Any], requested: str = "") -> dict[str, Any] | None:
    pdks = index.get("pdks") or []
    if not pdks:
        return None
    requested_lower = requested.lower().strip()
    active_lower = str(index.get("active_pdk") or "").lower()
    for needle in (requested_lower, active_lower):
        if not needle:
            continue
        for pdk in pdks:
            values = {
                str(pdk.get("name", "")).lower(),
                str(pdk.get("family", "")).lower(),
            }
            if needle in values or any(value.startswith(needle) or needle.startswith(value) for value in values if value):
                return pdk
    return pdks[0]
