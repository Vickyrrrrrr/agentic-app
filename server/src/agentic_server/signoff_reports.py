from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_server.report_parsers import parse_drc_report, parse_lvs_report


SIGNOFF_REPORT_VERSION = "agentic.signoff_report.v1"
MAX_REPORT_BYTES = 5 * 1024 * 1024


def build_signoff_report(design_root: str, design_name: str = "scratch") -> dict[str, Any]:
    root = Path(design_root).resolve()
    drc = _evidence_for_kind(root, design_name, "drc")
    lvs = _evidence_for_kind(root, design_name, "lvs")
    status = _combined_status(drc, lvs)
    return {
        "schema_version": SIGNOFF_REPORT_VERSION,
        "design_name": design_name or root.name,
        "status": status,
        "drc": drc,
        "lvs": lvs,
        "message": _message(status),
    }


def _evidence_for_kind(root: Path, design_name: str, kind: str) -> dict[str, Any]:
    checkpoint = _from_checkpoints(root, design_name, kind)
    if checkpoint:
        return checkpoint
    file_report = _from_report_files(root, kind)
    if file_report:
        return file_report
    metrics_report = _from_metrics_json(root, kind)
    if metrics_report:
        return metrics_report
    return {
        "kind": kind,
        "status": "missing",
        "source": None,
        "metrics": {},
        "diagnostics": [],
        "summary": {"diagnostic_count": 0, "error_count": 0, "warning_count": 0},
        "message": f"No {kind.upper()} report was found.",
    }


def _from_checkpoints(root: Path, design_name: str, kind: str) -> dict[str, Any] | None:
    checkpoint_file = root / ".agentic" / f"{design_name or 'scratch'}_checkpoints.json"
    if not checkpoint_file.is_file():
        # Fallback to scratch_checkpoints.json if the session used the default scratch profile name
        checkpoint_file = root / ".agentic" / "scratch_checkpoints.json"
    if not checkpoint_file.is_file():
        return None
    try:
        raw = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list):
        return None

    for item in reversed(raw):
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "").lower()
        tool = str(item.get("tool") or "").lower()
        parsed = item.get("parsed_report")
        if isinstance(parsed, dict) and parsed.get("kind") == kind:
            return _response_from_parsed(
                parsed,
                source={
                    "type": "checkpoint",
                    "stage": item.get("stage"),
                    "tool": item.get("tool"),
                    "exit_code": item.get("exit_code"),
                },
            )
        if kind == "drc" and ("drc" in stage or tool in {"magic", "calibre", "klayout"}):
            fallback = _parsed_from_checkpoint_metrics(item, "drc")
            return _response_from_parsed(fallback, source={"type": "checkpoint", "stage": item.get("stage"), "tool": item.get("tool")})
        if kind == "lvs" and ("lvs" in stage or tool in {"netgen"}):
            fallback = _parsed_from_checkpoint_metrics(item, "lvs")
            return _response_from_parsed(fallback, source={"type": "checkpoint", "stage": item.get("stage"), "tool": item.get("tool")})
    return None

def _from_report_files(root: Path, kind: str) -> dict[str, Any] | None:
    for path in _candidate_files(root, kind):
        try:
            if path.stat().st_size > MAX_REPORT_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_drc_report(text, tool=_tool_hint(path, kind)) if kind == "drc" else parse_lvs_report(text, tool=_tool_hint(path, kind))
        if not _has_evidence(parsed):
            continue
        return _response_from_parsed(
            parsed,
            source={
                "type": "file",
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "size_bytes": path.stat().st_size,
                "mtime": path.stat().st_mtime,
            },
        )
    return None
def _candidate_files(root: Path, kind: str) -> list[Path]:
    if not root.is_dir():
        return []

    # We want to find any report file anywhere in the workspace/session subfolders
    # that is related to DRC or LVS, supporting both OSS and proprietary tools.
    patterns = []
    if kind == "drc":
        patterns = [
            "**/*drc*.rpt",
            "**/*drc*.log",
            "**/*drc*.txt",
            "**/*drc*",
            "**/drt.drc",
            "**/*antenna*.rpt",
            "**/*antenna*.log",
            "**/*magic*.drc",
            "**/*klayout*.rpt",
            "**/*calibre*.rpt",
            "**/*calibre*.log",
        ]
    else:
        patterns = [
            "**/*lvs*.rpt",
            "**/*lvs*.log",
            "**/*lvs*.txt",
            "**/*lvs*",
            "**/*netgen*.log",
            "**/*calibre*.rpt",
            "**/*calibre*.log",
        ]

    seen: set[Path] = set()
    files: list[Path] = []

    # Exclude directories like .git, .agentic, and node_modules to avoid scanning unrelated files
    exclude_dirs = {".git", ".agentic", "node_modules", "out", "venv", ".venv"}

    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            # Check if path contains any excluded directory
            if any(part in exclude_dirs for part in path.parts):
                continue
            seen.add(path)
            files.append(path)

    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return files[:40]

def _response_from_parsed(parsed: dict[str, Any], *, source: dict[str, Any]) -> dict[str, Any]:
    kind = str(parsed.get("kind") or "unknown")
    metrics = parsed.get("metrics") if isinstance(parsed.get("metrics"), dict) else {}
    summary = parsed.get("summary") if isinstance(parsed.get("summary"), dict) else {}
    diagnostics = parsed.get("diagnostics") if isinstance(parsed.get("diagnostics"), list) else []
    status = _kind_status(kind, metrics, summary, diagnostics)
    return {
        "kind": kind,
        "status": status,
        "source": source,
        "metrics": metrics,
        "summary": summary,
        "diagnostics": diagnostics[:100],
        "message": f"{kind.upper()} evidence loaded.",
    }


