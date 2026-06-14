from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


class OpenCodeRuntimeError(RuntimeError):
    pass


_PROCESS: subprocess.Popen | None = None


def _base_url() -> str:
    raw = os.environ.get("AGENTIC_OPENCODE_URL") or os.environ.get("OPENCODE_SERVER_URL") or "http://127.0.0.1:4096"
    return raw.rstrip("/")


def _credentials() -> tuple[str, str] | None:
    password = os.environ.get("AGENTIC_OPENCODE_PASSWORD") or os.environ.get("OPENCODE_SERVER_PASSWORD")
    if not password:
        return None
    username = os.environ.get("AGENTIC_OPENCODE_USERNAME") or os.environ.get("OPENCODE_SERVER_USERNAME") or "opencode"
    return username, password


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    credentials = _credentials()
    if credentials:
        token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    if extra:
        headers.update(extra)
    return headers


def _url(path: str, query: dict[str, Any] | None = None) -> str:
    if not path.startswith("/"):
        path = "/" + path
    encoded = urllib.parse.urlencode({k: v for k, v in (query or {}).items() if v is not None})
    return f"{_base_url()}{path}" + (f"?{encoded}" if encoded else "")


def request_json(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    body = None
    headers = _headers()
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(_url(path, query), data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return None
            text = raw.decode("utf-8", errors="replace")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OpenCodeRuntimeError(f"OpenCode HTTP {exc.code} for {path}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OpenCodeRuntimeError(f"OpenCode is not reachable at {_base_url()}: {exc.reason}") from exc


def health(timeout: float = 2.0) -> dict[str, Any]:
    try:
        data = request_json("GET", "/global/health", timeout=timeout)
        return {
            "healthy": bool(isinstance(data, dict) and data.get("healthy")),
            "version": data.get("version") if isinstance(data, dict) else None,
            "base_url": _base_url(),
            "started_by_agentic": _PROCESS is not None and _PROCESS.poll() is None,
            "pid": _PROCESS.pid if _PROCESS is not None and _PROCESS.poll() is None else None,
        }
    except Exception as exc:
        return {
            "healthy": False,
            "version": None,
            "base_url": _base_url(),
            "started_by_agentic": _PROCESS is not None and _PROCESS.poll() is None,
            "pid": _PROCESS.pid if _PROCESS is not None and _PROCESS.poll() is None else None,
            "error": str(exc),
        }


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    explicit = os.environ.get("AGENTIC_OPENCODE_ROOT")
    if explicit:
        roots.append(Path(explicit).expanduser())
    cwd = Path.cwd().resolve()
    roots.extend([
        cwd.parent / "opencode" / "packages" / "opencode",
        Path.home() / "opencode" / "packages" / "opencode",
    ])
    return roots


def discover_root() -> Path | None:
    for root in _candidate_roots():
        try:
            if (root / "package.json").is_file() and (root / "src" / "index.ts").is_file():
                return root
        except OSError:
            continue
    return None


def _start_command(hostname: str, port: int) -> tuple[list[str], Path | None]:
    root = discover_root()
    template = os.environ.get("AGENTIC_OPENCODE_COMMAND", "").strip()
    if template:
        rendered = template.format(
            host=hostname,
            hostname=hostname,
            port=port,
            root=str(root or Path.cwd()),
        )
        return shlex.split(rendered), root
    if root and shutil.which("bun"):
        return ["bun", "run", "dev", "serve", "--hostname", hostname, "--port", str(port)], root
    raise OpenCodeRuntimeError(
        "OpenCode is not running and AgentIC cannot infer a start command. "
        "Set AGENTIC_OPENCODE_URL to an existing server or AGENTIC_OPENCODE_COMMAND to a serve command."
    )


def start(hostname: str = "127.0.0.1", port: int = 4096, timeout: float = 20.0) -> dict[str, Any]:
    current = health(timeout=1.0)
    if current.get("healthy"):
        return {**current, "started": False}

    global _PROCESS
    if _PROCESS is not None and _PROCESS.poll() is None:
        return {**health(timeout=1.0), "started": False}

    command, cwd = _start_command(hostname, port)
    log_dir = Path(os.environ.get("AGENTIC_WORKSPACE", str(Path.home() / "AgentIC-workspace"))) / ".agentic" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "opencode-server.log"
    env = os.environ.copy()
    env.setdefault("OPENCODE_SERVER_USERNAME", os.environ.get("AGENTIC_OPENCODE_USERNAME", "opencode"))
    if os.environ.get("AGENTIC_OPENCODE_PASSWORD"):
        env.setdefault("OPENCODE_SERVER_PASSWORD", os.environ["AGENTIC_OPENCODE_PASSWORD"])
    stream = log_file.open("ab")
    _PROCESS = subprocess.Popen(command, cwd=str(cwd) if cwd else None, env=env, stdout=stream, stderr=stream)

    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        time.sleep(0.35)
        last = health(timeout=1.0)
        if last.get("healthy"):
            return {
                **last,
                "started": True,
                "command": command,
                "cwd": str(cwd) if cwd else None,
                "log_path": str(log_file),
            }
        if _PROCESS.poll() is not None:
            break
    raise OpenCodeRuntimeError(
        f"OpenCode server did not become healthy. pid={_PROCESS.pid if _PROCESS else None}, "
        f"exit={_PROCESS.poll() if _PROCESS else None}, log={log_file}, last={last}"
    )


def create_session(directory: str, *, title: str | None = None, agent: str = "agentic-vlsi") -> dict[str, Any]:
    payload: dict[str, Any] = {"agent": agent}
    if title:
        payload["title"] = title
    data = request_json("POST", "/session", query={"directory": directory}, payload=payload)
    if not isinstance(data, dict) or not data.get("id"):
        raise OpenCodeRuntimeError(f"OpenCode returned an invalid session object: {data!r}")
    return data


def prompt_async(
    session_id: str,
    directory: str,
    text: str,
    *,
    agent: str = "agentic-vlsi",
    model: dict[str, str] | None = None,
    system: str | None = None,
    variant: str | None = None,
    message_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "agent": agent,
        "parts": [{"type": "text", "text": text}],
    }
    if model:
        payload["model"] = model
    if system:
        payload["system"] = system
    if variant:
        payload["variant"] = variant
    if message_id:
        payload["messageID"] = message_id
    request_json("POST", f"/session/{session_id}/prompt_async", query={"directory": directory}, payload=payload, timeout=30.0)


def list_messages(session_id: str, directory: str, *, limit: int = 200) -> list[dict[str, Any]]:
    data = request_json("GET", f"/session/{session_id}/message", query={"directory": directory, "limit": limit})
    return data if isinstance(data, list) else []


def abort_session(session_id: str, directory: str) -> bool:
    request_json("POST", f"/session/{session_id}/abort", query={"directory": directory})
    return True


def stream_events(directory: str) -> Iterable[dict[str, Any]]:
    request = urllib.request.Request(
        _url("/event", {"directory": directory}),
        headers=_headers({"Accept": "text/event-stream"}),
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=None) as response:
        data_lines: list[str] = []
        for raw in response:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line:
                continue
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            data_lines = []
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = {"type": "raw", "properties": {"text": payload}}
            if isinstance(parsed, dict):
                yield parsed
            else:
                yield {"type": "raw", "properties": parsed}
