// Randomized reference-model stress test for CROSS-BB dependency checkers.
//
// Include from a per-config wrapper that first defines
//   `define TB_MODULE  <name>_tb
//   `define DUT_MODULE <structure name>
// DUT: port 0 = load queue (q0) in BB 0, port 1 = store queue (q1) in BB 1.
// The config decides which checkers exist (L->S WAR only, or both
// directions); define WAR_ONLY for the former, so the program carries no RAW
// hazard.
//
// The program is a random schedule of BB executions in RUNS (e.g. PPPSSPS..):
// each BB 0 execution issues the next load, each BB 1 execution the next
// store, and program order is the BB execution order. The BB tokens are
// driven in program order through the DUT's bb_valid/bb_ready handshakes
// (back-pressured when a dep array is full); the load/store address and data
// channels run on their own random timelines, so an address may arrive well
// before or after its BB token. Memory model, golden model and watchdog as in
// same_bb_stress_body.sv.

`timescale 1ns/1ps

module `TB_MODULE;

localparam int ADDR_W   = 8;
localparam int DATA_W   = 32;
localparam int CLK_HALF = 5;
localparam int MEM_SIZE = 1 << ADDR_W;
localparam int K        = 240;   // iterations per program
localparam int N_SEEDS  = 6;     // programs per run
localparam int LD_LAT   = 4;     // max extra load latency (cycles)

logic clk, rst;

logic [ADDR_W-1:0] ld_circ_addr;      logic ld_circ_addr_valid, ld_circ_addr_ready;
logic [DATA_W-1:0] ld_circ_data;      logic ld_circ_data_valid, ld_circ_data_ready;
logic              ld_empty;
logic [ADDR_W-1:0] ld_mem_addr;       logic ld_mem_addr_valid, ld_mem_addr_ready;
logic [DATA_W-1:0] ld_mem_data;       logic ld_mem_data_valid, ld_mem_data_ready;

logic [ADDR_W-1:0] st_circ_addr;      logic st_circ_addr_valid, st_circ_addr_ready;
logic [DATA_W-1:0] st_circ_data;      logic st_circ_data_valid, st_circ_data_ready;
logic              st_empty;
logic [ADDR_W-1:0] st_mem_addr;       logic st_mem_addr_valid, st_mem_addr_ready;
logic [DATA_W-1:0] st_mem_data;       logic st_mem_data_valid, st_mem_data_ready;
logic              st_mem_exec_valid, st_mem_exec_ready;
logic              bb_valid_0, bb_ready_0, bb_valid_1, bb_ready_1;

`DUT_MODULE dut (
    .clk (clk),
    .rst (rst),
    .circ_addr_i_q0_array_0_i       (ld_circ_addr),
    .circ_addr_valid_i_q0_array_0_i (ld_circ_addr_valid),
    .circ_addr_ready_o_q0_array_0_o (ld_circ_addr_ready),
    .circ_data_o_q0_array_0_o       (ld_circ_data),
    .circ_data_valid_o_q0_array_0_o (ld_circ_data_valid),
    .circ_data_ready_i_q0_array_0_i (ld_circ_data_ready),
    .empty_o_q0_array_0_o           (ld_empty),
    .mem_addr_o_q0_array_0_o        (ld_mem_addr),
    .mem_addr_valid_o_q0_array_0_o  (ld_mem_addr_valid),
    .mem_addr_ready_i_q0_array_0_i  (ld_mem_addr_ready),
    .mem_data_i_q0_array_0_i        (ld_mem_data),
    .mem_data_valid_i_q0_array_0_i  (ld_mem_data_valid),
    .mem_data_ready_o_q0_array_0_o  (ld_mem_data_ready),
    .circ_addr_i_q1_array_0_i       (st_circ_addr),
    .circ_addr_valid_i_q1_array_0_i (st_circ_addr_valid),
    .circ_addr_ready_o_q1_array_0_o (st_circ_addr_ready),
    .circ_data_i_q1_array_0_i       (st_circ_data),
    .circ_data_valid_i_q1_array_0_i (st_circ_data_valid),
    .circ_data_ready_o_q1_array_0_o (st_circ_data_ready),
    .empty_o_q1_array_0_o           (st_empty),
    .mem_addr_o_q1_array_0_o        (st_mem_addr),
    .mem_addr_valid_o_q1_array_0_o  (st_mem_addr_valid),
    .mem_addr_ready_i_q1_array_0_i  (st_mem_addr_ready),
    .mem_data_o_q1_array_0_o        (st_mem_data),
    .mem_data_valid_o_q1_array_0_o  (st_mem_data_valid),
    .mem_data_ready_i_q1_array_0_i  (st_mem_data_ready),
    .mem_exec_valid_i_q1_array_0_i  (st_mem_exec_valid),
    .mem_exec_ready_o_q1_array_0_o  (st_mem_exec_ready),
    .bb_valid_0_i (bb_valid_0),
    .bb_ready_0_o (bb_ready_0),
    .bb_valid_1_i (bb_valid_1),
    .bb_ready_1_o (bb_ready_1)
);

initial clk = 0;
always #CLK_HALF clk = ~clk;

`include "utils.sv"   // resolved relative to this file

