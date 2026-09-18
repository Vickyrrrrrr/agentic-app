from __future__ import annotations

import re
from typing import Any


REPORT_PARSERS_VERSION = "agentic.report_parsers.v1"


def parse_report(*, stage: str, tool: str, text: str) -> dict[str, Any]:
    stage_norm = (stage or "").strip().lower()
    tool_norm = (tool or "").strip().lower()
    content = text or ""
    if stage_norm in {"lint", "rtl_lint"} or tool_norm in {"verilator", "spyglass", "hal", "questa_lint"}:
        return parse_lint_report(content, tool=tool_norm or "generic")
    if stage_norm in {"sta", "timing", "signoff_timing"} or tool_norm in {"opensta", "pt_shell", "primetime", "tempus"}:
        return parse_sta_report(content, tool=tool_norm or "generic")
    if stage_norm in {"drc", "physical_verification", "signoff"} or tool_norm in {"magic", "calibre", "klayout"}:
        return parse_drc_report(content, tool=tool_norm or "generic")
    if stage_norm in {"lvs"} or tool_norm in {"netgen"}:
        return parse_lvs_report(content, tool=tool_norm or "generic")
    if stage_norm in {"synthesis", "synth"} or tool_norm in {"yosys", "abc", "genus", "dc_shell"}:
        return parse_synthesis_log(content, tool=tool_norm or "generic")
    return _base("generic", tool_norm or "generic", {"line_count": len(content.splitlines())}, [])


def parse_lint_report(text: str, *, tool: str = "generic") -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    patterns = (
        re.compile(r"^%(?P<severity>Error|Warning)(?:-(?P<code>[A-Z0-9_]+))?:\s+(?P<file>[^:\s][^:]*):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.*)$"),
        re.compile(r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<severity>error|warning|fatal):\s*(?P<message>.*)$", re.I),
        re.compile(r"^(?P<severity>Warning|Error|Fatal):\s*(?:\[(?P<code>[^\]]+)\]\s*)?(?P<message>.*?)(?:\s+at\s+(?P<file>[^:\s]+):(?P<line>\d+))?$", re.I),
    )
    for line_no, line in enumerate(text.splitlines(), 1):
        item = _first_match(patterns, line)
        if not item:
            continue
        severity = _severity(item.get("severity"))
        diagnostics.append({
            "severity": severity,
            "code": _clean(item.get("code")),
            "message": _clean(item.get("message")) or line.strip(),
            "file": _clean(item.get("file")),
            "line": _to_int(item.get("line")),
            "column": _to_int(item.get("col")),
            "log_line": line_no,
        })
    return _base(
        "lint",
        tool,
        {
            "error_count": sum(1 for item in diagnostics if item["severity"] == "error"),
            "warning_count": sum(1 for item in diagnostics if item["severity"] == "warning"),
        },
        diagnostics,
    )


def parse_sta_report(text: str, *, tool: str = "generic") -> dict[str, Any]:
    metrics = {
        "wns_ns": _first_float(
            text,
            (
                r"(?im)^\s*(?:wns|worst\s+negative\s+slack|worst\s+slack)\s*(?:[:=]|\s)\s*(-?\d+(?:\.\d+)?)",
                r"(?im)^\s*slack\s+\((?:VIOLATED|MET)\)\s*(-?\d+(?:\.\d+)?)",
            ),
        ),
        "tns_ns": _first_float(text, (r"(?im)^\s*(?:tns|total\s+negative\s+slack)\s*(?:[:=]|\s)\s*(-?\d+(?:\.\d+)?)",)),
        "violating_paths": _first_int(text, (r"(?im)^\s*(?:violating\s+paths|num\s+violating\s+paths)\s*(?:[:=]|\s)\s*(\d+)",)),
    }
    diagnostics: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line_no, line in enumerate(text.splitlines(), 1):
        start = re.search(r"(?i)^\s*Startpoint:\s*(.+)$", line)
        end = re.search(r"(?i)^\s*Endpoint:\s*(.+)$", line)
        group = re.search(r"(?i)^\s*Path\s+Group:\s*(.+)$", line)
        slack = re.search(r"(?i)^\s*slack\s+(?:\((?P<status>VIOLATED|MET)\)\s*)?(?P<value>-?\d+(?:\.\d+)?)", line)
        if start:
            current = {"startpoint": start.group(1).strip(), "log_line": line_no}
        elif end:
            current["endpoint"] = end.group(1).strip()
        elif group:
            current["path_group"] = group.group(1).strip()
        elif slack:
            value = float(slack.group("value"))
            status = (slack.group("status") or "").lower()
            current["slack_ns"] = value
            current["severity"] = "error" if value < 0 or status == "violated" else "info"
            current["message"] = "Timing path violates slack" if current["severity"] == "error" else "Timing path meets slack"
            diagnostics.append(current)
            current = {}
    if metrics["wns_ns"] is not None and metrics["wns_ns"] < 0 and not diagnostics:
        diagnostics.append({
            "severity": "error",
            "message": "Negative worst slack reported",
            "slack_ns": metrics["wns_ns"],
        })
    return _base("sta", tool, metrics, diagnostics)


