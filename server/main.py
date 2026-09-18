"""Backward-compatible entry point — delegates to the agentic_server package.

Kept so existing launchers keep working unchanged:
  - server/run.sh (`python3 main.py`)
  - desktop findServerDir (searches for main.py)
  - `python main.py --bridge` (sidecar bridge mode)

New code should `pip install -e .` and `import agentic_server` instead.
"""
import os
import runpy
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if __name__ == "__main__":
    runpy.run_module("agentic_server.main", run_name="__main__", alter_sys=True)
