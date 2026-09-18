"""Wire contract lock: routes must not drift silently (UI is a thin client)."""
from agentic_server import main
from agentic_server import opencode_bridge


def _routes() -> set[str]:
    return {r.path for r in main.app.routes if hasattr(r, "path")}


def test_key_engine_routes_present():
    routes = _routes()
    for path in [
        "/health",
        "/version",
        "/pdks",
        "/tools/status",
        "/tools/adapters",
        "/vlsi/route",
        "/chat/converse",
        "/designs",
        "/build/artifacts",
        "/opencode/bridge/health",
        "/opencode/session/resolve",
        "/opencode/tool",
        "/opencode/runtime/status",
        "/opencode/desktop/sessions",
        "/opencode/desktop/message",
        "/opencode/rlm/execute",
        "/opencode/rlm/state",
    ]:
        assert path in routes, f"engine route missing: {path}"


def test_billing_auth_routes_stay_removed():
    routes = _routes()
    for path in [
        "/license/status",
        "/plans",
        "/auth/password-login",
        "/auth/signup",
        "/auth/refresh",
        "/auth/desktop-session",
        "/auth/profile",
        "/auth/logout",
        "/auth/google/start",
        "/purchase/start",
        "/usage/build",
        "/checkout/create",
        "/billing/status",
        "/opencode/collab/team/join",
        "/opencode/container/status",
        "/opencode/pr/list",
        "/opencode/space/list",
        "/opencode/background-tasks",
        "/opencode/kernel/flow-diff/request",
    ]:
        assert path not in routes, f"removed route resurrected: {path}"


def test_local_license_stub_always_active():
    status = main.resolve_license_status(None)
    assert status["active"] is True
    assert status["plan"] == "local"
    assert status["source"] == "local"


def test_config_defaults(monkeypatch):
    from agentic_server import config

    for var in ("AGENTIC_WORKSPACE", "AGENTIC_PORT", "PORT", "AGENTIC_MODE"):
        monkeypatch.delenv(var, raising=False)
    assert config.port() == 7860
    assert config.default_mode() == "advisor"
    assert config.workspace_root().endswith("AgentIC-workspace")
    assert config.bridge_token() == ""
    assert len(config.REGISTRY) >= 39


def test_config_env_override(monkeypatch):
    from agentic_server import config

    monkeypatch.setenv("AGENTIC_PORT", "19999")
    monkeypatch.setenv("AGENTIC_MODE", "builder")
    assert config.port() == 19999
    assert config.default_mode() == "builder"


def test_bridge_has_no_license_actions():
    assert hasattr(opencode_bridge, "run_pipeline")
    assert hasattr(opencode_bridge, "run_tool")
    assert not hasattr(opencode_bridge, "run_license_status")
    assert not hasattr(opencode_bridge, "run_auth_profile")
    assert not hasattr(opencode_bridge, "run_auth_logout")
