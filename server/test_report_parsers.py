import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from report_parsers import parse_drc_report, parse_lint_report, parse_lvs_report, parse_sta_report


class ReportParsersTest(unittest.TestCase):
    def test_verilator_lint_diagnostics_include_source_location(self):
        report = parse_lint_report(
            "%Warning-WIDTH: rtl/alu.sv:42:13: Operator ADD expects 8 bits\n"
            "%Error: rtl/top.sv:9:1: syntax error\n",
            tool="verilator",
        )

        self.assertEqual(report["kind"], "lint")
        self.assertEqual(report["summary"]["warning_count"], 1)
        self.assertEqual(report["summary"]["error_count"], 1)
        self.assertEqual(report["diagnostics"][0]["file"], "rtl/alu.sv")
        self.assertEqual(report["diagnostics"][0]["line"], 42)
        self.assertEqual(report["diagnostics"][0]["code"], "WIDTH")

    def test_sta_report_extracts_negative_slack_path(self):
        report = parse_sta_report(
            """
wns -0.05
tns -1.20
Startpoint: reg_a/Q
Endpoint: reg_b/D
Path Group: clk
slack (VIOLATED) -0.05
""",
            tool="opensta",
        )

        self.assertEqual(report["metrics"]["wns_ns"], -0.05)
        self.assertEqual(report["metrics"]["tns_ns"], -1.2)
        self.assertEqual(report["diagnostics"][0]["startpoint"], "reg_a/Q")
        self.assertEqual(report["diagnostics"][0]["endpoint"], "reg_b/D")
        self.assertEqual(report["diagnostics"][0]["severity"], "error")

    def test_magic_drc_report_extracts_total_and_rule(self):
        report = parse_drc_report(
            """
Total DRC errors found: 3
metal1 spacing: 2 violations
  - metal1 spacing (metal1.2): 2 violations
    [10.5um, 20.0um] to [11.5um, 21.0um]
poly width: 1 violation
""",
            tool="magic",
        )

        self.assertEqual(report["metrics"]["violation_count"], 3)
        self.assertEqual(report["summary"]["error_count"], 2)
        self.assertEqual(report["diagnostics"][0]["rule"], "metal1 spacing")
        self.assertEqual(report["diagnostics"][0]["bbox"], [10.5, 20.0, 11.5, 21.0])

    def test_lvs_report_detects_mismatch(self):
        report = parse_lvs_report(
            """
Netlists do not match.
Property mismatch on device X1.
Missing net reset_n.
""",
            tool="netgen",
        )

        self.assertEqual(report["metrics"]["status"], "fail")
        self.assertFalse(report["metrics"]["matched"])
        self.assertEqual(report["summary"]["error_count"], 3)


if __name__ == "__main__":
    unittest.main()
