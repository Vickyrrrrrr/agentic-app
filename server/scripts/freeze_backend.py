#!/usr/bin/env python3
"""Freeze the engine (PyInstaller one-dir, Linux-first).

One-dir: no unpack cost per launch; invisible inside the AppImage.
Excludes are safe: siliconcompiler has an ImportError fallback,
amaranth is never imported by the backend, edalize is dropped.
Build on ubuntu-22.04 to keep the glibc floor low.

Run from server/: pip install -r requirements.txt pyinstaller
                   python scripts/freeze_backend.py [--out <dir>]
"""
import argparse
import os
import shutil
import subprocess
import sys

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SERVER_DIR)
DEFAULT_OUT = os.path.join(
    REPO_ROOT, "apps", "agentic-desktop", "packages", "desktop",
    "resources", "backend", "agentic-backend",
)

EXCLUDES = [
    "siliconcompiler",
    "scipy", "numpy", "pandas", "matplotlib", "tkinter",
    "amaranth",
    "pytest", "_pytest",
]

HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "sse_starlette",
    "pydantic",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--name", default="agentic-backend")
    args = parser.parse_args()

    # Bake the prod channel marker the engine reads at startup.
    channel_path = os.path.join(SERVER_DIR, "src", "agentic_server", "_build_channel.py")
    with open(channel_path, "w", encoding="utf-8") as fh:
        fh.write('CHANNEL = "prod"\n')

    work = os.path.join(SERVER_DIR, "build", "freeze")
    dist = os.path.join(SERVER_DIR, "build", "dist")
    os.makedirs(work, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--name", args.name,
        "--workpath", work, "--distpath", dist,
        "--paths", os.path.join(SERVER_DIR, "src"),
    ]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    # Entry is the package module, not the runpy shim (PyInstaller cannot
    # follow run_module strings).
    cmd.append(os.path.join(SERVER_DIR, "src", "agentic_server", "main.py"))

    print("+", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=SERVER_DIR)
    if rc != 0:
        return rc

    built = os.path.join(dist, args.name)
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    shutil.copytree(built, args.out)
    print(f"bundled backend -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
