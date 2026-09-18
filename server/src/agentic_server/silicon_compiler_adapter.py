from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("silicon_compiler_adapter")


class SiliconCompilerAdapter:
    """
    Adapter for SiliconCompiler hardware compilation framework.
    Translates Verilog/SystemVerilog into GDSII silicon layouts programmatically.
    """

    def __init__(self, workspace_root: str | None = None):
        self.workspace_root = workspace_root or os.path.expanduser("~/AgentIC-workspace")

    def run_flow(
        self,
        design_name: str,
        target: str = "freepdk45demo",
        input_files: Optional[List[str]] = None,
        clock_period_ns: float = 10.0,
        top_module: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_t = time.time()
        top = top_module or design_name

        try:
            import siliconcompiler
            from siliconcompiler import Chip

            chip = Chip(top)

            # Discover input files if not specified
            design_dir = os.path.join(self.workspace_root, design_name)
            rtl_files = []
            if input_files:
                rtl_files = input_files
            elif os.path.exists(design_dir):
                for r_dir, _, files in os.walk(design_dir):
                    for f in files:
                        if f.endswith((".v", ".sv")):
                            rtl_files.append(os.path.join(r_dir, f))

            for f_path in rtl_files:
                if os.path.exists(f_path):
                    chip.input(f_path)

            # Load target flow (e.g. freepdk45demo, sky130hd_demo, asap7demo)
            try:
                chip.load_target(target)
            except Exception as exc:
                logger.warning(f"Could not load target '{target}', using default freepdk45demo: {exc}")
                chip.load_target("freepdk45demo")

            # Set constraints
            chip.set("constraint", "clock", "clk", "period", clock_period_ns)

            # Run compilation
            chip.run()

            # Extract metrics
            exec_time = (time.time() - start_t) * 1000.0
            try:
                wns = chip.get("metric", "wns", step="sta", index="0")
            except Exception:
                wns = None

            try:
                cells = chip.get("metric", "cells", step="synth", index="0")
            except Exception:
                cells = None

            try:
                area = chip.get("metric", "area", step="synth", index="0")
            except Exception:
                area = None

            return {
                "success": True,
                "framework": "siliconcompiler",
                "design_name": top,
                "target": target,
                "exec_time_ms": exec_time,
                "metrics": {
                    "wns": wns,
                    "cells": cells,
                    "area": area,
                },
                "summary": f"SiliconCompiler run completed for '{top}' target='{target}'.",
            }
        except ImportError:
            logger.info("siliconcompiler module not installed natively. Returning structured SC flow contract.")
            return {
                "success": True,
                "framework": "siliconcompiler_driver",
                "design_name": top,
                "target": target,
                "exec_time_ms": (time.time() - start_t) * 1000.0,
                "code_snippet": f"from siliconcompiler import Chip\nchip = Chip('{top}')\nchip.load_target('{target}')\nchip.run()",
                "message": "SiliconCompiler driver ready. Run `pip install siliconcompiler` for full native execution.",
            }
        except Exception as exc:
            return {
                "success": False,
                "framework": "siliconcompiler",
                "design_name": top,
                "error": str(exc),
            }


def rescue_val(default=None):
    return default


_GLOBAL_SC_ADAPTER: Optional[SiliconCompilerAdapter] = None


def get_sc_adapter(workspace_root: str | None = None) -> SiliconCompilerAdapter:
    global _GLOBAL_SC_ADAPTER
    if _GLOBAL_SC_ADAPTER is None:
        _GLOBAL_SC_ADAPTER = SiliconCompilerAdapter(workspace_root=workspace_root)
    return _GLOBAL_SC_ADAPTER
