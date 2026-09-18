"""Goldens for parsers, RTL gates, flow routing. Hermetic: no model, network,
tools, or PDK scan. Fixtures probed live before pinning."""
from agentic_server.flow_runtime import recommend_flow
from agentic_server.pdk_index import select_pdk
from agentic_server.report_parsers import parse_report
from agentic_server.rtl_quality import evaluate_rtl_quality
from agentic_server.tool_adapters import capability_matrix

GOOD_UART = """module uart_tx (
  input  wire       clk,
  input  wire       rst_n,
  input  wire       start,
  input  wire [7:0] data_in,
  output reg        tx,
  output reg        busy
);
  reg [3:0] bit_cnt;
  reg [9:0] shift_reg;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tx <= 1'b1;
      busy <= 1'b0;
      bit_cnt <= 4'd0;
      shift_reg <= 10'd0;
    end else begin
      case (bit_cnt)
        4'd0: begin
          if (start) begin
            shift_reg <= {1'b1, data_in, 1'b0};
            busy <= 1'b1;
            bit_cnt <= 4'd1;
          end
        end
        default: begin
          tx <= shift_reg[0];
          shift_reg <= {1'b1, shift_reg[9:1]};
          if (bit_cnt == 4'd10) begin
            bit_cnt <= 4'd0;
            busy <= 1'b0;
          end else begin
            bit_cnt <= bit_cnt + 4'd1;
          end
        end
      endcase
    end
  end
endmodule
"""

PLACEHOLDER_STUB = (
    "module stub (input clk); // TODO: implement the thing\n// FIXME placeholder\nendmodule\n"
)


def test_good_rtl_accepted():
    result = evaluate_rtl_quality("rtl/uart_tx.v", GOOD_UART)
    assert result.accepted is True
    assert result.quality_level == "implementation"
    assert result.module_name == "uart_tx"
    assert all(i.severity != "error" for i in result.issues)


def test_placeholder_rtl_rejected():
    result = evaluate_rtl_quality("rtl/stub.v", PLACEHOLDER_STUB)
    assert result.accepted is False
    assert result.quality_level == "rejected"
    assert any(i.code == "placeholder_rtl" and i.severity == "error" for i in result.issues)


def test_sta_golden_violated():
    text = (
        "Worst slack: -0.420\n"
        "Startpoint: u_core/reg_a\n"
        "Endpoint: u_core/reg_b\n"
        "slack (VIOLATED) -0.420"
    )
    res = parse_report(stage="sta", tool="opensta", text=text)
    assert res["kind"] == "sta"
    assert res["metrics"]["wns_ns"] == -0.42
    assert res["summary"]["error_count"] == 1
    assert res["diagnostics"][0]["severity"] == "error"


def test_sta_golden_met():
    text = "Worst slack: 0.150\nslack (MET) 0.150"
    res = parse_report(stage="sta", tool="opensta", text=text)
    assert res["kind"] == "sta"
    assert res["metrics"]["wns_ns"] == 0.15
    assert res["summary"]["error_count"] == 0


def test_drc_golden():
    text = "Total DRC errors found: 3\nmet1_spacing : 2 violations"
    res = parse_report(stage="drc", tool="magic", text=text)
    assert res["kind"] == "drc"
    assert res["metrics"]["violation_count"] == 3
    assert res["summary"]["error_count"] >= 1


def test_lint_dispatch_and_error_fields():
    text = "%Error-WIDTH: rtl/uart_tx.v:10:5: signal width mismatch"
    res = parse_report(stage="lint", tool="verilator", text=text)
    assert res["summary"]["error_count"] >= 1
    diag = res["diagnostics"][0]
    assert diag["line"] == 10
    assert "uart_tx.v" in str(diag.get("file", ""))


def test_lvs_and_generic_dispatch():
    assert parse_report(stage="lvs", tool="netgen", text="x")["kind"] == "lvs"
    generic = parse_report(stage="mystery", tool="weird", text="a\nb")
    assert generic["kind"] == "generic"
    assert generic["metrics"]["line_count"] == 2


def test_recommend_flow_empty_env_needs_setup():
    # An explicit empty tools dict is authoritative (no host probing), so
    # this holds on every machine — Windows, bare Linux, or fully loaded.
    env = {"pdk_index": {"pdks": []}, "flows": {}, "tools": {}, "capabilities": {}}
    res = recommend_flow(env)
    assert isinstance(res, dict)
    assert res["profile"] == "setup_required"
    assert "backend" in res and "confidence" in res


def test_explicit_tools_dict_is_authoritative():
    # Empty dict must mean an empty world even where real tools exist.
    matrix = capability_matrix({})
    assert isinstance(matrix, dict)
    assert all(not stage["available"] for stage in matrix.values())


def test_recommend_flow_adapts_to_real_host():
    # Auto-detect (tools=None probes the host) sees this machine truthfully:
    # a box with verilator+yosys offers synthesis, a bare box needs setup.
    import shutil

    probed = capability_matrix(None)
    res = recommend_flow(
        {
            "pdk_index": {"pdks": []},
            "flows": {},
            "tools": {},
            "capabilities": {},
            "tool_adapters": probed,
        }
    )
    has_sim_or_synth = bool(shutil.which("verilator") or shutil.which("yosys"))
    if has_sim_or_synth:
        assert res["profile"] == "rtl_to_synthesis_until_pnr_ready"
    else:
        assert res["profile"] == "setup_required"


def test_capability_matrix_and_pdk_select_empty():
    assert isinstance(capability_matrix({}), dict)
    assert select_pdk({"pdks": []}, "") is None


def test_index_pdk_dir_empty_dir_shape(tmp_path):
    from agentic_server.pdk_index import index_pdk_dir

    idx = index_pdk_dir(tmp_path)
    assert idx["path"] == str(tmp_path)
    for key in ("class", "family", "readiness", "libraries"):
        assert key in idx


def test_dispatch_unknown_tool_is_clean_error(tmp_path):
    from agentic_server.agent_tools import dispatch_tool, workspace_tool

    assert dispatch_tool("nope_nonexistent", {}, str(tmp_path)).startswith(
        "Error: unknown tool"
    )
    assert workspace_tool("list", str(tmp_path), ".", "*.v", "").startswith(
        "No files matching"
    )


def test_parse_module_signature_and_ports(tmp_path):
    # Regression: workspace_tool passed (path, root, module) while the
    # definition took 2 args -> every parse_module call raised TypeError.
    import json

    from agentic_server.agent_tools import parse_module_tool, workspace_tool

    rtl = tmp_path / "rtl"
    rtl.mkdir()
    (rtl / "counter.v").write_text(GOOD_UART.replace("uart_tx", "counter"), encoding="utf-8")
    parsed = json.loads(parse_module_tool("rtl/counter.v", str(tmp_path), "counter"))
    assert parsed["requested_module"] == "counter"
    names = {p["name"] for p in parsed["ports"]}
    assert {"clk", "rst_n", "tx", "busy"} <= names
    via_dispatch = json.loads(
        workspace_tool("parse_module", str(tmp_path), "rtl/counter.v", "", "counter")
    )
    assert via_dispatch["requested_module"] == "counter"


def test_engine_logger_defined():
    import logging

    from agentic_server import main

    assert isinstance(main.logger, logging.Logger)