def parse_drc_report(text: str, *, tool: str = "generic") -> dict[str, Any]:
    total = _first_int(
        text,
        (
            r"(?im)Total\s+DRC\s+errors\s+found:\s*(\d+)",
            r"(?im)TOTAL\s+Result\s+Count\s*=\s*(\d+)",
            r"(?im)^\s*DRC\s+(?:violations|errors)\s*[:=]\s*(\d+)",
        ),
    )
    diagnostics: list[dict[str, Any]] = []
    current_rule: str | None = None
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        rule_count = re.search(r"(?i)^(?:Rule|RULECHECK)\s+([A-Za-z0-9_.:-]+).*?(?:count|results?|generated)\s*[:=]?\s*(\d+)", line)
        magic_rule = re.search(r"(?i)^([^:]{3,80})\s*:\s*(\d+)\s+(?:violation|error|result)s?$", line)
        if rule_count or magic_rule:
            match = rule_count or magic_rule
            diagnostics.append({
                "severity": "error",
                "rule": match.group(1).strip(),
                "count": int(match.group(2)),
                "message": f"{match.group(1).strip()} has {match.group(2)} DRC result(s)",
                "log_line": line_no,
            })
            continue
        separator = set(line) <= {"-", "="} and len(line) >= 8
        if separator:
            current_rule = None
            continue
        if current_rule is None and re.search(r"(?i)(width|spacing|enclosure|antenna|overlap|short|drc)", line):
            current_rule = line[:120]
            continue
        if current_rule and re.search(r"(?i)\b(box|rect|polygon|at|um|micron|violation)\b", line):
            # Check for coordinates line, e.g. [12.34um, 5.67um] to [12.56um, 5.89um]
            coord_match = re.search(
                r"\[\s*([\d.-]+)(?:um)?[,\s]+([\d.-]+)(?:um)?\s*\]\s*(?:to|-|\s)\s*\[\s*([\d.-]+)(?:um)?[,\s]+([\d.-]+)(?:um)?\s*\]",
                line
            )
            bbox = None
            if coord_match:
                try:
                    x0 = float(coord_match.group(1))
                    y0 = float(coord_match.group(2))
                    x1 = float(coord_match.group(3))
                    y1 = float(coord_match.group(4))
                    bbox = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
                except Exception:
                    pass

            # Update last diagnostic if it doesn't have a bbox yet, otherwise add new one
            if bbox and diagnostics and diagnostics[-1]["rule"] == current_rule and "bbox" not in diagnostics[-1]:
                diagnostics[-1]["bbox"] = bbox
                diagnostics[-1]["message"] = f"{diagnostics[-1]['message']} at {line[:100]}"
            else:
                diag: dict[str, Any] = {
                    "severity": "error",
                    "rule": current_rule,
                    "message": line[:200],
                    "log_line": line_no,
                }
                if bbox:
                    diag["bbox"] = bbox
                diagnostics.append(diag)
            continue
    if total is None:
        total = sum(int(item.get("count") or 1) for item in diagnostics)
    return _base("drc", tool, {"violation_count": total}, diagnostics)


