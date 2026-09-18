"""Only subprocess funnel: argv-only, cwd jail, timeouts, output caps.

The agent `bash` tool keeps shell semantics in `local_tools.run_bash`.
Process outcomes are returned, never raised.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Sequence

DEFAULT_TIMEOUT = 120
DEFAULT_MAX_OUTPUT = 256_000


@dataclass
class ExecResult:
    ok: bool
    code: int | None
    stdout: str = ""
    stderr: str = ""
    combined: str = ""
    truncated: bool = False
    timed_out: bool = False
    refused: str = ""


def _refuse(reason: str) -> ExecResult:
    return ExecResult(ok=False, code=None, stderr=reason, refused=reason)


def _resolve_cwd(cwd: str | None, workspace_root: str | None) -> str | None:
    """Enforce the cwd jail: an explicit cwd must stay inside workspace_root."""
    if not cwd or not workspace_root:
        return None
    try:
        root = os.path.realpath(os.path.abspath(workspace_root))
        target = os.path.realpath(os.path.abspath(cwd))
    except Exception:
        return f"refusing to run outside resolvable workspace: {cwd}"
    try:
        if os.path.commonpath([root, target]) != root:
            return f"refusing to run outside workspace: {cwd}"
    except ValueError:
        return f"refusing to run outside workspace: {cwd}"
    return None


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n[truncated: output exceeded {limit} chars]", True


def run(
    cmd: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    workspace_root: str | None = None,
) -> ExecResult:
    """Run argv with jail + timeout + caps. Missing binary, timeouts and
    non-zero exits come back as results, not exceptions."""
    if not cmd or not all(isinstance(part, str) and part for part in cmd):
        return _refuse("refusing to run empty or non-string command")
    jail = _resolve_cwd(cwd, workspace_root)
    if jail:
        return _refuse(jail)
    try:
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(int(timeout), 1),
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return ExecResult(ok=False, code=None, timed_out=True,
                          stderr=f"command timed out after {timeout}s")
    except OSError as exc:
        return ExecResult(ok=False, code=None, stderr=f"failed to start: {exc}")
    half = max(int(max_output) // 2, 1024)
    out, t1 = _cap(proc.stdout or "", half)
    err, t2 = _cap(proc.stderr or "", half)
    code = proc.returncode
    return ExecResult(
        ok=code == 0,
        code=code,
        stdout=out,
        stderr=err,
        combined=out + "\n" + err,
        truncated=t1 or t2,
    )
