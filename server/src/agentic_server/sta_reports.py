from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentic_server.report_parsers import parse_sta_report


STA_REPORT_VERSION = "agentic.sta_report.v1"
MAX_REPORT_BYTES = 5 * 1024 * 1024


def build_sta_report(design_root: str, design_name: str = "scratch") -> dict[str, Any]:
    root = Path(design_root).resolve()
    checkpoint = _from_checkpoints(root, design_name)
    if checkpoint:
        return checkpoint

    file_report = _from_report_files(root)
    if file_report:
        return file_report

    metrics_report = _from_metrics_json(root)
    if metrics_report:
        return metrics_report

    return {
        "schema_version": STA_REPORT_VERSION,
        "design_name": design_name or root.name,
        "status": "missing",
        "source": None,
        "wns": None,
        "tns": None,
        "paths": [],
        "parsed_report": None,
        "message": "No STA checkpoint or timing report was found for this design.",
    }


def _from_checkpoints(root: Path, design_name: str) -> dict[str, Any] | None:
    checkpoint_file = root / ".agentic" / f"{design_name or 'scratch'}_checkpoints.json"
    if not checkpoint_file.is_file():
        # Fallback to scratch_checkpoints.json
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
        if "sta" not in stage and tool not in {"opensta", "pt_shell", "primetime", "tempus"}:
            continue
        parsed = item.get("parsed_report")
        if not isinstance(parsed, dict) or parsed.get("kind") != "sta":
            parsed = _parsed_from_checkpoint_metrics(item)
        return _response_from_parsed(
            parsed,
            design_name=design_name or root.name,
            source={
                "type": "checkpoint",
                "stage": item.get("stage"),
                "tool": item.get("tool"),
                "exit_code": item.get("exit_code"),
            },
        )
    return None

def _from_report_files(root: Path) -> dict[str, Any] | None:
    candidates = _candidate_sta_files(root)
    for path in candidates:
        try:
            if path.stat().st_size > MAX_REPORT_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_sta_report(text, tool=_tool_hint(path))
        has_timing_data = bool(parsed.get("diagnostics")) or bool(parsed.get("metrics"))
        if not has_timing_data:
            continue
        return _response_from_parsed(
            parsed,
            design_name=root.name,
            source={
                "type": "file",
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "size_bytes": path.stat().st_size,
                "mtime": path.stat().st_mtime,
            },
        )
    return None
def _candidate_sta_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    patterns = [
        "**/*sta*.rpt",
        "**/*sta*.log",
        "**/*timing*.rpt",
        "**/*timing*.log",
        "**/*slack*.rpt",
        "**/*.summary.rpt",
        "**/*primetime*.rpt",
        "**/*tempus*.rpt",
        "**/*opensta*.rpt",
    ]
    seen: set[Path] = set()
    files: list[Path] = []

    exclude_dirs = {".git", ".agentic", "node_modules", "out", "venv", ".venv"}

    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            if any(part in exclude_dirs for part in path.parts):
                continue
            seen.add(path)
            files.append(path)

    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return files[:40]
def _parsed_from_checkpoint_metrics(item: dict[str, Any]) -> dict[str, Any]:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    parsed_metrics: dict[str, Any] = {}
    for src, dest in (
        ("wns_ns", "wns_ns"),
        ("worst_slack_ns", "wns_ns"),
        ("tns_ns", "tns_ns"),
        ("violating_paths", "violating_paths"),
    ):
        value = metrics.get(src)
        if value is None:
            continue
        parsed_metrics[dest] = _number(value)
    return {
        "schema_version": "agentic.report_parsers.v1",
        "kind": "sta",
        "tool": item.get("tool") or "generic",
        "summary": {
            "diagnostic_count": 0,
            "error_count": 1 if _is_negative(parsed_metrics.get("wns_ns")) else 0,
            "warning_count": 0,
        },
        "metrics": {key: value for key, value in parsed_metrics.items() if value is not None},
        "diagnostics": [],
    }


def _response_from_parsed(parsed: dict[str, Any], *, design_name: str, source: dict[str, Any]) -> dict[str, Any]:
    metrics = parsed.get("metrics") if isinstance(parsed.get("metrics"), dict) else {}
    diagnostics = parsed.get("diagnostics") if isinstance(parsed.get("diagnostics"), list) else []
    wns = _number(metrics.get("wns_ns"))
    tns = _number(metrics.get("tns_ns"))
    paths = [_path_from_diagnostic(item) for item in diagnostics if isinstance(item, dict)]
    paths = [item for item in paths if item]
    violating = (wns is not None and wns < 0) or any((path.get("slack") or 0) < 0 for path in paths)
    return {
        "schema_version": STA_REPORT_VERSION,
        "design_name": design_name,
        "status": "violating" if violating else "ready",
        "source": source,
        "wns": wns,
        "tns": tns,
        "paths": paths,
        "parsed_report": parsed,
        "message": "STA evidence loaded.",
    }


def _path_from_diagnostic(item: dict[str, Any]) -> dict[str, Any] | None:
    slack = _number(item.get("slack_ns"))
    startpoint = item.get("startpoint")
    endpoint = item.get("endpoint")
    if slack is None and not startpoint and not endpoint:
        return None
    return {
        "startpoint": str(startpoint or "unknown"),
        "endpoint": str(endpoint or "unknown"),
        "path_group": item.get("path_group"),
        "slack": slack if slack is not None else 0.0,
        "delay": _number(item.get("delay_ns")),
        "severity": item.get("severity") or ("error" if slack is not None and slack < 0 else "info"),
        "message": item.get("message"),
    }


def _tool_hint(path: Path) -> str:
    text = str(path).lower()
    if "primetime" in text or "pt_" in text:
        return "pt_shell"
    if "tempus" in text:
        return "tempus"
    return "opensta"


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


def _is_negative(value: Any) -> bool:
    number = _number(value)
    return number is not None and number < 0


def _from_metrics_json(root: Path) -> dict[str, Any] | None:
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

            setup_ws = _number(data.get("timing__setup__ws"))
            hold_ws = _number(data.get("timing__hold__ws"))
            setup_wns = _number(data.get("timing__setup__wns"))
            hold_wns = _number(data.get("timing__hold__wns"))

            slack_values = [v for v in (setup_ws, hold_ws, setup_wns, hold_wns) if v is not None]
            wns_val = min(float(value) for value in slack_values) if slack_values else None

            if wns_val is not None:
                setup_tns = _number(data.get("timing__setup__tns")) or 0.0
                hold_tns = _number(data.get("timing__hold__tns")) or 0.0
                tns_val = float(setup_tns) + float(hold_tns)
                return {
                    "schema_version": STA_REPORT_VERSION,
                    "design_name": root.name,
                    "status": "clean" if wns_val >= 0 else "violating",
                    "source": {
                        "type": "file",
                        "path": str(path.relative_to(root)).replace("\\", "/"),
                        "size_bytes": path.stat().st_size,
                        "mtime": path.stat().st_mtime,
                    },
                    "wns": wns_val,
                    "tns": tns_val,
                    "paths": [],
                    "parsed_report": {
                        "kind": "sta",
                        "metrics": {
                            "wns_ns": wns_val,
                            "tns_ns": tns_val,
                            "setup_slack_ns": float(setup_ws if setup_ws is not None else setup_wns or 0.0),
                            "hold_slack_ns": float(hold_ws if hold_ws is not None else hold_wns or 0.0)
                        }
                    },
                    "message": f"Timing signoff loaded from {path.name}.",
                }
        except Exception:
            continue
    return None
