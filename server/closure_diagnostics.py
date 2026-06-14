from __future__ import annotations

import re
from typing import Any


CLOSURE_DIAGNOSTICS_VERSION = "agentic.closure_diagnostics.v1"


DIAGNOSTIC_RULES: tuple[dict[str, Any], ...] = (
    {
        "class": "setup_timing",
        "stage": {"sta", "pnr", "synthesis"},
        "severity": "error",
        "patterns": (
            r"(?i)\bsetup\b.*\bviolat",
            r"(?i)\bworst\s+(?:negative\s+)?slack\b.*-",
            r"(?i)\bwns\b\s*[:=]?\s*-",
            r"(?i)\bmax(?:imum)?\s+delay\b.*\bviolat",
        ),
        "root_causes": (
            "critical combinational path is too deep for the requested clock",
            "constraint/MMMC corner may not match the intended operating mode",
            "floorplan or routing detour may be inflating path delay after placement/route",
        ),
        "actions": (
            "inspect top violating path report with startpoint, endpoint, path group, and cell/net delay split",
            "check SDC clocks/generated clocks/input-output delays before changing RTL or frequency",
            "try targeted buffering/logic restructuring only after the constraint is proven correct",
        ),
        "required_evidence": ("timing path report", "SDC", "selected liberty corner", "netlist/DEF stage"),
    },
    {
        "class": "hold_timing",
        "stage": {"sta", "pnr"},
        "severity": "error",
        "patterns": (
            r"(?i)\bhold\b.*\bviolat",
            r"(?i)\bmin(?:imum)?\s+delay\b.*\bviolat",
            r"(?i)\bthold\b",
        ),
        "root_causes": (
            "clock skew/latency imbalance after CTS",
            "too-short data path requiring delay insertion",
            "incorrect generated-clock or false-path exception",
        ),
        "actions": (
            "inspect min-delay path report with launch/capture clock details",
            "repair with hold buffers or CTS constraints, not RTL behavior changes",
            "verify exceptions before accepting any hold-fix transformation",
        ),
        "required_evidence": ("hold timing report", "CTS report", "SDC clock definitions"),
    },
    {
        "class": "transition_capacitance",
        "stage": {"synthesis", "pnr", "sta"},
        "severity": "warning",
        "patterns": (
            r"(?i)\bmax(?:imum)?\s+transition\b.*\bviolat",
            r"(?i)\bmax(?:imum)?\s+cap(?:acitance)?\b.*\bviolat",
            r"(?i)\bmax_fanout\b.*\bviolat",
            r"(?i)\bslew\b.*\bviolat",
        ),
        "root_causes": (
            "driver too weak for fanout or net capacitance",
            "missing buffering on high-fanout control/reset/enable net",
            "routing capacitance increased after placement/route",
        ),
        "actions": (
            "classify violating net as clock/reset/control/data before fixing",
            "insert buffers or resize drivers through the implementation tool",
            "check fanout and physical span before changing architecture",
        ),
        "required_evidence": ("constraint report", "fanout report", "net capacitance report"),
    },
    {
        "class": "routing_congestion",
        "stage": {"pnr"},
        "severity": "error",
        "patterns": (
            r"(?i)\bcongestion\b",
            r"(?i)\boverflow\b",
            r"(?i)\brouting\s+demand\b",
            r"(?i)\bglobal\s+route\b.*(?:fail|overflow)",
            r"(?i)\bdetailed\s+route\b.*(?:fail|violation)",
        ),
        "root_causes": (
            "utilization or placement density is too aggressive",
            "macro placement creates narrow routing channels",
            "pin access around macros or block boundary is poor",
            "routing layer constraints are too restrictive for the design",
        ),
        "actions": (
            "read congestion/overflow map and identify hot regions before editing RTL",
            "reduce utilization or placement density if congestion is global",
            "move/rotate macros, add halos/blockages, or adjust pin placement if congestion is localized",
            "check routing layer limits and macro obstruction layers",
        ),
        "required_evidence": ("congestion report", "DEF", "macro placement", "route guide/global route report"),
    },
    {
        "class": "macro_placement_or_pin_access",
        "stage": {"pnr", "sta", "physical_verification"},
        "severity": "error",
        "patterns": (
            r"(?i)\bmacro\b.*\b(overlap|placement|halo|blockage|obstruction)",
            r"(?i)\bpin\s+access\b",
            r"(?i)\baccess\s+point\b",
            r"(?i)\bcell\b.*\boverlap\b",
            r"(?i)\bplaced\s+outside\b",
        ),
        "root_causes": (
            "macro floorplan is invalid or lacks halos/keepouts",
            "macro pins face blocked or congested routing channels",
            "die/core area is too small for macro plus standard-cell placement",
        ),
        "actions": (
            "inspect macro LEF size, orientation, pin sides, halos, and placement coordinates",
            "regenerate floorplan constraints from macro evidence",
            "verify macro power pins and obstructions before route",
        ),
        "required_evidence": ("macro LEF", "floorplan DEF", "placement log", "PDN report"),
    },
    {
        "class": "missing_or_invalid_cell",
        "stage": {"synthesis", "pnr", "sta", "physical_verification"},
        "severity": "error",
        "patterns": (
            r"(?i)\b(can(?:not|'t)|unable to)\s+find\s+(?:cell|macro|module|master|lib)",
            r"(?i)\bundefined\s+(?:cell|module|macro|reference)",
            r"(?i)\bcell\b.*\bnot\s+found\b",
            r"(?i)\bmodule\b.*\bnot\s+found\b",
            r"(?i)\blink\b.*\bfailed\b",
        ),
        "root_causes": (
            "RTL instantiated a macro/cell absent from the selected PDK views",
            "library search path or corner selection is wrong",
            "blackbox/simulation/LEF/Liberty names do not match",
        ),
        "actions": (
            "query local capability graph for the exact cell/macro name before editing source",
            "compare RTL instance name against Liberty cell and LEF MACRO names",
            "repair library search paths or use a macro that exists in all required views",
        ),
        "required_evidence": ("RTL instantiation", "Liberty cell list", "LEF macro list", "tool library setup"),
    },
    {
        "class": "drc_lvs_antenna",
        "stage": {"physical_verification", "signoff", "pnr"},
        "severity": "error",
        "patterns": (
            r"(?i)\bDRC\b.*(?:error|violat|result)",
            r"(?i)\bLVS\b.*(?:mismatch|fail|not\s+match)",
            r"(?i)\bantenna\b.*(?:violat|error)",
            r"(?i)\bshort\b.*\bnet\b",
            r"(?i)\bopen\b.*\bnet\b",
        ),
        "root_causes": (
            "route or generated layout violates foundry rules",
            "schematic/layout netlists disagree due to missing macro views or power pins",
            "antenna repair/fill/tap/endcap flow is incomplete",
        ),
        "actions": (
            "read the rule-specific DRC/LVS result, not just total count",
            "map violation coordinates back to DEF/GDS objects",
            "check macro abstracts, power pin naming, antenna diodes, fill, tap/endcap insertion",
        ),
        "required_evidence": ("DRC/LVS report", "rule deck name", "GDS/DEF", "extracted netlist"),
    },
    {
        "class": "power_integrity",
        "stage": {"power", "pnr", "signoff"},
        "severity": "error",
        "patterns": (
            r"(?i)\bIR\s*drop\b",
            r"(?i)\bEM\b.*\bviolat",
            r"(?i)\bpower\s+(?:grid|stripe|strap)\b.*(?:fail|weak|violat)",
            r"(?i)\bmissing\s+(?:power|ground|pg)\s+pin\b",
        ),
        "root_causes": (
            "power grid is undersized for switching current",
            "macro PG pins are not connected correctly",
            "voltage domain/power intent is incomplete",
        ),
        "actions": (
            "inspect PDN/rail analysis report and macro PG pin mapping",
            "increase straps/rings/vias or fix domain connectivity",
            "verify UPF/CPF and always-on/level-shifter requirements for multi-domain designs",
        ),
        "required_evidence": ("PDN config", "IR/EM report", "macro power pin list", "power intent"),
    },
    {
        "class": "tool_license_or_setup",
        "stage": {"simulation", "lint", "formal", "synthesis", "pnr", "sta", "physical_verification", "power"},
        "severity": "blocked",
        "patterns": (
            r"(?i)\blicen[cs]e\b.*(?:fail|denied|not\s+found|checkout)",
            r"(?i)\bcommand\s+not\s+found\b",
            r"(?i)\bno\s+such\s+file\s+or\s+directory\b",
            r"(?i)\bpermission\s+denied\b",
            r"(?i)\bnot\s+configured\b",
        ),
        "root_causes": (
            "tool is not installed or not on PATH",
            "license environment is missing or unreachable",
            "flow script path or PDK root is not configured",
        ),
        "actions": (
            "report the missing tool/license/path directly to the user",
            "do not fake the stage or switch flow without approval",
            "offer detected alternatives only after preserving the user's requested target",
        ),
        "required_evidence": ("which/command probe", "license env presence", "script path", "PDK root"),
    },
)


