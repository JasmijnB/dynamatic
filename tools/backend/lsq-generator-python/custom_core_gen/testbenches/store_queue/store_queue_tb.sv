`timescale 1ns/1ps

// Testbench for store_queue.v
// Generated from queue-config.json: NumEntries=4, DataWidth=32, AddrWidth=32

module store_queue_tb;

// ===----------------------------------------------------------------------===
// Parameters (match queue-config.json)
// ===----------------------------------------------------------------------===
localparam int ADDR_W    = 32;
localparam int DATA_W    = 32;
localparam int N_ENTRIES = 4;
localparam int CLK_HALF  = 5; // 10 ns period

// ===----------------------------------------------------------------------===
// DUT signals
// ===----------------------------------------------------------------------===
logic clk;
logic rst;

// Kernel -> queue (store address)
logic [ADDR_W-1:0] circ_addr_i;
logic              circ_addr_valid_i;
logic              circ_addr_ready_o;

// Kernel -> queue (store data — forwarded directly to memory)
logic [DATA_W-1:0] circ_data_i;
logic              circ_data_valid_i;
logic              circ_data_ready_o;

// Status
logic empty_o;

// Queue -> memory (address + data request)
logic              mem_addr_valid_o;
logic              mem_addr_ready_i;
logic [ADDR_W-1:0] mem_addr_o;
logic [DATA_W-1:0] mem_data_o;
logic              mem_data_valid_o;
logic              mem_data_ready_i;

// Memory -> queue (write execution response)
logic              mem_exec_valid_i;
logic              mem_exec_ready_o;

// Dependency checker
logic allow_alloc_i;
logic allow_access_i;

// ===----------------------------------------------------------------------===
// DUT instantiation
// ===----------------------------------------------------------------------===
store_queue dut (
    .clk                   (clk),
    .rst                   (rst),
    .circ_addr_i        (circ_addr_i),
    .circ_addr_valid_i  (circ_addr_valid_i),
    .circ_addr_ready_o  (circ_addr_ready_o),
    .circ_data_i           (circ_data_i),
    .circ_data_valid_i     (circ_data_valid_i),
    .circ_data_ready_o     (circ_data_ready_o),
    .empty_o               (empty_o),
    .mem_addr_valid_o (mem_addr_valid_o),
    .mem_addr_ready_i (mem_addr_ready_i),
    .mem_addr_o       (mem_addr_o),
    .mem_data_o       (mem_data_o),
    .mem_data_valid_o (mem_data_valid_o),
    .mem_data_ready_i (mem_data_ready_i),
    .mem_exec_valid_i (mem_exec_valid_i),
    .mem_exec_ready_o (mem_exec_ready_o),
    .allow_alloc_i         (allow_alloc_i),
    .allow_access_i        (allow_access_i)
);

// ===----------------------------------------------------------------------===
// Clock
// ===----------------------------------------------------------------------===
initial clk = 0;
always #CLK_HALF clk = ~clk;

// ===----------------------------------------------------------------------===
// Helpers
// ===----------------------------------------------------------------------===
`include "../utils.sv"

// Send a store address into the queue's buffer; blocks until accepted.
task automatic port_send_addr(input logic [ADDR_W-1:0] addr);
    circ_addr_i       = addr;
    circ_addr_valid_i = 1;
    @(posedge clk iff circ_addr_ready_o);
    #1;
    circ_addr_valid_i = 0;
endtask

