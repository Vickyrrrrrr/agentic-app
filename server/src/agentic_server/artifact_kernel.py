from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from agentic_server.design_intent import CANONICAL_DIRS, MANIFEST_NAME, DesignIntent, intent_from_state_or_manifest, normalize_rel_path


ARTIFACT_KERNEL_VERSION = "agentic.artifact_kernel.v1"

Disposition = Literal["current", "supersedes", "candidate", "rejected"]
Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class ArtifactDecision:
    schema_version: str
    allowed: bool
    severity: Severity
    disposition: Disposition
    path: str
    project_root: str | None
    stage: str
    kind: str
    logical_key: str
    owner_module: str | None = None
    canonical_path: str | None = None
    supersedes: list[str] = field(default_factory=list)
    reason: str = ""
    required_action: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


CANONICAL_STAGE_DIRS: tuple[tuple[str, str], ...] = (
    ("docs/plans/", "planning"),
    ("docs/diagrams/", "diagram"),
    ("rtl/", "rtl"),
    ("tb/", "testbench"),
    ("dv/", "verification"),
    ("constraints/", "constraints"),
    ("scripts/", "flow_script"),
    ("sim/runs/", "simulation"),
    ("synth/", "synthesis"),
    ("pnr/", "implementation"),
    ("sta/", "sta"),
    ("signoff/", "signoff"),
    ("reports/", "report"),
    ("logs/", "log"),
)

KIND_BY_SUFFIX = {
    ".v": "rtl",
    ".sv": "systemverilog",
    ".vh": "rtl_header",
    ".svh": "systemverilog_header",
    ".vhd": "vhdl",
    ".vhdl": "vhdl",
    ".sdc": "constraints",
    ".xdc": "constraints",
    ".tcl": "flow_script",
    ".ys": "flow_script",
    ".mk": "flow_script",
    ".json": "metadata",
    ".yaml": "metadata",
    ".yml": "metadata",
    ".md": "documentation",
    ".mermaid": "diagram",
    ".mmd": "diagram",
    ".rpt": "report",
    ".log": "log",
    ".gds": "layout",
    ".oas": "layout",
    ".lef": "abstract",
    ".def": "floorplan_or_route",
    ".spef": "parasitics",
    ".sdf": "delay",
    ".vcd": "waveform",
    ".fst": "waveform",
}


def evaluate_artifact_write(
    *,
    workspace_root: str,
    design_name: str,
    path: str,
    state: dict[str, Any] | None = None,
    content: str = "",
    old_string: str = "",
) -> ArtifactDecision:
    del design_name
    normalized = normalize_rel_path(path)
    if not normalized:
        return ArtifactDecision(
            schema_version=ARTIFACT_KERNEL_VERSION,
            allowed=False,
            severity="error",
            disposition="rejected",
            path=str(path or ""),
            project_root=None,
            stage="workspace",
            kind="unsafe",
            logical_key="unsafe:path",
            reason="Artifact path is empty, absolute, hidden, or escapes the workspace.",
            required_action="write_inside_active_workspace",
        )

    intent = intent_from_state_or_manifest(workspace_root, state or {})
    project_root = _project_root_for_path(normalized, intent)
    stage = _stage_for_path(normalized, project_root)
    kind = _kind_for_path(normalized)
    owner_module = _owner_module_for_path(intent, normalized)
    canonical_path = _canonical_path(intent, normalized, stage, kind, owner_module)
    logical_key = _logical_key(project_root, stage, kind, owner_module, normalized, canonical_path)
    supersedes = _superseded_current_paths(state or {}, logical_key, normalized)

    placement = _placement_guard(normalized, intent, project_root, stage, kind)
    if placement:
        return ArtifactDecision(
            schema_version=ARTIFACT_KERNEL_VERSION,
            allowed=False,
            severity="error",
            disposition="rejected",
            path=normalized,
            project_root=project_root,
            stage=stage,
            kind=kind,
            logical_key=logical_key,
            owner_module=owner_module,
            canonical_path=canonical_path,
            supersedes=supersedes,
            reason=placement["reason"],
            required_action=placement["required_action"],
            metadata=_metadata(workspace_root, normalized, content, old_string),
        )

    disposition: Disposition = "current"
    reason = "Artifact is owned by the active project contract."
    severity: Severity = "info"
    required_action = None
    if supersedes:
        disposition = "supersedes"
        reason = "Artifact updates the current logical slot and supersedes earlier generated variants."
    elif canonical_path and normalized != canonical_path:
        disposition = "candidate"
        severity = "warning"
        reason = f"Artifact is allowed, but canonical location for this logical slot is `{canonical_path}`."
        required_action = "prefer_canonical_artifact_path"

    return ArtifactDecision(
        schema_version=ARTIFACT_KERNEL_VERSION,
        allowed=True,
        severity=severity,
        disposition=disposition,
        path=normalized,
        project_root=project_root,
        stage=stage,
        kind=kind,
        logical_key=logical_key,
        owner_module=owner_module,
        canonical_path=canonical_path,
        supersedes=supersedes,
        reason=reason,
        required_action=required_action,
        metadata=_metadata(workspace_root, normalized, content, old_string),
    )


