import asyncio
import json
import logging
import os
import shutil
import subprocess
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from models import ChatRequest
from workspace import list_artifacts, list_designs, read_artifact, ensure_workspace
from chat_agent import converse_stream

app = FastAPI(title="AgentIC Local")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WS_ROOT = os.environ.get("AGENTIC_WORKSPACE") or os.path.expanduser("~/AgentIC-workspace")
ensure_workspace(WS_ROOT)


@app.get("/pdks")
async def get_pdks():
    return detect_environment()


@app.get("/profile")
async def get_profile():
    return {
        "auth_enabled": False,
        "plan": "local",
        "has_byok_key": True,
        "email": "local@agentic.app",
    }


@app.post("/profile/byok/test")
async def test_byok_connection():
    # Local mode: accept any key format, let the agent runtime handle errors
    return {"status": "ok", "message": "Local mode BYOK test skipped — agent will validate on first use"}


@app.post("/profile/byok")
async def save_byok():
    return {"status": "ok"}


@app.get("/jobs")
async def get_jobs():
    return {"jobs": []}


@app.get("/billing/status")
async def get_billing_status():
    return {"has_subscription": False, "plan": "local", "build_limit": 999, "used_builds": 0}


@app.get("/designs")
async def get_designs():
    return {"designs": list_designs(WS_ROOT)}


@app.get("/build/artifacts")
@app.get("/build/artifacts/{design_name}")
async def get_artifacts(design_name: str = ""):
    return list_artifacts(design_name, WS_ROOT)


@app.get("/build/artifacts/{design_name}/{file_name:path}")
async def get_artifact(design_name: str, file_name: str):
    content = read_artifact(design_name, file_name, WS_ROOT)
    if content is None:
        raise HTTPException(404, "Artifact not found")
    return content


@app.post("/chat/converse")
async def chat_converse(req: ChatRequest):
    api_key = req.api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = req.base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = req.model or os.environ.get("OPENAI_MODEL", "gpt-4o")

    async def event_generator():
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()
        LLM_TIMEOUT = 300

        def run_sync_gen():
            """Run the synchronous generator and push events to the queue."""
            try:
                for event in converse_stream(
                    messages=req.messages,
                    api_key=api_key,
                    workspace_root=WS_ROOT,
                    base_url=base_url,
                    model=model,
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))

        task = loop.run_in_executor(None, run_sync_gen)

        try:
            while True:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=LLM_TIMEOUT)

                if kind == "done":
                    yield {"event": "message", "data": json.dumps({
                        "type": "stream_end", "state": "done", "timestamp": time.time(),
                    })}
                    break

                if kind == "error":
                    yield {"event": "message", "data": json.dumps({
                        "type": "error", "state": "ERROR", "message": payload, "timestamp": time.time(),
                    })}
                    break

                event = payload
                event_type = event.get("type", "")
                content = event.get("content", "")
                state = event.get("state", "")

                sse_data = {
                    "type": event_type,
                    "state": state or ("THINKING" if event_type == "reasoning" else "done"),
                    "message": content,
                    "content": content,
                    "timestamp": time.time(),
                }

                yield {"event": "message", "data": json.dumps(sse_data)}

        except asyncio.TimeoutError:
            yield {"event": "message", "data": json.dumps({
                "type": "error", "state": "ERROR",
                "message": "Request timed out after 300 seconds. The LLM may be overloaded or unreachable. Try again or check your API key.",
                "timestamp": time.time(),
            })}
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    return EventSourceResponse(event_generator())


@app.post("/lab/syntax-check")
@app.post("/lab/synthesize")
@app.post("/lab/simulate")
@app.post("/lab/generate-testbench")
@app.post("/lab/ai-assist")
@app.post("/lab/gtkwave")
async def lab_endpoint_generic():
    return {
        "status": "ok",
        "message": "Lab endpoint available in local mode. Use the Design Studio for agent-driven flows.",
    }


@app.get("/workspace/active")
async def get_active_workspace():
    return {"active": None}


def detect_environment() -> dict:
    """Report which EDA tools are in PATH — no hardcoded PDK paths.
    The agent discovers PDKs dynamically at runtime using its tools."""
    tools = {}
    for name in ("docker", "yosys", "iverilog", "verilator", "opensta", "openroad",
                 "gtkwave", "make", "python3"):
        tools[name] = shutil.which(name) is not None

    if tools["docker"]:
        try:
            result = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True, text=True, timeout=10
            )
            tools["docker_images"] = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()] if result.returncode == 0 else []
        except Exception:
            tools["docker_images"] = []
    else:
        tools["docker_images"] = []

    return tools


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=7860)
