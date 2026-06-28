from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Literal


WORKFLOW_SCHEMA_VERSION = "agentic.session_workflow.v1"

WorkflowMode = Literal[
    "greeting",
    "advisor",
    "diagram_advisor",
    "diagram_repair",
    "rtl_repair",
    "design_plan",
    "design_execute",
    "design_edit",
]


@dataclass(frozen=True)
class WorkflowDecision:
    schema_version: str
    mode: WorkflowMode
    intent: Literal["INFORMATIONAL", "DESIGN_TASK"]
    requires_model: bool
    requires_environment: bool
    requires_design_kernel: bool
    execution_authorized: bool
    planning_round: bool
    confidence: float
    reason: str
    evidence: dict[str, object] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_record(self) -> dict[str, object]:
        return asdict(self)


def classify_session_workflow(user_text: str, messages: list[dict]) -> WorkflowDecision:
    text = (user_text or "").strip()
    lowered = text.lower()
    normalized = _normalize(lowered)
    tokens = set(_tokens(normalized))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    if _is_simple_greeting(normalized):
        return _decision("greeting", "INFORMATIONAL", False, False, False, False, False, 1.0, "simple greeting", digest)

    if _is_explicit_builder_mode_request(normalized):
        return _decision("design_execute", "DESIGN_TASK", True, True, True, True, False, 0.99, "user explicitly requested builder mode", digest)

    previous_approval_gate = _previous_assistant_requested_approval(messages)
    approved_plan = _has_approved_plan_turn(messages)
    approval = _looks_like_approval(normalized)
    followup_edit = _is_followup_edit_request(normalized)

    if approval and previous_approval_gate:
        return _decision("design_execute", "DESIGN_TASK", True, True, True, True, False, 0.98, "user approved the latest plan", digest)

    if approved_plan and followup_edit and _has_design_edit_target(normalized, tokens):
        return _decision("design_edit", "DESIGN_TASK", True, True, True, True, False, 0.92, "approved design follow-up edit", digest)

    if followup_edit and _has_workspace_artifact_context(normalized, messages):
        return _decision("design_edit", "DESIGN_TASK", True, True, True, True, False, 0.9, "workspace artifact edit request", digest)

    if _asks_to_repair_diagram(normalized):
        return _decision("diagram_repair", "INFORMATIONAL", False, False, False, False, False, 0.96, "diagram repair request", digest)

    if _asks_for_rtl_repair(normalized, tokens, messages):
        return _decision("rtl_repair", "DESIGN_TASK", False, False, False, True, False, 0.92, "RTL repair request", digest)

    if _asks_for_diagram(normalized):
        if _asks_to_save_artifact(normalized):
            return _decision("design_plan", "DESIGN_TASK", True, True, True, False, True, 0.86, "diagram artifact requested", digest)
        return _decision("diagram_advisor", "INFORMATIONAL", True, False, False, False, False, 0.95, "inline diagram/advisor request", digest)

    if _is_explicit_design_build(normalized, tokens):
        return _decision("design_plan", "DESIGN_TASK", True, True, True, False, True, 0.9, "explicit chip/design build request", digest)

    if _is_eda_execution_request(normalized, tokens):
        return _decision("design_execute", "DESIGN_TASK", True, True, True, approved_plan, not approved_plan, 0.86, "EDA execution request", digest)

    if _is_information_request(normalized, tokens):
        return _decision("advisor", "INFORMATIONAL", True, False, False, False, False, 0.91, "question/advice request", digest)

    return _decision("advisor", "INFORMATIONAL", True, False, False, False, False, 0.62, "default safe advisor route", digest)


def _decision(
    mode: WorkflowMode,
    intent: Literal["INFORMATIONAL", "DESIGN_TASK"],
    requires_model: bool,
    requires_environment: bool,
    requires_design_kernel: bool,
    execution_authorized: bool,
    planning_round: bool,
    confidence: float,
    reason: str,
    digest: str,
) -> WorkflowDecision:
    return WorkflowDecision(
        schema_version=WORKFLOW_SCHEMA_VERSION,
        mode=mode,
        intent=intent,
        requires_model=requires_model,
        requires_environment=requires_environment,
        requires_design_kernel=requires_design_kernel,
        execution_authorized=execution_authorized,
        planning_round=planning_round,
        confidence=confidence,
        reason=reason,
        evidence={"turn_digest": digest},
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9_+-]*", text or "")


def _has_token(tokens: set[str], *items: str) -> bool:
    return any(item in tokens for item in items)


def _has_phrase(text: str, *phrases: str) -> bool:
    return any(phrase in text for phrase in phrases)


def _has_word_or_phrase(text: str, tokens: set[str], *items: str) -> bool:
    for item in items:
        if " " in item or "-" in item:
            if item in text:
                return True
        elif item in tokens:
            return True
    return False


def _is_simple_greeting(text: str) -> bool:
    stripped = re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()
    if not stripped:
        return True
    words = stripped.split()
    return len(words) <= 3 and all(word in {"hi", "hii", "hiii", "hiiii", "hello", "hey", "yo", "namaste"} for word in words)


