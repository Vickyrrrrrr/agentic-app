from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from artifact_kernel import apply_artifact_decision, evaluate_artifact_write, summarize_artifact_index


STATE_VERSION = 1

STAGE_HINTS = {
    "rtl": ("rtl/", ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl"),
    "testbench": ("tb/", "_tb.", "testbench"),
    "constraints": ("constraints/", ".sdc"),
    "synthesis": ("synth/", ".ys"),
    "implementation": ("pnr/", "openlane/", "openroad/", "hardening/"),
    "sta": ("sta/", ".spef", ".sdf"),
    "signoff": ("signoff/", ".gds", ".lef", ".def", ".lvs", ".drc"),
    "reporting": ("reports/", ".rpt", ".md"),
}


def _now() -> float:
    return time.time()


def _safe_design_name(design_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in (design_name or "scratch"))
    return cleaned.strip("._") or "scratch"


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def infer_stage(path_or_stage: str) -> str:
    lower = (path_or_stage or "").replace("\\", "/").lower()
    for stage, hints in STAGE_HINTS.items():
        if any(hint in lower or lower.endswith(hint) for hint in hints):
            return stage
    return "workspace"


class DesignStateStore:
    """Persistent AgentIC VLSI build ledger.

    This is intentionally structured: the LLM may operate the flow, but AgentIC
    owns the evidence model for files, checkpoints, flow choices, and artifacts.
    """

    def __init__(self, workspace_root: str, design_name: str = "scratch"):
        self.workspace_root = Path(workspace_root).resolve()
        self.design_name = _safe_design_name(design_name)
        self.state_dir = self.workspace_root / ".agentic" / "design_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / f"{self.design_name}.json"

    def load(self) -> dict[str, Any]:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return self._normalize(data)
            except (OSError, json.JSONDecodeError):
                pass
        return self._normalize({})

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(state)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        return normalized

    def summary(self, max_events: int = 12) -> dict[str, Any]:
        state = self.load()
        return {
            "design_name": state["design_name"],
            "updated_at": state["updated_at"],
            "intent": state.get("intent", {}),
            "design_intent": state.get("design_intent"),
            "module_ownership": state.get("module_ownership") or {},
            "implementation_policy": state.get("implementation_policy") or {},
            "mental_model": _compact_mental_model(state.get("mental_model") or {}),
            "context_contract": state.get("context_contract"),
            "permission_scope": state.get("permission_scope"),
            "flow_decision": _compact_flow_decision(state.get("flow_decision", {})),
            "stage_status": state.get("stage_status", {}),
            "artifact_count": len(state.get("artifacts", {})),
            "artifact_index": summarize_artifact_index(state),
            "checkpoint_count": len(state.get("checkpoints", [])),
            "evidence_count": len((state.get("evidence_graph") or {}).get("nodes", {})),
            "handoff_count": len(state.get("handoffs", [])),
            "latest_handoffs": state.get("handoffs", [])[-6:],
            "latest_evidence": _latest_evidence_nodes(state.get("evidence_graph") or {}, limit=8),
            "latest_events": state.get("timeline", [])[-max_events:],
        }

    def set_intent(self, user_text: str, pdk_profile: str = "") -> dict[str, Any]:
        state = self.load()
        state["intent"] = {
            "user_text_digest": hashlib.sha256((user_text or "").encode("utf-8")).hexdigest(),
            "user_text_preview": (user_text or "")[:500],
            "pdk_profile": pdk_profile or None,
            "captured_at": _now(),
        }
        self._append_event(state, "intent", "captured", {"pdk_profile": pdk_profile or None})
        return self.save(state)

    def set_flow_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        state["flow_decision"] = decision
        backend = decision.get("backend") or "none"
        self._append_event(state, "flow", "selected", {
            "backend": backend,
            "profile": decision.get("profile"),
            "confidence": decision.get("confidence"),
        })
        return self.save(state)

    def set_context_contract(self, contract: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        state["context_contract"] = contract
        state["permission_scope"] = contract.get("permission_scope") or {}
        self._append_event(state, "kernel", "context_contract", {
            "turn_digest": contract.get("turn_digest"),
            "scope": (contract.get("permission_scope") or {}).get("name"),
            "roles": contract.get("active_roles", []),
        })
        return self.save(state)

    def set_design_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        state["design_intent"] = intent
        modules = intent.get("modules") if isinstance(intent, dict) else {}
        ownership = {}
        if isinstance(modules, dict):
            for name, module in modules.items():
                if isinstance(module, dict):
                    ownership[name] = {
                        "owner_file": module.get("owner_file"),
                        "verification_owner": module.get("verification_owner"),
                        "status": module.get("status"),
                        "dialect": module.get("dialect"),
                        "create_policy": module.get("create_policy"),
                    }
        state["module_ownership"] = ownership
        state["implementation_policy"] = intent.get("implementation_policy") if isinstance(intent, dict) else {}
        self._append_event(state, "intent", "design_intent_updated", {
            "intent_id": intent.get("intent_id") if isinstance(intent, dict) else None,
            "project_root": intent.get("project_root") if isinstance(intent, dict) else None,
            "module_count": len(ownership),
            "revision": intent.get("revision") if isinstance(intent, dict) else None,
        })
        return self.save(state)

    def upsert_design_fact(self, namespace: str, key: str, value: Any, source: str = "agent") -> dict[str, Any]:
        state = self.load()
        model = state["mental_model"]
        bucket = model.setdefault(namespace, {})
        bucket[key] = {
            "value": value,
            "source": source,
            "updated_at": _now(),
        }
        self._append_event(state, "mental_model", "upsert", {
            "namespace": namespace,
            "key": key,
            "source": source,
        })
        return self.save(state)

    def record_handoff(self, source_role: str, target_role: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        handoff = {
            "id": _node_id("handoff", f"{source_role}->{target_role}", payload),
            "source_role": source_role,
            "target_role": target_role,
            "payload": _compact_payload(payload),
            "created_at": _now(),
        }
        state["handoffs"].append(handoff)
        state["handoffs"] = state["handoffs"][-200:]
        self._append_event(state, "handoff", "created", {
            "id": handoff["id"],
            "source_role": source_role,
            "target_role": target_role,
        })
        return self.save(state)

    def record_evidence(self, kind: str, ref: str, payload: dict[str, Any], links: list[dict[str, str]] | None = None) -> dict[str, Any]:
        state = self.load()
        graph = state["evidence_graph"]
        node_id = _node_id(kind, ref, payload)
        graph["nodes"][node_id] = {
            "id": node_id,
            "kind": kind,
            "ref": ref,
            "payload": _compact_payload(payload),
            "created_at": _now(),
        }
        for link in links or []:
            graph["edges"].append({
                "source": link.get("source") or node_id,
                "target": link.get("target") or "",
                "kind": link.get("kind") or "relates_to",
                "created_at": _now(),
            })
        graph["edges"] = graph["edges"][-500:]
        self._append_event(state, "evidence", "recorded", {
            "id": node_id,
            "kind": kind,
            "ref": ref,
        })
        return self.save(state)

    def record_file(self, rel_path: str, action: str = "write") -> dict[str, Any]:
        state = self.load()
        full = (self.workspace_root / rel_path).resolve()
        decision = evaluate_artifact_write(
            workspace_root=str(self.workspace_root),
            design_name=self.design_name,
            path=rel_path,
            state=state,
        )
        artifact = {
            "path": rel_path,
            "stage": decision.stage or infer_stage(rel_path),
            "kind": decision.kind or _kind_for_path(rel_path),
            "size": full.stat().st_size if full.is_file() else None,
            "sha256": _sha256_file(full),
            "status": "current",
            "logical_key": decision.logical_key,
            "owner_module": decision.owner_module,
            "canonical_path": decision.canonical_path,
            "disposition": decision.disposition,
            "updated_at": _now(),
        }
        state["artifacts"][rel_path] = artifact
        state = apply_artifact_decision(state, decision, artifact)
        self._set_stage_status(state, artifact["stage"], "updated")
        self._append_event(state, "artifact", action, {
            "path": rel_path,
            "stage": artifact["stage"],
            "kind": artifact["kind"],
            "logical_key": artifact["logical_key"],
            "disposition": artifact["disposition"],
            "supersedes": decision.supersedes,
            "sha256": artifact["sha256"],
        })
        node_id = _node_id("artifact", rel_path, artifact)
        state["evidence_graph"]["nodes"][node_id] = {
            "id": node_id,
            "kind": "artifact",
            "ref": rel_path,
            "payload": _compact_payload(artifact),
            "created_at": _now(),
        }
        return self.save(state)

    def record_checkpoint(self, tool: str, stage: str, verdict: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        stage_name = stage or verdict.get("stage") or infer_stage(tool)
        checkpoint = {
            "tool": tool or verdict.get("tool") or "unknown",
            "stage": stage_name,
            "pass": bool(verdict.get("pass")),
            "exit_code": verdict.get("exit_code"),
            "errors": verdict.get("errors", [])[:15],
            "warnings": verdict.get("warnings", [])[:15],
            "metrics": verdict.get("metrics", {}),
            "semantic_diagnostics": verdict.get("semantic_diagnostics") or {},
            "artifact_hashes": verdict.get("artifact_hashes", {}),
            "captured_at": _now(),
        }
        state["checkpoints"].append(checkpoint)
        self._set_stage_status(state, stage_name, "passed" if checkpoint["pass"] else "failed")
        self._append_event(state, "checkpoint", "passed" if checkpoint["pass"] else "failed", {
            "tool": checkpoint["tool"],
            "stage": checkpoint["stage"],
            "metrics": checkpoint["metrics"],
            "dominant_class": (checkpoint.get("semantic_diagnostics") or {}).get("dominant_class"),
        })
        node_id = _node_id("checkpoint", f"{checkpoint['tool']}:{checkpoint['stage']}", checkpoint)
        state["evidence_graph"]["nodes"][node_id] = {
            "id": node_id,
            "kind": "checkpoint",
            "ref": f"{checkpoint['tool']}:{checkpoint['stage']}",
            "payload": _compact_payload(checkpoint),
            "created_at": _now(),
        }
        return self.save(state)

    def record_command(self, command: str, tool: str = "", stage: str = "", exit_code: int | None = None) -> dict[str, Any]:
        state = self.load()
        command_digest = hashlib.sha256((command or "").encode("utf-8")).hexdigest()
        command_record = {
            "tool": tool or "shell",
            "stage": stage or infer_stage(command),
            "exit_code": exit_code,
            "command_digest": command_digest,
            "command_preview": _safe_command_preview(command),
            "captured_at": _now(),
        }
        state["commands"].append(command_record)
        self._append_event(state, "command", "ran", {
            "tool": tool or "shell",
            "stage": stage or infer_stage(command),
            "exit_code": exit_code,
            "command_digest": command_digest,
        })
        node_id = _node_id("command", command_digest, command_record)
        state["evidence_graph"]["nodes"][node_id] = {
            "id": node_id,
            "kind": "command",
            "ref": command_digest,
            "payload": _compact_payload(command_record),
            "created_at": _now(),
        }
        return self.save(state)

    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        created_at = data.get("created_at") or _now()
        return {
            "version": data.get("version") or STATE_VERSION,
            "design_name": data.get("design_name") or self.design_name,
            "created_at": created_at,
            "updated_at": _now(),
            "intent": data.get("intent") or {},
            "design_intent": data.get("design_intent"),
            "module_ownership": data.get("module_ownership") or {},
            "implementation_policy": data.get("implementation_policy") or {},
            "mental_model": data.get("mental_model") or {
                "spec": {},
                "interfaces": {},
                "constraints": {},
                "assumptions": {},
                "decisions": {},
            },
            "context_contract": data.get("context_contract"),
            "permission_scope": data.get("permission_scope"),
            "flow_decision": data.get("flow_decision"),
            "stage_status": data.get("stage_status") or {},
            "artifacts": data.get("artifacts") or {},
            "artifact_index": data.get("artifact_index") or {
                "schema_version": "agentic.artifact_kernel.v1",
                "current_by_key": {},
                "entries": {},
                "superseded": {},
                "revisions": [],
            },
            "checkpoints": data.get("checkpoints") or [],
            "commands": data.get("commands") or [],
            "handoffs": data.get("handoffs") or [],
            "evidence_graph": data.get("evidence_graph") or {"nodes": {}, "edges": []},
            "timeline": data.get("timeline") or [],
        }

    def _set_stage_status(self, state: dict[str, Any], stage: str, status: str) -> None:
        state["stage_status"][stage or "workspace"] = {
            "status": status,
            "updated_at": _now(),
        }

    def _append_event(self, state: dict[str, Any], kind: str, action: str, payload: dict[str, Any]) -> None:
        state["timeline"].append({
            "time": _now(),
            "kind": kind,
            "action": action,
            "payload": payload,
        })
        state["timeline"] = state["timeline"][-300:]


def _safe_command_preview(command: str) -> str:
    text = " ".join((command or "").split())
    sensitive = ("LM_LICENSE_FILE", "CDS_LIC_FILE", "SNPSLMD_LICENSE_FILE", "MGLS_LICENSE_FILE", "license", "token", "secret")
    if any(marker.lower() in text.lower() for marker in sensitive):
        return "[redacted command with license or secret material]"
    return text[:240]


def _kind_for_path(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl")):
        return "rtl"
    if lower.endswith((".sdc", ".xdc")):
        return "constraints"
    if lower.endswith((".tcl", ".ys", ".mk", "makefile")):
        return "flow_script"
    if lower.endswith((".gds", ".lef", ".def", ".spef", ".sdf")):
        return "physical_artifact"
    if lower.endswith((".rpt", ".log", ".md", ".json", ".yaml", ".yml")):
        return "report"
    return "artifact"


def _node_id(kind: str, ref: str, payload: Any) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    safe_ref = hashlib.sha256((ref or "").encode("utf-8")).hexdigest()[:8]
    return f"{kind}:{safe_ref}:{digest}"


def _compact_payload(payload: Any, max_chars: int = 2400) -> Any:
    try:
        text = json.dumps(payload, sort_keys=True, default=str)
    except TypeError:
        text = str(payload)
    if len(text) <= max_chars:
        return payload
    return {
        "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "preview": text[:max_chars],
        "truncated": True,
    }


def _compact_mental_model(model: dict[str, Any]) -> dict[str, Any]:
    compact = {}
    for namespace, facts in (model or {}).items():
        if isinstance(facts, dict):
            compact[namespace] = {
                key: value
                for key, value in list(facts.items())[:20]
            }
        else:
            compact[namespace] = facts
    return compact


def _latest_evidence_nodes(graph: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    nodes = list((graph.get("nodes") or {}).values())
    nodes.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return [
        {
            "id": node.get("id"),
            "kind": node.get("kind"),
            "ref": node.get("ref"),
            "created_at": node.get("created_at"),
        }
        for node in nodes[:limit]
    ]


def _compact_flow_decision(decision: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(decision, dict):
        decision = {}
    selected = decision.get("selected_pdk") or {}
    return {
        "profile": decision.get("profile"),
        "backend": decision.get("backend"),
        "confidence": decision.get("confidence"),
        "selected_pdk": {
            "name": selected.get("name"),
            "family": selected.get("family"),
            "node_nm": selected.get("node_nm"),
            "class": selected.get("class"),
            "readiness": selected.get("readiness"),
        } if selected else None,
        "rationale": decision.get("rationale", [])[:5],
        "blockers": decision.get("blockers", [])[:8],
        "setup_actions": decision.get("setup_actions", [])[:5],
        "run_strategy": decision.get("run_strategy", {}),
    }