def apply_artifact_decision(state: dict[str, Any], decision: ArtifactDecision, artifact_record: dict[str, Any]) -> dict[str, Any]:
    index = state.setdefault("artifact_index", _empty_index())
    if not isinstance(index, dict):
        index = state["artifact_index"] = _empty_index()
    index.setdefault("schema_version", ARTIFACT_KERNEL_VERSION)
    index.setdefault("current_by_key", {})
    index.setdefault("entries", {})
    index.setdefault("superseded", {})
    index.setdefault("revisions", [])

    now = time.time()
    previous_paths = [
        path for path in decision.supersedes
        if path and path != decision.path
    ]
    previous_current = index["current_by_key"].get(decision.logical_key)
    if previous_current and previous_current != decision.path and previous_current not in previous_paths:
        previous_paths.append(previous_current)

    for old_path in previous_paths:
        old_entry = index["entries"].get(old_path) or {}
        old_entry.update({
            "path": old_path,
            "status": "superseded",
            "superseded_by": decision.path,
            "superseded_at": now,
        })
        index["entries"][old_path] = old_entry
        index["superseded"][old_path] = {
            "superseded_by": decision.path,
            "logical_key": decision.logical_key,
            "time": now,
        }
        if old_path in state.get("artifacts", {}):
            state["artifacts"][old_path]["status"] = "superseded"
            state["artifacts"][old_path]["superseded_by"] = decision.path

    entry = {
        **artifact_record,
        "schema_version": ARTIFACT_KERNEL_VERSION,
        "path": decision.path,
        "project_root": decision.project_root,
        "stage": decision.stage,
        "kind": decision.kind,
        "logical_key": decision.logical_key,
        "owner_module": decision.owner_module,
        "canonical_path": decision.canonical_path,
        "status": "current" if decision.disposition in {"current", "supersedes"} else decision.disposition,
        "disposition": decision.disposition,
        "reason": decision.reason,
        "required_action": decision.required_action,
        "updated_at": now,
    }
    index["entries"][decision.path] = entry
    if decision.disposition in {"current", "supersedes"}:
        index["current_by_key"][decision.logical_key] = decision.path
    index["revisions"].append({
        "id": _revision_id(decision, entry),
        "time": now,
        "path": decision.path,
        "logical_key": decision.logical_key,
        "stage": decision.stage,
        "kind": decision.kind,
        "disposition": decision.disposition,
        "supersedes": previous_paths,
        "sha256": artifact_record.get("sha256"),
    })
    index["revisions"] = index["revisions"][-500:]
    return state


def summarize_artifact_index(state: dict[str, Any], limit: int = 16) -> dict[str, Any]:
    index = state.get("artifact_index") or {}
    entries = list((index.get("entries") or {}).values())
    entries.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return {
        "schema_version": index.get("schema_version") or ARTIFACT_KERNEL_VERSION,
        "current_count": len(index.get("current_by_key") or {}),
        "superseded_count": len(index.get("superseded") or {}),
        "latest": [
            {
                "path": entry.get("path"),
                "stage": entry.get("stage"),
                "kind": entry.get("kind"),
                "status": entry.get("status"),
                "logical_key": entry.get("logical_key"),
                "owner_module": entry.get("owner_module"),
                "canonical_path": entry.get("canonical_path"),
            }
            for entry in entries[:limit]
        ],
    }


def _empty_index() -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_KERNEL_VERSION,
        "current_by_key": {},
        "entries": {},
        "superseded": {},
        "revisions": [],
    }


def _project_root_for_path(path: str, intent: DesignIntent | None) -> str | None:
    if intent and (path == intent.project_root or path.startswith(f"{intent.project_root}/")):
        return intent.project_root
    first = path.split("/", 1)[0]
    if intent:
        return intent.project_root
    if "/" in path and first not in {item.split("/", 1)[0] for item in CANONICAL_DIRS}:
        return first
    return None


def _stage_for_path(path: str, project_root: str | None) -> str:
    local = _local_path(path, project_root)
    for prefix, stage in CANONICAL_STAGE_DIRS:
        if local.startswith(prefix):
            return stage
    suffix = Path(local).suffix.lower()
    if suffix in {".rpt", ".log"}:
        return "report" if suffix == ".rpt" else "log"
    if suffix in {".vcd", ".fst"}:
        return "simulation"
    if suffix in {".gds", ".oas", ".lef", ".def"}:
        return "signoff"
    return "workspace"


