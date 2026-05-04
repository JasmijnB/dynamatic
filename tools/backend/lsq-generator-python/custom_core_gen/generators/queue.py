from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import *
from core_gen.configs import Configs
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, reduce_bin
from core_gen.utils import isPow2


class Queue:
    def __init__(self, name: str, suffix: str, configs: Configs):
        self.name = name
        self.module_name = name + suffix
        self.configs = configs

    def generate(self, em: Emitter, lsq_submodules, path_rtl) -> None:
        if self.configs.q_type == "load":
            self.generate_load_queue(em, lsq_submodules, path_rtl)
        elif self.configs.q_type == "store":
            self.generate_store_queue(em, lsq_submodules, path_rtl)

    # ===----------------------------------------------------------------------===
    # Shared helpers
    # ===----------------------------------------------------------------------===

    def _generate_master_interface(self, em: Emitter, empty: Logic):
        memStart_ready = Logic(em, "memStart_ready", "o")
        memStart_valid = Logic(em, "memStart_valid", "i")
        ctrlEnd_ready  = Logic(em, "ctrlEnd_ready",  "o")
        ctrlEnd_valid  = Logic(em, "ctrlEnd_valid",  "i")
        memEnd_ready   = Logic(em, "memEnd_ready",   "i")
        memEnd_valid   = Logic(em, "memEnd_valid",   "o")

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
        use_bit_flip = isPow2(self.configs.num_entries)
        ptr_width = n + 1 if use_bit_flip else n

        q_done  = LogicVec(em, "q_done",  "r", ptr_width)
        q_issue = LogicVec(em, "q_issue", "r", ptr_width)
        q_tail  = LogicVec(em, "q_tail",  "r", ptr_width)
        q_head  = LogicVec(em, "q_head",  "r", ptr_width)

        q_done_next  = LogicVec(em, "q_done_next",  "w", ptr_width)
        q_issue_next = LogicVec(em, "q_issue_next", "w", ptr_width)
        q_tail_next  = LogicVec(em, "q_tail_next",  "w", ptr_width)
        q_head_next  = LogicVec(em, "q_head_next",  "w", ptr_width)

        q_tail_oh = LogicVec(em, "q_tail_oh", "w", self.configs.num_entries)

        if use_bit_flip:
            # BitsToOH and MuxLookUp need the physical index (lower n bits only)
            q_tail_idx = LogicVec(em, "q_tail_idx", "w", n)
            em.add_assignment(q_tail_idx, Val(em.slice_var(q_tail.getNameRead(), n - 1, 0)))
            BitsToOH(em, q_tail_oh, q_tail_idx)

            q_issue_sel = LogicVec(em, "q_issue_sel", "w", n)
            em.add_assignment(q_issue_sel, Val(em.slice_var(q_issue.getNameRead(), n - 1, 0)))
        else:
            BitsToOH(em, q_tail_oh, q_tail)
            q_issue_sel = q_issue

        done_en  = Logic(em, "done_en",  "w")
        issue_en = Logic(em, "issue_en", "w")
        alloc_en = Logic(em, "alloc_en", "w")
        load_en  = Logic(em, "load_en",  "w")

        q_full         = Logic(em, "q_full",         "w" if use_bit_flip else "r")
        q_empty        = Logic(em, "q_empty",        "w")
        q_full_w_issue = Logic(em, "q_full_w_issue", "w" if use_bit_flip else "r")

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

        if use_bit_flip:
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
        else:
            em.add_assignment(q_full, (q_tail_next == q_done) & alloc_en | (q_full & (q_tail == q_done)))
            em.add_assignment(q_empty, (q_tail == q_head) & ~q_full)
            em.add_assignment(q_full_w_issue, (q_head_next == q_issue) | (q_full_w_issue & (q_head == q_issue)))

            q_full        .regInit(init=0)
            q_full_w_issue.regInit(init=0)

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

    def _write_to_file(self, em: Emitter, path_rtl: str):
        output_str = em.get_definition_str(self.module_name)
        with open(f"{path_rtl}/{self.name}.{em.get_file_suffix()}", "a") as file:
            file.write(output_str)

    # ===----------------------------------------------------------------------===
    # Load queue
    # ===----------------------------------------------------------------------===

    def generate_load_queue(self, em: Emitter, lsq_submodules, path_rtl) -> None:
        # IOs
        empty_o = Logic(em, "empty", "o")

        port_addr_i       = LogicVec(em, "port_addr",       "i", self.configs.addr_width)
        port_addr_valid_i = Logic   (em, "port_addr_valid", "i")
        port_addr_ready_o = Logic   (em, "port_addr_ready", "o")

        port_data_o       = LogicVec(em, "port_data",       "o", self.configs.data_width)
        port_data_valid_o = Logic   (em, "port_data_valid", "o")
        port_data_ready_i = Logic   (em, "port_data_ready", "i")

        rreq_valid_o = Logic   (em, "rreq_valid", "o")
        rreq_ready_i = Logic   (em, "rreq_ready", "i")
        rreq_id_o    = LogicVec(em, "rreq_id",    "o", self.configs.id_width)
        rreq_addr_o  = LogicVec(em, "rreq_addr",  "o", self.configs.addr_width)

        rresp_valid_i = Logic   (em, "rresp_valid", "i")
        rresp_ready_o = Logic   (em, "rresp_ready", "o")
        rresp_id_i    = LogicVec(em, "rresp_id",    "i", self.configs.id_width)
        rresp_data_i  = LogicVec(em, "rresp_data",  "i", self.configs.data_width)

        allow_alloc_i = Logic(em, "allow_alloc", "i")
        allow_access_i  = Logic(em, "allow_access",  "i")

        # Shared pointer infrastructure
        (q_done, q_issue, q_tail, q_head,
         q_tail_oh,
         done_en, issue_en, load_en, alloc_en,
         q_full, q_empty, q_full_w_issue,
         q_issue_sel) = self._setup_pointers(em)

        # Queue entries
        q_addr = LogicVecArray(em, "q_addr", "r", self.configs.num_entries, self.configs.addr_width)
        for i in range(self.configs.num_entries):
            em.add_assignment(q_addr[i],
                port_addr_i.when(Val(q_tail_oh, i) & alloc_en).else_(q_addr[i]))
        q_addr.regInit()

        em.add_assignment(empty_o, q_empty)

        # Allocation
        can_alloc = Logic(em, "can_alloc", "w")
        em.add_assignment(can_alloc, ~q_full & allow_alloc_i)
        em.add_assignment(port_addr_ready_o, can_alloc)
        em.add_assignment(alloc_en, port_addr_valid_i & can_alloc)

        # Retirement: head advances whenever the queue is non-empty and loads are allowed
        em.add_assignment(load_en, ~q_empty & allow_access_i)

        # Issue
        can_issue = self._setup_can_issue(
            em, q_issue, q_head, q_full_w_issue, load_en, issue_en, rreq_ready_i)

        # AXI read request
        em.add_assignment(rreq_id_o,    Val(self.configs.id_val))
        em.add_assignment(rreq_valid_o, can_issue)
        MuxLookUp(em, rreq_addr_o, q_addr, q_issue_sel)

        # AXI read response → kernel (pure combinatorial passthrough)
        em.add_assignment(rresp_ready_o,    port_data_ready_i)
        em.add_assignment(port_data_o,      rresp_data_i)
        em.add_assignment(port_data_valid_o,
            rresp_valid_i & (rresp_id_i == Val(self.configs.id_val)))
        em.add_assignment(done_en, rresp_valid_i & (rresp_id_i == Val(self.configs.id_val) & port_data_ready_i))

        if self.configs.master:
            self._generate_master_interface(em, q_empty)

        self._write_to_file(em, path_rtl)

    # ===----------------------------------------------------------------------===
    # Store queue
    # ===----------------------------------------------------------------------===

    def generate_store_queue(self, em: Emitter, lsq_submodules, path_rtl) -> None:
        # IOs
        empty_o = Logic(em, "empty", "o")

        port_addr_i       = LogicVec(em, "port_addr",       "i", self.configs.addr_width)
        port_addr_valid_i = Logic   (em, "port_addr_valid", "i")
        port_addr_ready_o = Logic   (em, "port_addr_ready", "o")

        port_data_i       = LogicVec(em, "port_data",       "i", self.configs.data_width)
        port_data_valid_i = Logic   (em, "port_data_valid", "i")
        port_data_ready_o = Logic   (em, "port_data_ready", "o")

        if self.configs.st_resp:
            port_exec_valid_o = Logic(em, "port_exec_valid", "o")
            port_exec_ready_i = Logic(em, "port_exec_ready", "i")

        wreq_valid_o = Logic   (em, "wreq_valid", "o")
        wreq_ready_i = Logic   (em, "wreq_ready", "i")
        wreq_id_o    = LogicVec(em, "wreq_id",    "o", self.configs.id_width)
        wreq_addr_o  = LogicVec(em, "wreq_addr",  "o", self.configs.addr_width)
        wreq_data_o  = LogicVec(em, "wreq_data",  "o", self.configs.data_width)

        wresp_valid_i = Logic   (em, "wresp_valid", "i")
        wresp_ready_o = Logic   (em, "wresp_ready", "o")
        wresp_id_i    = LogicVec(em, "wresp_id",    "i", self.configs.id_width)

        allow_alloc_i = Logic(em, "allow_alloc", "i")
        allow_access_i = Logic(em, "allow_access", "i")

        # Shared pointer infrastructure
        (q_done, q_issue, q_tail, q_head,
         q_tail_oh,
         done_en, issue_en, load_en, alloc_en,
         q_full, q_empty, q_full_w_issue,
         q_issue_sel) = self._setup_pointers(em)

        # Queue entries
        q_addr = LogicVecArray(em, "q_addr", "r", self.configs.num_entries, self.configs.addr_width)
        q_data = LogicVecArray(em, "q_data", "r", self.configs.num_entries, self.configs.data_width)

        # Staging registers: track whether the current tail slot already has addr/data
        tail_addr_valid = Logic(em, "tail_addr_valid", "r")
        tail_data_valid = Logic(em, "tail_data_valid", "r")

        alloc_addr_en = Logic(em, "alloc_addr_en", "w")
        alloc_data_en = Logic(em, "alloc_data_en", "w")

        # Per-entry writes: update on the cycle addr/data arrive for the tail slot
        for i in range(self.configs.num_entries):
            em.add_assignment(q_addr[i],
                port_addr_i.when(Val(q_tail_oh, i) & alloc_addr_en).else_(q_addr[i]))
            em.add_assignment(q_data[i],
                port_data_i.when(Val(q_tail_oh, i) & alloc_data_en).else_(q_data[i]))
        q_addr.regInit()
        q_data.regInit()

        # Staging: set when addr/data arrive, cleared when tail advances (alloc_en)
        em.add_assignment(tail_addr_valid, (alloc_addr_en | tail_addr_valid) & ~alloc_en)
        em.add_assignment(tail_data_valid, (alloc_data_en | tail_data_valid) & ~alloc_en)
        tail_addr_valid.regInit(init=0)
        tail_data_valid.regInit(init=0)

        em.add_assignment(empty_o, q_empty)

        # Allocation: addr and data may arrive independently; tail advances once both are valid
        can_alloc_addr = Logic(em, "can_alloc_addr", "w")
        can_alloc_data = Logic(em, "can_alloc_data", "w")
        em.add_assignment(can_alloc_addr, allow_alloc_i & ~tail_addr_valid & ~q_full)
        em.add_assignment(can_alloc_data, allow_alloc_i & ~tail_data_valid & ~q_full)
        em.add_assignment(port_addr_ready_o, can_alloc_addr)
        em.add_assignment(port_data_ready_o, can_alloc_data)
        em.add_assignment(alloc_addr_en, port_addr_valid_i & can_alloc_addr)
        em.add_assignment(alloc_data_en, port_data_valid_i & can_alloc_data)
        em.add_assignment(alloc_en,
            (tail_addr_valid | alloc_addr_en) & (tail_data_valid | alloc_data_en))

        # Retirement: head advances when queue is non-empty and stores are allowed
        em.add_assignment(load_en, ~q_empty & allow_access_i)

        # Issue
        can_issue = self._setup_can_issue(
            em, q_issue, q_head, q_full_w_issue, load_en, issue_en, wreq_ready_i)

        # AXI write request
        em.add_assignment(wreq_id_o,    Val(self.configs.id_val))
        em.add_assignment(wreq_valid_o, can_issue)
        MuxLookUp(em, wreq_addr_o, q_addr, q_issue_sel)
        MuxLookUp(em, wreq_data_o, q_data, q_issue_sel)

        # AXI write response
        if self.configs.st_resp:
            em.add_assignment(port_exec_valid_o,
                wresp_valid_i & (wresp_id_i == Val(self.configs.id_val)))
            em.add_assignment(wresp_ready_o, port_exec_ready_i)
            em.add_assignment(done_en, wresp_valid_i & (wresp_id_i == Val(self.configs.id_val) & port_exec_ready_i))
        else:
            em.add_assignment(wresp_ready_o, Val(1))
            em.add_assignment(done_en, wresp_valid_i & (wresp_id_i == Val(self.configs.id_val)))

        if self.configs.master:
            self._generate_master_interface(em, q_empty)

        self._write_to_file(em, path_rtl)

    def instantiate(
        self,
        em: Emitter,
        empty_o: Logic,
        port_addr_i: LogicVec,
        port_addr_valid_i: Logic,
        port_addr_ready_o: Logic,
        allow_alloc_i: Logic,
        # Load-specific
        port_data_o: LogicVec = None,
        port_data_valid_o: Logic = None,
        port_data_ready_i: Logic = None,
        rreq_valid_o: Logic = None,
        rreq_ready_i: Logic = None,
        rreq_id_o: LogicVec = None,
        rreq_addr_o: LogicVec = None,
        rresp_valid_i: Logic = None,
        rresp_ready_o: Logic = None,
        rresp_id_i: LogicVec = None,
        rresp_data_i: LogicVec = None,
        allow_access_i: Logic = None,
        # Store-specific
        port_data_i: LogicVec = None,
        port_data_valid_i: Logic = None,
        port_data_ready_o: Logic = None,
        wreq_valid_o: Logic = None,
        wreq_ready_i: Logic = None,
        wreq_id_o: LogicVec = None,
        wreq_addr_o: LogicVec = None,
        wreq_data_o: LogicVec = None,
        wresp_valid_i: Logic = None,
        wresp_ready_o: Logic = None,
        wresp_id_i: LogicVec = None,
        port_exec_valid_o: Logic = None,
        port_exec_ready_i: Logic = None,
        # Master interface (optional)
        memStart_ready_o: Logic = None,
        memStart_valid_i: Logic = None,
        ctrlEnd_ready_o: Logic = None,
        ctrlEnd_valid_i: Logic = None,
        memEnd_ready_i: Logic = None,
        memEnd_valid_o: Logic = None,
    ) -> Emitter:
        """
        Queue Instantiation

        Creates the port mapping for the Queue entity (load or store variant).
        The queue type is determined by self.configs.q_type.

        Parameters:
            em                  : Emitter for code generation
            empty_o             : Output indicating the queue is empty
            port_addr_i         : Input address from the kernel port
            port_addr_valid_i   : Valid signal for the incoming address
            port_addr_ready_o   : Ready signal back to the kernel port
            allow_alloc_i       : Permission to allocate a new entry

            Load-specific:
            port_data_o         : Output data to the kernel port
            port_data_valid_o   : Valid signal for the outgoing data
            port_data_ready_i   : Ready signal from the kernel port
            rreq_valid_o        : AXI read request valid
            rreq_ready_i        : AXI read request ready
            rreq_id_o           : AXI read request ID
            rreq_addr_o         : AXI read request address
            rresp_valid_i       : AXI read response valid
            rresp_ready_o       : AXI read response ready
            rresp_id_i          : AXI read response ID
            rresp_data_i        : AXI read response data
            allow_access_i        : Permission to retire (advance head) a load entry

            Store-specific:
            port_data_i         : Input data from the kernel port
            port_data_valid_i   : Valid signal for the incoming data
            port_data_ready_o   : Ready signal back to the kernel port
            wreq_valid_o        : AXI write request valid
            wreq_ready_i        : AXI write request ready
            wreq_id_o           : AXI write request ID
            wreq_addr_o         : AXI write request address
            wreq_data_o         : AXI write request data
            wresp_valid_i       : AXI write response valid
            wresp_ready_o       : AXI write response ready
            wresp_id_i          : AXI write response ID
            allow_access_i       : Permission to retire (advance head) a store entry
            port_exec_valid_o   : Store execution acknowledgement valid (if st_resp)
            port_exec_ready_i   : Store execution acknowledgement ready (if st_resp)

            Master interface (optional, only when self.configs.master is True):
            memStart_ready_o    : Memory start handshake ready
            memStart_valid_i    : Memory start handshake valid
            ctrlEnd_ready_o     : Control end handshake ready
            ctrlEnd_valid_i     : Control end handshake valid
            memEnd_ready_i      : Memory end handshake ready
            memEnd_valid_o      : Memory end handshake valid

        Returns:
            The emitter after the instantiation has been appended.
        """
        em.start_instantiation(self.module_name)

        em.add_map("rst", "rst")
        em.add_map("clk", "clk")

        em.add_map("empty_o",             empty_o.getNameWrite())
        em.add_map("port_addr_i",         port_addr_i.getNameRead())
        em.add_map("port_addr_valid_i",   port_addr_valid_i.getNameRead())
        em.add_map("port_addr_ready_o",   port_addr_ready_o.getNameWrite())
        em.add_map("allow_alloc_i",       allow_alloc_i.getNameRead())

        if self.configs.q_type == "load":
            em.add_map("port_data_o",       port_data_o.getNameWrite())
            em.add_map("port_data_valid_o", port_data_valid_o.getNameWrite())
            em.add_map("port_data_ready_i", port_data_ready_i.getNameRead())
            em.add_map("rreq_valid_o",      rreq_valid_o.getNameWrite())
            em.add_map("rreq_ready_i",      rreq_ready_i.getNameRead())
            em.add_map("rreq_id_o",         rreq_id_o.getNameWrite())
            em.add_map("rreq_addr_o",       rreq_addr_o.getNameWrite())
            em.add_map("rresp_valid_i",     rresp_valid_i.getNameRead())
            em.add_map("rresp_ready_o",     rresp_ready_o.getNameWrite())
            em.add_map("rresp_id_i",        rresp_id_i.getNameRead())
            em.add_map("rresp_data_i",      rresp_data_i.getNameRead())
            em.add_map("allow_access_i",      allow_access_i.getNameRead())
        elif self.configs.q_type == "store":
            em.add_map("port_data_i",       port_data_i.getNameRead())
            em.add_map("port_data_valid_i", port_data_valid_i.getNameRead())
            em.add_map("port_data_ready_o", port_data_ready_o.getNameWrite())
            em.add_map("wreq_valid_o",      wreq_valid_o.getNameWrite())
            em.add_map("wreq_ready_i",      wreq_ready_i.getNameRead())
            em.add_map("wreq_id_o",         wreq_id_o.getNameWrite())
            em.add_map("wreq_addr_o",       wreq_addr_o.getNameWrite())
            em.add_map("wreq_data_o",       wreq_data_o.getNameWrite())
            em.add_map("wresp_valid_i",     wresp_valid_i.getNameRead())
            em.add_map("wresp_ready_o",     wresp_ready_o.getNameWrite())
            em.add_map("wresp_id_i",        wresp_id_i.getNameRead())
            em.add_map("allow_access_i",     allow_access_i.getNameRead())
            if self.configs.st_resp:
                em.add_map("port_exec_valid_o", port_exec_valid_o.getNameWrite())
                em.add_map("port_exec_ready_i", port_exec_ready_i.getNameRead())

        if self.configs.master:
            em.add_map("memStart_ready_o", memStart_ready_o.getNameWrite())
            em.add_map("memStart_valid_i", memStart_valid_i.getNameRead())
            em.add_map("ctrlEnd_ready_o",  ctrlEnd_ready_o.getNameWrite())
            em.add_map("ctrlEnd_valid_i",  ctrlEnd_valid_i.getNameRead())
            em.add_map("memEnd_ready_i",   memEnd_ready_i.getNameRead())
            em.add_map("memEnd_valid_o",   memEnd_valid_o.getNameWrite())

        em.complete_instantiation()
        return em
