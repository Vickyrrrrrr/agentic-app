"""Exec funnel tests: hermetic (uses the running interpreter only)."""
import sys

from agentic_server import exec_service


def test_echo_ok():
    res = exec_service.run([sys.executable, "-c", "print('hello')"], timeout=30)
    assert res.ok is True
    assert res.code == 0
    assert "hello" in res.stdout
    assert res.timed_out is False
    assert res.truncated is False


def test_nonzero_is_data_not_exception():
    res = exec_service.run([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=30)
    assert res.ok is False
    assert res.code == 3
    assert res.timed_out is False


def test_timeout_kills_and_reports():
    res = exec_service.run(
        [sys.executable, "-c", "import time; time.sleep(60)"], timeout=2
    )
    assert res.ok is False
    assert res.timed_out is True
    assert res.code is None


def test_missing_binary_is_data():
    res = exec_service.run(["agentic-definitely-not-a-binary-xyz"], timeout=30)
    assert res.ok is False
    assert res.code is None
    assert res.stderr != ""


def test_empty_command_refused():
    res = exec_service.run([], timeout=30)
    assert res.ok is False
    assert res.refused != ""


def test_output_cap_truncates(tmp_path):
    res = exec_service.run(
        [sys.executable, "-c", "print('x' * 100000)"],
        timeout=30,
        max_output=4096,
    )
    assert res.truncated is True
    assert len(res.stdout) < 100000
    assert "truncated" in res.stdout


def test_cwd_jail(tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ok = exec_service.run(
        [sys.executable, "-c", "print('in')"],
        timeout=30,
        cwd=str(inside),
        workspace_root=str(inside),
    )
    assert ok.ok is True
    refused = exec_service.run(
        [sys.executable, "-c", "print('out')"],
        timeout=30,
        cwd=str(outside),
        workspace_root=str(inside),
    )
    assert refused.ok is False
    assert refused.refused != ""
