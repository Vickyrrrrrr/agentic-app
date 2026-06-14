from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from tool_adapters import adapter_regex_commands


DISCOVERY_BASH_RE = re.compile(
    r"^\s*(ls|pwd|find\s+\.\s+-maxdepth\s+[0-3]\b|command\s+-v|which|env\s*\|\s*sort|python3?\s+-V|docker\s+--version)\b"
)

def _eda_tool_re() -> re.Pattern[str]:
    commands = [re.escape(command) for command in adapter_regex_commands() if command not in {"python3"}]
    return re.compile(r"\b(" + "|".join(commands or ["__agentic_no_tool__"]) + r")\b", re.IGNORECASE)

DESTRUCTIVE_RE = re.compile(
    r"(^|\s)(rm\s+-rf|rm\s+-fr|git\s+clean|git\s+reset\s+--hard|mkfs|dd\s+if=|"
    r"chmod\s+-R\s+777|chown\s+-R|truncate\s+-s\s+0)\b",
    re.IGNORECASE,
)

INSTALL_RE = re.compile(
    r"\b(apt(-get)?\s+install|dnf\s+install|yum\s+install|pacman\s+-S|brew\s+install|"
    r"pip\s+install|npm\s+install|cargo\s+install|docker\s+pull|volare\s+enable)\b",
    re.IGNORECASE,
)


ROLE_PROFILES: dict[str, dict[str, Any]] = {
    "principal": {
        "owns": ["turn routing", "approval state", "handoff selection"],
        "outputs": ["permission_scope", "context_contract", "final_response"],
    },
    "context_librarian": {
        "owns": ["context packet", "repo map", "evidence retrieval"],
        "outputs": ["minimal_context_bundle", "missing_context_request"],
    },
    "spec_architect": {
        "owns": ["design intent", "interfaces", "constraints", "acceptance criteria"],
        "outputs": ["DesignSpec", "open_questions", "approval_plan"],
    },
    "rtl_author": {
        "owns": ["synthesizable RTL", "module contracts", "reset/clock conventions"],
        "outputs": ["RTLDelta", "module_manifest"],
    },
    "verification_engineer": {
        "owns": ["testbench", "assertions", "coverage", "simulation closure"],
        "outputs": ["VerificationPlan", "FailureReproducer", "CoverageEvidence"],
    },
    "flow_planner": {
        "owns": ["toolchain selection", "PDK/run strategy", "adapter contract"],
        "outputs": ["FlowPlan", "ToolAdapterSelection", "blockers"],
    },
    "debug_engineer": {
        "owns": ["root cause", "minimal patch plan", "regression rerun"],
        "outputs": ["FailureAnalysis", "PatchPlan", "recheck_scope"],
    },
    "signoff_critic": {
        "owns": ["evidence audit", "spec drift checks", "signoff completeness"],
        "outputs": ["SignoffAudit", "residual_risk"],
    },
}


@dataclass(frozen=True)
class PermissionScope:
    name: str
    reason: str
    allowed_tools: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    write_extensions: tuple[str, ...] = ()
    allow_bash: bool = False
    allow_eda: bool = False
    allow_install: bool = False
    allow_destructive: bool = False
    allow_web: bool = False
    allowed_bash: str = "none"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["roles"] = role_chain_for_scope(self.name)
        return data


@dataclass
class ToolDecision:
    allowed: bool
    reason: str
    required_scope: str | None = None

    def to_tool_result(self) -> str:
        if self.allowed:
            return "OK"
        required = f" Required scope: {self.required_scope}." if self.required_scope else ""
        return f"Error: permission denied by AgentIC kernel. {self.reason}{required}"


@dataclass
class ContextContract:
    turn_digest: str
    scope: PermissionScope
    active_roles: list[str]
    retrieval_policy: dict[str, Any]
    handoff_schema: dict[str, Any]
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_digest": self.turn_digest,
            "permission_scope": self.scope.to_dict(),
            "active_roles": self.active_roles,
            "retrieval_policy": self.retrieval_policy,
            "handoff_schema": self.handoff_schema,
            "created_at": self.created_at,
        }


def turn_digest(user_text: str) -> str:
    return hashlib.sha256((user_text or "").encode("utf-8")).hexdigest()[:16]


def role_chain_for_scope(scope_name: str) -> list[str]:
    if scope_name == "advisor":
        return ["principal", "context_librarian"]
    if scope_name == "advisor_artifact":
        return ["principal", "context_librarian", "spec_architect"]
    if scope_name == "artifact_repair":
        return ["principal", "debug_engineer", "context_librarian"]
    if scope_name == "planning":
        return ["principal", "context_librarian", "spec_architect", "flow_planner", "signoff_critic"]
    if scope_name == "execution":
        return [
            "principal",
            "context_librarian",
            "rtl_author",
            "verification_engineer",
            "flow_planner",
            "debug_engineer",
            "signoff_critic",
        ]
    return ["principal"]


