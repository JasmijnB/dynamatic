`timescale 1ns/1ps

// Testbench for structure_cross_group
// Config: load queue (BB0) -> store queue (BB1), NumEntries=4, dep_depth=8
//
// BB execution semantics:
//   - A load/store can NEVER execute before its BB fires (BB handshake first).
//   - Pipelining is allowed: BB0, BB0, ld0, ld1 is legal.
//   - The successor access waits for the last pred access IN ORDER:
//       pred, pred, succ  -> succ waits for BOTH preds
//       succ, pred, succ  -> first succ immediate; second waits for the one pred

module structure_cross_group_tb;

// ===----------------------------------------------------------------------===
// Parameters
// ===----------------------------------------------------------------------===
localparam int ADDR_W   = 32;
localparam int DATA_W   = 32;
localparam int CLK_HALF = 5;
localparam int DEP_DEPTH = 8; // pq.num_entries * dep_entry_ratio = 8 * 1

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

// Cross-BB handshake: one pair per distinct BB.
// bb_valid_N: testbench asserts to signal BB N wants to execute.
// bb_ready_N: DUT asserts back when dep array can accept the execution.
// Handshake completes (BB "executed") on rising edge where both are high.
logic bb_valid_0;
logic bb_ready_0;
logic bb_valid_1;
logic bb_ready_1;

// ===----------------------------------------------------------------------===
// DUT instantiation
// ===----------------------------------------------------------------------===
structure_cross_group dut (
    .clk (clk),
    .rst (rst),
    // Load queue (q0) — kernel
    .circ_addr_i_q0_array_0_i        (ld_circ_addr),
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
    .circ_addr_i_q1_array_0_i        (st_circ_addr),
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
    .mem_exec_ready_o_q1_array_0_o   (st_mem_exec_ready),
    // Cross-BB handshake
    .bb_valid_0_i (bb_valid_0),
    .bb_ready_0_o (bb_ready_0),
    .bb_valid_1_i (bb_valid_1),
    .bb_ready_1_o (bb_ready_1)
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

// --- BB handshake tasks ---
// A BB "executes" when the valid/ready handshake completes on a rising edge.
// The testbench drives valid; the DUT drives ready (back-pressure when dep array full).

task automatic bb0_execute();
    bb_valid_0 = 1;
    @(posedge clk iff bb_ready_0);
    #1;
    bb_valid_0 = 0;
endtask

task automatic bb1_execute();
    bb_valid_1 = 1;
    @(posedge clk iff bb_ready_1);
    #1;
    bb_valid_1 = 0;
endtask

// --- Load queue tasks ---

task automatic ld_send_addr(input logic [ADDR_W-1:0] addr);
    ld_circ_addr       = addr;
    ld_circ_addr_valid = 1;
    @(posedge clk iff ld_circ_addr_ready);
    #1;
    ld_circ_addr_valid = 0;
endtask

task automatic ld_recv_data(output logic [DATA_W-1:0] data);
    ld_circ_data_ready = 1;
    @(posedge clk iff ld_circ_data_valid);
    data = ld_circ_data;
    #1;
    ld_circ_data_ready = 0;
endtask

task automatic ld_mem_recv_addr(output logic [ADDR_W-1:0] addr);
    ld_mem_addr_ready = 1;
    @(posedge clk iff ld_mem_addr_valid);
    addr = ld_mem_addr;
    #1;
    ld_mem_addr_ready = 0;
endtask

task automatic ld_mem_send_data(input logic [DATA_W-1:0] data);
    ld_mem_data_valid = 1;
    ld_mem_data       = data;
    @(posedge clk iff ld_mem_data_ready);
    #1;
    ld_mem_data_valid = 0;
endtask

// Accept the memory request for a load and return data; check address.
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

task automatic st_send_addr(input logic [ADDR_W-1:0] addr);
    st_circ_addr       = addr;
    st_circ_addr_valid = 1;
    @(posedge clk iff st_circ_addr_ready);
    #1;
    st_circ_addr_valid = 0;
endtask

// Service a store: accept addr+data from memory side, send exec ack.
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
    bb_valid_0         = 0;
    bb_valid_1         = 0;
    ticks(3);
    rst = 0;
    ticks(2);
endtask

// ===----------------------------------------------------------------------===
// Tests
// ===----------------------------------------------------------------------===

initial begin
    $dumpfile("structure_cross_group_tb.vcd");
    $dumpvars(0, structure_cross_group_tb);

    // ------------------------------------------------------------------
    // TEST 1: After reset — both queues empty, dep arrays empty and ready.
    //         BB handshake ready signals must be asserted (arrays not full).
    //         Store cannot issue because the store queue is empty.
    // ------------------------------------------------------------------
    test_counter = 1;
    reset();
    ticks(2);
    check(ld_empty,  1, "T1: load queue empty after reset");
    check(st_empty,  1, "T1: store queue empty after reset");
    check(bb_ready_0, 1, "T1: BB0 dep array ready (not full)");
    check(bb_ready_1, 1, "T1: BB1 dep array ready (not full)");
    check(st_mem_addr_valid, 0, "T1: store cannot issue — queue empty");

    // ------------------------------------------------------------------
    // TEST 2: In-order, no address conflict.
    // Pattern: BB0 → ld_alloc → BB1 → st_alloc → st issues immediately.
    // BB0 fires before the load address is sent; BB1 before the store.
    // Different addresses so there is no RAW hazard.
    // ------------------------------------------------------------------
    test_counter = 2;
    reset();
    begin
        logic [DATA_W-1:0] ld_data;
        // BB0 must fire before the corresponding load is allocated.
        bb0_execute();
        fork
            ld_send_addr(32'hAAAA_0001);
            ld_mem_respond(32'hAAAA_0001, 32'hCAFE_0001);
            ld_recv_data(ld_data);
        join
        // BB1 must fire before the corresponding store is allocated.
        bb1_execute();
        st_send_addr(32'hBBBB_0002);  // different addr → no conflict
        st_mem_respond(32'hBBBB_0002, 32'hDEAD_0002);
        check(ld_data, 32'hCAFE_0001, "T2: load data");
        ticks(5);
        check(ld_empty, 1, "T2: load queue drained");
        check(st_empty, 1, "T2: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 3: Pipelined BBs before queue allocations.
    // Pattern: BB0, BB1 fire (pipeline), then ld_alloc, then st_alloc.
    // Both BBs execute before any memory address is presented.
    // This is legal because BB execution represents control-flow, not the
    // actual memory operation.
    // ------------------------------------------------------------------
    test_counter = 3;
    reset();
    begin
        logic [DATA_W-1:0] ld_data;
        // Both BBs fire before any queue operation.
        fork
            bb0_execute();
            bb1_execute();
        join
        // Now allocate both queue entries.
        fork
            begin
                ld_send_addr(32'hAAAA_0010);
                st_send_addr(32'hBBBB_0010);  // different addr
            end
            ld_mem_respond(32'hAAAA_0010, 32'hCAFE_0010);
            ld_recv_data(ld_data);
            st_mem_respond(32'hBBBB_0010, 32'hDEAD_0010);
        join
        check(ld_data, 32'hCAFE_0010, "T3: pipelined load data");
        ticks(5);
        check(ld_empty, 1, "T3: load queue drained");
        check(st_empty, 1, "T3: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 4: pred_BB, pred_BB, succ_BB — store must wait for BOTH loads.
    // Both BB0 executions happen before the store is allocated.
    // Both loads are allocated before BB1 fires.
    // The store uses the same address as both loads (RAW conflict).
    // It may only issue after the *last* matching load completes.
    // ------------------------------------------------------------------
    test_counter = 4;
    reset();
    begin
        logic [DATA_W-1:0] ld_data0, ld_data1;
        // pred_BB fires twice (pipelining two loop iterations).
        bb0_execute();
        bb0_execute();
        // Allocate both loads.
        ld_send_addr(32'hCCCC_0001);
        ld_send_addr(32'hCCCC_0001); // same address → two RAW conflicts for the store
        // succ_BB fires, store is allocated.
        bb1_execute();
        st_send_addr(32'hCCCC_0001);
        // Store must be blocked: loads not yet serviced.
        ticks(10);
        check(st_mem_addr_valid, 0, "T4: store blocked — both loads pending");
        // Service load0.
        fork
            ld_mem_respond(32'hCCCC_0001, 32'h1111_0001);
            ld_recv_data(ld_data0);
        join
        ticks(5);
        // Store still blocked because load1 hasn't completed.
        check(st_mem_addr_valid, 0, "T4: store still blocked — load1 pending");
        // Service load1 — store unblocks.
        fork
            ld_mem_respond(32'hCCCC_0001, 32'h2222_0001);
            ld_recv_data(ld_data1);
        join
        ticks(5);
        st_mem_respond(32'hCCCC_0001, 32'hDEAD_0001);
        ticks(5);
        check(ld_empty, 1, "T4: load queue drained");
        check(st_empty, 1, "T4: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 5: succ_BB, pred_BB, succ_BB pattern.
    // Because access_disparity is initialised to -1 for cross-BB DCs,
    // the first successor access does not need to wait for any predecessor:
    //   - BB1 fires, st0 allocated (different addr) → issues immediately.
    // Then pred_BB fires and its load is allocated.
    // The second successor access conflicts with that load:
    //   - BB1 fires, st1 allocated at same addr → blocked until load completes.
    // ------------------------------------------------------------------
    test_counter = 5;
    reset();
    begin
        logic [DATA_W-1:0] ld_data;
        // succ_BB fires first — first store can issue without any matching load.
        bb1_execute();
        st_send_addr(32'hEEEE_0001);  // addr with no pending load → no conflict
        ticks(5);
        check(st_mem_addr_valid, 1, "T5: first store issues immediately (no pred yet)");
        st_mem_respond(32'hEEEE_0001, 32'hDEAD_0001);
        // pred_BB fires, load is allocated and serviced.
        bb0_execute();
        fork
            ld_send_addr(32'hFFFF_0002);
            ld_mem_respond(32'hFFFF_0002, 32'hCAFE_0002);
            ld_recv_data(ld_data);
        join
        check(ld_data, 32'hCAFE_0002, "T5: load data");
        // succ_BB fires again — second store conflicts with the (now done) load.
        // Since the load already completed, corresponding_entry_sent is true →
        // the store can proceed immediately.
        bb1_execute();
        st_send_addr(32'hFFFF_0002);  // same addr as load — RAW, but load is done
        ticks(5);
        check(st_mem_addr_valid, 1, "T5: second store issues (load already done)");
        st_mem_respond(32'hFFFF_0002, 32'hBEEF_0002);
        ticks(5);
        check(ld_empty, 1, "T5: load queue drained");
        check(st_empty, 1, "T5: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 6: succ_BB, pred_BB, succ_BB with pending load (store must wait).
    // Same pattern as TEST 5, but this time the load has NOT completed when
    // the second store is allocated — the store must block.
    // ------------------------------------------------------------------
    test_counter = 6;
    reset();
    begin
        logic [DATA_W-1:0] ld_data;
        // First succ fires and issues immediately.
        bb1_execute();
        st_send_addr(32'h1111_0001);
        ticks(5);
        check(st_mem_addr_valid, 1, "T6: first store issues immediately");
        st_mem_respond(32'h1111_0001, 32'hDEAD_0001);
        // pred_BB fires, load allocated but memory NOT yet serviced.
        bb0_execute();
        ld_send_addr(32'h2222_0002);
        // Second succ fires and store is allocated at the same conflicting address.
        bb1_execute();
        st_send_addr(32'h2222_0002);
        // Store must be blocked: load pending, address conflict.
        ticks(10);
        check(st_mem_addr_valid, 0, "T6: second store blocked — conflicting load pending");
        // Now service the load — store should unblock.
        fork
            ld_mem_respond(32'h2222_0002, 32'hCAFE_0002);
            ld_recv_data(ld_data);
        join
        ticks(5);
        check(st_mem_addr_valid, 1, "T6: second store unblocked after load done");
        st_mem_respond(32'h2222_0002, 32'hBEEF_0002);
        ticks(5);
        check(ld_empty, 1, "T6: load queue drained");
        check(st_empty, 1, "T6: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 7: Dep array back-pressure — BB0 dep array fills up.
    // dep_depth = pq.num_entries * dep_entry_ratio = 4 * 2 = 8.
    // After 8 BB0 executions without BB1 catching up, bb_ready_0 must
    // go low (back-pressure); a 9th attempt must stall.
    // ------------------------------------------------------------------
    test_counter = 7;
    reset();
    begin
        logic [DATA_W-1:0] ld_data;
        // Fire BB0 DEP_DEPTH times, allocating a load each time.
        for (int i = 0; i < DEP_DEPTH; i++) begin
            bb0_execute();
            ld_send_addr(32'hCCCC_0000 + i);
        end
        ticks(3);
        check(bb_ready_0, 0, "T7: BB0 dep array full — back-pressure asserted");
        // Service the head load: completing it advances the predecessor dep head
        // (pq_done_en), draining one dep entry and releasing back-pressure.
        fork
            ld_mem_respond(32'hCCCC_0000, 32'h1111_0000);
            ld_recv_data(ld_data);
        join
        ticks(3);
        check(bb_ready_0, 1, "T7: BB0 dep array has room again after load completed");
    end

    // ------------------------------------------------------------------
    // TEST 8: Stress — random pipelined cross-BB schedule, conflicting addr.
    // A single kernel thread fires a random interleaving of N predecessor BBs
    // (each allocating a load) and N successor BBs (each allocating a store),
    // all at the SAME address, racing ahead until the dep arrays / queues
    // back-pressure. Memory is serviced by concurrent threads with random
    // timing, so loads and stores pipeline and the queues fill and drain.
    //
    // The dependency is set by BB execution order, NOT by a fixed load/store
    // pairing: the k-th store (stores issue FIFO) must wait for every load
    // whose BB executed before that store's BB. deps[k] = number of predecessor
    // BBs before the (k+1)-th successor BB in the schedule. The monitor flags
    // any store that issues before deps[k] loads have completed.
    // ------------------------------------------------------------------
    test_counter = 8;
    reset();
    begin
        localparam int N = 4;
        localparam logic [ADDR_W-1:0] ADDR = 32'hC0DE_0000;
        bit                bb_is_succ[2*N]; // 0 = predecessor (load) BB, 1 = successor (store) BB
        int                deps[N];         // # of loads the k-th store must wait for
        logic [DATA_W-1:0] ld_datas[N];
        int                seen_p, k;

        // Random interleaving of N predecessor (load) BBs and N successor (store) BBs:
        // start with the N preds followed by the N succs, then shuffle into a
        // random order. Dependencies only point backward (a store waits for loads
        // whose BB executed before it), so every interleaving is a legal schedule.
        foreach (bb_is_succ[i]) bb_is_succ[i] = (i >= N);
        bb_is_succ.shuffle();
        // Precompute each store's dependency count = predecessor BBs before it.
        seen_p = 0; k = 0;
        for (int i = 0; i < 2*N; i++) begin
            if (bb_is_succ[i]) begin deps[k] = seen_p; k++; end
            else                     seen_p++;
        end

        fork
            // Kernel: execute BBs (and allocate their memory op) in schedule order.
            begin
                for (int i = 0; i < 2*N; i++) begin
                    if (bb_is_succ[i]) begin
                        bb1_execute();
                        st_send_addr(ADDR);
                    end else begin
                        bb0_execute();
                        ld_send_addr(ADDR);
                    end
                end
            end
            // Memory: service load requests, returning distinct data per load.
            begin
                for (int i = 0; i < N; i++) begin
                    ticks(12);  // DEBUG slow loads so they stay pending
                    ld_mem_respond(ADDR, 32'h1234_0000 + i);
                end
            end
            // Kernel: consume load return data in FIFO order.
            begin
                for (int i = 0; i < N; i++)
                    ld_recv_data(ld_datas[i]);
            end
            // Memory: service store requests (only issued once their WARs clear).
            begin
                for (int i = 0; i < N; i++) begin
                    ticks($urandom_range(0, 2));
                    st_mem_respond(ADDR, 32'hEEEE_0000 + i);
                end
            end
            // Monitor: every store must respect its BB-order dependency count.
            begin
                int ld_done;
                int st_issued;
                ld_done = 0; st_issued = 0;
                while (st_issued < N) begin
                    @(posedge clk);
                    if (ld_mem_data_valid && ld_mem_data_ready) ld_done++;
                    if (st_mem_addr_valid && ld_done < deps[st_issued]) begin
                        $display("FAIL [T8: store %0d issued with only %0d of %0d dep loads done]",
                                 st_issued, ld_done, deps[st_issued]);
                        fail_count++;
                    end
                    if (st_mem_addr_valid && st_mem_addr_ready) st_issued++;
                end
            end
        join

        for (int i = 0; i < N; i++)
            check(ld_datas[i], 32'h1234_0000 + i, $sformatf("T8: ld data[%0d]", i));
        $display("T8: random %0d-load/%0d-store schedule completed without ordering violations", N, N);
        ticks(20);
        check(ld_empty, 1, "T8: load queue drained");
        check(st_empty, 1, "T8: store queue drained");
    end

    // ------------------------------------------------------------------
    // TEST 10: Two consecutive successor BB executions (no predecessor BB
    // between them), where a predecessor BB executes BETWEEN the two — the
    // second successor's boundary must extend to cover that new
    // predecessor entry, not inherit the first successor's (now stale)
    // boundary.
    //
    // Reproduces the exact mechanism found in the `lu` integration-test
    // failure (lsq7's dp_q3_q2). Execution order: P0 S0 P1 S1 P2 P3 S2 P4 S3.
    // S2 is the first successor BB execution after P2/P3 pushed, so it gets
    // its own first_sq_bb mark and a fresh boundary search landing on PQ3
    // (P3, the latest predecessor pushed so far) — correct for S2, since by
    // execution order S2 only needs to wait through P3.
    // S3 fires after P4 has ALSO pushed, but with NO successor BB execution
    // between S2 and S3 to trigger a fresh first_sq_bb mark for P4: S3's SQ
    // array entry is therefore left unmarked (s_is_first=0), so S3 does NOT
    // perform its own boundary search (go_to_next_p stays 0) and simply
    // inherits ad_oh from S2's search: PQ3. But S3's true boundary (by
    // execution order, since P4 fired between S2 and S3) is PQ4. Once PQ3
    // retires, S3 is wrongly released even though PQ4 (P4) is still pending.
    // ------------------------------------------------------------------
    test_counter = 10;
    reset();
    begin
        localparam logic [ADDR_W-1:0] A0 = 32'hD00D_0000;
        localparam logic [ADDR_W-1:0] A1 = 32'hD00D_0001;
        localparam logic [ADDR_W-1:0] A2 = 32'hD00D_0002;
        localparam logic [ADDR_W-1:0] A3 = 32'hD00D_0003;
        localparam logic [ADDR_W-1:0] A4 = 32'hD00D_0004;
        logic [DATA_W-1:0] ld_data0, ld_data1, ld_data2, ld_data3, ld_data4;

        // P0 -> S0 (marks PQ0, binds S0 -> PQ0).
        bb0_execute();  ld_send_addr(A0);
        bb1_execute();  st_send_addr(A0);

        // P1 -> S1 (marks PQ1, binds S1 -> PQ1).
        bb0_execute();  ld_send_addr(A1);
        bb1_execute();  st_send_addr(A1);

        // P2, P3 both push with NO successor BB execution in between.
        bb0_execute();  ld_send_addr(A2);
        bb0_execute();  ld_send_addr(A3);

        // S2: first successor BB execution since P2/P3 pushed -> gets its
        // own first_sq_bb mark, landing on PQ3 (the latest predecessor
        // pushed so far). Correct for S2.
        bb1_execute();  st_send_addr(A2);

        // P4 pushes, then S3 fires immediately after S2 with NO predecessor
        // BB execution between THEM (S2 and S3 are the consecutive pair) --
        // but P4 DID execute between S2 and S3, so S3's true boundary must
        // extend to PQ4. S3 gets no first_sq_bb mark of its own and
        // inherits S2's PQ3 instead.
        bb0_execute();  ld_send_addr(A4);
        bb1_execute();  st_send_addr(A4);

        // All five stores must block: none of the five loads has completed.
        ticks(10);
        check(st_mem_addr_valid, 0,
              "T10: all stores must block — no predecessor load has completed yet");

        // Complete A0, A1, A2, A3's loads in order (unblocking stores 0-3).
        // A4's load is deliberately left pending: store 4 (which depends on
        // PQ4 = A4's load by execution order) must NOT be released until
        // A4's own load completes, even though PQ3 (A3's entry, which the
        // bug leaves ad_oh stuck on for S3) is now retired.
        fork
            ld_mem_respond(A0, 32'h9999_0000);
            ld_recv_data(ld_data0);
        join
        fork
            ld_mem_respond(A1, 32'h9999_0001);
            ld_recv_data(ld_data1);
        join
        fork
            ld_mem_respond(A2, 32'h9999_0002);
            ld_recv_data(ld_data2);
        join
        fork
            ld_mem_respond(A3, 32'h9999_0003);
            ld_recv_data(ld_data3);
        join
        ticks(5);
        st_mem_respond(A0, 32'hAAAA_0000);
        ticks(5);
        st_mem_respond(A1, 32'hBBBB_0000);
        ticks(5);
        st_mem_respond(A2, 32'hCCCC_0000);
        ticks(5);
        check(st_mem_addr_valid, 0,
              "T10: 5th store (S3, depends on PQ4=A4) must STILL block — A4's load is still pending");

        // Now complete A4's load — only now may the fifth store proceed.
        fork
            ld_mem_respond(A4, 32'h9999_0004);
            ld_recv_data(ld_data4);
        join
        ticks(5);
        st_mem_respond(A4, 32'hDDDD_0000);
        ticks(5);
        check(ld_empty, 1, "T10: load queue drained");
        check(st_empty, 1, "T10: store queue drained");
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