def analyze_closure_log(
    *,
    tool: str,
    stage: str,
    log: str,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = log or ""
    stage_norm = (stage or "").strip().lower() or "execution"
    matches = []
    for rule in DIAGNOSTIC_RULES:
        if stage_norm not in rule["stage"] and "signoff" not in {stage_norm, *rule["stage"]}:
            if not _stage_family_matches(stage_norm, rule["stage"]):
                continue
        excerpts = _matching_excerpts(text, rule["patterns"])
        if not excerpts:
            continue
        matches.append({
            "class": rule["class"],
            "severity": rule["severity"],
            "confidence": _confidence_for_match(excerpts, metrics or {}, rule["class"]),
            "excerpts": excerpts[:5],
            "root_cause_candidates": list(rule["root_causes"]),
            "recommended_actions": list(rule["actions"]),
            "required_evidence": list(rule["required_evidence"]),
        })

    metric_findings = _metric_findings(metrics or {}, stage_norm)
    for finding in metric_findings:
        if not any(item["class"] == finding["class"] for item in matches):
            matches.append(finding)

    if not matches and text:
        matches.append({
            "class": "unclassified_tool_failure",
            "severity": "error",
            "confidence": "low",
            "excerpts": _interesting_lines(text)[:5],
            "root_cause_candidates": ["tool reported a failure that is not yet classified by AgentIC's semantic parser"],
            "recommended_actions": ["read the full tool log, identify the failing command/stage, and add a parser rule if this class recurs"],
            "required_evidence": ["full log", "tool command", "stage input files"],
        })

    dominant = _dominant(matches)
    return {
        "schema_version": CLOSURE_DIAGNOSTICS_VERSION,
        "tool": tool,
        "stage": stage_norm,
        "issue_count": len(matches),
        "dominant_class": dominant.get("class") if dominant else None,
        "dominant_severity": dominant.get("severity") if dominant else None,
        "issues": matches[:8],
        "summary": _summary(matches, tool, stage_norm),
    }


def _matching_excerpts(text: str, patterns: tuple[str, ...]) -> list[str]:
    lines = text.splitlines()
    excerpts = []
    for line in lines:
        clean = line.strip()
        if not clean:
            continue
        if any(re.search(pattern, clean) for pattern in patterns):
            excerpts.append(clean[:320])
        if len(excerpts) >= 8:
            break
    return excerpts


def _metric_findings(metrics: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    findings = []
    for key in ("worst_slack_ns", "wns_ns", "slack"):
        value = _float_or_none(metrics.get(key))
        if value is not None and value < 0:
            findings.append({
                "class": "setup_timing",
                "severity": "error",
                "confidence": "high",
                "excerpts": [f"{key}={value}"],
                "root_cause_candidates": ["negative timing slack reported by checkpoint metrics"],
                "recommended_actions": ["read the violating timing path report and validate SDC/MMMC before modifying implementation"],
                "required_evidence": ["timing report", "SDC", "selected corner"],
            })
    drc = _int_or_none(metrics.get("drc_violations"))
    if drc is not None and drc > 0:
        findings.append({
            "class": "drc_lvs_antenna",
            "severity": "error",
            "confidence": "high",
            "excerpts": [f"drc_violations={drc}"],
            "root_cause_candidates": ["physical verification reported rule violations"],
            "recommended_actions": ["open the rule-level DRC result and map violations to layout objects before retrying route/fill"],
            "required_evidence": ["DRC report", "GDS/DEF", "rule deck"],
        })
    overflow = _int_or_none(metrics.get("overflow"))
    if stage == "pnr" and overflow is not None and overflow > 0:
        findings.append({
            "class": "routing_congestion",
            "severity": "error",
            "confidence": "high",
            "excerpts": [f"overflow={overflow}"],
            "root_cause_candidates": ["routing overflow reported by implementation metrics"],
            "recommended_actions": ["read congestion map and decide whether utilization, macro placement, or routing layer constraints are root cause"],
            "required_evidence": ["congestion report", "DEF", "placement utilization"],
        })
    return findings


def _interesting_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        if re.search(r"(?i)(error|fatal|fail|warning|violat|slack|overflow|congestion|license)", line):
            lines.append(line.strip()[:320])
        if len(lines) >= 8:
            break
    return lines or [line.strip()[:320] for line in text.splitlines() if line.strip()][:3]


def _stage_family_matches(stage: str, rule_stages: set[str]) -> bool:
    aliases = {
        "drc": "physical_verification",
        "lvs": "physical_verification",
        "route": "pnr",
        "place": "pnr",
        "cts": "pnr",
        "harden": "pnr",
        "implementation": "pnr",
        "timing": "sta",
    }
    return aliases.get(stage, stage) in rule_stages


def _confidence_for_match(excerpts: list[str], metrics: dict[str, Any], issue_class: str) -> str:
    if issue_class == "setup_timing" and any(_float_or_none(metrics.get(key)) is not None for key in ("worst_slack_ns", "wns_ns", "slack")):
        return "high"
    if len(excerpts) >= 2:
        return "high"
    return "medium"


def _dominant(matches: list[dict[str, Any]]) -> dict[str, Any]:
    severity_rank = {"blocked": 4, "error": 3, "warning": 2, "info": 1}
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    ranked = sorted(
        matches,
        key=lambda item: (
            severity_rank.get(item.get("severity"), 0),
            confidence_rank.get(item.get("confidence"), 0),
            len(item.get("excerpts") or []),
        ),
        reverse=True,
    )
    return ranked[0] if ranked else {}


def _summary(matches: list[dict[str, Any]], tool: str, stage: str) -> str:
    if not matches:
        return f"No semantic closure issue was classified for {tool or 'tool'} {stage}."
    dominant = _dominant(matches)
    return (
        f"{tool or 'tool'} {stage} appears blocked by `{dominant.get('class')}` "
        f"({dominant.get('confidence')} confidence). Required evidence: "
        + ", ".join((dominant.get("required_evidence") or [])[:4])
        + "."
    )


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
