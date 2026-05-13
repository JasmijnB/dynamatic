`timescale 1ns/1ps

// Testbench for store_queue.v
// Generated from queue-config.json: NumEntries=4, DataWidth=32, AddrWidth=32, IDWidth=2, IDVal=0

module store_queue_tb;

// ===----------------------------------------------------------------------===
// Parameters (match queue-config.json)
// ===----------------------------------------------------------------------===
localparam int ADDR_W    = 32;
localparam int DATA_W    = 32;
localparam int ID_W      = 2;
localparam int ID_VAL    = 0;
localparam int N_ENTRIES = 4;
localparam int CLK_HALF  = 5; // 10 ns period

// ===----------------------------------------------------------------------===
// DUT signals
// ===----------------------------------------------------------------------===
logic clk;
logic rst;

// Kernel -> queue (store address)
logic [ADDR_W-1:0] port_addr_i;
logic              port_addr_valid_i;
logic              port_addr_ready_o;

// Kernel -> queue (store data)
logic [DATA_W-1:0] port_data_i;
logic              port_data_valid_i;
logic              port_data_ready_o;

// Status
logic empty_o;

// Queue -> AXI (write request)
logic              wreq_valid_o;
logic              wreq_ready_i;
logic [ID_W-1:0]   wreq_id_o;
logic [ADDR_W-1:0] wreq_addr_o;
logic [DATA_W-1:0] wreq_data_o;

// AXI -> queue (write response)
logic              wresp_valid_i;
logic              wresp_ready_o;
logic [ID_W-1:0]   wresp_id_i;

// Dependency checker
logic allow_alloc_i;
logic allow_access_i;

// ===----------------------------------------------------------------------===
// DUT instantiation
// ===----------------------------------------------------------------------===
store_queue dut (
    .clk               (clk),
    .rst               (rst),
    .port_addr_i       (port_addr_i),
    .port_addr_valid_i (port_addr_valid_i),
    .port_addr_ready_o (port_addr_ready_o),
    .port_data_i       (port_data_i),
    .port_data_valid_i (port_data_valid_i),
    .port_data_ready_o (port_data_ready_o),
    .empty_o           (empty_o),
    .wreq_valid_o      (wreq_valid_o),
    .wreq_ready_i      (wreq_ready_i),
    .wreq_id_o         (wreq_id_o),
    .wreq_addr_o       (wreq_addr_o),
    .wreq_data_o       (wreq_data_o),
    .wresp_valid_i     (wresp_valid_i),
    .wresp_ready_o     (wresp_ready_o),
    .wresp_id_i        (wresp_id_i),
    .allow_alloc_i     (allow_alloc_i),
    .allow_access_i     (allow_access_i)
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

// Send a store address; blocks until the queue accepts it.
// The queue buffers addresses independently of data.
task automatic port_send_addr(
    input logic [ADDR_W-1:0] addr
);
    port_addr_i       = addr;
    port_addr_valid_i = 1;
    @(posedge clk iff port_addr_ready_o);
    #1;
    port_addr_valid_i = 0;
endtask

// Present store data and block until the queue forwards it to AXI.
// The write fires when can_issue && wreq_ready_i, so the AXI slave
// must be asserting wreq_ready_i concurrently for this task to complete.
task automatic port_send_data(
    input logic [DATA_W-1:0] data
);
    port_data_i       = data;
    port_data_valid_i = 1;
    @(posedge clk iff port_data_ready_o);
    #1;
    port_data_valid_i = 0;
endtask

// Send a store (addr then data). Addr is buffered immediately; data is
// forwarded to AXI at issue time. The caller must have an AXI slave
// (axi_respond / axi_receive_store) running concurrently, otherwise
// port_send_data will block indefinitely.
task automatic port_send_store(
    input logic [ADDR_W-1:0] addr,
    input logic [DATA_W-1:0] data
);
    port_send_addr(addr);
    port_send_data(data);
endtask

// AXI write-request channel (AW+W): TB is slave.
// Assert ready and block at the posedge where wreq_valid_o is high.
task automatic axi_receive_store(
    output logic [ADDR_W-1:0] addr,
    output logic [DATA_W-1:0] data
);
    wreq_ready_i = 1;
    @(posedge clk iff wreq_valid_o);
    addr = wreq_addr_o;
    data = wreq_data_o;
    #1;
    wreq_ready_i = 0;
endtask

// AXI write-response channel (B): TB is master.
// Send a write response for the given ID.
task automatic axi_send_resp();
    wresp_valid_i = 1;
    wresp_id_i    = ID_VAL;
    @(posedge clk iff wresp_ready_o);
    #1;
    wresp_valid_i = 0;
endtask

// Accept one AXI write request, check addr and data, then send the response.
task automatic axi_respond(
    input logic [ADDR_W-1:0] exp_addr,
    input logic [DATA_W-1:0] exp_data
);
    logic [ADDR_W-1:0] got_addr;
    logic [DATA_W-1:0] got_data;
    axi_receive_store(got_addr, got_data);
    repeat ($urandom_range(0, 5)) tick();
    check(got_addr, exp_addr, $sformatf("T%0d: AXI wreq addr", test_counter));
    check(got_data, exp_data, $sformatf("T%0d: AXI wreq data", test_counter));
    axi_send_resp();
endtask

task automatic reset();
    rst               = 1;
    port_addr_i       = '0;
    port_addr_valid_i = 0;
    port_data_i       = '0;
    port_data_valid_i = 0;
    wreq_ready_i      = 0;
    wresp_valid_i     = 0;
    wresp_id_i        = '0;
    allow_alloc_i     = 1;
    allow_access_i     = 1;
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
    // Data ready is 0: there are no queued addresses to issue, so
    // port_data_ready_o = wreq_ready_i & can_issue = 0.
    // ------------------------------------------------------------------
    test_counter = 1;
    reset();
    check(empty_o,           1, "T1: empty after reset");
    check(port_addr_ready_o, 1, "T1: addr ready after reset");
    check(port_data_ready_o, 0, "T1: data not ready when queue empty");
    check(wreq_valid_o,      0, "T1: no wreq after reset");

    // ------------------------------------------------------------------
    // TEST 2: Single store end-to-end.
    // The address is buffered immediately; data flows through to AXI at
    // issue time. Both sides must run concurrently.
    // ------------------------------------------------------------------
    test_counter = 2;
    reset();
    fork
        port_send_store(32'hDEAD_0001, 32'hCAFE_0001);
        axi_respond(32'hDEAD_0001, 32'hCAFE_0001);
    join
    tick();
    check(empty_o, 1, "T2: empty after store completes");

    // ------------------------------------------------------------------
    // TEST 3: Two back-to-back stores
    // ------------------------------------------------------------------
    test_counter = 3;
    reset();
    fork
        begin
            port_send_store(32'hAAAA_0001, 32'h1111_0001);
            port_send_store(32'hAAAA_0002, 32'h1111_0002);
        end
        begin
            axi_respond(32'hAAAA_0001, 32'h1111_0001);
            axi_respond(32'hAAAA_0002, 32'h1111_0002);
        end
    join
    tick();
    check(empty_o, 1, "T3: empty after two stores complete");

    // ------------------------------------------------------------------
    // TEST 4: allow_alloc_i = 0 blocks address allocation.
    // Data ready is also 0 because can_issue = 0 (queue is empty).
    // ------------------------------------------------------------------
    test_counter = 4;
    reset();
    allow_alloc_i     = 0;
    port_addr_i       = 32'hBEEF_0001;
    port_data_i       = 32'hBEEF_BEEF;
    port_addr_valid_i = 1;
    port_data_valid_i = 1;
    tick();
    check(port_addr_ready_o, 0, "T4: addr blocked when allow_alloc=0");
    check(port_data_ready_o, 0, "T4: data not ready (queue empty, no issueable entry)");
    tick();
    check(wreq_valid_o, 0, "T4: no wreq while blocked");
    port_addr_valid_i = 0;
    port_data_valid_i = 0;
    allow_alloc_i     = 1;

    // ------------------------------------------------------------------
    // TEST 5: AXI write-request back-pressure stalls issue.
    // The address is buffered first (outside the fork), then data is
    // presented concurrently with the AXI side.
    // ------------------------------------------------------------------
    test_counter = 5;
    reset();
    wreq_ready_i = 0;
    // Buffer the address; this completes immediately regardless of wreq_ready.
    port_send_addr(32'hCCCC_0001);
    // Now can_issue=1. Fork: present data / check wreq / release back-pressure.
    fork
        begin
            port_data_i       = 32'hCCCC_CCCC;
            port_data_valid_i = 1;
            @(posedge clk iff port_data_ready_o);
            #1;
            port_data_valid_i = 0;
        end
        begin
            tick();
            check(wreq_valid_o, 1, "T5: wreq asserted under back-pressure");
            tick();
            check(wreq_valid_o, 1, "T5: wreq held while not accepted");
            wreq_ready_i = 1;
            @(posedge clk iff wreq_valid_o);
            #1;
            wreq_ready_i = 0;
            axi_send_resp();
        end
    join
    tick();
    check(empty_o, 1, "T5: empty after un-stall");

    // ------------------------------------------------------------------
    // TEST 6: Fill address buffer to capacity, check full condition.
    // Data is not buffered, so we fill the queue by sending addresses only.
    // ------------------------------------------------------------------
    test_counter = 6;
    reset();
    allow_access_i = 0;
    port_send_addr(32'hF001_0001);
    port_send_addr(32'hF001_0002);
    port_send_addr(32'hF001_0003);
    port_send_addr(32'hF001_0004);
    tick();
    check(port_addr_ready_o, 0, "T6: addr not ready when full");
    check(port_data_ready_o, 0, "T6: data not ready when full (wreq_ready=0)");
    check(empty_o,           0, "T6: not empty when full");
    allow_access_i = 1;
    // Drain: data must flow concurrently with the AXI slave.
    fork
        begin
            port_send_data(32'hF001_F001);
            port_send_data(32'hF001_F002);
            port_send_data(32'hF001_F003);
            port_send_data(32'hF001_F004);
        end
        begin
            axi_respond(32'hF001_0001, 32'hF001_F001);
            axi_respond(32'hF001_0002, 32'hF001_F002);
            axi_respond(32'hF001_0003, 32'hF001_F003);
            axi_respond(32'hF001_0004, 32'hF001_F004);
        end
    join

    // ------------------------------------------------------------------
    // TEST 7: allow_access_i = 0 blocks issue
    // ------------------------------------------------------------------
    test_counter = 7;
    reset();
    allow_access_i = 0;
    fork
        port_send_store(32'hEEEE_0001, 32'hEEEE_EEEE);
        begin
            tick();
            check(wreq_valid_o, 0, "T7: no wreq when allow_store=0");
            allow_access_i = 1;
            tick();
            check(wreq_valid_o, 1, "T7: wreq fires after allow_store=1");
            axi_respond(32'hEEEE_0001, 32'hEEEE_EEEE);
        end
    join

    // ------------------------------------------------------------------
    // TEST 8: Pre-queue addresses, then drain with data and AXI.
    // This tests the key difference from a fully-buffered design: addresses
    // can be queued ahead of data, and data flows through in FIFO order.
    // ------------------------------------------------------------------
    test_counter = 8;
    reset();
    // Queue up addresses before any data arrives.
    port_send_addr(32'hAAAA_0001);
    port_send_addr(32'hAAAA_0002);
    port_send_addr(32'hAAAA_0003);
    // Drain: data flows through at issue time, paired in FIFO order.
    fork
        begin
            port_send_data(32'hDDDD_0001);
            port_send_data(32'hDDDD_0002);
            port_send_data(32'hDDDD_0003);
        end
        begin
            axi_respond(32'hAAAA_0001, 32'hDDDD_0001);
            axi_respond(32'hAAAA_0002, 32'hDDDD_0002);
            axi_respond(32'hAAAA_0003, 32'hDDDD_0003);
        end
    join
    tick();
    check(empty_o, 1, "T8: empty after stores complete");

    // ------------------------------------------------------------------
    // TEST 9: Stress — N stores with random pauses on all channels.
    // Addresses and data arrive independently with random delays.
    // FIFO ordering ensures data[i] is always consumed for address[i].
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
            // --- Address Sender ---
            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0, 5)) tick();
                    port_send_addr(addrs[i]);
                end
                $display("T9: Address Sender done");
            end

            // --- Data Sender: each data[i] is held until consumed at issue time ---
            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0, 5)) tick();
                    port_send_data(datas[i]);
                end
                $display("T9: Data Sender done");
            end

            // --- AXI memory model ---
            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0, 5)) tick();
                    axi_respond(addrs[i], datas[i]);
                end
                $display("T9: AXI model done");
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
