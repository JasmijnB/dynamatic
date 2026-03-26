// Common testbench utilities.
// Include inside a module body with: `include "../utils.sv"
// Requires: logic clk (declared in the enclosing module)

int fail_count   = 0;
int test_counter = 0;

task automatic check(
    input logic [63:0] got,
    input logic [63:0] expected,
    input string       label
);
    if (got !== expected) begin
        $display("FAIL [%s]: got 0x%0h, expected 0x%0h", label, got, expected);
        fail_count++;
    end else begin
        $display("PASS [%s]", label);
    end
endtask

// Advance one clock cycle, settle 1 ns after the posedge.
task automatic tick; @(posedge clk); #1; endtask