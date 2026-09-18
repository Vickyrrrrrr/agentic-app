from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentic_server.design_intent import DesignIntent, intent_from_state_or_manifest


CACHE_SCHEMA_VERSION = "agentic.context_cache.v1"
MAX_SEGMENT_SUMMARY_CHARS = 1400
MAX_SEGMENT_PAYLOAD_CHARS = 6000
ROLE_VISIBILITY = {
    "principal": {"intent", "manifest", "module", "file", "evidence", "handoff"},
    "supervisor": {"intent", "manifest", "module", "file", "evidence", "handoff"},
    "spec_architect": {"intent", "manifest", "module", "handoff"},
    "flow_planner": {"intent", "manifest", "evidence", "handoff"},
    "rtl_author": {"intent", "manifest", "module", "file", "evidence", "handoff"},
    "verification_engineer": {"intent", "manifest", "module", "file", "evidence", "handoff"},
    "signoff_critic": {"intent", "manifest", "evidence", "handoff"},
}
RTL_EXTENSIONS = {".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl"}
TB_HINTS = ("/tb/", "/dv/", "testbench", "_tb.")


class StrictContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)


class ContextSegment(StrictContextModel):
    segment_id: str
    kind: Literal["intent", "manifest", "module", "file", "evidence", "handoff"]
    sha256: str
    token_estimate: int
    owner: str | None = None
    path: str | None = None
    role_visibility: list[str] = Field(default_factory=list)
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    updated_at: float = Field(default_factory=time.time)


class ContextCacheFile(StrictContextModel):
    schema_version: str = CACHE_SCHEMA_VERSION
    design_name: str
    workspace_digest: str
    segments: dict[str, ContextSegment] = Field(default_factory=dict)
    updated_at: float = Field(default_factory=time.time)


