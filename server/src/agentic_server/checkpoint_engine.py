import re
import json
import os
from pathlib import Path
from agentic_server.tool_adapters import adapter_parser
from agentic_server.closure_diagnostics import analyze_closure_log
from agentic_server.report_parsers import parse_report

# Tool-specific regex parsers.
# Note: "generic" relies almost entirely on the exit code to prevent false failures.
TOOL_PARSERS = {
    "iverilog":   {"error": r"^.*:\d+: error:.*$", "warning": r"^.*:\d+: warning:.*$"},
    "verilator":  {"error": r"%Error.*$", "warning": r"%Warning.*$"},
    "yosys":      {"error": r"^ERROR:.*$", "warning": r"^Warning:.*$"},
    "openroad":   {"error": r"^\[ERROR.*$", "warning": r"^\[WARNING.*$"},
    "opensta":    {"error": r"^Error:.*$", "warning": r"^Warning:.*$"},
    "magic":      {"error": r"^Error:.*$", "warning": r"^Warning:.*$"},
    "netgen":     {"error": r"^Netlists do not match", "warning": r"^Warning:.*$"},
    "genus":      {"error": r"^\*\*ERROR.*$", "warning": r"^\*\*WARN.*$"},
    "innovus":    {"error": r"^\*\*ERROR.*$", "warning": r"^\*\*WARN.*$"},
    "xcelium":    {"error": r"^xmvlog: \*E.*$", "warning": r"^xmvlog: \*W.*$"},
    "joules":     {"error": r"^\*\*ERROR.*$", "warning": r"^\*\*WARN.*$"},
    "dc_shell":   {"error": r"^Error:.*$", "warning": r"^Warning:.*$"},
    "icc2_shell": {"error": r"^Error:.*$", "warning": r"^Warning:.*$"},
    "pt_shell":   {"error": r"^Error:.*$", "warning": r"^Warning:.*$"},
    "vcs":        {"error": r"^Error-\[.*$", "warning": r"^Warning-\[.*$"},
    "questa":     {"error": r"^\*\* Error:.*$", "warning": r"^\*\* Warning:.*$"},
    "calibre":    {"error": r"^ERROR:.*$", "warning": r"^WARNING:.*$"},
    "generic":    {"error": r"(?!x)x", "warning": r"(?!x)x"}  # Empty match, relies purely on exit_code
}

METRIC_EXTRACTORS = {
    "yosys": {
        "cell_count":     r"Number of cells:\s+(\d+)",
        "wire_count":     r"Number of wires:\s+(\d+)",
        "memory_bits":    r"Number of memory bits:\s+(\d+)",
    },
    "opensta": {
        "worst_slack_ns": r"worst slack\s+([-\d.]+)",
        "tns_ns":         r"tns\s+([-\d.]+)",
    },
    "genus": {
        "area_um2":       r"Total area of.*?:\s+([\d.]+)",
        "cell_count":     r"Total cell count.*?:\s+(\d+)",
    },
    "magic": {
        "drc_violations": r"Total DRC errors found:\s+(\d+)",
    },
    "calibre": {
        "drc_violations": r"TOTAL Result Count =\s+(\d+)",
    },
}

