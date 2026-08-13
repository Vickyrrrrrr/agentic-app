"""
agentic_flow_lock.py — Enforce enterprise flow lock contracts for CAD/Team Leads.

Prevents unauthorized modification or bypass of locked company Makefile/TCL flows,
while allowing authorized parameter overrides (e.g., NETLIST, BLOCK, SEED).
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from agentic_handoffs import FlowLockContract


@dataclass
class FlowLockValidationResult:
    is_valid: bool
    reason: str
    requires_approval: bool = False
    violating_rules: list[str] = field(default_factory=list)


class FlowLockManager:
    """Enforces enterprise flow lock rules across agent executions."""

    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root

    def evaluate_action(
        self,
        contract: FlowLockContract | None,
        action_type: str,  # 'run_command', 'modify_script', 'override_param'
        target_path: str,
        user_or_caller: str = "",
        command_str: str = "",
    ) -> FlowLockValidationResult:
        """Evaluate if an action complies with the FlowLockContract."""
        if not contract or not contract.is_locked:
            return FlowLockValidationResult(
                is_valid=True,
                reason="No active flow lock contract or flow is unlocked.",
            )

        # Check if caller is an authorized Team Lead
        is_lead = user_or_caller in contract.team_leads if contract.team_leads else False

        # 1. Direct flow script modifications
        if action_type in ("modify_script", "write_file", "edit_file"):
            rel_target = os.path.relpath(target_path, self.workspace_root) if os.path.isabs(target_path) else target_path
            rel_master = os.path.relpath(contract.master_script_path, self.workspace_root) if os.path.isabs(contract.master_script_path) else contract.master_script_path

            if rel_target == rel_master or "master_flow" in rel_target.lower():
                if not is_lead:
                    return FlowLockValidationResult(
                        is_valid=False,
                        reason=f"Target file '{rel_target}' is locked by {contract.signed_off_by}. Flow modification requires Team Lead sign-off.",
                        requires_approval=True,
                        violating_rules=["modify_locked_master_flow"],
                    )

        # 2. Command execution checks
        if action_type == "run_command" and command_str:
            # Check for prohibited action strings
            for prohibited in contract.prohibited_actions:
                if prohibited in command_str:
                    return FlowLockValidationResult(
                        is_valid=False,
                        reason=f"Command violates flow lock rule: prohibited action '{prohibited}' detected.",
                        requires_approval=True,
                        violating_rules=[f"prohibited:{prohibited}"],
                    )

        return FlowLockValidationResult(
            is_valid=True,
            reason="Action complies with FlowLockContract.",
        )


def validate_flow_lock(
    workspace_root: str,
    contract: FlowLockContract | None,
    action_type: str,
    target_path: str,
    command_str: str = "",
    caller: str = "",
) -> FlowLockValidationResult:
    """Helper function to validate actions against flow lock."""
    mgr = FlowLockManager(workspace_root)
    return mgr.evaluate_action(contract, action_type, target_path, caller, command_str)