class RoleContextPacket(StrictContextModel):
    schema_version: str = CACHE_SCHEMA_VERSION
    design_name: str
    role: str
    workspace_digest: str
    budget: dict[str, int]
    selected_segments: list[ContextSegment] = Field(default_factory=list)
    omitted: list[dict[str, Any]] = Field(default_factory=list)
    cache_stats: dict[str, Any] = Field(default_factory=dict)

    def to_prompt_contract(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def build_role_context_packet(
    *,
    workspace_root: str,
    design_name: str,
    role: str,
    user_text: str,
    state: dict[str, Any] | None = None,
    max_segments: int = 14,
    max_tokens: int = 4200,
) -> RoleContextPacket:
    root = Path(workspace_root).resolve()
    normalized_role = _normalize_role(role)
    current_state = state if isinstance(state, dict) else {}
    intent = intent_from_state_or_manifest(str(root), current_state)
    cache = _load_cache(root, design_name)
    before_ids = set(cache.segments)
    proposed = _build_segments(root, design_name, normalized_role, user_text, current_state, intent)
    changed = 0
    for segment in proposed:
        previous = cache.segments.get(segment.segment_id)
        if previous is None or previous.sha256 != segment.sha256:
            changed += 1
        cache.segments[segment.segment_id] = segment
    cache.workspace_digest = _workspace_digest(root, intent)
    cache.updated_at = time.time()
    _save_cache(root, design_name, cache)
    selected, omitted = _select_segments(cache.segments.values(), normalized_role, user_text, max_segments, max_tokens)
    return RoleContextPacket(
        design_name=design_name,
        role=normalized_role,
        workspace_digest=cache.workspace_digest,
        budget={"max_segments": max_segments, "max_tokens": max_tokens},
        selected_segments=selected,
        omitted=omitted,
        cache_stats={
            "stored_segments": len(cache.segments),
            "new_or_changed_segments": changed,
            "reused_segments": len(before_ids & set(cache.segments)) - changed,
            "cache_path": _cache_path(root, design_name).relative_to(root).as_posix(),
        },
    )


def _build_segments(
    root: Path,
    design_name: str,
    role: str,
    user_text: str,
    state: dict[str, Any],
    intent: DesignIntent | None,
) -> list[ContextSegment]:
    segments: list[ContextSegment] = []
    if intent:
        intent_payload = intent.model_dump(mode="json")
        segments.append(_segment(
            kind="intent",
            owner="design_intent",
            path=f"{intent.project_root}/PROJECT_MANIFEST.json",
            visibility=sorted(ROLE_VISIBILITY),
            payload=_compact_intent_payload(intent_payload),
            summary=_intent_summary(intent),
        ))
        manifest_path = root / intent.project_root / "PROJECT_MANIFEST.json"
        if manifest_path.is_file():
            segments.append(_file_segment(
                root=root,
                path=manifest_path,
                kind="manifest",
                owner="design_intent",
                visibility=sorted(ROLE_VISIBILITY),
                max_chars=MAX_SEGMENT_PAYLOAD_CHARS,
            ))
        for module in intent.modules.values():
            segments.append(_segment(
                kind="module",
                owner=module.name,
                path=module.owner_file,
                visibility=_module_visibility(module.name, role),
                payload=module.model_dump(mode="json"),
                summary=(
                    f"{module.name}: kind={module.kind}, owner={module.owner_file}, "
                    f"tb={module.verification_owner or 'none'}, status={module.status}, dialect={module.dialect}"
                ),
            ))
            for rel in (module.owner_file, module.verification_owner):
                path = root / rel if rel else None
                if path and path.is_file():
                    segments.append(_file_segment(
                        root=root,
                        path=path,
                        kind="file",
                        owner=module.name,
                        visibility=_file_visibility(rel, module.name),
                    ))
    segments.extend(_evidence_segments(state))
    segments.extend(_handoff_segments(state))
    return segments


def _segment(
    *,
    kind: Literal["intent", "manifest", "module", "file", "evidence", "handoff"],
    owner: str | None,
    path: str | None,
    visibility: list[str],
    payload: dict[str, Any],
    summary: str,
) -> ContextSegment:
    canonical = json.dumps({"kind": kind, "owner": owner, "path": path, "payload": payload, "summary": summary}, sort_keys=True, default=str)
    return ContextSegment(
        segment_id=_segment_id(kind, owner, path, canonical),
        kind=kind,
        owner=owner,
        path=path,
        role_visibility=visibility,
        payload=_trim_payload(payload),
        summary=summary[:MAX_SEGMENT_SUMMARY_CHARS],
        sha256=_sha256(canonical),
        token_estimate=_estimate_tokens(summary) + _estimate_tokens(json.dumps(payload, default=str)),
    )


def _file_segment(
    *,
    root: Path,
    path: Path,
    kind: Literal["manifest", "file"],
    owner: str | None,
    visibility: list[str],
    max_chars: int = 5000,
) -> ContextSegment:
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    symbols = _extract_hdl_symbols(text) if path.suffix.lower() in RTL_EXTENSIONS else []
    payload = {
        "path": rel,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(text),
        "symbols": symbols,
        "preview": _preview(text, max_chars),
    }
    summary = f"{rel}: {len(text)} chars, sha256={payload['sha256'][:12]}"
    if symbols:
        summary += ", symbols=" + ", ".join(f"{item['kind']}:{item['name']}" for item in symbols[:12])
    return _segment(kind=kind, owner=owner, path=rel, visibility=visibility, payload=payload, summary=summary)


def _evidence_segments(state: dict[str, Any]) -> list[ContextSegment]:
    graph = state.get("evidence_graph") or {}
    nodes = graph.get("nodes") or {}
    segments = []
    for key, node in sorted(nodes.items(), key=lambda item: str((item[1] or {}).get("created_at", "")))[-12:]:
        if not isinstance(node, dict):
            continue
        payload = {
            "id": key,
            "kind": node.get("kind"),
            "ref": node.get("ref"),
            "payload": node.get("payload"),
            "created_at": node.get("created_at"),
        }
        segments.append(_segment(
            kind="evidence",
            owner=str(node.get("kind") or "evidence"),
            path=str(node.get("ref") or ""),
            visibility=["principal", "supervisor", "rtl_author", "verification_engineer", "flow_planner", "signoff_critic"],
            payload=payload,
            summary=f"Evidence {node.get('kind')}: {node.get('ref')}",
        ))
    return segments


def _handoff_segments(state: dict[str, Any]) -> list[ContextSegment]:
    handoffs = state.get("handoffs") or []
    segments = []
    for idx, handoff in enumerate(handoffs[-10:]):
        if not isinstance(handoff, dict):
            continue
        payload = {
            "source_role": handoff.get("source_role"),
            "target_role": handoff.get("target_role"),
            "payload": handoff.get("payload"),
            "created_at": handoff.get("created_at"),
        }
        target = str(handoff.get("target_role") or "supervisor")
        source = str(handoff.get("source_role") or "supervisor")
        visibility = sorted({"principal", "supervisor", _normalize_role(target), _normalize_role(source)})
        segments.append(_segment(
            kind="handoff",
            owner=f"{source}->{target}",
            path=None,
            visibility=visibility,
            payload=payload,
            summary=f"Handoff {source} -> {target}",
        ))
    return segments


def _select_segments(
    segments: Any,
    role: str,
    user_text: str,
    max_segments: int,
    max_tokens: int,
) -> tuple[list[ContextSegment], list[dict[str, Any]]]:
    scored = []
    for segment in segments:
        if role not in segment.role_visibility and "principal" not in segment.role_visibility:
            continue
        scored.append((_score_segment(segment, role, user_text), segment))
    scored.sort(key=lambda item: (-item[0], item[1].kind, item[1].segment_id))
    selected: list[ContextSegment] = []
    omitted: list[dict[str, Any]] = []
    used_tokens = 0
    for score, segment in scored:
        if len(selected) >= max_segments or used_tokens + segment.token_estimate > max_tokens:
            omitted.append({
                "segment_id": segment.segment_id,
                "kind": segment.kind,
                "owner": segment.owner,
                "path": segment.path,
                "score": score,
                "token_estimate": segment.token_estimate,
            })
            continue
        selected.append(segment)
        used_tokens += segment.token_estimate
    return selected, omitted[:40]


def _score_segment(segment: ContextSegment, role: str, user_text: str) -> int:
    score = {"intent": 1000, "manifest": 900, "module": 700, "file": 620, "evidence": 520, "handoff": 500}.get(segment.kind, 100)
    text = f"{segment.owner or ''} {segment.path or ''} {segment.summary}".lower()
    terms = _query_terms(user_text)
    score += sum(45 for term in terms if term in text)
    if role == "rtl_author" and segment.kind in {"module", "file"} and _is_rtl_path(segment.path):
        score += 220
    if role == "verification_engineer" and segment.kind == "file" and _is_tb_path(segment.path):
        score += 220
    if role == "signoff_critic" and segment.kind == "evidence":
        score += 180
    if segment.kind == "evidence" and "rejected" in text:
        score += 120
    return score


def _compact_intent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "intent_id": payload.get("intent_id"),
        "design_name": payload.get("design_name"),
        "project_root": payload.get("project_root"),
        "target": payload.get("target"),
        "modules": payload.get("modules"),
        "implementation_policy": payload.get("implementation_policy"),
        "acceptance": payload.get("acceptance"),
        "unresolved": payload.get("unresolved"),
        "revision": payload.get("revision"),
    }


