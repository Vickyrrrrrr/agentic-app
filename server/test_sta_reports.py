import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from sta_reports import build_sta_report


class StaReportsTest(unittest.TestCase):
    def test_prefers_latest_sta_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".agentic"
            state.mkdir()
            (state / "demo_checkpoints.json").write_text(
                json.dumps(
                    [
                        {"stage": "simulation", "tool": "verilator", "metrics": {}},
                        {
                            "stage": "sta",
                            "tool": "opensta",
                            "exit_code": 0,
                            "parsed_report": {
                                "schema_version": "agentic.report_parsers.v1",
                                "kind": "sta",
                                "tool": "opensta",
                                "summary": {"diagnostic_count": 1, "error_count": 1, "warning_count": 0},
                                "metrics": {"wns_ns": -0.12, "tns_ns": -2.4},
                                "diagnostics": [
                                    {
                                        "severity": "error",
                                        "startpoint": "u_a/Q",
                                        "endpoint": "u_b/D",
                                        "path_group": "clk",
                                        "slack_ns": -0.12,
                                    }
                                ],
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )

            report = build_sta_report(str(root), "demo")

            self.assertEqual(report["status"], "violating")
            self.assertEqual(report["source"]["type"], "checkpoint")
            self.assertEqual(report["wns"], -0.12)
            self.assertEqual(report["paths"][0]["startpoint"], "u_a/Q")

    def test_reads_sta_report_file_when_checkpoint_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir()
            (reports / "top_timing.rpt").write_text(
                """
wns 0.03
tns 0.00
Startpoint: src_reg/Q
Endpoint: dst_reg/D
Path Group: clk
slack (MET) 0.03
""",
                encoding="utf-8",
            )

            report = build_sta_report(str(root), "demo")

            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["source"]["type"], "file")
            self.assertEqual(report["wns"], 0.03)
            self.assertEqual(report["paths"][0]["endpoint"], "dst_reg/D")

    def test_missing_when_no_timing_evidence_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_sta_report(tmp, "demo")

            self.assertEqual(report["status"], "missing")
            self.assertIsNone(report["wns"])
            self.assertEqual(report["paths"], [])


if __name__ == "__main__":
    unittest.main()