def _kind_status(kind: str, metrics: dict[str, Any], summary: dict[str, Any], diagnostics: list[Any]) -> str:
    if kind == "drc":
        violations = _number(metrics.get("violation_count"))
        errors = _number(summary.get("error_count"))
        if violations is not None:
            return "clean" if violations == 0 else "violating"
        if errors is not None:
            return "clean" if errors == 0 else "violating"
        return "unknown" if diagnostics else "missing"
    if kind == "lvs":
        status = str(metrics.get("status") or "").lower()
        if status == "pass":
            return "clean"
        if status == "fail":
            return "violating"
        errors = _number(summary.get("error_count"))
        if errors is not None and errors > 0:
            return "violating"
        return "unknown"
    return "unknown"


def _combined_status(drc: dict[str, Any], lvs: dict[str, Any]) -> str:
    statuses = {str(drc.get("status") or ""), str(lvs.get("status") or "")}
    if "violating" in statuses:
        return "violating"
    if statuses == {"clean"}:
        return "clean"
    if statuses <= {"missing"}:
        return "missing"
    return "partial"


def _message(status: str) -> str:
    if status == "clean":
        return "DRC and LVS evidence are clean."
    if status == "violating":
        return "Physical verification has violations or mismatches."
    if status == "partial":
        return "Physical verification evidence is incomplete."
    return "No DRC/LVS evidence was found for this design."


def _parsed_from_checkpoint_metrics(item: dict[str, Any], kind: str) -> dict[str, Any]:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    if kind == "drc":
        violations = _number(metrics.get("drc_violations") or metrics.get("violation_count"))
        return {
            "schema_version": "agentic.report_parsers.v1",
            "kind": "drc",
            "tool": item.get("tool") or "generic",
            "summary": {"diagnostic_count": 0, "error_count": 1 if violations and violations > 0 else 0, "warning_count": 0},
            "metrics": {"violation_count": violations} if violations is not None else {},
            "diagnostics": [],
        }
    return {
        "schema_version": "agentic.report_parsers.v1",
        "kind": "lvs",
        "tool": item.get("tool") or "generic",
        "summary": {"diagnostic_count": 0, "error_count": 0, "warning_count": 0},
        "metrics": {},
        "diagnostics": [],
    }


def _tool_hint(path: Path, kind: str) -> str:
    text = str(path).lower()
    if "calibre" in text:
        return "calibre"
    if "klayout" in text:
        return "klayout"
    if "netgen" in text:
        return "netgen"
    return "magic" if kind == "drc" else "netgen"


def _has_evidence(parsed: dict[str, Any]) -> bool:
    metrics = parsed.get("metrics")
    diagnostics = parsed.get("diagnostics")
    return bool(metrics) or bool(diagnostics)


def _number(value: Any) -> float | int | None:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


def _from_metrics_json(root: Path, kind: str) -> dict[str, Any] | None:
    candidates = []
    for pattern in ("**/metrics.json", "**/resolved.json", "metrics.json"):
        candidates.extend(root.glob(pattern))

    candidates = [p for p in candidates if p.is_file()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue

            if kind == "drc":
                drc_err = data.get("magic__drc_error__count")
                if drc_err is None:
                    drc_err = data.get("klayout__drc_error__count")
                if drc_err is None:
                    drc_err = data.get("route__drc_errors")
                if drc_err is None:
                    drc_err = data.get("design__violations")

                if drc_err is not None:
                    violations = int(drc_err)
                    return {
                        "kind": "drc",
                        "status": "clean" if violations == 0 else "violating",
                        "source": {
                            "type": "file",
                            "path": str(path.relative_to(root)).replace("\\", "/"),
                            "size_bytes": path.stat().st_size,
                            "mtime": path.stat().st_mtime,
                        },
                        "metrics": {"violation_count": violations},
                        "summary": {"diagnostic_count": 0, "error_count": violations, "warning_count": 0},
                        "diagnostics": [],
                        "message": f"DRC signoff loaded from {path.name}.",
                    }

            elif kind == "lvs":
                lvs_error_keys = (
                    "design__lvs_error__count",
                    "design__lvs_device_difference__count",
                    "design__lvs_net_difference__count",
                    "design__lvs_property_difference__count",
                )
                lvs_values = [_number(data.get(key)) for key in lvs_error_keys if data.get(key) is not None]

                if lvs_values:
                    errors = int(sum(float(value) for value in lvs_values if value is not None))
                    return {
                        "kind": "lvs",
                        "status": "clean" if errors == 0 else "violating",
                        "source": {
                            "type": "file",
                            "path": str(path.relative_to(root)).replace("\\", "/"),
                            "size_bytes": path.stat().st_size,
                            "mtime": path.stat().st_mtime,
                        },
                        "metrics": {"error_count": errors},
                        "summary": {"diagnostic_count": 0, "error_count": errors, "warning_count": 0},
                        "diagnostics": [],
                        "message": f"LVS signoff loaded from {path.name}.",
                    }
        except Exception:
            continue
    return None