def _intent_summary(intent: DesignIntent) -> str:
    modules = ", ".join(intent.modules.keys()) or "none"
    target = intent.target.pdk or intent.target.node_nm or "unspecified target"
    policy = intent.implementation_policy.rtl_dialect
    return f"DesignIntent {intent.intent_id}: root={intent.project_root}, target={target}, rtl={policy}, modules={modules}"


def _module_visibility(module_name: str, role: str) -> list[str]:
    return sorted({"principal", "supervisor", role, "rtl_author", "verification_engineer", "spec_architect"})


def _file_visibility(rel: str | None, module_name: str) -> list[str]:
    base = {"principal", "supervisor", "rtl_author"}
    if _is_tb_path(rel):
        base.add("verification_engineer")
    if _is_rtl_path(rel):
        base.update({"verification_engineer", "signoff_critic"})
    return sorted(base)


def _cache_path(root: Path, design_name: str) -> Path:
    return root / ".agentic" / "context_cache" / f"{_safe_design_name(design_name)}.json"


def _load_cache(root: Path, design_name: str) -> ContextCacheFile:
    path = _cache_path(root, design_name)
    if path.is_file():
        try:
            return ContextCacheFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return ContextCacheFile(design_name=design_name, workspace_digest="")


def _save_cache(root: Path, design_name: str, cache: ContextCacheFile) -> None:
    path = _cache_path(root, design_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _workspace_digest(root: Path, intent: DesignIntent | None) -> str:
    manifest = ""
    if intent:
        path = root / intent.project_root / "PROJECT_MANIFEST.json"
        if path.is_file():
            manifest = path.read_text(encoding="utf-8", errors="replace")
    return _sha256(f"{root}:{manifest}")


def _segment_id(kind: str, owner: str | None, path: str | None, canonical: str) -> str:
    stable = path or owner or canonical
    return f"{kind}:{_safe_id(stable)}:{_sha256(canonical)[:10]}"


def _trim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, default=str)
    if len(text) <= MAX_SEGMENT_PAYLOAD_CHARS:
        return payload
    return {
        "truncated": True,
        "sha256": _sha256(text),
        "preview": text[:MAX_SEGMENT_PAYLOAD_CHARS],
    }


def _preview(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 3:]
    return f"{head}\n... [context cache omitted {len(text) - len(head) - len(tail)} chars] ...\n{tail}"


def _extract_hdl_symbols(text: str) -> list[dict[str, Any]]:
    symbols = []
    patterns = [
        ("module", re.compile(r"(?m)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b")),
        ("interface", re.compile(r"(?m)^\s*interface\s+([A-Za-z_][A-Za-z0-9_$]*)\b")),
        ("package", re.compile(r"(?m)^\s*package\s+([A-Za-z_][A-Za-z0-9_$]*)\b")),
    ]
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            symbols.append({"kind": kind, "name": match.group(1), "offset": match.start()})
    return symbols[:64]


def _query_terms(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
    stop = {"the", "and", "for", "with", "chip", "design", "make", "create", "build", "agentic"}
    out = []
    for word in words:
        lowered = word.lower()
        if lowered not in stop and lowered not in out:
            out.append(lowered)
    return out[:16]


def _is_rtl_path(path: str | None) -> bool:
    lower = (path or "").lower()
    return "/rtl/" in f"/{lower}" or Path(lower).suffix in RTL_EXTENSIONS


def _is_tb_path(path: str | None) -> bool:
    lower = (path or "").lower()
    return any(hint in f"/{lower}" for hint in TB_HINTS)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _safe_design_name(design_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in (design_name or "scratch"))
    return cleaned.strip("._") or "scratch"


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "segment").strip("._")
    return cleaned[:80] or "segment"


def _normalize_role(role: str) -> str:
    cleaned = (role or "principal").strip().lower()
    return cleaned if cleaned in ROLE_VISIBILITY else "principal"
