from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import *
from core_gen.configs import Configs
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, reduce_bin


class LoadQueue:
    def __init__(self, name: str, suffix: str, configs: Configs):
        """
        LoadQueue

        Models the top-level Load Queue (LQ) module with a single load port and
        single AXI read channel.

        This class integrates the core load queue logic without a store queue,
        dependency checking, dispatcher submodules, group allocator, or pipeline
        stages (pipe0/pipe1/pipeComp are not supported).

        Entries are allocated directly when a load address arrives from the kernel:
        on the port_addr handshake, the entry at q_tail is allocated and the
        address is written into it in a single cycle.

        Parameters:
            name    : Base name of the LQ. "<name saved in configs>_core"
            suffix  : Suffix appended to the name to form the VHDL entity name.
            configs : configuration generated from JSON

        Instance Variable:
            self.module_name = name + suffix : Entity and architecture identifier

        Example:
            lq_core = LoadQueue("config_0_core", '', configs)
            lq_core.generate(...)

        """

        self.name = name
        self.module_name = name + suffix
        self.configs = configs
        
    def generate_master_interface(self, em: Emitter, empty_o: Logic):
        #! If this is the lq master, then we need the following logic
        #! Define new interfaces needed by dynamatic
        memStart_ready = Logic(em, "memStart_ready", "o")
        memStart_valid = Logic(em, "memStart_valid", "i")
        ctrlEnd_ready = Logic(em, "ctrlEnd_ready", "o")
        ctrlEnd_valid = Logic(em, "ctrlEnd_valid", "i")
        memEnd_ready = Logic(em, "memEnd_ready", "i")
        memEnd_valid = Logic(em, "memEnd_valid", "o")

        #! Add extra signals required
        memStartReady = Logic(em, "memStartReady", "w", force_reg=True)
        memEndValid = Logic(em, "memEndValid", "w", force_reg=True)
        ctrlEndReady = Logic(em, "ctrlEndReady", "w", force_reg=True)
        temp_gen_mem = Logic(em, "TEMP_GEN_MEM", "w", force_reg=True)

        #! Define the needed logic
        em.add_comment(
            "This signal indicates that all mem. ops are completed and func. can return."
        )
        em.add_comment("LoadQueue can return iff all the following conditions are true:")
        em.add_comment("1. No more upcoming BBs containing memory accesses.")
        em.add_comment("2. The load queue is empty.")
        em.add_assignment(
            temp_gen_mem, ctrlEnd_valid & empty_o
        )

        em.add_comment("Define logic for the new interfaces needed by dynamatic")
        vhdl_str = ""
        # TODO: Add proper emitter functions in order to do this
        vhdl_str += "\tprocess (clk) is\n\tbegin\n"
        vhdl_str += "\t" * 2 + "if rising_edge(clk) then\n"
        vhdl_str += "\t" * 3 + "if rst = '1' then\n"
        vhdl_str += "\t" * 4 + "memStartReady <= '1';\n"
        vhdl_str += "\t" * 4 + "memEndValid <= '0';\n"
        vhdl_str += "\t" * 4 + "ctrlEndReady <= '0';\n"
        vhdl_str += "\t" * 3 + "else\n"
        vhdl_str += (
            "\t" * 4
            + "memStartReady <= (memEndValid and memEnd_ready_i) or ((not (memStart_valid_i and memStartReady)) and memStartReady);\n"
        )
        vhdl_str += "\t" * 4 + "memEndValid <= TEMP_GEN_MEM or memEndValid;\n"
        vhdl_str += (
            "\t" * 4
            + "ctrlEndReady <= (not (ctrlEnd_valid_i and ctrlEndReady)) and (TEMP_GEN_MEM or ctrlEndReady);\n"
        )
        vhdl_str += "\t" * 3 + "end if;\n"
        vhdl_str += "\t" * 2 + "end if;\n"
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

        #! Assign signals for the newly added ports
        em.add_comment("Update new memory interfaces")
        em.add_assignment(memStart_ready, memStartReady)
        em.add_assignment(ctrlEnd_ready, ctrlEndReady)
        em.add_assignment(memEnd_valid, memEndValid)


    def generate_load_queue(self, em: Emitter, lsq_submodules, path_rtl) -> None:
        """
        Generates the VHDL 'entity' and 'architecture' sections for a Load Queue.

        Appends the following to '<path_rtl>/<self.name>.vhd':
            1. 'entity <self.module_name>' declaration
            2. 'architecture arch of <self.module_name>' implementation

        Parameters:
            em              : an instance of the Emitter class used for code generation
            lsq_submodules  : unused (kept for API compatibility)
            path_rtl        : Output directory for VHDL files.

        """
        ######          IOs           ######
        # queue empty signal
        empty_o = Logic(em, "empty", "o")

        if self.configs.master:
            self.generate_master_interface(em, empty_o)

        # Single load port: connection "kernel -> LoadQueue"
        # Load address channel (addr, valid, ready) from kernel
        port_addr_i = LogicVec(em, "port_addr", "i", self.configs.addr_width)
        port_addr_valid_i = Logic(em, "port_addr_valid", "i")
        port_addr_ready_o = Logic(em, "port_addr_ready", "o")

        # Load data channel (data, valid, ready) to kernel
        port_data_o = LogicVec(em, "port_data", "o", self.configs.data_width)
        port_data_valid_o = Logic(em, "port_data_valid", "o")
        port_data_ready_i = Logic(em, "port_data_ready", "i")


        # Single AXI read channel: connection LoadQueue -> AXI
        rreq_valid_o = Logic(em, "rreq_valid", "o")
        rreq_ready_i = Logic(em, "rreq_ready", "i")
        rreq_id_o = LogicVec(em, "rreq_id", "o", self.configs.id_width)
        rreq_addr_o = LogicVec(em, "rreq_addr", "o", self.configs.addr_width)

        rresp_valid_i = Logic(em, "rresp_valid", "i")
        rresp_ready_o = Logic(em, "rresp_ready", "o")
        rresp_id_i = LogicVec(em, "rresp_id", "i", self.configs.id_width)
        rresp_data_i = LogicVec(em, "rresp_data", "i", self.configs.data_width)
        
        # Inputs from dependence checker
        allow_alloc_i = Logic(em, "allow_alloc", "i")
        allow_load_i = Logic(em, "allow_load", "i" )
        

        ######  Queue Registers ######
        # Load Queue Entries
        q_addr = LogicVecArray(
            em, "q_addr", "r", self.configs.num_entries, self.configs.addr_width
        )

        # Queue logic
        # there are three pointers: issue pointer, head pointer, tail pointer
        # issue pointer tracks the current issuing entry, which is the next entry to be issued
        # head pointer tracks the oldest entry in the queue, which is the next entry to be retired
        # tail pointer tracks the next free entry, which is the next entry to be allocated
        q_issue = LogicVec(em, "q_issue", "r", self.configs.q_addr_width)
        q_tail = LogicVec(em, "q_tail", "r", self.configs.q_addr_width)
        q_head = LogicVec(em, "q_head", "r", self.configs.q_addr_width)

        q_issue_next = LogicVec(em, "q_issue_next", "w", self.configs.q_addr_width)
        q_tail_next = LogicVec(em, "q_tail_next", "w", self.configs.q_addr_width)
        q_head_next = LogicVec(em, "q_head_next", "w", self.configs.q_addr_width)

        q_tail_oh = LogicVec(em, "q_tail_oh", "w", self.configs.num_entries)
        BitsToOH(em, q_tail_oh, q_tail)
        
        issue_en = Logic(em, "issue_en", "w")
        alloc_en = Logic(em, "alloc_en", "w")
        load_en = Logic(em, "load_en", "w")

        q_full = Logic(em, "q_full", "r")
        q_empty = Logic(em, "q_empty", "w")

        q_full_w_issue = Logic(em, "q_full_w_issue", "r")
        
        WrapAddConst(em, q_issue_next, q_issue, 1, self.configs.num_entries)
        WrapAddConst(em, q_head_next, q_head, 1, self.configs.num_entries)
        WrapAddConst(em, q_tail_next, q_tail, 1, self.configs.num_entries)
        
        # Update pointers
        em.add_assignment(q_tail, q_tail_next)
        em.add_assignment(q_head, q_head_next)
        em.add_assignment(q_issue, q_issue_next)

        q_tail.regInit(init=0, enable=alloc_en)  # advances by 1 on each allocation
        q_head.regInit(init=0, enable=load_en)
        q_issue.regInit(init=0, enable=issue_en)

        # queue is full
        em.add_assignment(q_full, (q_tail_next == q_issue) & alloc_en | (q_full & (q_tail == q_issue)))
        em.add_assignment(q_empty, (q_tail == q_head) & ~q_full)
        
        em.add_assignment(q_full_w_issue, (q_head_next == q_issue) | (q_full_w_issue & (q_head == q_issue)))
        
        # update load queue entries
        for i in range(0, self.configs.num_entries):
            em.add_assignment(q_addr[i], port_addr_i.when(Val(q_tail_oh, i) & alloc_en).else_(q_addr[i]))

        # empty queue 
        em.add_assignment(empty_o, q_empty)

        ###### Direct Allocation ######
        # Allocate one entry at q_tail whenever the kernel presents a valid load address
        # and the tail slot is free. The address is written into the entry in the same cycle.

        # Port ready when queue not full
        can_alloc = Logic(em, "can_alloc", "w")
        em.add_assignment(can_alloc, ~q_full & allow_alloc_i)

        em.add_assignment(port_addr_ready_o, can_alloc)
        em.add_assignment(alloc_en, port_addr_valid_i & can_alloc)
        ######   Register Initializations   ######

        q_addr.regInit()
        q_full.regInit(init=0)
        q_full_w_issue.regInit(init=0)

        ###### Load Scheduling ######
        em.add_assignment(load_en, ~q_empty & allow_load_i)
        # there are items left to issue
        # TODO: Check for situation when q_issue == q_head but it's bc the queue is full
        can_issue = Logic(em, "can_issue", "w")
        em.add_assignment(can_issue, (q_issue == q_head & load_en) | (q_issue != q_head) | q_full_w_issue)
        em.add_assignment(issue_en, can_issue & rreq_ready_i)

        # Read Request
        # ID is always equal to the configuration ID
        em.add_assignment(rreq_id_o, Val(self.configs.id_val))
        # Address is from the issuing entry in the queue
        MuxLookUp(em, rreq_addr_o, q_addr, q_issue)
        em.add_assignment(rreq_valid_o, can_issue)

        # Map the AXI read response channel to the load data read response channel to the kernel
        em.add_assignment(rresp_ready_o, port_data_ready_i)
        em.add_assignment(port_data_o, rresp_data_i)
        em.add_assignment(port_data_valid_o, rresp_valid_i & (rresp_id_i == Val(self.configs.id_val)))



        # Write to the file
        output_str = em.get_definition_str(self.module_name)
        with open(f"{path_rtl}/{self.name}.{em.get_file_suffix()}", "a") as file:
            file.write(output_str)

    def instantiate(self, **kwargs) -> str:
        """
        *Instantiation of LoadQueue is in lsq-generator.py.
        """
        pass
