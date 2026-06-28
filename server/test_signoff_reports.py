import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from signoff_reports import build_signoff_report


class SignoffReportsTest(unittest.TestCase):
    def test_reads_drc_and_lvs_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "signoff"
            reports.mkdir()
            (reports / "magic.drc.rpt").write_text("Total DRC errors found: 2\nmetal1 spacing: 2 violations\n", encoding="utf-8")
            (reports / "netgen.lvs.log").write_text("Netlists match uniquely.\n", encoding="utf-8")

            report = build_signoff_report(str(root), "demo")

            self.assertEqual(report["status"], "violating")
            self.assertEqual(report["drc"]["metrics"]["violation_count"], 2)
            self.assertEqual(report["drc"]["source"]["type"], "file")
            self.assertEqual(report["lvs"]["status"], "clean")

    def test_prefers_checkpoint_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".agentic"
            state.mkdir()
            (state / "demo_checkpoints.json").write_text(
                json.dumps(
                    [
                        {
                            "stage": "drc",
                            "tool": "magic",
                            "parsed_report": {
                                "schema_version": "agentic.report_parsers.v1",
                                "kind": "drc",
                                "tool": "magic",
                                "summary": {"diagnostic_count": 0, "error_count": 0, "warning_count": 0},
                                "metrics": {"violation_count": 0},
                                "diagnostics": [],
                            },
                        },
                        {
                            "stage": "lvs",
                            "tool": "netgen",
                            "parsed_report": {
                                "schema_version": "agentic.report_parsers.v1",
                                "kind": "lvs",
                                "tool": "netgen",
                                "summary": {"diagnostic_count": 1, "error_count": 1, "warning_count": 0},
                                "metrics": {"status": "fail", "matched": False, "mismatch_count": 1},
                                "diagnostics": [{"severity": "error", "message": "Missing net reset_n"}],
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )

            report = build_signoff_report(str(root), "demo")

            self.assertEqual(report["status"], "violating")
            self.assertEqual(report["drc"]["status"], "clean")
            self.assertEqual(report["lvs"]["diagnostics"][0]["message"], "Missing net reset_n")

    def test_missing_without_signoff_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_signoff_report(tmp, "demo")

            self.assertEqual(report["status"], "missing")
            self.assertEqual(report["drc"]["status"], "missing")
            self.assertEqual(report["lvs"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