def _kind_for_path(path: str) -> str:
    lower = path.lower()
    if lower.endswith("makefile"):
        return "flow_script"
    return KIND_BY_SUFFIX.get(Path(lower).suffix, "artifact")


def _owner_module_for_path(intent: DesignIntent | None, path: str) -> str | None:
    if not intent:
        return None
    for name, module in intent.modules.items():
        if path in {module.owner_file, module.verification_owner}:
            return name
    local = _local_path(path, intent.project_root)
    stem = Path(local).stem
    for name in intent.modules:
        if stem in {name, f"tb_{name}", f"{name}_tb"}:
            return name
    return None


def _canonical_path(intent: DesignIntent | None, path: str, stage: str, kind: str, owner_module: str | None) -> str | None:
    if not intent:
        return None
    if owner_module and owner_module in intent.modules:
        module = intent.modules[owner_module]
        if stage == "rtl":
            return module.owner_file
        if stage in {"testbench", "verification"} and module.verification_owner:
            return module.verification_owner
    root = intent.project_root
    if stage == "planning":
        return f"{root}/docs/plans/approval_plan.md"
    if kind == "diagram" or stage == "diagram":
        if Path(path).stem.startswith("architecture"):
            return f"{root}/docs/diagrams/architecture.mermaid"
    if path.endswith(MANIFEST_NAME):
        return f"{root}/{MANIFEST_NAME}"
    return None


def _logical_key(
    project_root: str | None,
    stage: str,
    kind: str,
    owner_module: str | None,
    path: str,
    canonical_path: str | None,
) -> str:
    root = project_root or "workspace"
    if owner_module:
        return f"{root}:{stage}:{kind}:{owner_module}"
    if canonical_path:
        return f"{root}:{stage}:{kind}:{Path(canonical_path).stem}"
    local = _local_path(path, project_root)
    stem = Path(local).stem or hashlib.sha256(path.encode("utf-8")).hexdigest()[:10]
    if stage in {"planning", "diagram"}:
        normalized_stem = "architecture" if "architecture" in stem.lower() else stem
        return f"{root}:{stage}:{kind}:{normalized_stem}"
    return f"{root}:{stage}:{kind}:{stem}"


def _placement_guard(
    path: str,
    intent: DesignIntent | None,
    project_root: str | None,
    stage: str,
    kind: str,
) -> dict[str, str] | None:
    if not intent:
        return None
    if not path.startswith(f"{intent.project_root}/") and path != intent.project_root:
        return {
            "reason": f"`{path}` is outside the active project root `{intent.project_root}/`.",
            "required_action": "write_under_active_project_root_or_create_new_design_revision",
        }
    local = _local_path(path, project_root)
    if kind == "diagram" and not local.startswith("docs/diagrams/"):
        return {
            "reason": "Mermaid/diagram artifacts must live under docs/diagrams for the active project.",
            "required_action": "write_diagram_under_docs_diagrams",
        }
    if stage == "planning" and not local.startswith("docs/plans/"):
        return {
            "reason": "Approval and planning documents must live under docs/plans for the active project.",
            "required_action": "write_plan_under_docs_plans",
        }
    return None


def _local_path(path: str, project_root: str | None) -> str:
    if project_root and path.startswith(f"{project_root}/"):
        return path[len(project_root) + 1:]
    return path


def _superseded_current_paths(state: dict[str, Any], logical_key: str, new_path: str) -> list[str]:
    index = state.get("artifact_index") or {}
    current = (index.get("current_by_key") or {}).get(logical_key)
    if current and current != new_path:
        return [current]
    return []


def _metadata(workspace_root: str, path: str, content: str, old_string: str) -> dict[str, Any]:
    full = Path(workspace_root).resolve() / path
    return {
        "exists_before_write": full.is_file(),
        "old_string_edit": bool(old_string),
        "content_digest": hashlib.sha256((content or "").encode("utf-8")).hexdigest() if content else None,
        "extension": Path(path).suffix.lower(),
        "workspace_relative": os.path.relpath(full, Path(workspace_root).resolve()) if full.is_absolute() else path,
    }


def _revision_id(decision: ArtifactDecision, entry: dict[str, Any]) -> str:
    payload = {
        "path": decision.path,
        "logical_key": decision.logical_key,
        "sha256": entry.get("sha256"),
        "time": entry.get("updated_at"),
    }
    return "artrev_" + hashlib.sha256(repr(sorted(payload.items())).encode("utf-8")).hexdigest()[:16]
