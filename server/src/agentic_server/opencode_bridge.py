import sys
import os
import json

from agentic_server.agent_tools import dispatch_tool
from agentic_server.app_capabilities import build_app_capability_contract
from agentic_server.agentic_handoffs import schema_catalog
from agentic_server.agentic_kernel import build_context_contract, scope_for_turn
from agentic_server.agentic_role_runner import RoleContext, persist_role_results, run_role_pipeline
from agentic_server.agentic_validators import validation_schema_catalog
from agentic_server.design_intent import build_or_update_design_intent
from agentic_server.local_tools import detect_environment
from agentic_server.flow_runtime import recommend_flow
from agentic_server.vlsi_state import DesignStateStore
from agentic_server.vlsi_capability_graph import assess_design_readiness
from agentic_server.session_workflow import classify_session_workflow

import time
from agentic_server.models import OpenCodeSessionRequest, OpenCodeToolRequest
from agentic_server.main import (
    _resolve_opencode_mapping,
    _stored_agentic_mode,
    _normalize_agentic_mode,
    _read_agentic_sessions,
    _write_agentic_sessions,
    _session_modes
)

def run_tool(payload):
    name = payload.get("name")
    args = payload.get("args", {})
    workspace_root = payload.get("workspace_root")
    design_name = payload.get("design_name", "scratch")

    result = dispatch_tool(name, args, workspace_root, design_name)
    return {"success": True, "result": result}

def run_get_mode(payload):
    from agentic_server import main
    session_id = payload.get("session_id")
    mode = _session_modes.get(session_id) or getattr(main, "_global_agentic_mode", "advisor")
    return {"success": True, "agentic_mode": mode}

def run_set_mode(payload):
    from agentic_server import main
    session_id = payload.get("session_id")
    mode = _normalize_agentic_mode(payload.get("mode"))
    main._global_agentic_mode = mode
    if session_id:
        _session_modes[session_id] = mode
        data = _read_agentic_sessions()
        existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
        existing["agentic_mode"] = mode
        existing["updated_at"] = time.time()
        data[session_id] = existing
        _write_agentic_sessions(data)
    return {"success": True, "agentic_mode": mode}

def run_git_clone(payload):
    args = payload.get("args", {})
    req = OpenCodeToolRequest(
        session_id=payload.get("session_id", "ui"),
        agent="agentic-vlsi",
        agentic_mode=payload.get("agentic_mode", "advisor"),
        workspace_root=payload.get("workspace_root", ""),
        design_name=payload.get("design_name", ""),
        name="git_clone",
        args=args
    )
    mapping = _resolve_opencode_mapping(req)
    from agentic_server.agent_tools import git_clone
    result = git_clone(
        url=args.get("url", ""),
        workspace_root=mapping["design_root"],
        target_dir=args.get("target_dir"),
        branch=args.get("branch", "main"),
        token=args.get("token", ""),
    )
    return {"success": not result.startswith("Error:"), "result": result, "session": mapping}

def run_signoff_report(payload):
    from agentic_server.signoff_reports import build_signoff_report
    from agentic_server.main import _read_agentic_sessions, _safe_workspace_root, _safe_design_dir_name, _session_hash, WS_ROOT
    import os
    session_id = payload.get("session_id")
    workspace_root = payload.get("workspace_root", "")
    data = _read_agentic_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    root = _safe_workspace_root(str(existing.get("workspace_root") or workspace_root or WS_ROOT))
    fallback = f"session_{_session_hash(session_id)}"
    design_name = _safe_design_dir_name(str(existing.get("design_name") or fallback), fallback)
    design_root = str(existing.get("design_root") or os.path.join(root, design_name))
    return build_signoff_report(design_root, design_name)

def run_sta_report(payload):
    from agentic_server.sta_reports import build_sta_report
    from agentic_server.main import _read_agentic_sessions, _safe_workspace_root, _safe_design_dir_name, _session_hash, WS_ROOT
    import os
    session_id = payload.get("session_id")
    workspace_root = payload.get("workspace_root", "")
    data = _read_agentic_sessions()
    existing = data.get(session_id) if isinstance(data.get(session_id), dict) else {}
    root = _safe_workspace_root(str(existing.get("workspace_root") or workspace_root or WS_ROOT))
    fallback = f"session_{_session_hash(session_id)}"
    design_name = _safe_design_dir_name(str(existing.get("design_name") or fallback), fallback)
    design_root = str(existing.get("design_root") or os.path.join(root, design_name))
    return build_sta_report(design_root, design_name)

