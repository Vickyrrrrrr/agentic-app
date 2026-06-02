import os
import shutil
from pathlib import Path

DEFAULT_ROOT = os.path.expanduser("~/AgentIC-workspace")


def ensure_workspace(workspace_root: str | None = None) -> str:
    root = workspace_root or DEFAULT_ROOT
    os.makedirs(root, exist_ok=True)
    return root


def design_path(design_name: str, workspace_root: str | None = None) -> str:
    root = ensure_workspace(workspace_root)
    path = os.path.join(root, design_name)
    os.makedirs(path, exist_ok=True)
    return path


def list_designs(workspace_root: str | None = None) -> list[dict]:
    root = ensure_workspace(workspace_root)
    designs = []
    for entry in sorted(os.listdir(root)):
        if entry.startswith("."):
            continue
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            has_gds = bool(list(Path(full).rglob("*.gds")))
            updated_at = max((p.stat().st_mtime for p in Path(full).rglob("*") if p.exists()), default=os.path.getmtime(full))
            designs.append({"name": entry, "has_gds": has_gds, "updated_at": updated_at})
    return designs


def list_artifacts(design_name: str, workspace_root: str | None = None) -> list[dict]:
    root = ensure_workspace(workspace_root)
    rel_base = root
    if not design_name:
        dp = root
    else:
        dp = os.path.join(root, design_name)
        if not os.path.isdir(dp):
            return []
        rel_base = dp
    artifacts = []
    for root_dir, dirs, files in os.walk(dp):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"__pycache__", "node_modules", ".git"}]
        for f in sorted(files):
            if f.startswith("."):
                continue
            full = os.path.join(root_dir, f)
            rel = os.path.relpath(full, rel_base)
            size = os.path.getsize(full)
            artifacts.append({"name": rel, "size": size})
    return artifacts


def read_artifact(design_name: str, file_name: str, workspace_root: str | None = None) -> str | None:
    if not design_name or design_name.startswith("."):
        return None
    root = ensure_workspace(workspace_root)
    dp = os.path.normpath(os.path.join(root, design_name))
    if not os.path.isdir(dp):
        return None
    full = os.path.normpath(os.path.join(dp, file_name))
    dp_norm = os.path.normpath(dp)
    if not (full == dp_norm or full.startswith(dp_norm + os.sep)):
        return None
    rel = os.path.relpath(full, dp_norm)
    if rel.startswith("..") or any(part.startswith(".") for part in Path(rel).parts):
        return None
    if not os.path.isfile(full):
        return None
    if os.path.getsize(full) > 2 * 1024 * 1024:
        return "Preview unavailable: file is larger than 2 MB."
    try:
        with open(full, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def remove_design(design_name: str, workspace_root: str | None = None) -> bool:
    dp = design_path(design_name, workspace_root)
    if os.path.isdir(dp):
        shutil.rmtree(dp)
        return True
    return False
