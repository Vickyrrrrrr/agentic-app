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
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            has_gds = bool(list(Path(full).rglob("*.gds")))
            designs.append({"name": entry, "has_gds": has_gds})
    return designs


def list_artifacts(design_name: str, workspace_root: str | None = None) -> list[dict]:
    root = ensure_workspace(workspace_root)
    # If design_name is empty, list from the workspace root
    if not design_name:
        dp = root
    else:
        dp = os.path.join(root, design_name)
        if not os.path.isdir(dp):
            # Design dir doesn't exist yet — still return root files
            dp = root
    artifacts = []
    for root_dir, dirs, files in os.walk(dp):
        for f in sorted(files):
            full = os.path.join(root_dir, f)
            rel = os.path.relpath(full, root)
            size = os.path.getsize(full)
            artifacts.append({"name": rel, "path": full, "size": size})
    return artifacts


def read_artifact(design_name: str, file_name: str, workspace_root: str | None = None) -> str | None:
    dp = design_path(design_name, workspace_root)
    full = os.path.normpath(os.path.join(dp, file_name))
    if not full.startswith(os.path.normpath(dp)):
        return None
    if not os.path.isfile(full):
        return None
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
