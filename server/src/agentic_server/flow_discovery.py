"""
flow_discovery.py — Automatically discover and parse existing Makefiles, TCL scripts,
and EDA flow configurations in the workspace.

Exposes discovered targets to the AgentIC context engine so the agent reuses the
team's existing signed-off flows instead of inventing arbitrary commands.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MakefileTarget:
    name: str
    prerequisites: list[str] = field(default_factory=list)
    description: str = ""
    command_snippet: str = ""


@dataclass
class DiscoveredMakefile:
    path: str
    rel_path: str
    targets: list[MakefileTarget] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)


@dataclass
class DiscoveredFlowScript:
    path: str
    rel_path: str
    stage: str  # 'synthesis', 'pnr', 'sta', 'simulation', 'dft_atpg', 'drc_lvs', 'unknown'
    language: str  # 'tcl', 'yosys', 'sdc', 'bash', 'python'
    description: str = ""


@dataclass
class FlowSummary:
    makefiles: list[DiscoveredMakefile] = field(default_factory=list)
    flow_scripts: list[DiscoveredFlowScript] = field(default_factory=list)
    recommended_targets: dict[str, str] = field(default_factory=dict)
    has_existing_flow: bool = False


# Regex patterns for Makefile parsing
_MAKE_TARGET_RE = re.compile(r"^([a-zA-Z0-9_\-\.]+)\s*:(?:([^=\n]*))?")
_MAKE_VAR_RE = re.compile(r"^([a-zA-Z0-9_\-]+)\s*[:\?\+]?=\s*(.*)")


def parse_makefile(file_path: str, workspace_root: str) -> DiscoveredMakefile | None:
    """Parse a Makefile to extract targets, prerequisites, and variables."""
    if not os.path.exists(file_path):
        return None

    try:
        rel_path = os.path.relpath(file_path, workspace_root)
        targets: list[MakefileTarget] = []
        variables: dict[str, str] = {}
        last_comment = ""

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for line in lines:
            line_str = line.strip()
            if not line_str:
                last_comment = ""
                continue

            if line_str.startswith("#"):
                comment_text = line_str.lstrip("# ").strip()
                if comment_text:
                    last_comment = comment_text
                continue

            # Check variable assignments
            var_match = _MAKE_VAR_RE.match(line_str)
            if var_match:
                vname, vval = var_match.groups()
                variables[vname.strip()] = vval.strip()
                continue

            # Check target definitions
            target_match = _MAKE_TARGET_RE.match(line)
            if target_match and not line.startswith("\t"):
                tname, prereqs_str = target_match.groups()
                tname = tname.strip()
                if tname in (".PHONY", "FORCE", ".SUFFIXES"):
                    continue

                prereqs = [p.strip() for p in (prereqs_str or "").split() if p.strip()]
                targets.append(
                    MakefileTarget(
                        name=tname,
                        prerequisites=prereqs,
                        description=last_comment,
                    )
                )
                last_comment = ""

        return DiscoveredMakefile(
            path=file_path,
            rel_path=rel_path,
            targets=targets,
            variables=variables,
        )
    except Exception:
        return None


def classify_flow_script(file_path: str) -> tuple[str, str]:
    """Classify a script by its EDA stage, vendor tool, and language."""
    lower = file_path.lower()
    ext = os.path.splitext(lower)[1]

    lang = "unknown"
    if ext in (".tcl",):
        lang = "tcl"
    elif ext in (".ys",):
        lang = "yosys"
    elif ext in (".sdc",):
        lang = "sdc"
    elif ext in (".sh", ".bash"):
        lang = "bash"
    elif ext in (".py",):
        lang = "python"
    elif ext in (".upf", ".cpf"):
        lang = "power_intent"

    stage = "unknown"
    if any(k in lower for k in ("genus", "dc_shell", "design_compiler", "yosys", "synth", "synthesis")):
        stage = "synthesis"
    elif any(k in lower for k in ("innovus", "icc2", "icc2_shell", "openroad", "apr", "pnr", "place", "route")):
        stage = "pnr"
    elif any(k in lower for k in ("tempus", "pt_shell", "primetime", "sta", "timing")):
        stage = "sta"
    elif any(k in lower for k in ("xrun", "irun", "vcs", "vsim", "questasim", "verilator", "iverilog", "sim", "tb")):
        stage = "simulation"
    elif any(k in lower for k in ("tessent", "dft_max", "atpg", "dft")):
        stage = "dft_atpg"
    elif any(k in lower for k in ("calibre", "pvs", "icv", "ic_validator", "pegasus", "drc", "lvs")):
        stage = "drc_lvs"
    elif any(k in lower for k in ("voltus", "redhawk", "joules", "power")):
        stage = "power_analysis"
    elif any(k in lower for k in ("quantus", "starrc", "spef")):
        stage = "parasitic_extraction"
    elif any(k in lower for k in ("formality", "lec", "conformal", "eqc")):
        stage = "equivalence_checking"

    return stage, lang



def discover_flow(workspace_root: str) -> FlowSummary:
    """Discover all Makefiles, TCL scripts, and flow targets in the workspace."""
    summary = FlowSummary()

    if not os.path.exists(workspace_root):
        return summary

    # 1. Search for Makefiles
    makefiles_to_check = []
    for fname in ("Makefile", "makefile", "GNUmakefile", "flow.mk"):
        p = os.path.join(workspace_root, fname)
        if os.path.exists(p):
            makefiles_to_check.append(p)

    for root, dirs, files in os.walk(workspace_root):
        # Ignore dotdirs and build outputs
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("build", "dist", "node_modules", "__pycache__")]
        for fname in files:
            if fname in ("Makefile", "makefile", "flow.mk"):
                p = os.path.join(root, fname)
                if p not in makefiles_to_check:
                    makefiles_to_check.append(p)

    for mfile in makefiles_to_check[:5]:
        parsed = parse_makefile(mfile, workspace_root)
        if parsed:
            summary.makefiles.append(parsed)

    # 2. Search for EDA flow scripts
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("build", "dist", "node_modules", "__pycache__")]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".tcl", ".ys", ".sdc", ".sh", ".py"):
                p = os.path.join(root, fname)
                stage, lang = classify_flow_script(p)
                if stage != "unknown":
                    summary.flow_scripts.append(
                        DiscoveredFlowScript(
                            path=p,
                            rel_path=os.path.relpath(p, workspace_root),
                            stage=stage,
                            language=lang,
                        )
                    )

    # 3. Build recommended targets map
    for mk in summary.makefiles:
        for t in mk.targets:
            tname = t.name.lower()
            if "synth" in tname and "synthesis" not in summary.recommended_targets:
                summary.recommended_targets["synthesis"] = f"make {t.name}"
            elif "pnr" in tname and "pnr" not in summary.recommended_targets:
                summary.recommended_targets["pnr"] = f"make {t.name}"
            elif "sta" in tname and "sta" not in summary.recommended_targets:
                summary.recommended_targets["sta"] = f"make {t.name}"
            elif "sim" in tname and "simulation" not in summary.recommended_targets:
                summary.recommended_targets["simulation"] = f"make {t.name}"
            elif "atpg" in tname and "dft_atpg" not in summary.recommended_targets:
                summary.recommended_targets["dft_atpg"] = f"make {t.name}"

    summary.has_existing_flow = len(summary.makefiles) > 0 or len(summary.flow_scripts) > 0
    return summary


def format_flow_context_prompt(summary: FlowSummary) -> str:
    """Format discovered flow targets as system context prompt for the LLM agent."""
    if not summary.has_existing_flow:
        return ""

    lines = ["## DISCOVERED USER FLOW (IMMUTABLE GROUND TRUTH)"]
    lines.append("The project has existing signed-off Makefile targets and flow scripts. ALWAYS prefer running these targets:")

    if summary.recommended_targets:
        lines.append("\nRecommended Stage Commands:")
        for stage, cmd in summary.recommended_targets.items():
            lines.append(f"  - {stage.upper()}: `{cmd}`")

    if summary.makefiles:
        lines.append("\nAvailable Makefile Targets:")
        for mk in summary.makefiles:
            lines.append(f"  [{mk.rel_path}]:")
            for t in mk.targets[:10]:
                desc = f" ({t.description})" if t.description else ""
                lines.append(f"    - `make {t.name}`{desc}")

    if summary.flow_scripts:
        lines.append("\nDiscovered EDA Flow Scripts:")
        for script in summary.flow_scripts[:10]:
            lines.append(f"  - `{script.rel_path}` (stage: {script.stage}, lang: {script.language})")

    lines.append("\nRULES FOR AGENT:")
    lines.append("1. Do NOT invent new tool command lines if an existing Makefile target exists.")
    lines.append("2. Execute operations using the team's `make <target>` commands whenever possible.")

    return "\n".join(lines)
