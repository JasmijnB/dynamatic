`timescale 1ns/1ps

// Testbench for load_queue.v
// Generated from config.json: NumEntries=4, DataWidth=32, AddrWidth=32

module load_queue_tb;

// ===----------------------------------------------------------------------===
// Parameters (match config.json)
// ===----------------------------------------------------------------------===
localparam int ADDR_W    = 32;
localparam int DATA_W    = 32;
localparam int N_ENTRIES = 4;
localparam int CLK_HALF  = 5; // 10 ns period

// ===----------------------------------------------------------------------===
// DUT signals
// ===----------------------------------------------------------------------===
logic                 clk;
logic                 rst;

// Kernel -> queue (load address)
logic [ADDR_W-1:0]    circ_addr_i;
logic                 circ_addr_valid_i;
logic                 circ_addr_ready_o;

// Queue -> kernel (load data)
logic [DATA_W-1:0]    circ_data_o;
logic                 circ_data_valid_o;
logic                 circ_data_ready_i;

// Status
logic                 empty_o;

// Queue -> memory (address request)
logic                 mem_addr_valid_o;
logic                 mem_addr_ready_i;
logic [ADDR_W-1:0]    mem_addr_o;

// Memory -> queue (data response)
logic                 mem_data_valid_i;
logic                 mem_data_ready_o;
logic [DATA_W-1:0]    mem_data_i;

// Dependency checker
logic                 allow_alloc_i;
logic                 allow_access_i;

// ===----------------------------------------------------------------------===
// DUT instantiation
// ===----------------------------------------------------------------------===
load_queue dut (
    .clk                   (clk),
    .rst                   (rst),
    .circ_addr_i        (circ_addr_i),
    .circ_addr_valid_i  (circ_addr_valid_i),
    .circ_addr_ready_o  (circ_addr_ready_o),
    .circ_data_o           (circ_data_o),
    .circ_data_valid_o     (circ_data_valid_o),
    .circ_data_ready_i     (circ_data_ready_i),
    .empty_o               (empty_o),
    .mem_addr_valid_o (mem_addr_valid_o),
    .mem_addr_ready_i (mem_addr_ready_i),
    .mem_addr_o       (mem_addr_o),
    .mem_data_valid_i  (mem_data_valid_i),
    .mem_data_ready_o  (mem_data_ready_o),
    .mem_data_i        (mem_data_i),
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

// Drive a load-address handshake into the queue.
task automatic port_send_addr(input logic [ADDR_W-1:0] addr);
    circ_addr_i       = addr;
    circ_addr_valid_i = 1;
    @(posedge clk iff circ_addr_ready_o);
    #1;
    circ_addr_valid_i = 0;
endtask

// Assert circ_data_ready_i and capture the next data word from the queue.
task automatic port_recv_data(output logic [DATA_W-1:0] data);
    circ_data_ready_i = 1;
    @(posedge clk iff circ_data_valid_o);
    data = circ_data_o;
    #1;
    circ_data_ready_i = 0;
endtask

// Accept one outgoing address request from the queue (TB acts as memory).
task automatic mem_receive_addr(output logic [ADDR_W-1:0] addr);
    mem_addr_ready_i = 1;
    @(posedge clk iff mem_addr_valid_o);
    addr = mem_addr_o;
    #1;
    mem_addr_ready_i = 0;
endtask

// Send a data response back into the queue (TB acts as memory).
// mem_data_ready_o is a direct passthrough of circ_data_ready_i,
// so the kernel must assert circ_data_ready_i concurrently.
task automatic mem_send_data(input logic [DATA_W-1:0] data);
    mem_data_valid_i = 1;
    mem_data_i       = data;
    @(posedge clk iff mem_data_ready_o);
    #1;
    mem_data_valid_i = 0;
endtask

// Accept one address request, check it, then send back data.
task automatic mem_respond(
    input logic [ADDR_W-1:0] exp_addr,
    input logic [DATA_W-1:0] data
);
    logic [ADDR_W-1:0] got_addr;
    mem_receive_addr(got_addr);
    check(got_addr, exp_addr, $sformatf("T%0d: mem req addr", test_counter));
    mem_send_data(data);
endtask

task automatic reset();
    rst                   = 1;
    circ_addr_valid_i  = 0;
    circ_addr_i        = '0;
    circ_data_ready_i     = 0;
    mem_addr_ready_i = 0;
    mem_data_valid_i  = 0;
    mem_data_i        = '0;
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
    $dumpfile("load_queue_tb.vcd");
    $dumpvars(0, load_queue_tb);

    // ------------------------------------------------------------------
    // TEST 1: After reset the queue is empty and ready to accept
    // ------------------------------------------------------------------
    test_counter = 1;
    reset();
    check(empty_o,               1, "T1: empty after reset");
    check(circ_addr_ready_o,  1, "T1: addr ready after reset");
    check(mem_addr_valid_o, 0, "T1: no request after reset");

    // ------------------------------------------------------------------
    // TEST 2: Single load end-to-end with immediate data acceptance
    // ------------------------------------------------------------------
    test_counter = 2;
    reset();
    begin
        logic [DATA_W-1:0] received;
        fork
            port_send_addr(32'hDEAD_0001);
            begin
                tick();
                check(mem_addr_valid_o, 1,             "T2: request fires");
                check(mem_addr_o,       32'hDEAD_0001, "T2: request addr");
                mem_respond(32'hDEAD_0001, 32'hCAFE_0001);
            end
            port_recv_data(received);
        join
        check(received, 32'hCAFE_0001, "T2: data value");
    end

    // ------------------------------------------------------------------
    // TEST 3: Two back-to-back loads
    // ------------------------------------------------------------------
    test_counter = 3;
    reset();
    begin
        logic [DATA_W-1:0] d0, d1;
        mem_addr_ready_i = 1;
        fork
            begin
                port_send_addr(32'hAAAA_0001);
                port_send_addr(32'hAAAA_0002);
            end
            begin
                mem_respond(32'hAAAA_0001, 32'h1111_0001);
                mem_respond(32'hAAAA_0002, 32'h1111_0002);
            end
            begin
                port_recv_data(d0);
                port_recv_data(d1);
            end
        join
        check(d0, 32'h1111_0001, "T3: first data value");
        check(d1, 32'h1111_0002, "T3: second data value");
        mem_addr_ready_i = 0;
    end

    // ------------------------------------------------------------------
    // TEST 4: allow_alloc_i = 0 blocks allocation
    // ------------------------------------------------------------------
    test_counter = 4;
    reset();
    allow_alloc_i        = 0;
    circ_addr_i       = 32'hBEEF_0001;
    circ_addr_valid_i = 1;
    tick();
    check(circ_addr_ready_o,  0, "T4: addr blocked when allow_alloc=0");
    tick();
    check(mem_addr_valid_o, 0, "T4: no request while blocked");
    circ_addr_valid_i = 0;
    allow_alloc_i        = 1;

    // ------------------------------------------------------------------
    // TEST 5: Back-pressure on memory request stalls issue
    // ------------------------------------------------------------------
    test_counter = 5;
    reset();
    begin
        logic [DATA_W-1:0] received;
        mem_addr_ready_i = 0;
        fork
            port_send_addr(32'hCCCC_0001);
            begin
                tick();
                check(mem_addr_valid_o, 1, "T5: request asserted under back-pressure");
                tick();
                check(mem_addr_valid_o, 1, "T5: request held while not accepted");
                mem_addr_ready_i = 1;
                tick();
                mem_addr_ready_i = 0;
                mem_send_data(32'hFACE_0001);
            end
            port_recv_data(received);
        join
        check(received, 32'hFACE_0001, "T5: correct data after un-stall");
    end

    // ------------------------------------------------------------------
    // TEST 6: Data back-pressure — kernel not ready stalls data response
    // ------------------------------------------------------------------
    test_counter = 6;
    reset();
    begin
        logic [DATA_W-1:0] received;
        circ_data_ready_i = 0;
        fork
            port_send_addr(32'hBBBB_0001);
            begin
                // Accept the address request
                do tick(); while (!mem_addr_valid_o);
                mem_addr_ready_i = 1; tick(); mem_addr_ready_i = 0;
                // Present the response — queue should hold valid high until kernel is ready
                mem_data_valid_i = 1;
                mem_data_i       = 32'hD47A_0001;
                tick();
                check(circ_data_valid_o, 1, "T6: data valid with kernel not ready");
                // Now signal kernel is ready
                circ_data_ready_i = 1;
                tick();
                mem_data_valid_i = 0;
            end
            begin
                check(mem_data_ready_o, 0, "T6: mem_data_ready reflects kernel not ready");
                port_recv_data(received);
            end
        join
        check(received, 32'hD47A_0001, "T6: data received after kernel becomes ready");
    end

    // ------------------------------------------------------------------
    // TEST 7: Fill queue to capacity, check full condition
    // ------------------------------------------------------------------
    test_counter = 7;
    reset();
    mem_addr_ready_i = 0;
    allow_access_i        = 0;
    port_send_addr(32'hF001_0001);
    port_send_addr(32'hF001_0002);
    port_send_addr(32'hF001_0003);
    port_send_addr(32'hF001_0004);
    tick();
    check(circ_addr_ready_o, 0, "T7: not ready when full");
    check(empty_o,              0, "T7: not empty when full");
    allow_access_i = 1;

    // ------------------------------------------------------------------
    // TEST 8: Two queued addresses, both issued before responses arrive
    // ------------------------------------------------------------------
    test_counter = 8;
    reset();
    begin
        logic [DATA_W-1:0] d0, d1;
        fork
            begin
                port_send_addr(32'hF0F0_0001);
                port_send_addr(32'hF0F0_0002);
            end
            begin
                mem_respond(32'hF0F0_0001, 32'h1234_0001);
                mem_respond(32'hF0F0_0002, 32'h1234_0002);
            end
            begin
                port_recv_data(d0);
                port_recv_data(d1);
            end
        join
        check(d0, 32'h1234_0001, "T8: first response data");
        check(d1, 32'h1234_0002, "T8: second response data");
    end

    // ------------------------------------------------------------------
    // TEST 9: 100 random loads with random pauses on all three channels
    // ------------------------------------------------------------------
    test_counter = 9;
    reset();
    begin
        localparam int N = 100;
        logic [ADDR_W-1:0] sent_addrs [N];
        logic [DATA_W-1:0] recv_buf   [N];

        for (int i = 0; i < N; i++)
            sent_addrs[i] = $urandom();

        fork
            // --- Sender ---
            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0, 10)) tick();
                    port_send_addr(sent_addrs[i]);
                end
                $display("T9: Sender done, sent %0d addresses", N);
            end

            // --- Memory model ---
            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0, 10)) tick();
                    mem_respond(sent_addrs[i], sent_addrs[i] + 1);
                end
            end

            // --- Receiver ---
            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0, 10)) tick();
                    port_recv_data(recv_buf[i]);
                end
            end
        join

        for (int i = 0; i < N; i++)
            check(recv_buf[i], sent_addrs[i] + 1, $sformatf("T9: item %0d", i));
        $display("T9: All %0d items verified", N);
    end

    // ------------------------------------------------------------------
    // TEST 10: Fill the queue then drain
    // ------------------------------------------------------------------
    test_counter = 10;
    reset();
    begin
        logic [DATA_W-1:0] d0, d1, d2, d3;
        port_send_addr(32'hAAAA_0001);
        port_send_addr(32'hAAAA_0002);
        port_send_addr(32'hAAAA_0003);
        port_send_addr(32'hAAAA_0004);
        $display("T10: sent 4 addresses to fill the queue");
        fork
            begin
                mem_respond(32'hAAAA_0001, 32'h1111_0001);
                mem_respond(32'hAAAA_0002, 32'h1111_0002);
                mem_respond(32'hAAAA_0003, 32'h1111_0003);
                mem_respond(32'hAAAA_0004, 32'h1111_0004);
            end
            begin
                port_recv_data(d0);
                port_recv_data(d1);
                port_recv_data(d2);
                port_recv_data(d3);
            end
        join
        check(d0, 32'h1111_0001, "T10: first data value");
        check(d1, 32'h1111_0002, "T10: second data value");
        check(d2, 32'h1111_0003, "T10: third data value");
        check(d3, 32'h1111_0004, "T10: fourth data value");
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
