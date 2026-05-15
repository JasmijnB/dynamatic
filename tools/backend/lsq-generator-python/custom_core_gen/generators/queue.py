from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import *
from core_gen.configs import Configs
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, reduce_bin
from custom_core_gen.generators.generator import Generator


class Queue(Generator):
    def __init__(self, name: str, suffix: str, configs: Configs):
        super().__init__(name, suffix, configs)


    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        self.ports.clear()
        is_store = self.configs.q_type == "store"

        # IOs - common
        empty_o           = self._add_port(Logic   (em, "empty",          "o"))
        circ_addr_i       = self._add_port(LogicVec(em, "circ_addr",      "i", self.configs.addr_width))
        circ_addr_valid_i = self._add_port(Logic   (em, "circ_addr_valid", "i"))
        circ_addr_ready_o = self._add_port(Logic   (em, "circ_addr_ready", "o"))

        mem_addr_valid_o  = self._add_port(Logic   (em, "mem_addr_valid",  "o"))
        mem_addr_ready_i  = self._add_port(Logic   (em, "mem_addr_ready",  "i"))
        mem_addr_o        = self._add_port(LogicVec(em, "mem_addr",        "o", self.configs.addr_width))

        # IOs - type-specific
        if is_store:
            circ_data_i       = self._add_port(LogicVec(em, "circ_data",       "i", self.configs.data_width))
            circ_data_valid_i = self._add_port(Logic   (em, "circ_data_valid", "i"))
            circ_data_ready_o = self._add_port(Logic   (em, "circ_data_ready", "o"))
            mem_data_o        = self._add_port(LogicVec(em, "mem_data",        "o", self.configs.data_width))
            mem_data_valid_o  = self._add_port(Logic   (em, "mem_data_valid",  "o"))
            mem_data_ready_i  = self._add_port(Logic   (em, "mem_data_ready",  "i"))
            mem_exec_valid_i  = self._add_port(Logic(em, "mem_exec_valid",  "i"))
            mem_exec_ready_o  = self._add_port(Logic(em, "mem_exec_ready",  "o"))
            if self.configs.st_resp:
                circ_exec_valid_o = self._add_port(Logic(em, "circ_exec_valid", "o"))
                circ_exec_ready_i = self._add_port(Logic(em, "circ_exec_ready", "i"))
        else:
            mem_data_valid_i  = self._add_port(Logic   (em, "mem_data_valid",  "i"))
            mem_data_ready_o  = self._add_port(Logic   (em, "mem_data_ready",  "o"))
            mem_data_i        = self._add_port(LogicVec(em, "mem_data",        "i", self.configs.data_width))
            circ_data_o       = self._add_port(LogicVec(em, "circ_data",       "o", self.configs.data_width))
            circ_data_valid_o = self._add_port(Logic   (em, "circ_data_valid", "o"))
            circ_data_ready_i = self._add_port(Logic   (em, "circ_data_ready", "i"))

        allow_access_i = self._add_port(Logic(em, "allow_access", "i"))

        # Shared pointer infrastructure
        (q_done, q_issue, q_tail, q_head,
         q_tail_oh,
         done_en, issue_en, load_en, alloc_en,
         q_full, q_empty, q_full_w_issue,
         q_issue_sel) = self._setup_pointers(em)

        # Address buffer (both types)
        q_addr = LogicVecArray(em, "q_addr", "r", self.configs.num_entries, self.configs.addr_width)
        for i in range(self.configs.num_entries):
            em.add_assignment(q_addr[i],
                circ_addr_i.when(Val(q_tail_oh, i) & alloc_en).else_(q_addr[i]))
        q_addr.regInit()

        em.add_assignment(empty_o, q_empty)

        # Allocation
        can_alloc = Logic(em, "can_alloc", "w")
        em.add_assignment(can_alloc, ~q_full)
        em.add_assignment(circ_addr_ready_o, can_alloc)
        em.add_assignment(alloc_en, circ_addr_valid_i & can_alloc)

        # Retirement
        em.add_assignment(load_en, ~q_empty & allow_access_i)

        # Issue
        can_issue = self._setup_can_issue(
            em, q_issue, q_head, q_full_w_issue, load_en, issue_en, mem_addr_ready_i)
        em.add_assignment(mem_addr_valid_o, can_issue)

        MuxLookUp(em, mem_addr_o, q_addr, q_issue_sel)

        if is_store:
            em.add_assignment(mem_data_o,        circ_data_i)
            em.add_assignment(mem_data_valid_o,  circ_data_valid_i)
            em.add_assignment(circ_data_ready_o, mem_data_ready_i)

            if self.configs.st_resp:
                em.add_assignment(circ_exec_valid_o, mem_exec_valid_i)
                em.add_assignment(mem_exec_ready_o,  circ_exec_ready_i)
                em.add_assignment(done_en, mem_exec_valid_i & circ_exec_ready_i)
            else:
                em.add_assignment(mem_exec_ready_o, Val(1))
                em.add_assignment(done_en, mem_exec_valid_i)
        else:
            em.add_assignment(mem_data_ready_o,   circ_data_ready_i)
            em.add_assignment(circ_data_o,         mem_data_i)
            em.add_assignment(circ_data_valid_o,   mem_data_valid_i)
            em.add_assignment(done_en, mem_data_valid_i & circ_data_ready_i)

        self._generate_observable_ports(em, q_addr, q_done, q_tail, q_head, done_en, alloc_en, load_en)

        if self.configs.master:
            self._generate_master_interface(em, q_empty)

        self._write_to_file(em, path_rtl, out_file)

    # ===----------------------------------------------------------------------===
    # Shared helpers
    # ===----------------------------------------------------------------------===

    def _generate_master_interface(self, em: Emitter, empty: Logic):
        memStart_ready = self._add_port(Logic(em, "memStart_ready", "o"))
        memStart_valid = self._add_port(Logic(em, "memStart_valid", "i"))
        ctrlEnd_ready  = self._add_port(Logic(em, "ctrlEnd_ready",  "o"))
        ctrlEnd_valid  = self._add_port(Logic(em, "ctrlEnd_valid",  "i"))
        memEnd_ready   = self._add_port(Logic(em, "memEnd_ready",   "i"))
        memEnd_valid   = self._add_port(Logic(em, "memEnd_valid",   "o"))

        memStartReady = Logic(em, "memStartReady", "w", force_reg=True)
        memEndValid   = Logic(em, "memEndValid",   "w", force_reg=True)
        ctrlEndReady  = Logic(em, "ctrlEndReady",  "w", force_reg=True)
        temp_gen_mem  = Logic(em, "TEMP_GEN_MEM",  "w")

        em.add_comment("This signal indicates that all mem. ops are completed and func. can return.")
        em.add_comment("Queue can return iff all the following conditions are true:")
        em.add_comment("1. No more upcoming BBs containing memory accesses.")
        em.add_comment("2. The queue is empty.")
        em.add_assignment(temp_gen_mem, ctrlEnd_valid & empty)

        em.add_comment("Define logic for the new interfaces needed by dynamatic")
        vhdl_str  = "\tprocess (clk) is\n\tbegin\n"
        vhdl_str += "\t\tif rising_edge(clk) then\n"
        vhdl_str += "\t\t\tif rst = '1' then\n"
        vhdl_str += "\t\t\t\tmemStartReady <= '1';\n"
        vhdl_str += "\t\t\t\tmemEndValid <= '0';\n"
        vhdl_str += "\t\t\t\tctrlEndReady <= '0';\n"
        vhdl_str += "\t\t\telse\n"
        vhdl_str += "\t\t\t\tmemStartReady <= (memEndValid and memEnd_ready_i) or ((not (memStart_valid_i and memStartReady)) and memStartReady);\n"
        vhdl_str += "\t\t\t\tmemEndValid <= TEMP_GEN_MEM or memEndValid;\n"
        vhdl_str += "\t\t\t\tctrlEndReady <= (not (ctrlEnd_valid_i and ctrlEndReady)) and (TEMP_GEN_MEM or ctrlEndReady);\n"
        vhdl_str += "\t\t\tend if;\n"
        vhdl_str += "\t\tend if;\n"
        vhdl_str += "\tend process;\n\n"

        verilog_str = """
always @(posedge clk) begin
    if (rst) begin
        memStartReady <= 1'b1;
        memEndValid   <= 1'b0;
        ctrlEndReady  <= 1'b0;
    end
    else begin
        memStartReady <= (memEndValid && memEnd_ready_i) ||
                         ((!(memStart_valid_i && memStartReady)) && memStartReady);
        memEndValid   <= TEMP_GEN_MEM || memEndValid;
        ctrlEndReady  <= (!(ctrlEnd_valid_i && ctrlEndReady)) &&
                         (TEMP_GEN_MEM || ctrlEndReady);
    end
end
        """
        em.add_custom_statement(CustomStatement(vhdl_str, verilog_str))

        em.add_comment("Update new memory interfaces")
        em.add_assignment(memStart_ready, memStartReady)
        em.add_assignment(ctrlEnd_ready,  ctrlEndReady)
        em.add_assignment(memEnd_valid,   memEndValid)

    def _setup_pointers(self, em: Emitter):
        """
        Declare the circular-buffer pointers (done/issue/tail/head), their
        look-ahead wires, the tail one-hot, and the full/empty status signals.

        When num_entries is a power of 2, uses the bit-flip trick: pointers are
        q_addr_width+1 bits wide and full/empty are purely combinatorial wires.
        The MSB acts as a generation bit so that equal pointers (all bits) means
        empty, and equal lower bits with differing MSBs means full.

        alloc_en, issue_en, and load_en (head-retirement enable) are declared as
        wires; the caller is responsible for assigning logic to each of them.

        Returns:
            q_done, q_issue, q_tail, q_head — pointer registers
            q_tail_oh                       — one-hot of q_tail index
            done_en, issue_en, load_en, alloc_en — enable wires (to be driven by caller)
            q_full, q_empty, q_full_w_issue — status signals
            q_issue_sel                     — lower bits of q_issue for MuxLookUp
        """
        n = self.configs.q_addr_width
        ptr_width = n + 1

        q_done  = LogicVec(em, "q_done",  "r", ptr_width)
        q_issue = LogicVec(em, "q_issue", "r", ptr_width)
        q_tail  = LogicVec(em, "q_tail",  "r", ptr_width)
        q_head  = LogicVec(em, "q_head",  "r", ptr_width)

        q_done_next  = LogicVec(em, "q_done_next",  "w", ptr_width)
        q_issue_next = LogicVec(em, "q_issue_next", "w", ptr_width)
        q_tail_next  = LogicVec(em, "q_tail_next",  "w", ptr_width)
        q_head_next  = LogicVec(em, "q_head_next",  "w", ptr_width)

        q_tail_oh = LogicVec(em, "q_tail_oh", "w", self.configs.num_entries)

        # BitsToOH and MuxLookUp need the physical index (lower n bits only)
        q_tail_idx = LogicVec(em, "q_tail_idx", "w", n)
        em.add_assignment(q_tail_idx, Val(em.slice_var(q_tail.getNameRead(), n - 1, 0)))
        BitsToOH(em, q_tail_oh, q_tail_idx)

        q_issue_sel = LogicVec(em, "q_issue_sel", "w", n)
        em.add_assignment(q_issue_sel, Val(em.slice_var(q_issue.getNameRead(), n - 1, 0)))

        done_en  = Logic(em, "done_en",  "w")
        issue_en = Logic(em, "issue_en", "w")
        alloc_en = Logic(em, "alloc_en", "w")
        load_en  = Logic(em, "load_en",  "w")

        q_full         = Logic(em, "q_full",         "w")
        q_empty        = Logic(em, "q_empty",        "w")
        q_full_w_issue = Logic(em, "q_full_w_issue", "w")

        WrapAddConst(em, q_issue_next, q_issue, 1, self.configs.num_entries)
        WrapAddConst(em, q_done_next,  q_done,  1, self.configs.num_entries)
        WrapAddConst(em, q_tail_next,  q_tail,  1, self.configs.num_entries)
        WrapAddConst(em, q_head_next,  q_head,  1, self.configs.num_entries)

        em.add_assignment(q_done,  q_done_next)
        em.add_assignment(q_tail,  q_tail_next)
        em.add_assignment(q_issue, q_issue_next)
        em.add_assignment(q_head,  q_head_next)

        q_done .regInit(init=0, enable=done_en)
        q_issue.regInit(init=0, enable=issue_en)
        q_tail .regInit(init=0, enable=alloc_en)
        q_head .regInit(init=0, enable=load_en)

        # Full between tail and done: lower bits match but generation bits differ
        tail_msb = Val(em.index_var(q_tail.getNameRead(), n))
        done_msb = Val(em.index_var(q_done.getNameRead(), n))
        tail_low = Val(em.slice_var(q_tail.getNameRead(), n - 1, 0))
        done_low = Val(em.slice_var(q_done.getNameRead(), n - 1, 0))
        em.add_assignment(q_full, (tail_msb != done_msb) & (tail_low == done_low))

        # Empty: all bits (including generation) equal
        em.add_assignment(q_empty, q_tail == q_head)

        # Full between issue and head: same lower bits, different generation
        issue_msb = Val(em.index_var(q_issue.getNameRead(), n))
        head_msb  = Val(em.index_var(q_head.getNameRead(),  n))
        issue_low = Val(em.slice_var(q_issue.getNameRead(), n - 1, 0))
        head_low  = Val(em.slice_var(q_head.getNameRead(),  n - 1, 0))
        em.add_assignment(q_full_w_issue, (issue_msb != head_msb) & (issue_low == head_low))

        return (q_done, q_issue, q_tail, q_head,
                q_tail_oh,
                done_en, issue_en, load_en, alloc_en,
                q_full, q_empty, q_full_w_issue,
                q_issue_sel)

    def _setup_can_issue(self, em: Emitter, q_issue, q_head, q_full_w_issue,
                         load_en, issue_en, axi_ready_i):
        """
        Assign the shared issue-readiness logic and connect issue_en.
        Returns can_issue.
        """
        can_issue = Logic(em, "can_issue", "w")
        em.add_assignment(can_issue,
            (q_issue == q_head & load_en) | (q_issue != q_head) | q_full_w_issue)
        em.add_assignment(issue_en, can_issue & axi_ready_i)
        return can_issue

    def _generate_observable_ports(self, em: Emitter, q_addr,
                                    q_done, q_tail, q_head,
                                    done_en, alloc_en, load_en) -> None:
        """Add output ports that expose internal queue state for observation."""
        n = self.configs.q_addr_width
        ptr_width = n + 1

        q_addr_out_o = self._add_port(
            LogicVecArray(em, "q_addr", "o", self.configs.num_entries, self.configs.addr_width))
        for i in range(self.configs.num_entries):
            em.add_assignment(q_addr_out_o[i], q_addr[i])

        done_ptr_o  = self._add_port(LogicVec(em, "done_ptr",  "o", n))
        alloc_ptr_o = self._add_port(LogicVec(em, "alloc_ptr", "o", n))
        head_ptr_o  = self._add_port(LogicVec(em, "head_ptr",  "o", n))

        em.add_assignment(done_ptr_o,  Val(em.slice_var(q_done.getNameRead(), n - 1, 0)))
        em.add_assignment(alloc_ptr_o, Val(em.slice_var(q_tail.getNameRead(), n - 1, 0)))
        em.add_assignment(head_ptr_o,  Val(em.slice_var(q_head.getNameRead(), n - 1, 0)))

        # Number of entries from done to tail (allocated but not yet complete).
        length_o    = self._add_port(LogicVec(em, "length",    "o", ptr_width))
        em.add_assignment(length_o, q_tail - q_done)

        done_en_o   = self._add_port(Logic(em, "done_en",   "o"))
        access_en_o = self._add_port(Logic(em, "access_en", "o"))

        em.add_assignment(done_en_o,   done_en)
        em.add_assignment(access_en_o, load_en)

    def _write_to_file(self, em: Emitter, path_rtl: str, out_file: str = None):
        output_str = em.get_definition_str(self.module_name)
        path = out_file if out_file is not None else f"{path_rtl}/{self.name}.{em.get_file_suffix()}"
        with open(path, "a") as file:
            file.write(output_str)