class CheckpointEngine:
    def __init__(self, design_name: str, workspace_root: str):
        self.design_name = design_name or "scratch"
        self.workspace_root = Path(workspace_root)
        self.state_dir = self.workspace_root / ".agentic"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Persistence file specific to the design
        self.checkpoint_file = self.state_dir / f"{self.design_name}_checkpoints.json"
        self.session_results = self._load_state()

    def _load_state(self) -> list[dict]:
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, "r") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return []
        return []

    def _save_state(self):
        with open(self.checkpoint_file, "w") as f:
            json.dump(self.session_results, f, indent=2)

    def _truncate_log(self, log: str, errors: list[str]) -> str:
        lines = log.splitlines()
        if len(lines) <= 200:
            return log

        first_50 = lines[:50]
        last_100 = lines[-100:]

        # Find any lines that look like errors/warnings to ensure they aren't lost
        middle_interest = []
        for line in lines[50:-100]:
            if "error" in line.lower() or "warning" in line.lower() or "fail" in line.lower():
                middle_interest.append(line)

        truncated = first_50 + ["\n... [LOG TRUNCATED BY CHECKPOINT ENGINE] ...\n"] + middle_interest + ["\n... [END OF EXTRACTED MIDDLE] ...\n"] + last_100
        return "\n".join(truncated)

    def evaluate(self, tool: str, stage: str, exit_code: int, stdout_log: str = "", log_file: str = "") -> dict:
        """Parse log, extract errors/warnings/metrics, return verdict."""

        # Read from log file if provided and exists
        log_content = stdout_log
        if log_file and os.path.exists(log_file):
            try:
                with open(log_file, "r", errors="replace") as f:
                    log_content = f.read()
            except Exception as e:
                log_content += f"\n[Checkpoint Error: Failed to read log file {log_file}: {e}]"

        adapter_contract = adapter_parser(tool)
        parser = {
            "error": adapter_contract.error,
            "warning": adapter_contract.warning,
        }
        legacy_parser = TOOL_PARSERS.get(tool)
        if legacy_parser and adapter_contract.error == r"(?i)\b(error|fatal|failed)\b":
            parser = legacy_parser

        # Extract errors and warnings safely
        errors = re.findall(parser["error"], log_content, re.MULTILINE) if parser["error"] != r"(?!x)x" else []
        warnings = re.findall(parser["warning"], log_content, re.MULTILINE) if parser["warning"] != r"(?!x)x" else []

        # A tool passes if exit_code is 0 AND we didn't find explicit error regex matches
        passed = (exit_code == 0)
        if errors:
            passed = False

        metrics = {}
        metric_patterns = dict(adapter_contract.metrics or {})
        metric_patterns.update(METRIC_EXTRACTORS.get(tool, {}))
        for key, regex in metric_patterns.items():
            match = re.search(regex, log_content)
            if match:
                metrics[key] = match.group(1)
        semantic_diagnostics = analyze_closure_log(
            tool=tool,
            stage=stage,
            log=log_content,
            metrics=metrics,
        ) if not passed else {
            "schema_version": "agentic.closure_diagnostics.v1",
            "tool": tool,
            "stage": stage,
            "issue_count": 0,
            "dominant_class": None,
            "dominant_severity": None,
            "issues": [],
            "summary": "Stage passed; no closure issue classified.",
        }

        result = {
            "pass": passed,
            "stage": stage,
            "tool": tool,
            "exit_code": exit_code,
            "errors": list(set(errors))[:15],      # Dedup and cap at 15
            "warnings": list(set(warnings))[:15],  # Dedup and cap at 15
            "metrics": metrics,
            "parsed_report": parse_report(stage=stage, tool=tool, text=log_content),
            "semantic_diagnostics": semantic_diagnostics,
        }

        # Update persistent state
        self.session_results.append(result)
        self._save_state()

        # We also want to return a truncated version of the log to save LLM context
        truncated_log = self._truncate_log(log_content, result["errors"])

        return {
            "verdict": result,
            "truncated_log": truncated_log
        }

    def signoff_report(self) -> dict:
        """Aggregate all session checkpoints into a signoff report."""
        stages = [{"stage": r["stage"], "tool": r["tool"], "pass": r["pass"]} for r in self.session_results]
        all_pass = all(r["pass"] for r in self.session_results)
        return {
            "design_name": self.design_name,
            "stages": stages,
            "gds_ready": all_pass and any(r["stage"] in ("drc", "lvs", "signoff", "harden") for r in self.session_results),
            "total_stages": len(stages),
            "passed_stages": sum(1 for r in self.session_results if r["pass"]),
            "summary": "All stages passed." if all_pass else "Some stages have failures.",
        }