def _looks_like_approval(text: str) -> bool:
    stripped = re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()
    if stripped in {"yes", "y", "ok", "okay", "approved", "approve", "proceed", "continue", "start", "begin", "execute"}:
        return True
    return _has_phrase(stripped, "go ahead", "do it", "start execution", "yes proceed", "ok proceed", "approve it")


def _previous_assistant_requested_approval(messages: list[dict]) -> bool:
    for msg in reversed(messages[:-1]):
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content") or "").lower()
        return "approve this plan" in content or "plan approval needed" in content or "begin execution" in content
    return False


def _has_approved_plan_turn(messages: list[dict]) -> bool:
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content") or "").lower()
        if "approve this plan" not in content and "begin execution" not in content and "plan approval needed" not in content:
            continue
        for later in messages[idx + 1:]:
            if later.get("role") == "user" and _looks_like_approval(str(later.get("content") or "")):
                return True
    return False


def _is_followup_edit_request(text: str) -> bool:
    return _has_phrase(
        text,
        "fix",
        "change",
        "update",
        "modify",
        "add ",
        "debug",
        "repair",
        "rerun",
        "re-run",
        "continue from",
        "improve",
        "resolve",
        "clean up",
    )


def _has_design_edit_target(text: str, tokens: set[str]) -> bool:
    return _has_word_or_phrase(
        text,
        tokens,
        "rtl",
        "verilog",
        "testbench",
        "tb",
        "uvm",
        "sdc",
        "constraint",
        "timing",
        "synthesis",
        "pnr",
        "gds",
        "gdsii",
        "module",
        "port",
        "register",
        "bus",
        "axi",
        "apb",
        "sram",
        "fifo",
        "controller",
    )


def _has_workspace_artifact_context(text: str, messages: list[dict]) -> bool:
    current_turn_mentions_artifact = _has_phrase(
        text,
        "workspace",
        "attached file:",
        "attached file",
        ".mermaid",
        ".mmd",
        ".md",
        ".v",
        ".sv",
        "docs/diagrams",
        "docs/plans",
        "rtl/",
        "tb/",
        "constraints/",
        "scripts/",
        "reports/",
    )
    if current_turn_mentions_artifact:
        return True
    history = "\n".join(str(item.get("content") or "") for item in messages[-6:]).lower()
    return _has_phrase(
        history,
        "saved workspace artifacts",
        "updated workspace artifacts",
        "attached file:",
        ".mermaid",
        ".mmd",
        "docs/diagrams",
        "rtl/",
        "tb/",
        "constraints/",
        "scripts/",
    )


def _asks_for_diagram(text: str) -> bool:
    return _has_phrase(text, "diagram", "mermaid", "flowchart", "block diagram", "architecture picture")


def _asks_to_save_artifact(text: str) -> bool:
    return _has_phrase(text, "save", "write file", "create file", "put it in", "export", "generate files", "workspace")


def _asks_to_repair_diagram(text: str) -> bool:
    repair = _has_phrase(text, "fix", "repair", "syntax", "parse error", "not showing", "broken", "doesn't render", "doesnt render")
    return repair and _asks_for_diagram(text)


def _asks_for_rtl_repair(text: str, tokens: set[str], messages: list[dict]) -> bool:
    repair = _has_word_or_phrase(text, tokens, "fix", "repair", "correct", "patch", "lint", "syntax", "compile")
    rtl = _has_word_or_phrase(text, tokens, "rtl", "verilog", "systemverilog", "sv", "hdl", "module")
    if repair and rtl:
        return True
    if repair and len(tokens) <= 6:
        history = "\n".join(str(item.get("content") or "") for item in messages[-4:]).lower()
        return _has_phrase(history, "rtl", "verilog", "systemverilog", ".v", ".sv")
    return False


def _is_explicit_design_build(text: str, tokens: set[str]) -> bool:
    build_verb = _has_word_or_phrase(text, tokens, "build", "implement", "create", "generate", "design", "make", "harden", "plan")
    chip_object = _has_word_or_phrase(text, tokens, "chip", "soc", "core", "rtl", "gds", "gdsii", "module", "accelerator", "controller")
    if build_verb and chip_object:
        return True
    return _has_phrase(text, "write rtl", "generate rtl", "create rtl", "make a chip", "make chip", "rtl to gds", "design and verify")


def _is_eda_execution_request(text: str, tokens: set[str]) -> bool:
    return _has_word_or_phrase(
        text,
        tokens,
        "simulate",
        "compile",
        "synthesize",
        "synthesis",
        "openlane",
        "openroad",
        "pnr",
        "route",
        "sta",
        "drc",
        "lvs",
        "signoff",
        "tapeout",
    )


def _is_information_request(text: str, tokens: set[str]) -> bool:
    if "?" in text:
        return True
    return _has_word_or_phrase(
        text,
        tokens,
        "are",
        "is",
        "what",
        "which",
        "why",
        "how",
        "explain",
        "tell",
        "suggest",
        "recommend",
        "compare",
        "list",
        "show",
        "can",
        "will",
        "would",
        "best",
        "fork",
        "opencode",
    )


def _is_explicit_builder_mode_request(text: str) -> bool:
    return any(phrase in text for phrase in [
        "builder mode",
        "switch to builder",
        "go to builder",
        "allowing you to go to builder",
        "allow builder",
        "authorize builder",
        "enable builder",
        "move to builder"
    ])