def run_waveforms(payload):
    design_name = payload.get("design_name")
    return {
        "design_name": design_name,
        "status": "ready",
        "format": "vcd",
        "signals": ["clk", "data_in", "data_out"],
        "transitions": 10000
    }

def run_pipeline(payload):
    from agentic_server import main
    user_text = payload.get("user_text", "")
    session_id = payload.get("session_id", "")
    mode = payload.get("agentic_mode") or _session_modes.get(session_id) or getattr(main, "_global_agentic_mode", "advisor")
    req = OpenCodeSessionRequest(
        session_id=session_id,
        agent="agentic-vlsi",
        agentic_mode=mode,
        workspace_root=payload.get("workspace_root", ""),
        design_name=payload.get("design_name", ""),
        user_text=user_text
    )
    mapping = _resolve_opencode_mapping(req)
    if payload.get("fast"):
        from agentic_server.main import _fast_opencode_session_response
        return _fast_opencode_session_response(req, mapping)

    workspace_root = mapping["workspace_root"]
    design_name = mapping["design_name"]
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
    kernel_contract["app_capabilities"] = build_app_capability_contract(workspace_root, env)
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

    from agentic_server.context_engine import build_agent_context_packet
    context_packet = build_agent_context_packet(
        workspace_root=workspace_root,
        design_name=design_name,
        user_text=user_text,
        env=env,
        flow_decision=flow_decision,
    )

    return {
        "success": True,
        "session": mapping,
        "workflow": workflow_decision.to_record(),
        "kernel_scope": kernel_scope.name,
        "kernel_contract": kernel_contract,
        "schema_catalog": schema_catalog(),
        "validation_schema_catalog": validation_schema_catalog(),
        "context_packet": context_packet,
        "flow_decision": flow_decision,
    }

def _start_http_background():
    try:
        import uvicorn
        from agentic_server.main import app
        port = int(os.environ.get("AGENTIC_PORT") or os.environ.get("PORT") or "7860")
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except Exception as e:
        sys.stderr.write(f"[Python Bridge Daemon] HTTP server start failed or port in use: {e}\n")
        sys.stderr.flush()

def main():
    if "--daemon" in sys.argv:
        import threading as _threading
        t = _threading.Thread(target=_start_http_background, daemon=True)
        t.start()
        try:

            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                if line == "exit":
                    break
                try:
                    payload = json.loads(line)
                    action = payload.get("action")
                    if action == "tool":
                        res = run_tool(payload)
                    elif action == "pipeline":
                        res = run_pipeline(payload)
                    elif action == "get_mode":
                        res = run_get_mode(payload)
                    elif action == "set_mode":
                        res = run_set_mode(payload)
                    elif action == "git_clone":
                        res = run_git_clone(payload)
                    elif action == "signoff_report":
                        res = run_signoff_report(payload)
                    elif action == "sta_report":
                        res = run_sta_report(payload)
                    elif action == "waveforms":
                        res = run_waveforms(payload)
                    else:
                        res = {"success": False, "error": f"Unknown action: {action}"}
                except Exception as e:
                    import traceback
                    res = {"success": False, "error": str(e), "traceback": traceback.format_exc()}
                sys.stdout.write(json.dumps(res) + "\n")
                sys.stdout.flush()
        except KeyboardInterrupt:
            pass
        return

    try:
        payload = json.loads(sys.stdin.read())
        action = payload.get("action")
        if action == "tool":
            res = run_tool(payload)
        elif action == "pipeline":
            res = run_pipeline(payload)
        elif action == "get_mode":
            res = run_get_mode(payload)
        elif action == "set_mode":
            res = run_set_mode(payload)
        elif action == "git_clone":
            res = run_git_clone(payload)
        elif action == "signoff_report":
            res = run_signoff_report(payload)
        elif action == "sta_report":
            res = run_sta_report(payload)
        elif action == "waveforms":
            res = run_waveforms(payload)
        else:
            res = {"success": False, "error": f"Unknown action: {action}"}
        print(json.dumps(res))
    except Exception as e:
        import traceback
        print(json.dumps({"success": False, "error": str(e), "traceback": traceback.format_exc()}))

if __name__ == "__main__":
    main()