def parse_synthesis_log(text: str, *, tool: str = "generic") -> dict[str, Any]:
    """Parse Yosys/OpenROAD/Design Compiler synthesis logs into structured summary."""
    diagnostics: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}

    # Cell count (Yosys: "Number of cells: 1247", OpenROAD: "Core cells: 1247")
    cell_count = _first_int(text, (
        r"(?im)^\s*(?:Number of cells|Core cells|Total cells)\s*:?\s*(\d+)",
        r"(?im)^\s*cells\s*:?\s*(\d+)",
    ))
    if cell_count is not None:
        metrics["cell_count"] = cell_count

    # Area (Design Compiler, Genus, Innovus — proprietary tools report area)
    area = _first_float(text, (
        r"(?im)^\s*(?:Total area|Combinational area|Total core area)\s*[=:]\s*(\d+(?:\.\d+)?)",
        r"(?im)^\s*area\s*[=:]\s*(\d+(?:\.\d+)?)",
    ))
    if area is not None:
        metrics["area_um2"] = area

    # Wires/nets count
    wire_count = _first_int(text, (r"(?im)^\s*(?:Number of wires|Net count)\s*:?\s*(\d+)",))
    if wire_count is not None:
        metrics["wire_count"] = wire_count

    # Scan for warnings and errors
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue

        # Yosys: "Warning: message", "Error: message"
        # OpenROAD: "[WARNING] message", "[ERROR] message"
        # Verilator: "%Warning: message", "%Error: message"
        warn_match = re.match(r"^(?:Warning:|\[WARNING\]|%Warning)\s*(.*)", stripped, re.I)
        err_match = re.match(r"^(?:Error:|\[ERROR\]|%Error|ERROR:)\s*(.*)", stripped, re.I)

        if err_match:
            msg = err_match.group(1).strip()
            file_ref = _extract_file_ref(stripped)
            diagnostics.append({
                "severity": "error",
                "message": msg[:200],
                "file": file_ref.get("file"),
                "line": file_ref.get("line"),
                "log_line": line_no,
            })
        elif warn_match:
            msg = warn_match.group(1).strip()
            file_ref = _extract_file_ref(stripped)
            diagnostics.append({
                "severity": "warning",
                "message": msg[:200],
                "file": file_ref.get("file"),
                "line": file_ref.get("line"),
                "log_line": line_no,
            })

    # Cap at 100 diagnostics to avoid explosion
    diagnostics = diagnostics[:100]

    return _base("synthesis", tool, metrics, diagnostics)


def _extract_file_ref(text: str) -> dict[str, Any]:
    """Extract file:line reference from a log line."""
    match = re.search(r"([^\s:]+\.[vsvh]+):(\d+)", text)
    if match:
        return {"file": match.group(1), "line": int(match.group(2))}
    return {}


def parse_lvs_report(text: str, *, tool: str = "generic") -> dict[str, Any]:
    lowered = text.lower()
    matched = bool(re.search(r"(?i)\b(netlists\s+match|circuits\s+match|lvs\s+clean)\b", text))
    if not matched and ("total errors = 0" in lowered or ("unmatched nets = 0" in lowered and "unmatched devices = 0" in lowered and "unmatched pins = 0" in lowered)):
        matched = True
    mismatched = bool(re.search(r"(?i)\b(netlists\s+do\s+not\s+match|mismatch|different|incorrect|failed)\b", text))
    diagnostics: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if re.search(r"(?i)\b(mismatch|do\s+not\s+match|different|unmatched|missing|extra)\b", line):
            diagnostics.append({
                "severity": "error",
                "message": line.strip(),
                "log_line": line_no,
            })
    status = "fail" if mismatched else "pass" if matched and not diagnostics else "unknown"
    return _base(
        "lvs",
        tool,
        {
            "status": status,
            "matched": status == "pass",
            "mismatch_count": len(diagnostics),
            "mentions_devices": "device" in lowered,
            "mentions_nets": "net" in lowered,
        },
        diagnostics,
    )


def _base(kind: str, tool: str, metrics: dict[str, Any], diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": REPORT_PARSERS_VERSION,
        "kind": kind,
        "tool": tool or "generic",
        "summary": {
            "diagnostic_count": len(diagnostics),
            "error_count": sum(1 for item in diagnostics if item.get("severity") == "error"),
            "warning_count": sum(1 for item in diagnostics if item.get("severity") == "warning"),
        },
        "metrics": {key: value for key, value in metrics.items() if value is not None},
        "diagnostics": diagnostics[:100],
    }


def _first_match(patterns: tuple[re.Pattern[str], ...], line: str) -> dict[str, str | None] | None:
    for pattern in patterns:
        match = pattern.search(line)
        if match:
            return match.groupdict()
    return None


def _first_float(text: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def _first_int(text: str, patterns: tuple[str, ...]) -> int | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def _severity(value: str | None) -> str:
    lowered = (value or "").strip().lower()
    if lowered in {"error", "fatal"}:
        return "error"
    if lowered == "warning":
        return "warning"
    return "info"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
