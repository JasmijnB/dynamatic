// Randomized reference-model stress test for SAME-BB dependency checkers.
//
// Include from a per-config wrapper that first defines
//   `define TB_MODULE  <name>_tb
//   `define DUT_MODULE <structure name>
// DUT: port 0 = load queue (q0), port 1 = store queue (q1), both in BB 0.
// The config decides which checkers exist (L->S WAR only, or both directions);
// define WAR_ONLY for the former, so the program carries no RAW hazard.
//
// The kernel executes a sequential program: iteration i issues load L_i from
// ld_addr[i], then store S_i of st_data[i] to st_addr[i]. Kernel-side address
// and data channels are driven with independent random gaps, in phases that
// let loads run far ahead of stores and vice versa -- the skew that drives a
// same-BB checker's disparity counter towards its limits. The memory model
// mirrors the ordering-network wrapper's MC interface:
//   - a load READS at its address handshake (read-first against a store
//     writing in the same cycle) and returns data in order after a random
//     latency;
//   - a store writes when address and data handshake together (MC join), and
//     its completion (mem_exec_valid) is a one-cycle pulse on the NEXT cycle,
//     like the wrapper's registered wresp_valid. It is never back-pressured.
// Every returned load value is checked against the sequential golden model,
// and so is the final memory. A deadlock trips the watchdog (TIMEOUT).

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
    .mem_exec_ready_o_q1_array_0_o  (st_mem_exec_ready)
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

// Per-phase random gaps (cycles) for the four kernel channels.
int gap_ld_addr [K];
int gap_st_addr [K];
int gap_st_data [K];
int gap_ld_ret  [K];

function automatic int phase_of(input int i);
    return (i * 4) / K;   // 4 phases of K/4 iterations
endfunction

task automatic make_program(input int seed);
    int unused;
    unused = $urandom(seed);
    for (int a = 0; a < MEM_SIZE; a++) golden_mem[a] = 32'hA000_0000 + a;
    for (int i = 0; i < K; i++) begin
        case (phase_of(i))
            // Phase 0: loads race far ahead of stores (disjoint addresses, so
            // nothing orders them): the L->S counter is pushed towards its min.
            0: begin
                prog_ld_addr[i] = $urandom_range(0, 3);
                prog_st_addr[i] = $urandom_range(8, 11);
                gap_ld_addr[i] = $urandom_range(0, 1);
                gap_st_addr[i] = $urandom_range(0, 14);
                gap_st_data[i] = $urandom_range(0, 14);
                gap_ld_ret[i]  = $urandom_range(0, 1);
            end
            // Phase 1: stores race ahead (their address AND data arrive fast,
            // loads slowly): the S->L counter (if present) towards its min.
            1: begin
                prog_ld_addr[i] = $urandom_range(0, 3);
                prog_st_addr[i] = $urandom_range(8, 11);
                gap_ld_addr[i] = $urandom_range(0, 14);
                gap_st_addr[i] = $urandom_range(0, 1);
                gap_st_data[i] = $urandom_range(0, 1);
                gap_ld_ret[i]  = $urandom_range(0, 6);
            end
            // Phase 2: heavily conflicting, mixed timing.
            2: begin
                prog_ld_addr[i] = $urandom_range(0, 3);
                prog_st_addr[i] = $urandom_range(0, 3);
                gap_ld_addr[i] = $urandom_range(0, 4);
                gap_st_addr[i] = $urandom_range(0, 4);
                gap_st_data[i] = $urandom_range(0, 4);
                gap_ld_ret[i]  = $urandom_range(0, 3);
            end
            // Phase 3: conflicting, with loads again allowed to run ahead.
            default: begin
                prog_ld_addr[i] = $urandom_range(0, 5);
                prog_st_addr[i] = $urandom_range(0, 5);
                gap_ld_addr[i] = $urandom_range(0, 1);
                gap_st_addr[i] = $urandom_range(0, 10);
                gap_st_data[i] = $urandom_range(0, 10);
                gap_ld_ret[i]  = $urandom_range(0, 1);
            end
        endcase
`ifdef STORE_STARVE
        // Every iteration like phase 0 with a far slower store side, so the
        // loads end up more than 128 accesses ahead: enough to wrap even an
        // 8-bit disparity counter.
        gap_ld_addr[i] = 0;
        gap_ld_ret[i]  = 0;
        gap_st_addr[i] = (i < K / 2) ? $urandom_range(20, 40) : 0;
        gap_st_data[i] = 0;
`endif
`ifdef WAR_ONLY
        // Only a L->S (WAR) checker exists, so the program must carry no RAW
        // hazard at all -- the compiler would have emitted an S->L edge
        // otherwise. `a[i] = a[i + k]`: load j reads j + k_j, store i writes
        // i; a load never reads an address a program-earlier store wrote
        // (that would need j + k_j = i < j), while every store still has to
        // wait for the loads that read its address first (k_j small).
        prog_ld_addr[i] = i + ((phase_of(i) == 2) ? $urandom_range(0, 1)
                                                  : $urandom_range(0, 3));
        prog_st_addr[i] = i;
        if (phase_of(i) == 2) begin
            gap_st_addr[i] = 0;
            gap_st_data[i] = 0;
        end
`endif
        prog_st_data[i] = {seed[7:0], 8'h00, i[15:0]} + 1;
        // Sequential semantics: L_i sees every S_j with j < i, and not S_i.
        exp_ld_data[i] = golden_mem[prog_ld_addr[i]];
        golden_mem[prog_st_addr[i]] = prog_st_data[i];
    end
endtask

// ===----------------------------------------------------------------------===
// Kernel-side drivers (drive after posedge, transfer on the next posedge
// when valid & ready hold at the negedge before it).
// ===----------------------------------------------------------------------===
logic running;

// Phase 2 starves the load memory port: loads sit allocated but unread while
// stores rush in, which is the window a missed WAR check writes through.
logic ld_mem_starve;
task automatic drive_ld_addr();
    for (int i = 0; i < K; i++) begin
        ld_mem_starve = (phase_of(i) == 2);
        ticks(gap_ld_addr[i]);
        ld_circ_addr = prog_ld_addr[i]; ld_circ_addr_valid = 1;
        @(negedge clk); while (!ld_circ_addr_ready) @(negedge clk);
        tick(); ld_circ_addr_valid = 0;
    end
endtask

task automatic drive_st_addr();
    for (int i = 0; i < K; i++) begin
        ticks(gap_st_addr[i]);
        st_circ_addr = prog_st_addr[i]; st_circ_addr_valid = 1;
        @(negedge clk); while (!st_circ_addr_ready) @(negedge clk);
        tick(); st_circ_addr_valid = 0;
    end
endtask

task automatic drive_st_data();
    for (int i = 0; i < K; i++) begin
        ticks(gap_st_data[i]);
        st_circ_data = prog_st_data[i]; st_circ_data_valid = 1;
        @(negedge clk); while (!st_circ_data_ready) @(negedge clk);
        tick(); st_circ_data_valid = 0;
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
    ticks(3);
    rst = 0;
    ticks(5);
endtask

initial begin
    for (int s = 0; s < N_SEEDS; s++) begin
        test_counter = s + 1;
        make_program(32'hC0DE + 17 * s);
        for (int a = 0; a < MEM_SIZE; a++) mem[a] = 32'hA000_0000 + a;
        ld_returned = 0; st_done = 0;
        reset();
        running = 1;
        fork
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
    $display("TIMEOUT (loads returned %0d, stores completed %0d, T%0d)",
             ld_returned, st_done, test_counter);
    $finish;
end

endmodule