def scope_for_turn(
    *,
    is_design_task: bool,
    is_planning_round: bool,
    execution_authorized: bool,
    wants_diagram_artifact: bool,
    repairs_artifact: bool,
) -> PermissionScope:
    if repairs_artifact:
        return PermissionScope(
            name="artifact_repair",
            reason="User asked to repair a generated documentation artifact.",
            allowed_tools=("workspace", "write", "ledger"),
            write_roots=("reports/", "docs/"),
            write_extensions=(".md", ".mermaid", ".mmd", ".json"),
        )
    if not is_design_task and wants_diagram_artifact:
        return PermissionScope(
            name="advisor_artifact",
            reason="Advisor response may persist safe documentation artifacts only.",
            allowed_tools=("write", "ledger"),
            write_roots=("reports/", "docs/"),
            write_extensions=(".md", ".mermaid", ".mmd", ".json"),
        )
    if not is_design_task:
        return PermissionScope(
            name="advisor",
            reason="Conversational answer; no local tools are required.",
            allowed_tools=(),
        )
    if is_planning_round:
        return PermissionScope(
            name="planning",
            reason="Design work is not approved yet; discovery and PDK queries only.",
            allowed_tools=("workspace", "bash", "query_pdk", "ledger"),
            allow_bash=True,
            allowed_bash="read_only_discovery",
        )
    if execution_authorized:
        return PermissionScope(
            name="execution",
            reason="User approved the plan or requested a scoped follow-up edit after approval.",
            allowed_tools=("workspace", "write", "bash", "report", "web_search", "query_pdk", "ledger"),
            write_roots=(
                "docs/", "rtl/", "tb/", "dv/", "constraints/", "synth/", "sim/", "hardening/",
                "pnr/", "sta/", "signoff/", "scripts/", "logs/", "reports/",
            ),
            write_extensions=(
                ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".sdc", ".tcl", ".ys",
                ".mk", ".json", ".yaml", ".yml", ".md", ".txt", ".log", ".rpt", ".cfg",
                ".py", ".sh", ".csv", ".sp", ".spi", ".lvs", ".mermaid", ".mmd",
            ),
            allow_bash=True,
            allow_eda=True,
            allow_web=True,
            allowed_bash="eda_and_workspace",
        )
    return PermissionScope(
        name="planning",
        reason="Defaulting to planning because execution was not explicitly authorized.",
        allowed_tools=("workspace", "bash", "query_pdk", "ledger"),
        allow_bash=True,
        allowed_bash="read_only_discovery",
    )


def validate_tool_call(scope: PermissionScope, tool_name: str, args: dict[str, Any]) -> ToolDecision:
    if tool_name not in scope.allowed_tools:
        return ToolDecision(False, f"`{tool_name}` is not allowed in `{scope.name}` scope.", scope.name)

    if tool_name == "write":
        path = str(args.get("path") or "").replace("\\", "/").lstrip("/")
        if not _path_allowed(path, scope.write_roots, scope.write_extensions):
            return ToolDecision(
                False,
                f"`{path}` is outside allowed write roots/extensions for `{scope.name}`.",
                scope.name,
            )

    if tool_name == "bash":
        command = str(args.get("command") or "")
        if not scope.allow_bash:
            return ToolDecision(False, "Shell execution is disabled in this scope.", "execution")
        if DESTRUCTIVE_RE.search(command) and not scope.allow_destructive:
            return ToolDecision(False, "Destructive shell commands require explicit destructive scope.", "destructive")
        if INSTALL_RE.search(command) and not scope.allow_install:
            return ToolDecision(False, "Install/setup commands require explicit install approval.", "install")
        if scope.allowed_bash == "read_only_discovery":
            if not DISCOVERY_BASH_RE.search(command) or _eda_tool_re().search(command):
                return ToolDecision(
                    False,
                    "Planning scope only permits read-only discovery commands, not EDA execution.",
                    "execution",
                )

    if tool_name == "web_search" and not scope.allow_web:
        return ToolDecision(False, "Public web search is disabled in this scope.", "execution")

    return ToolDecision(True, "allowed")


def build_context_contract(user_text: str, scope: PermissionScope) -> ContextContract:
    roles = role_chain_for_scope(scope.name)
    return ContextContract(
        turn_digest=turn_digest(user_text),
        scope=scope,
        active_roles=roles,
        retrieval_policy={
            "budget": "role_scoped",
            "primary_sources": ["design_state", "evidence_graph", "repo_map", "targeted_file_reads"],
            "forbidden": ["full_logs_by_default", "full_pdk_dump", "unbounded_chat_history"],
            "snippets": {
                "max_relevant_snippets": 8,
                "max_lines_per_snippet": 34,
                "require_exact_file_read_before_edit": True,
            },
        },
        handoff_schema={
            "roles": {name: ROLE_PROFILES[name] for name in roles if name in ROLE_PROFILES},
            "required_fields": ["intent_digest", "scope", "inputs", "outputs", "evidence_refs", "open_risks"],
        },
    )


def _path_allowed(path: str, roots: tuple[str, ...], extensions: tuple[str, ...]) -> bool:
    if not path or path.startswith(".") or "/." in path or ".." in path.split("/"):
        return False
    lower = path.lower()
    if roots and not any(lower.startswith(root.lower()) or f"/{root.lower()}" in f"/{lower}" for root in roots):
        return False
    if extensions and not any(lower.endswith(ext.lower()) for ext in extensions):
        return False
    return True