// Accept one store request from the queue and send the execution response.
// Drives circ_data_i simultaneously so data is valid when memory samples it.
// With st_resp=false, mem_exec_ready_o is always 1.
task automatic mem_respond(
    input logic [ADDR_W-1:0] exp_addr,
    input logic [DATA_W-1:0] exp_data
);
    logic [ADDR_W-1:0] got_addr;
    logic [DATA_W-1:0] got_data;
    circ_data_i       = exp_data;
    circ_data_valid_i = 1;
    mem_data_ready_i  = 1;
    mem_addr_ready_i  = 1;
    @(posedge clk iff mem_addr_valid_o);
    got_addr = mem_addr_o;
    got_data = mem_data_o;
    #1;
    circ_data_valid_i = 0;
    mem_data_ready_i  = 0;
    mem_addr_ready_i  = 0;
    repeat ($urandom_range(0, 5)) tick();
    check(got_addr, exp_addr, $sformatf("T%0d: store addr", test_counter));
    check(got_data, exp_data, $sformatf("T%0d: store data", test_counter));
    mem_exec_valid_i = 1;
    @(posedge clk iff mem_exec_ready_o);
    #1;
    mem_exec_valid_i = 0;
endtask

task automatic reset();
    rst                   = 1;
    circ_addr_i        = '0;
    circ_addr_valid_i  = 0;
    circ_data_i           = '0;
    circ_data_valid_i     = 0;
    mem_addr_ready_i  = 0;
    mem_data_ready_i  = 0;
    mem_exec_valid_i  = 0;
    allow_alloc_i         = 1;
    allow_access_i        = 1;
    repeat (3) tick();
    rst = 0;
    tick();
endtask

// ===----------------------------------------------------------------------===
// Tests
// ===----------------------------------------------------------------------===