// ===----------------------------------------------------------------------===
// Program and golden model
// ===----------------------------------------------------------------------===
logic [ADDR_W-1:0] prog_ld_addr [K];
logic [ADDR_W-1:0] prog_st_addr [K];
logic [DATA_W-1:0] prog_st_data [K];
logic [DATA_W-1:0] exp_ld_data  [K];
logic [DATA_W-1:0] mem          [MEM_SIZE];
logic [DATA_W-1:0] golden_mem   [MEM_SIZE];

// Per-access random gaps (cycles) for the four kernel channels, and per BB
// execution for the token driver.
int gap_ld_addr [K];
int gap_st_addr [K];
int gap_st_data [K];
int gap_ld_ret  [K];
int gap_bb      [2*K];

// The BB schedule: sched[t] = 0 (a load, BB 0) or 1 (a store, BB 1), K of each.
int sched [2*K];

function automatic int phase_of(input int i);
    return (i * 4) / K;   // 4 phases of K/4 accesses (per port)
endfunction

task automatic make_program(input int seed);
    int unused, t, nl, ns, run, which;
    unused = $urandom(seed);
    for (int a = 0; a < MEM_SIZE; a++) golden_mem[a] = 32'hA000_0000 + a;
    // Runs of random length (1..6), alternating P and S, until K of each.
    t = 0; nl = 0; ns = 0; which = $urandom_range(0, 1);
    while (nl < K || ns < K) begin
        run = $urandom_range(1, 6);
        for (int r = 0; r < run; r++) begin
            if (which == 0 && nl < K) begin sched[t++] = 0; nl++; end
            else if (which == 1 && ns < K) begin sched[t++] = 1; ns++; end
        end
        which = 1 - which;
    end
    for (int i = 0; i < 2*K; i++) gap_bb[i] = $urandom_range(0, 3);
    for (int i = 0; i < K; i++) begin
        case (phase_of(i))
            0: begin   // loads' addresses early, stores' late
                gap_ld_addr[i] = $urandom_range(0, 1);
                gap_st_addr[i] = $urandom_range(0, 12);
                gap_st_data[i] = $urandom_range(0, 12);
                gap_ld_ret[i]  = $urandom_range(0, 1);
            end
            1: begin   // stores' addresses early, loads' late
                gap_ld_addr[i] = $urandom_range(0, 12);
                gap_st_addr[i] = $urandom_range(0, 1);
                gap_st_data[i] = $urandom_range(0, 1);
                gap_ld_ret[i]  = $urandom_range(0, 6);
            end
            default: begin   // mixed
                gap_ld_addr[i] = $urandom_range(0, 4);
                gap_st_addr[i] = $urandom_range(0, 4);
                gap_st_data[i] = $urandom_range(0, 4);
                gap_ld_ret[i]  = $urandom_range(0, 3);
            end
        endcase
    end
    // Addresses and the golden model, walking the schedule in program order.
    nl = 0; ns = 0;
    for (int t2 = 0; t2 < 2*K; t2++) begin
        if (sched[t2] == 0) begin
`ifdef WAR_ONLY
            // RAW-free: read an address no program-earlier store wrote
            // (stores write 0, 1, 2, ... in order), but one a later store
            // will -- so every such store must wait for this load.
            prog_ld_addr[nl] = ns + $urandom_range(0, 3);
`else
            prog_ld_addr[nl] = (phase_of(nl) < 2) ? $urandom_range(0, 3)
                                                  : $urandom_range(0, 5);
`endif
            exp_ld_data[nl] = golden_mem[prog_ld_addr[nl]];
            nl++;
        end else begin
`ifdef WAR_ONLY
            prog_st_addr[ns] = ns;
