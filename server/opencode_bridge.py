import sys
import os
import json

# Add current directory to path if running directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_tools import dispatch_tool
from agentic_handoffs import schema_catalog
from agentic_kernel import build_context_contract, scope_for_turn
from agentic_role_runner import RoleContext, persist_role_results, run_role_pipeline
from agentic_validators import validation_schema_catalog
from design_intent import build_or_update_design_intent
from local_tools import detect_environment
from flow_runtime import recommend_flow
from vlsi_state import DesignStateStore
from vlsi_capability_graph import assess_design_readiness
from session_workflow import classify_session_workflow

def run_tool(payload):
    name = payload.get("name")
    args = payload.get("args", {})
    workspace_root = payload.get("workspace_root")
    design_name = payload.get("design_name", "scratch")

    result = dispatch_tool(name, args, workspace_root, design_name)
    return {"success": True, "result": result}

def run_pipeline(payload):
    user_text = payload.get("user_text", "")
    workspace_root = payload.get("workspace_root")
    design_name = payload.get("design_name", "scratch")
    pdk_profile = payload.get("pdk_profile", os.environ.get("PDK", ""))

    # Simulate a design task turn categorization
    messages = [{"role": "user", "content": user_text}]
    workflow_decision = classify_session_workflow(user_text, messages)

    state_store = DesignStateStore(workspace_root, design_name)
    state_store.set_intent(user_text, pdk_profile)
    state_store.upsert_design_fact("session_workflow", workflow_decision.mode, workflow_decision.to_record(), source="session_workflow_router")

    env = detect_environment()
    flow_decision = recommend_flow(env, requested_pdk=pdk_profile)
    state_store.set_flow_decision(flow_decision)

    is_design_task = True
    is_planning_round = False
    execution_authorized = True

    kernel_scope = scope_for_turn(
        is_design_task=is_design_task,
        is_planning_round=is_planning_round,
        execution_authorized=execution_authorized,
        wants_diagram_artifact=False,
        repairs_artifact=False,
    )
    kernel_contract = build_context_contract(user_text, kernel_scope).to_dict()
    state_store.set_context_contract(kernel_contract)

    role_ctx = RoleContext(
        user_text=user_text,
        workspace_root=workspace_root,
        design_name=design_name,
        context_contract=kernel_contract,
        flow_decision=flow_decision,
        env=env,
        design_state=state_store.load(),
        needs_spec_clarification=False,
    )
    role_results = run_role_pipeline(role_ctx)
    role_counts = persist_role_results(state_store, role_results)

    design_intent = build_or_update_design_intent(
        workspace_root=workspace_root,
        design_name=design_name,
        user_text=user_text,
        flow_decision=flow_decision,
        role_results=role_results,
        env=env,
        previous=state_store.load(),
    )
    state_store.set_design_intent(design_intent.model_dump(mode="json"))

    readiness = assess_design_readiness(design_intent.model_dump(mode="json"), env.get("capability_graph") or {})
    state_store.record_evidence("design_readiness", design_intent.intent_id, readiness)
    state_store.upsert_design_fact("readiness", design_intent.intent_id, readiness, source="capability_graph")

    state_store.record_evidence("role_pipeline", kernel_scope.name, {
        "roles": [result.role for result in role_results],
        "counts": role_counts,
        "risks": [risk for result in role_results for risk in result.risks][:20],
        "intent_id": design_intent.intent_id,
        "project_root": design_intent.project_root,
    })

    from context_engine import build_agent_context_packet
    context_packet = build_agent_context_packet(
        workspace_root=workspace_root,
        design_name=design_name,
        user_text=user_text,
        env=env,
        flow_decision=flow_decision,
    )

    return {
        "success": True,
        "kernel_contract": kernel_contract,
        "schema_catalog": schema_catalog(),
        "validation_schema_catalog": validation_schema_catalog(),
        "context_packet": context_packet,
        "flow_decision": flow_decision,
    }

def main():
    try:
        payload = json.loads(sys.stdin.read())
        action = payload.get("action")
        if action == "tool":
            res = run_tool(payload)
        elif action == "pipeline":
            res = run_pipeline(payload)
        else:
            res = {"success": False, "error": f"Unknown action: {action}"}
        print(json.dumps(res))
    except Exception as e:
        import traceback
        print(json.dumps({"success": False, "error": str(e), "traceback": traceback.format_exc()}))

if __name__ == "__main__":
    main()