initial begin
    $dumpfile("store_queue_tb.vcd");
    $dumpvars(0, store_queue_tb);

    // ------------------------------------------------------------------
    // TEST 1: After reset the queue is empty and ready to accept addresses.
    // circ_data_ready_o = mem_addr_ready_i & can_issue = 0 after reset.
    // ------------------------------------------------------------------
    test_counter = 1;
    reset();
    check(empty_o,               1, "T1: empty after reset");
    check(circ_addr_ready_o,  1, "T1: addr ready after reset");
    check(circ_data_ready_o,     0, "T1: data not ready when queue empty");
    check(mem_addr_valid_o, 0, "T1: no request after reset");

    // ------------------------------------------------------------------
    // TEST 2: Single store end-to-end.
    // ------------------------------------------------------------------
    test_counter = 2;
    reset();
    fork
        port_send_addr(32'hDEAD_0001);
        mem_respond(32'hDEAD_0001, 32'hCAFE_0001);
    join
    tick();
    check(empty_o, 1, "T2: empty after store completes");

    // ------------------------------------------------------------------
    // TEST 3: Two back-to-back stores — addr sender pre-queues both.
    // ------------------------------------------------------------------
    test_counter = 3;
    reset();
    fork
        begin
            port_send_addr(32'hAAAA_0001);
            port_send_addr(32'hAAAA_0002);
        end
        begin
            mem_respond(32'hAAAA_0001, 32'h1111_0001);
            mem_respond(32'hAAAA_0002, 32'h1111_0002);
        end
    join
    tick();
    check(empty_o, 1, "T3: empty after two stores complete");

    // ------------------------------------------------------------------
    // TEST 4: allow_alloc_i = 0 blocks address allocation.
    // ------------------------------------------------------------------
    test_counter = 4;
    reset();
    allow_alloc_i        = 0;
    circ_addr_i       = 32'hBEEF_0001;
    circ_data_i          = 32'hBEEF_BEEF;
    circ_addr_valid_i = 1;
    circ_data_valid_i    = 1;
    tick();
    check(circ_addr_ready_o,  0, "T4: addr blocked when allow_alloc=0");
    check(circ_data_ready_o,     0, "T4: data not ready (queue empty, no issueable entry)");
    tick();
    check(mem_addr_valid_o, 0, "T4: no request while blocked");
    circ_addr_valid_i = 0;
    circ_data_valid_i    = 0;
    allow_alloc_i        = 1;

    // ------------------------------------------------------------------
    // TEST 5: Back-pressure on memory request stalls issue.
    // ------------------------------------------------------------------
    test_counter = 5;
    reset();
    mem_addr_ready_i = 0;
    port_send_addr(32'hCCCC_0001);
    tick();
    check(mem_addr_valid_o, 1, "T5: request asserted under back-pressure");
    tick();
    check(mem_addr_valid_o, 1, "T5: request held while not accepted");
    mem_respond(32'hCCCC_0001, 32'hCCCC_CCCC);
    tick();
    check(empty_o, 1, "T5: empty after un-stall");

    // ------------------------------------------------------------------
    // TEST 6: Fill address buffer to capacity, check full condition.
    // ------------------------------------------------------------------
    test_counter = 6;
    reset();
    allow_access_i = 0;
    port_send_addr(32'hF001_0001);
    port_send_addr(32'hF001_0002);
    port_send_addr(32'hF001_0003);
    port_send_addr(32'hF001_0004);
    tick();
    check(circ_addr_ready_o, 0, "T6: addr not ready when full");
    check(circ_data_ready_o,    0, "T6: data not ready when full (mem_addr_ready=0)");
    check(empty_o,              0, "T6: not empty when full");
    allow_access_i = 1;
    begin
        mem_respond(32'hF001_0001, 32'hF001_F001);
        mem_respond(32'hF001_0002, 32'hF001_F002);
        mem_respond(32'hF001_0003, 32'hF001_F003);
        mem_respond(32'hF001_0004, 32'hF001_F004);
    end

    // ------------------------------------------------------------------
    // TEST 7: allow_access_i = 0 blocks issue
    // ------------------------------------------------------------------
    test_counter = 7;
    reset();
    allow_access_i = 0;
    fork
        port_send_addr(32'hEEEE_0001);
        begin
            tick();
            check(mem_addr_valid_o, 0, "T7: no request when allow_access=0");
            allow_access_i = 1;
            tick();
            check(mem_addr_valid_o, 1, "T7: request fires after allow_access=1");
            mem_respond(32'hEEEE_0001, 32'hEEEE_EEEE);
        end
    join

    // ------------------------------------------------------------------
    // TEST 8: Pre-queue addresses, then drain with memory model.
    // ------------------------------------------------------------------
    test_counter = 8;
    reset();
    port_send_addr(32'hAAAA_0001);
    port_send_addr(32'hAAAA_0002);
    port_send_addr(32'hAAAA_0003);
    begin
        mem_respond(32'hAAAA_0001, 32'hDDDD_0001);
        mem_respond(32'hAAAA_0002, 32'hDDDD_0002);
        mem_respond(32'hAAAA_0003, 32'hDDDD_0003);
    end
    tick();
    check(empty_o, 1, "T8: empty after stores complete");

    // ------------------------------------------------------------------
    // TEST 9: Stress — N stores, addr sender runs independently of memory.
    // mem_respond drives data in-band so data is always valid at issue time.
    // ------------------------------------------------------------------
    test_counter = 9;
    reset();
    begin
        localparam int N = 100;
        logic [ADDR_W-1:0] addrs [N];
        logic [DATA_W-1:0] datas [N];

        for (int i = 0; i < N; i++) begin
            addrs[i] = 32'hADD0_0000 + i;
            datas[i] = 32'hDA1A_0000 + i;
        end

        fork
            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0, 5)) tick();
                    port_send_addr(addrs[i]);
                end
                $display("T9: Address Sender done");
            end

            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0, 5)) tick();
                    mem_respond(addrs[i], datas[i]);
                end
                $display("T9: Memory model done");
            end
        join

        tick();
        check(empty_o, 1, "T9: empty after stress test");
        $display("T9: All %0d stores verified", N);
    end

    // ------------------------------------------------------------------
    // Summary
    // ------------------------------------------------------------------
    $display("\n%0d test(s) failed.", fail_count);
    $finish;
end

// Timeout watchdog
initial begin
    #10_000_000;
    $display("TIMEOUT");
    $finish;
end

endmodule
