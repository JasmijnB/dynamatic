`timescale 1ns/1ps

// Testbench for load_queue.v
// Generated from config.json: NumEntries=4, DataWidth=32, AddrWidth=32, IDWidth=2, IDVal=0

module load_queue_tb;

// ===----------------------------------------------------------------------===
// Parameters (match config.json)
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
logic                 clk;
logic                 rst;

// Kernel -> queue (load address)
logic [ADDR_W-1:0]    ldp_addr_i;
logic                 ldp_addr_valid_i;
logic                 ldp_addr_ready_o;

// Queue -> kernel (load data)
logic [DATA_W-1:0]    ldp_data_o;
logic                 ldp_data_valid_o;
logic                 ldp_data_ready_i;

// Status
logic                 empty_o;

// Queue -> AXI (read request)
logic                 rreq_valid_o;
logic                 rreq_ready_i;
logic [ID_W-1:0]      rreq_id_o;
logic [ADDR_W-1:0]    rreq_addr_o;

// AXI -> queue (read response)
logic                 rresp_valid_i;
logic                 rresp_ready_o;
logic [ID_W-1:0]      rresp_id_i;
logic [DATA_W-1:0]    rresp_data_i;

// Dependency checker
logic                 allow_alloc_i;
logic                 allow_load_i;

int test_counter = 0; // for debugging: 

// ===----------------------------------------------------------------------===
// DUT instantiation
// ===----------------------------------------------------------------------===
load_queue dut (
    .clk              (clk),
    .rst              (rst),
    .ldp_addr_i       (ldp_addr_i),
    .ldp_addr_valid_i (ldp_addr_valid_i),
    .ldp_addr_ready_o (ldp_addr_ready_o),
    .ldp_data_o       (ldp_data_o),
    .ldp_data_valid_o (ldp_data_valid_o),
    .ldp_data_ready_i (ldp_data_ready_i),
    .empty_o          (empty_o),
    .rreq_valid_o     (rreq_valid_o),
    .rreq_ready_i     (rreq_ready_i),
    .rreq_id_o        (rreq_id_o),
    .rreq_addr_o      (rreq_addr_o),
    .rresp_valid_i    (rresp_valid_i),
    .rresp_ready_o    (rresp_ready_o),
    .rresp_id_i       (rresp_id_i),
    .rresp_data_i     (rresp_data_i),
    .allow_alloc_i    (allow_alloc_i),
    .allow_load_i     (allow_load_i)
);

// ===----------------------------------------------------------------------===
// Clock
// ===----------------------------------------------------------------------===
initial clk = 0;
always #CLK_HALF clk = ~clk;

// ===----------------------------------------------------------------------===
// Helpers
// ===----------------------------------------------------------------------===
int fail_count = 0;

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

task automatic tick; @(posedge clk); #1; endtask

// Drive a load-address handshake. Blocks at the posedge where both
// ldp_addr_valid_i and ldp_addr_ready_o are simultaneously high.
task automatic port_send_addr(input logic [ADDR_W-1:0] addr);
    ldp_addr_i       = addr;
    ldp_addr_valid_i = 1;
    @(posedge clk iff ldp_addr_ready_o);
    #1;
    ldp_addr_valid_i = 0;
endtask

// Assert ldp_data_ready_i and block at the posedge where both
// ldp_data_valid_o and ldp_data_ready_i are simultaneously high.
task automatic port_recv_data(output logic [DATA_W-1:0] data);
    ldp_data_ready_i = 1;
    @(posedge clk iff ldp_data_valid_o);
    data = ldp_data_o;
    #1;
    ldp_data_ready_i = 0;
endtask

// AXI read-data channel (R): TB is master.
// Drive valid+data and block at the posedge where rresp_ready_o is high.
task automatic axi_send_data(input logic [DATA_W-1:0] data);
    rresp_valid_i = 1;
    rresp_id_i    = ID_VAL;
    rresp_data_i  = data;
    @(posedge clk iff rresp_ready_o);
    #1;
    rresp_valid_i = 0;
endtask

// AXI read-address channel (AR): TB is slave.
// Assert ready and block at the posedge where rreq_valid_o is high.
task automatic axi_receive_addr(output logic [ADDR_W-1:0] addr);
    rreq_ready_i = 1;
    @(posedge clk iff rreq_valid_o);
    addr = rreq_addr_o;
    #1;
    rreq_ready_i = 0;
endtask

// Accept the next pending AXI read request and hold rresp_valid high until
// rresp_ready_o (= ldp_data_ready_i) completes the handshake.
// NOTE: rresp_ready_o is a direct passthrough of ldp_data_ready_i, so this
// task must run in parallel with port_recv_data() whenever ldp_data_ready_i is
// not already asserted.
task automatic axi_respond(input logic [ADDR_W-1:0] addr, input logic [DATA_W-1:0] data);
    logic [ADDR_W-1:0] captured_addr;
    axi_receive_addr(captured_addr);
    check(captured_addr, addr, $sformatf("T%d", test_counter));
    axi_send_data(data);
endtask


task automatic reset();
    rst              = 1;
    ldp_addr_valid_i = 0;
    ldp_addr_i       = '0;
    ldp_data_ready_i = 0;  // kernel not ready by default; tests opt-in
    rreq_ready_i     = 0;
    rresp_valid_i    = 0;
    rresp_id_i       = '0;
    rresp_data_i     = '0;
    allow_alloc_i    = 1;
    allow_load_i     = 1;
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
    check(empty_o,          1, "T1: empty after reset");
    check(ldp_addr_ready_o, 1, "T1: addr ready after reset");
    check(rreq_valid_o,     0, "T1: no rreq after reset");

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
                check(rreq_valid_o, 1,             "T2: rreq fires");
                check(rreq_addr_o,  32'hDEAD_0001, "T2: rreq addr");
                check(rreq_id_o,    ID_VAL,         "T2: rreq id");
                axi_respond(32'hDEAD_0001, 32'hCAFE_0001);
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
        rreq_ready_i = 1;
        fork
            begin
                port_send_addr(32'hAAAA_0001);
                port_send_addr(32'hAAAA_0002);
            end
            begin
                axi_respond(32'hAAAA_0001, 32'h1111_0001);
                axi_respond(32'hAAAA_0002, 32'h1111_0002);
            end
            begin
                port_recv_data(d0);
                port_recv_data(d1);
            end
        join
        check(d0, 32'h1111_0001, "T3: first data value");
        check(d1, 32'h1111_0002, "T3: second data value");
        rreq_ready_i = 0;
    end

    // ------------------------------------------------------------------
    // TEST 4: allow_alloc_i = 0 blocks allocation
    // ------------------------------------------------------------------
    test_counter = 4;
    reset();
    allow_alloc_i    = 0;
    ldp_addr_i       = 32'hBEEF_0001;
    ldp_addr_valid_i = 1;
    tick();
    check(ldp_addr_ready_o, 0, "T4: addr blocked when allow_alloc=0");
    tick();
    check(rreq_valid_o, 0, "T4: no request while blocked");
    ldp_addr_valid_i = 0;
    allow_alloc_i    = 1;

    // ------------------------------------------------------------------
    // TEST 5: AXI read-request back-pressure stalls issue
    // ------------------------------------------------------------------
    test_counter = 5;
    reset();
    begin
        logic [DATA_W-1:0] received;
        rreq_ready_i = 0;
        fork
            port_send_addr(32'hCCCC_0001);
            begin
                tick();
                check(rreq_valid_o, 1, "T5: rreq asserted under back-pressure");
                tick();
                check(rreq_valid_o, 1, "T5: rreq held while not accepted");
                rreq_ready_i = 1;
                tick();
                rreq_ready_i = 0;
                axi_send_data(32'hFACE_0001);
            end
            port_recv_data(received);
        join
        check(received, 32'hFACE_0001, "T5: correct data after un-stall");
    end

    // ------------------------------------------------------------------
    // TEST 6: Data back-pressure — kernel not ready stalls AXI response
    // ------------------------------------------------------------------
    test_counter = 6;
    reset();
    begin
        logic [DATA_W-1:0] received;
        // kernel is not ready for data yet
        ldp_data_ready_i = 0;
        fork
            port_send_addr(32'hBBBB_0001);
            begin
                // Accept the AXI request
                do tick(); while (!rreq_valid_o);
                rreq_ready_i = 1; tick(); rreq_ready_i = 0;
                // Present the response — queue should hold valid high
                rresp_valid_i = 1; rresp_id_i = ID_VAL; rresp_data_i = 32'hD47A_0001;
                tick();
                check(ldp_data_valid_o, 1, "T6: data valid with kernel not ready");
                // Now signal kernel is ready
                ldp_data_ready_i = 1;
                tick();
                rresp_valid_i = 0;
            end
            begin
                check(rresp_ready_o,    0, "T6: rresp_ready reflects kernel not ready");
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
    rreq_ready_i = 0;
    allow_load_i = 0;
    port_send_addr(32'hF001_0001);
    port_send_addr(32'hF001_0002);
    port_send_addr(32'hF001_0003);
    port_send_addr(32'hF001_0004);
    tick();
    check(ldp_addr_ready_o, 0, "T7: not ready when full");
    check(empty_o,          0, "T7: not empty when full");
    allow_load_i = 1;

    // ------------------------------------------------------------------
    // TEST 8: Response with wrong ID is ignored
    // ------------------------------------------------------------------
    test_counter = 8;
    reset();
    begin
        logic [DATA_W-1:0] received;
        rreq_ready_i = 1;
        fork
            port_send_addr(32'hDDDD_0001);
            begin
                // Accept the request
                do tick(); while (!rreq_valid_o);
                rreq_ready_i = 1; tick(); rreq_ready_i = 0;
                // Wrong ID response — should not complete the handshake
                rresp_valid_i = 1; rresp_id_i = 2'(ID_VAL + 1); rresp_data_i = 32'hBAD_DADA;
                tick();
                check(ldp_data_valid_o, 0, "T8: wrong ID not forwarded to kernel");
                rresp_valid_i = 0;
                tick();
                    tick(); // acce
                // Correct ID response
                axi_send_data(32'hD00D_D00D);
            end
            begin
                // Kernel should not receive anything for the bad response
                repeat(4) tick();
                // Then open up for the good response
                port_recv_data(received);
            end
        join
        check(received, 32'hD00D_D00D, "T8: correct data after wrong-ID response");
    end

    // ------------------------------------------------------------------
    // TEST 9: 100 random loads with random pauses on all three channels
    //
    // Three concurrent threads:
    //   - Sender  : sends 100 addresses with random idle gaps
    //   - AXI     : accepts each request and replies with data = addr + 1,
    //               with random gaps between request acceptance and response
    //   - Receiver: collects 100 data words with random idle gaps,
    //               checks each one equals addr + 1
    // ------------------------------------------------------------------
    test_counter = 9;
    reset();
    begin
        localparam int N = 20;
        logic [ADDR_W-1:0] sent_addrs [N];
        logic [DATA_W-1:0] recv_buf   [N];
        int                recv_count;
        int                pause_len;

        recv_count = 0;

        // Pre-generate addresses
        for (int i = 0; i < N; i++)
            sent_addrs[i] = $urandom();

        fork
            // --- Sender ---
            begin
                for (int i = 0; i < N; i++) begin
                    // Random idle: 0-3 cycles before asserting valid
                    repeat ($urandom_range(0,10)) tick();
                    port_send_addr(sent_addrs[i]);
                end
                $display("T9: Sender done, sent %0d addresses", N);
            end

            // --- AXI memory model ---
            begin
                for (int i = 0; i < N; i++) begin
                    repeat ($urandom_range(0,10)) tick();
                    axi_respond(sent_addrs[i], sent_addrs[i] + 1);
                end
            end

            // --- Receiver ---
            begin
                for (int i = 0; i < N; i++) begin
                    // Random idle: 0-3 cycles before asserting ready
                    repeat ($urandom_range(0,10)) tick();
                    port_recv_data(recv_buf[i]);
                end
            end
        join

        // Verify all received values
        for (int i = 0; i < N; i++) begin
            check(recv_buf[i], sent_addrs[i] + 1, $sformatf("T9: item %0d", i));
        end
        $display("T9: All %0d items verified", N);
    end

    // ------------------------------------------------------------------
    // TEST 10: Fill the queue
    // ------------------------------------------------------------------
    test_counter = 3;
    reset();
    begin
        logic [DATA_W-1:0] d0, d1, d2, d3;
        begin
            port_send_addr(32'hAAAA_0001);
            port_send_addr(32'hAAAA_0002);
            port_send_addr(32'hAAAA_0003);
            port_send_addr(32'hAAAA_0004);
            $display("T7: sent 4 addresses to fill the queue");
        end
        fork
            begin
                axi_respond(32'hAAAA_0001, 32'h1111_0001);
                axi_respond(32'hAAAA_0002, 32'h1111_0002);
                axi_respond(32'hAAAA_0003, 32'h1111_0003);
                axi_respond(32'hAAAA_0004, 32'h1111_0004);
                $display("T7: sent responses for all 4 addresses");
            end
            begin
                port_recv_data(d0);
                port_recv_data(d1);
                port_recv_data(d2); 
                port_recv_data(d3);
                $display("T7: received 3 data values");
            end
        join
        check(d0, 32'h1111_0001, "T3: first data value");
        check(d1, 32'h1111_0002, "T3: second data value");
        check(d2, 32'h1111_0003, "T3: third data value");
        check(d3, 32'h1111_0004, "T3: fourth data value");
    end

    // ------------------------------------------------------------------
    // TEST 10: Fill the queue
    // ------------------------------------------------------------------
    test_counter = 3;
    reset();
    begin
        logic [DATA_W-1:0] d0, d1, d2, d3;
        allow_load_i = 0;
        fork
            begin
                port_send_addr(32'hAAAA_0001);
                port_send_addr(32'hAAAA_0002);
                port_send_addr(32'hAAAA_0003);
                port_send_addr(32'hAAAA_0004);
                $display("T7: sent 4 addresses to fill the queue");
                check(ldp_addr_ready_o, 0, "T7: not ready when no loads are allowed");
                allow_load_i = 1;
            end
            begin
                axi_respond(32'hAAAA_0001, 32'h1111_0001);
                axi_respond(32'hAAAA_0002, 32'h1111_0002);
                axi_respond(32'hAAAA_0003, 32'h1111_0003);
                axi_respond(32'hAAAA_0004, 32'h1111_0004);
                $display("T7: sent responses for all 4 addresses");
            end
            begin
                port_recv_data(d0);
                port_recv_data(d1);
                port_recv_data(d2); 
                port_recv_data(d3);
                $display("T7: received 3 data values");
            end
        join
        check(d0, 32'h1111_0001, "T3: first data value");
        check(d1, 32'h1111_0002, "T3: second data value");
        check(d2, 32'h1111_0003, "T3: third data value");
        check(d3, 32'h1111_0004, "T3: fourth data value");
    end


    // ------------------------------------------------------------------
    // Summary
    // ------------------------------------------------------------------
    $display("\n%0d test(s) failed.", fail_count);
    $finish;
end

// Timeout watchdog
initial begin
    #100_000_00;
    $display("TIMEOUT");
    $finish;
end

endmodule