`else
            prog_st_addr[ns] = (phase_of(ns) < 2) ? $urandom_range(8, 11)
                                                  : $urandom_range(0, 5);
`endif
            prog_st_data[ns] = {seed[7:0], 8'h00, ns[15:0]} + 1;
            golden_mem[prog_st_addr[ns]] = prog_st_data[ns];
            ns++;
        end
    end
endtask

// ===----------------------------------------------------------------------===
// Kernel-side drivers (drive after posedge, transfer on the next posedge
// when valid & ready hold at the negedge before it).
// ===----------------------------------------------------------------------===
logic running;
// Driver progress, for the optional DEBUG_DUMP at a timeout.
int bb_tok_idx, st_addr_idx, st_data_idx;
// BB tokens accepted by the DUT so far, per BB. The k-th address of a port is
// only presented once that port's k-th BB token has been accepted: in the
// generated circuits an access is never entered before its BB execution is
// recorded. Define ADDR_BEFORE_TOKEN to drop that ordering (addresses then
// run on a fully independent timeline); the cross-BB predecessor dep array can
// then underflow, see /scratch/jasmijn/on-area/patches/
// dynamatic-cross-bb-pred-retire-gate.patch for the gate that would close it.
int ld_tok_done, st_tok_done;

// Phase 2 starves the load memory port: loads sit allocated but unread while
// stores rush in, which is the window a missed WAR check writes through.
logic ld_mem_starve;
task automatic drive_ld_addr();
    for (int i = 0; i < K; i++) begin
        ld_mem_starve = (phase_of(i) == 3);
        ticks(gap_ld_addr[i]);
`ifndef ADDR_BEFORE_TOKEN
        while (ld_tok_done <= i) tick();
`endif
        ld_circ_addr = prog_ld_addr[i]; ld_circ_addr_valid = 1;
        @(negedge clk); while (!ld_circ_addr_ready) @(negedge clk);
        tick(); ld_circ_addr_valid = 0;
    end
endtask

task automatic drive_st_addr();
    for (int i = 0; i < K; i++) begin
        st_addr_idx = i;
        ticks(gap_st_addr[i]);
`ifndef ADDR_BEFORE_TOKEN
        while (st_tok_done <= i) tick();
`endif
        st_circ_addr = prog_st_addr[i]; st_circ_addr_valid = 1;
        @(negedge clk); while (!st_circ_addr_ready) @(negedge clk);
        tick(); st_circ_addr_valid = 0;
    end
endtask

task automatic drive_st_data();
    for (int i = 0; i < K; i++) begin
        st_data_idx = i;
        ticks(gap_st_data[i]);
        st_circ_data = prog_st_data[i]; st_circ_data_valid = 1;
        @(negedge clk); while (!st_circ_data_ready) @(negedge clk);
        tick(); st_circ_data_valid = 0;
    end
endtask

task automatic drive_bb_tokens();
    for (int t = 0; t < 2*K; t++) begin
        bb_tok_idx = t;
        ticks(gap_bb[t]);
        if (sched[t] == 0) begin
            bb_valid_0 = 1;
            @(negedge clk); while (!bb_ready_0) @(negedge clk);
            tick(); bb_valid_0 = 0; ld_tok_done++;
        end else begin
            bb_valid_1 = 1;
            @(negedge clk); while (!bb_ready_1) @(negedge clk);
            tick(); bb_valid_1 = 0; st_tok_done++;
        end
    end
endtask

int ld_returned;
task automatic consume_ld_data();
    for (int i = 0; i < K; i++) begin
        ticks(gap_ld_ret[i]);
        ld_circ_data_ready = 1;
        @(negedge clk); while (!ld_circ_data_valid) @(negedge clk);
        check(ld_circ_data, exp_ld_data[i], $sformatf("T%0d: L%0d data", test_counter, i));
        ld_returned++;
        tick(); ld_circ_data_ready = 0;
    end
endtask

// ===----------------------------------------------------------------------===
// Memory model
// ===----------------------------------------------------------------------===
logic [DATA_W-1:0] ld_pending [$];
int                ld_ready_at [$];
int                cycle;
int                st_done;

logic ld_mem_stall, st_mem_stall;
logic ld_room;   // fewer than 8 loads in flight; updated at each posedge
assign ld_mem_addr_ready = running && !ld_mem_stall && ld_room;
// MC join: address and data are accepted together.
assign st_mem_addr_ready = running && !st_mem_stall && st_mem_addr_valid && st_mem_data_valid;
assign st_mem_data_ready = st_mem_addr_ready;

// Transfers are decided at the negedge (everything is settled and nothing
// changes until the posedge) and committed at the posedge.
logic              f_ld_addr, f_ld_data, f_st;
logic [ADDR_W-1:0] f_ld_addr_v, f_st_addr_v;
logic [DATA_W-1:0] f_st_data_v;
always @(negedge clk) begin
    f_ld_addr   = ld_mem_addr_valid && ld_mem_addr_ready;
    f_ld_addr_v = ld_mem_addr;
    f_ld_data   = ld_mem_data_valid && ld_mem_data_ready;
    f_st        = st_mem_addr_valid && st_mem_addr_ready;
    f_st_addr_v = st_mem_addr;
    f_st_data_v = st_mem_data;
end

always @(posedge clk) begin
    #1;
    cycle++;
    if (rst || !running) begin
        ld_pending.delete(); ld_ready_at.delete();
        ld_mem_data_valid = 0; st_mem_exec_valid = 0;
        ld_mem_stall = 0; st_mem_stall = 0; ld_room = 1; ld_mem_starve = 0;
    end else begin
        if (f_ld_data) begin void'(ld_pending.pop_front()); void'(ld_ready_at.pop_front()); end
        // Read-first: a load accepted in the same cycle as a store write to
        // the same address sees the OLD value (which the checker must avoid).
        if (f_ld_addr) begin
            ld_pending.push_back(mem[f_ld_addr_v]);
            ld_ready_at.push_back(cycle + $urandom_range(0, LD_LAT));
        end
        // Store completion: registered one-cycle pulse after the write.
        st_mem_exec_valid = f_st;
        if (f_st) begin mem[f_st_addr_v] = f_st_data_v; st_done++; end
        ld_mem_data_valid = (ld_pending.size() > 0) && (cycle >= ld_ready_at[0]);
        ld_mem_data       = (ld_pending.size() > 0) ? ld_pending[0] : '0;
        ld_room           = (ld_pending.size() < 8);
        ld_mem_stall      = ld_mem_starve ? ($urandom_range(0, 7) != 0)
                                          : ($urandom_range(0, 3) == 0);
        st_mem_stall      = ($urandom_range(0, 3) == 0);
    end
end

task automatic reset();
    running = 0;
    rst = 1;
    ld_circ_addr = '0; ld_circ_addr_valid = 0; ld_circ_data_ready = 0;
    st_circ_addr = '0; st_circ_addr_valid = 0;
    st_circ_data = '0; st_circ_data_valid = 0;
    bb_valid_0 = 0; bb_valid_1 = 0;
    ticks(3);
    rst = 0;
    ticks(5);
endtask

initial begin
    for (int s = 0; s < N_SEEDS; s++) begin
        test_counter = s + 1;
        make_program(32'hC0DE + 17 * s);
        for (int a = 0; a < MEM_SIZE; a++) mem[a] = 32'hA000_0000 + a;
        ld_returned = 0; st_done = 0; ld_tok_done = 0; st_tok_done = 0;
        reset();
        running = 1;
        fork
            drive_bb_tokens();
            drive_ld_addr();
            drive_st_addr();
            drive_st_data();
            consume_ld_data();
        join
        // Let the last stores drain.
        while (st_done < K) tick();
        ticks(10);
        check(ld_returned, K, $sformatf("T%0d: loads returned", test_counter));
        check(st_done, K, $sformatf("T%0d: stores completed", test_counter));
        for (int a = 0; a < MEM_SIZE; a++)
            if (mem[a] !== golden_mem[a]) begin
                $display("FAIL [T%0d: mem[%0d]]: got 0x%0h, expected 0x%0h",
                         test_counter, a, mem[a], golden_mem[a]);
                fail_count++;
            end
        check(ld_empty, 1, $sformatf("T%0d: load queue drained", test_counter));
        check(st_empty, 1, $sformatf("T%0d: store queue drained", test_counter));
        $display("T%0d: program done at cycle %0d", test_counter, cycle);
    end
    $display("\n%0d test(s) failed.", fail_count);
    $finish;
end

// Watchdog: a deadlocked checker never drains the program.
initial begin
    #(N_SEEDS * K * 400 * 2 * CLK_HALF);
`ifdef DEBUG_DUMP
    // Optional: dump DUT internals (Verilog DUT only; see debug_dump.svh).
    `include "debug_dump.svh"
`endif
    $display("TIMEOUT (loads returned %0d, stores completed %0d, T%0d)",
             ld_returned, st_done, test_counter);
    $finish;
end

endmodule
