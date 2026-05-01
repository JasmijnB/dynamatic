`timescale 1ns/1ps

// Testbench for load_queue.v
// Generated from config.json: NumEntries=4, DataWidth=32, AddrWidth=32, IDWidth=2, IDVal=0

module dependency_checker_tb;

initial begin
  $display("Not implemented yet");
  $finish;
end

// Timeout watchdog
initial begin
    #100_000_00;
    $display("TIMEOUT");
    $finish;
end

endmodule
