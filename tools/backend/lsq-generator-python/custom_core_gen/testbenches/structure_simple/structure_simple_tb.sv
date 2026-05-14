`timescale 1ns/1ps

// Testbench for structure_simple.v
// Dependency: load queue (pq, q0) -> store queue (sq, q1)
// WAR semantics: a store to address X must not issue to memory until every
// preceding load to X has completed (data returned to the kernel).
// Config: NumEntries=4, DataWidth=32, AddrWidth=32

module structure_simple_tb;

// ===----------------------------------------------------------------------===
// Parameters
// ===----------------------------------------------------------------------===
localparam int ADDR_W   = 32;
localparam int DATA_W   = 32;
localparam int CLK_HALF = 5; // 10 ns period

// ===----------------------------------------------------------------------===
// DUT signals
// ===----------------------------------------------------------------------===
logic clk;
logic rst;

// Load queue (q0) — kernel side
logic [ADDR_W-1:0] ld_circ_addr;
logic              ld_circ_addr_valid;
logic              ld_circ_addr_ready;
logic [DATA_W-1:0] ld_circ_data;
logic              ld_circ_data_valid;
logic              ld_circ_data_ready;
logic              ld_empty;

// Load queue (q0) — memory side
logic [ADDR_W-1:0] ld_mem_addr;
logic              ld_mem_addr_valid;
logic              ld_mem_addr_ready;
logic [DATA_W-1:0] ld_mem_data;
logic              ld_mem_data_valid;
logic              ld_mem_data_ready;

// Store queue (q1) — kernel side
logic [ADDR_W-1:0] st_circ_addr;
logic              st_circ_addr_valid;
logic              st_circ_addr_ready;
logic [DATA_W-1:0] st_circ_data;
logic              st_circ_data_valid;
logic              st_circ_data_ready;
logic              st_empty;

// Store queue (q1) — memory side
logic [ADDR_W-1:0] st_mem_addr;
logic              st_mem_addr_valid;
logic              st_mem_addr_ready;
logic [DATA_W-1:0] st_mem_data;
logic              st_mem_data_valid;
logic              st_mem_data_ready;
logic              st_mem_exec_valid;
logic              st_mem_exec_ready;

// ===----------------------------------------------------------------------===
// DUT instantiation
// ===----------------------------------------------------------------------===
structure_simple dut (
    .clk (clk),
    .rst (rst),
    // Load queue (q0) — kernel
    .circ_addr_i_q0_array_0_i       (ld_circ_addr),
    .circ_addr_valid_i_q0_array_0_i  (ld_circ_addr_valid),
    .circ_addr_ready_o_q0_array_0_o  (ld_circ_addr_ready),
    .circ_data_o_q0_array_0_o        (ld_circ_data),
    .circ_data_valid_o_q0_array_0_o  (ld_circ_data_valid),
    .circ_data_ready_i_q0_array_0_i  (ld_circ_data_ready),
    .empty_o_q0_array_0_o            (ld_empty),
    // Load queue (q0) — memory
    .mem_addr_o_q0_array_0_o         (ld_mem_addr),
    .mem_addr_valid_o_q0_array_0_o   (ld_mem_addr_valid),
    .mem_addr_ready_i_q0_array_0_i   (ld_mem_addr_ready),
    .mem_data_i_q0_array_0_i         (ld_mem_data),
    .mem_data_valid_i_q0_array_0_i   (ld_mem_data_valid),
    .mem_data_ready_o_q0_array_0_o   (ld_mem_data_ready),
    // Store queue (q1) — kernel
    .circ_addr_i_q1_array_0_i       (st_circ_addr),
    .circ_addr_valid_i_q1_array_0_i  (st_circ_addr_valid),
    .circ_addr_ready_o_q1_array_0_o  (st_circ_addr_ready),
    .circ_data_i_q1_array_0_i        (st_circ_data),
    .circ_data_valid_i_q1_array_0_i  (st_circ_data_valid),
    .circ_data_ready_o_q1_array_0_o  (st_circ_data_ready),
    .empty_o_q1_array_0_o            (st_empty),
    // Store queue (q1) — memory
    .mem_addr_o_q1_array_0_o         (st_mem_addr),
    .mem_addr_valid_o_q1_array_0_o   (st_mem_addr_valid),
    .mem_addr_ready_i_q1_array_0_i   (st_mem_addr_ready),
    .mem_data_o_q1_array_0_o         (st_mem_data),
    .mem_data_valid_o_q1_array_0_o   (st_mem_data_valid),
    .mem_data_ready_i_q1_array_0_i   (st_mem_data_ready),
    .mem_exec_valid_i_q1_array_0_i   (st_mem_exec_valid),
    .mem_exec_ready_o_q1_array_0_o   (st_mem_exec_ready)
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

// --- Load queue tasks ---

// Send a load address from the kernel into the load queue.
task automatic ld_send_addr(input logic [ADDR_W-1:0] addr);
    ld_circ_addr       = addr;
    ld_circ_addr_valid = 1;
    @(posedge clk iff ld_circ_addr_ready);
    #1;
    ld_circ_addr_valid = 0;
endtask

// Assert circ_data_ready and capture the returned load word.
task automatic ld_recv_data(output logic [DATA_W-1:0] data);
    ld_circ_data_ready = 1;
    @(posedge clk iff ld_circ_data_valid);
    data = ld_circ_data;
    #1;
    ld_circ_data_ready = 0;
endtask

// Accept one outgoing load address request (TB acts as memory controller).
task automatic ld_mem_recv_addr(output logic [ADDR_W-1:0] addr);
    ld_mem_addr_ready = 1;
    @(posedge clk iff ld_mem_addr_valid);
    addr = ld_mem_addr;
    #1;
    ld_mem_addr_ready = 0;
endtask

// Push load response data into the queue (TB acts as memory).
// mem_data_ready_o is a passthrough of circ_data_ready_i, so the kernel
// must assert circ_data_ready concurrently (via ld_recv_data).
task automatic ld_mem_send_data(input logic [DATA_W-1:0] data);
    ld_mem_data_valid = 1;
    ld_mem_data       = data;
    @(posedge clk iff ld_mem_data_ready);
    #1;
    ld_mem_data_valid = 0;
endtask

// Accept one load address request, verify it, and return data.
task automatic ld_mem_respond(
    input logic [ADDR_W-1:0] exp_addr,
    input logic [DATA_W-1:0] data
);
    logic [ADDR_W-1:0] got_addr;
    ld_mem_recv_addr(got_addr);
    check(got_addr, exp_addr, $sformatf("T%0d: ld mem addr", test_counter));
    ld_mem_send_data(data);
endtask

// --- Store queue tasks ---

// Send a store address from the kernel into the store queue.
task automatic st_send_addr(input logic [ADDR_W-1:0] addr);
    st_circ_addr       = addr;
    st_circ_addr_valid = 1;
    @(posedge clk iff st_circ_addr_ready);
    #1;
    st_circ_addr_valid = 0;
endtask

// Accept one outgoing store request (addr + data), verify both, and send
// back the execution acknowledgement.
task automatic st_mem_respond(
    input logic [ADDR_W-1:0] exp_addr,
    input logic [DATA_W-1:0] exp_data
);
    logic [ADDR_W-1:0] got_addr;
    logic [DATA_W-1:0] got_data;
    st_circ_data       = exp_data;
    st_circ_data_valid = 1;
    st_mem_data_ready  = 1;
    st_mem_addr_ready  = 1;
    @(posedge clk iff st_mem_addr_valid);
    got_addr = st_mem_addr;
    got_data = st_mem_data;
    #1;
    st_circ_data_valid = 0;
    st_mem_data_ready  = 0;
    st_mem_addr_ready  = 0;
    check(got_addr, exp_addr, $sformatf("T%0d: st mem addr", test_counter));
    check(got_data, exp_data, $sformatf("T%0d: st mem data", test_counter));
    ticks($urandom_range(0, 3));
    st_mem_exec_valid = 1;
    @(posedge clk iff st_mem_exec_ready);
    #1;
    st_mem_exec_valid = 0;
endtask

task automatic reset();
    rst                = 1;
    ld_circ_addr       = '0;
    ld_circ_addr_valid = 0;
    ld_circ_data_ready = 0;
    ld_mem_addr_ready  = 0;
    ld_mem_data        = '0;
    ld_mem_data_valid  = 0;
    st_circ_addr       = '0;
    st_circ_addr_valid = 0;
    st_circ_data       = '0;
    st_circ_data_valid = 0;
    st_mem_addr_ready  = 0;
    st_mem_data_ready  = 0;
    st_mem_exec_valid  = 0;
    ticks(3);
    rst = 0;
    ticks(10);
endtask

// ===----------------------------------------------------------------------===
// Tests
// ===----------------------------------------------------------------------===

initial begin
    $dumpfile("structure_simple_tb.vcd");
    $dumpvars(0, structure_simple_tb);

    // ------------------------------------------------------------------
    // TEST 1: After reset — both queues are empty and ready.
    //         The store cannot issue: DC starts with tail_offset=0,
    //         so allow_sq_access=0 regardless of any address match.
    // ------------------------------------------------------------------
    test_counter = 1;
    reset();
    check(ld_empty,           1, "T1: load queue empty after reset");
    check(st_empty,           1, "T1: store queue empty after reset");
    check(ld_circ_addr_ready, 1, "T1: load queue ready to accept addr");
    check(st_circ_addr_ready, 1, "T1: store queue ready to accept addr");
    check(st_mem_addr_valid,  0, "T1: store cannot issue — no load allocated yet");

    // ------------------------------------------------------------------
    // TEST 2: Non-conflicting addresses — store issues without waiting.
    //         Load at addr A, store at addr B (A ≠ B).
    //         Once the load is allocated (tail_offset=1) and conflict=0
    //         the DC immediately grants allow_sq_access, so the store
    //         can issue in parallel with the load completing.
    // ------------------------------------------------------------------
    test_counter = 2;
    reset();
    begin
        logic [DATA_W-1:0] ld_data;
        fork
            ld_send_addr(32'hAAAA_0001);
            st_send_addr(32'hBBBB_0002);
            begin
                ld_mem_respond(32'hAAAA_0001, 32'hCAFE_0001);
            end
            ld_recv_data(ld_data);
            st_mem_respond(32'hBBBB_0002, 32'hDEAD_0002);
        join
        check(ld_data, 32'hCAFE_0001, "T2: load data value");
        ticks(10);
        check(ld_empty, 1, "T2: load queue drained");
        check(st_empty, 1, "T2: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 3: WAR dependency — store at same address blocked until load done.
    //
    //   1. Load is allocated at addr X → DC: tail_offset=1, conflict=1
    //      → allow_sq_access=0, store cannot issue.
    //   2. Store is allocated at addr X.
    //   3. Load request goes to memory; store is still blocked.
    //   4. Load data is returned (pq_send_en fires) → access_disparity=-1
    //      → conflict bypassed → allow_sq_access=1.
    //   5. Store is now free to issue.
    // ------------------------------------------------------------------
    test_counter = 3;
    reset();
    begin
        logic [DATA_W-1:0] ld_data;
        // Allocate load first, then store at the same address.
        ld_send_addr(32'hDEAD_BEEF);
        st_send_addr(32'hDEAD_BEEF);
        // Store must be blocked: conflict=1, access_disparity=0.
        ticks(20);
        check(st_mem_addr_valid, 0, "T3: store blocked — WAR conflict, load not yet done");
        // Complete the load: memory responds, kernel accepts data.
        fork
            ld_mem_respond(32'hDEAD_BEEF, 32'h1234_5678);
            ld_recv_data(ld_data);
        join
        check(ld_data, 32'h1234_5678, "T3: load data received");
        // After pq_send_en fires, access_disparity<0 → conflict bypassed.
        ticks(10);
        check(st_mem_addr_valid, 1, "T3: store unblocked after load completes");
        st_mem_respond(32'hDEAD_BEEF, 32'hBEEF_CAFE);
        ticks(10);
        check(st_empty, 1, "T3: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 4: Two sequential WAR iterations at the same address.
    //         Verifies the DC resets correctly between iterations so that
    //         the second store is also blocked until the second load completes.
    // ------------------------------------------------------------------
    test_counter = 4;
    reset();
    begin
        logic [DATA_W-1:0] d0, d1;
        // --- Iteration 1 ---
        ld_send_addr(32'hF00D_0001);
        st_send_addr(32'hF00D_0001);
        ticks(20);
        check(st_mem_addr_valid, 0, "T4-iter1: store blocked");
        fork
            ld_mem_respond(32'hF00D_0001, 32'hAAAA_0001);
            ld_recv_data(d0);
        join
        check(d0, 32'hAAAA_0001, "T4-iter1: load data");
        ticks(10);
        check(st_mem_addr_valid, 1, "T4-iter1: store unblocked");
        st_mem_respond(32'hF00D_0001, 32'h1111_0001);
        // --- Iteration 2 ---
        ld_send_addr(32'hF00D_0002);
        st_send_addr(32'hF00D_0002);
        ticks(20);
        check(st_mem_addr_valid, 0, "T4-iter2: store blocked");
        fork
            ld_mem_respond(32'hF00D_0002, 32'hBBBB_0002);
            ld_recv_data(d1);
        join
        check(d1, 32'hBBBB_0002, "T4-iter2: load data");
        ticks(10);
        check(st_mem_addr_valid, 1, "T4-iter2: store unblocked");
        st_mem_respond(32'hF00D_0002, 32'h2222_0002);
        ticks(10);
        check(ld_empty, 1, "T4: load queue drained");
        check(st_empty, 1, "T4: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 5: Back-pressure on load memory side — store stays blocked
    //         while the memory controller holds back the load response.
    // ------------------------------------------------------------------
    test_counter = 5;
    reset();
    begin
        logic [DATA_W-1:0] ld_data;
        logic [ADDR_W-1:0] got_addr;
        ld_send_addr(32'hCCCC_0001);
        st_send_addr(32'hCCCC_0001);
        // Accept the load address request but do NOT yet return data.
        ld_mem_recv_addr(got_addr);
        check(got_addr, 32'hCCCC_0001, "T5: ld mem addr captured");
        ticks(20);
        check(st_mem_addr_valid, 0, "T5: store blocked while load data not returned");
        ticks(20);
        check(st_mem_addr_valid, 0, "T5: store still blocked one cycle later");
        // Now return the load data — store should unblock.
        fork
            ld_mem_send_data(32'hFACE_0001);
            ld_recv_data(ld_data);
        join
        check(ld_data, 32'hFACE_0001, "T5: load data value");
        ticks(10);
        check(st_mem_addr_valid, 1, "T5: store unblocked after data returned");
        st_mem_respond(32'hCCCC_0001, 32'hD00D_0001);
    end

    // ------------------------------------------------------------------
    // TEST 6: Stress — N pairs at non-conflicting addresses run in parallel.
    //         Stores can issue as soon as their paired load is allocated
    //         (no WAR conflict since addresses are disjoint).
    // ------------------------------------------------------------------
    test_counter = 6;
    reset();
    begin
        localparam int N = 8;
        logic [ADDR_W-1:0] ld_addrs[N];
        logic [ADDR_W-1:0] st_addrs[N];
        logic [DATA_W-1:0] ld_datas[N];

        for (int i = 0; i < N; i++) begin
            ld_addrs[i] = 32'hA000_0000 + (i * 4);
            st_addrs[i] = 32'hB000_0000 + (i * 4);  // distinct from all ld addrs
        end

        fork
            begin
                for (int i = 0; i < N; i++) begin
                    ticks($urandom_range(0, 2));
                    ld_send_addr(ld_addrs[i]);
                end
            end
            begin
                for (int i = 0; i < N; i++) begin
                    ticks($urandom_range(0, 2));
                    st_send_addr(st_addrs[i]);
                end
            end
            begin
                for (int i = 0; i < N; i++) begin
                    ticks($urandom_range(0, 4));
                    ld_mem_respond(ld_addrs[i], ld_addrs[i] + 32'h100);
                end
            end
            begin
                for (int i = 0; i < N; i++) begin
                    ticks($urandom_range(0, 4));
                    ld_recv_data(ld_datas[i]);
                end
            end
            begin
                for (int i = 0; i < N; i++) begin
                    ticks($urandom_range(0, 4));
                    st_mem_respond(st_addrs[i], 32'hDDDD_0000 + i);
                end
            end
        join

        for (int i = 0; i < N; i++)
            check(ld_datas[i], ld_addrs[i] + 32'h100, $sformatf("T6: ld data[%0d]", i));
        $display("T6: All %0d non-conflicting pairs completed", N);
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
