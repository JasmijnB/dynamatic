from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import *
from core_gen.configs import Configs
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, reduce_bin

import core_gen.generators.lsq_submodule_wrapper as lsq_submodule_wrapper


class LSQ:
    def __init__(self, name: str, suffix: str, configs: Configs):
        """
        LSQ

        Models the top-level Load-Store Queue (LSQ) module.

        This class integrates all necessary sub-components to form a complete LSQ.
        It is responsible for generating the top-level entity that wires
        together the Group Allocator, various Port/Queue Dispatchers, and the core
        queue logic with dependency checking.

        Parameters:
            name    : Base name of the LSQ. "<name saved in configs>_core"
            suffix  : Suffix appended to the name to form the entity name.
                      Since LSQ is the top module, you do not need to add any suffix.
            configs : configuration generated from JSON


        Instance Variable:
            self.module_name = name + suffix : Entity and architecture identifier


        Example:
            lsq_core = LSQ("config_0_core", '', configs)

            # You can later generate entity and architecture by
            #     lsq_core.generate(...)

            # Instantiation of the LSQ module does not use this class.
            # It considers more conditions, and it is done in lsq-generator.py.

        """

        self.name = name
        self.module_name = name + suffix
        self.configs = configs

    def generate(self, em: Emitter, lsq_submodules, path_rtl) -> None:
        """
        Generates the 'entity' and 'architecture' sections for an LSQ.

        This function appends the following to the file '<path_rtl>/<self.name>.vhd:
            1. 'entity <self.module_name>' declaration
            2. 'architecture arch of <self.module_name>' implementation

        The generated code also instantitates:
            - Group Allocator
            - Port-to-Queue Dispatcher
                - Load Address Port Dispatcher
                - Store Address Port Dispatcher
                - Store Data Port Dispatcher
            - Queue-to-Port Dispatcher
                - Load Data Port Dispatcher
                - (Optionally) Store Backward Port Dispatcher

        Parameters:
            em              : an instance of the Emitter class used for code generation
            lsq_submodules  : A collection of objects representing submodules whose entity
                              definitions are already generated. This parameter is used to
                              generate their port map instantiations.
            path_rtl        : Output directory for files.

        Output:
            Appends the 'entity' and 'architecture' definitions
            to the .vhd file at <path_rtl>/<self.name>.vhd.
            Entity and architecture use the identifier: <self.module_name>

        Example:
            lsq_core.generate(lsq_submodules, path_rtl)

        """
        ###### LSQ Architecture ######
        ######        IOs       ######

        # group initialzation signals
        group_init_valid_i = LogicArray(
            em, 'group_init_valid', 'i', self.configs.numGroups
        )
        group_init_ready_o = LogicArray(
            em, 'group_init_ready', 'o', self.configs.numGroups
        )

        # Memory access ports, i.e., the connection "kernel -> LSQ"
        # Load address channel (addr, valid, ready) from kernel, contains signals:
        ldp_addr_i = LogicVecArray(
            em, 'ldp_addr', 'i', self.configs.numLdPorts, self.configs.addrW
        )
        ldp_addr_valid_i = LogicArray(
            em, 'ldp_addr_valid', 'i', self.configs.numLdPorts
        )
        ldp_addr_ready_o = LogicArray(
            em, 'ldp_addr_ready', 'o', self.configs.numLdPorts
        )

        # Load data channel (data, valid, ready) to kernel
        ldp_data_o = LogicVecArray(
            em, 'ldp_data', 'o', self.configs.numLdPorts, self.configs.dataW
        )
        ldp_data_valid_o = LogicArray(
            em, 'ldp_data_valid', 'o', self.configs.numLdPorts
        )
        ldp_data_ready_i = LogicArray(
            em, 'ldp_data_ready', 'i', self.configs.numLdPorts
        )

        # Store address channel (addr, valid, ready) from kernel
        stp_addr_i = LogicVecArray(
            em, 'stp_addr', 'i', self.configs.numStPorts, self.configs.addrW
        )
        stp_addr_valid_i = LogicArray(
            em, 'stp_addr_valid', 'i', self.configs.numStPorts
        )
        stp_addr_ready_o = LogicArray(
            em, 'stp_addr_ready', 'o', self.configs.numStPorts
        )

        # Store data channel (data, valid, ready) from kernel
        stp_data_i = LogicVecArray(
            em, 'stp_data', 'i', self.configs.numStPorts, self.configs.dataW
        )
        stp_data_valid_i = LogicArray(
            em, 'stp_data_valid', 'i', self.configs.numStPorts
        )
        stp_data_ready_o = LogicArray(
            em, 'stp_data_ready', 'o', self.configs.numStPorts
        )

        if self.configs.stResp:
            stp_exec_valid_o = LogicArray(
                em, 'stp_exec_valid', 'o', self.configs.numStPorts
            )
            stp_exec_ready_i = LogicArray(
                em, 'stp_exec_ready', 'i', self.configs.numStPorts
            )

        # queue empty signal
        empty_o = Logic(em, 'empty', 'o')

        # Memory interface: i.e., the connection LSQ -> AXI
        # We assume that the memory interface has
        # 1. A read request channel (rreq) and a read response channel (rresp).
        # 2. A write request channel (wreq) and a write response channel (wresp).
        rreq_valid_o = LogicArray(em, 'rreq_valid', 'o', self.configs.numLdMem)
        rreq_ready_i = LogicArray(em, 'rreq_ready', 'i', self.configs.numLdMem)
        rreq_id_o = LogicVecArray(
            em, 'rreq_id', 'o', self.configs.numLdMem, self.configs.idW
        )
        rreq_addr_o = LogicVecArray(
            em, 'rreq_addr', 'o', self.configs.numLdMem, self.configs.addrW
        )

        rresp_valid_i = LogicArray(em, 'rresp_valid', 'i', self.configs.numLdMem)
        rresp_ready_o = LogicArray(em, 'rresp_ready', 'o', self.configs.numLdMem)
        rresp_id_i = LogicVecArray(
            em, 'rresp_id', 'i', self.configs.numLdMem, self.configs.idW
        )
        rresp_data_i = LogicVecArray(
            em, 'rresp_data', 'i', self.configs.numLdMem, self.configs.dataW
        )

        wreq_valid_o = LogicArray(em, 'wreq_valid', 'o', self.configs.numStMem)
        wreq_ready_i = LogicArray(em, 'wreq_ready', 'i', self.configs.numStMem)
        wreq_id_o = LogicVecArray(
            em, 'wreq_id', 'o', self.configs.numStMem, self.configs.idW
        )
        wreq_addr_o = LogicVecArray(
            em, 'wreq_addr', 'o', self.configs.numStMem, self.configs.addrW
        )
        wreq_data_o = LogicVecArray(
            em, 'wreq_data', 'o', self.configs.numStMem, self.configs.dataW
        )
        if self.configs.stDataPass:
            # The store data gets its own handshake towards memory: it is taken
            # straight from the access port, independently of the address.
            wreq_data_valid_o = LogicArray(
                em, 'wreq_data_valid', 'o', self.configs.numStMem
            )
            wreq_data_ready_i = LogicArray(
                em, 'wreq_data_ready', 'i', self.configs.numStMem
            )

        wresp_valid_i = LogicArray(em, 'wresp_valid', 'i', self.configs.numStMem)
        wresp_ready_o = LogicArray(em, 'wresp_ready', 'o', self.configs.numStMem)
        wresp_id_i = LogicVecArray(
            em, 'wresp_id', 'i', self.configs.numStMem, self.configs.idW
        )

        # Pointer related signals
        # For updating pointers
        num_loads = LogicVec(em, 'num_loads', 'w', self.configs.ldqAddrW)
        num_stores = LogicVec(em, 'num_stores', 'w', self.configs.stqAddrW)
        stq_issue_en = Logic(em, 'stq_issue_en', 'w')
        stq_resp_en = Logic(em, 'stq_resp_en', 'w')
        # Generated by pointers
        ldq_empty = Logic(em, 'ldq_empty', 'w')
        stq_empty = Logic(em, 'stq_empty', 'w')
        ldq_head_oh = LogicVec(em, 'ldq_head_oh', 'w', self.configs.numLdqEntries)
        stq_head_oh = LogicVec(em, 'stq_head_oh', 'w', self.configs.numStqEntries)
        #! If this is the lsq master, then we need the following logic
        #! Define new interfaces needed by dynamatic
        if self.configs.master:
            memStart_ready = Logic(em, 'memStart_ready', 'o')
            memStart_valid = Logic(em, 'memStart_valid', 'i')
            ctrlEnd_ready = Logic(em, 'ctrlEnd_ready', 'o')
            ctrlEnd_valid = Logic(em, 'ctrlEnd_valid', 'i')
            memEnd_ready = Logic(em, 'memEnd_ready', 'i')
            memEnd_valid = Logic(em, 'memEnd_valid', 'o')

            #! Add extra signals required
            memStartReady = Logic(em, 'memStartReady', 'w', force_reg=True)
            memEndValid = Logic(em, 'memEndValid', 'w', force_reg=True)
            ctrlEndReady = Logic(em, 'ctrlEndReady', 'w', force_reg=True)
            temp_gen_mem = Logic(em, 'TEMP_GEN_MEM', 'w')

            #! The memory completion signal cannot be set to 1 when any group is allocating:
            no_curr_ga = ~reduce_bin(
                BinOp.OR,
                [Val(group_init_valid_i, i) for i in range(group_init_valid_i.length)],
            )

            #! Define the needed logic
            em.add_comment(
                'This signal indicates that all mem. ops are completed and func. can return.'
            )
            em.add_comment('LSQ can return iff all the following conditions are true:')
            em.add_comment('1. No more upcoming BBs containing memory accesses.')
            em.add_comment('2. Both store and load queues are empty.')
            em.add_comment('3. No GA in the same cycle.')
            em.add_assignment(
                temp_gen_mem, ctrlEnd_valid & stq_empty & ldq_empty & no_curr_ga
            )

            em.add_comment('Define logic for the new interfaces needed by dynamatic')
            vhdl_str = ''
            # TODO: Add proper emitter functions in order to do this
            vhdl_str += "\tprocess (clk) is\n\tbegin\n"
            vhdl_str += '\t' * 2 + "if rising_edge(clk) then\n"
            vhdl_str += '\t' * 3 + "if rst = '1' then\n"
            vhdl_str += '\t' * 4 + "memStartReady <= '1';\n"
            vhdl_str += '\t' * 4 + "memEndValid <= '0';\n"
            vhdl_str += '\t' * 4 + "ctrlEndReady <= '0';\n"
            vhdl_str += '\t' * 3 + "else\n"
            vhdl_str += (
                '\t' * 4
                + "memStartReady <= (memEndValid and memEnd_ready_i) or ((not (memStart_valid_i and memStartReady)) and memStartReady);\n"
            )
            vhdl_str += '\t' * 4 + "memEndValid <= TEMP_GEN_MEM or memEndValid;\n"
            vhdl_str += (
                '\t' * 4
                + "ctrlEndReady <= (not (ctrlEnd_valid_i and ctrlEndReady)) and (TEMP_GEN_MEM or ctrlEndReady);\n"
            )
            vhdl_str += '\t' * 3 + "end if;\n"
            vhdl_str += '\t' * 2 + "end if;\n"
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
            em.add_comment('Update new memory interfaces')
            em.add_assignment(memStart_ready, memStartReady)
            em.add_assignment(ctrlEnd_ready, ctrlEndReady)
            em.add_assignment(memEnd_valid, memEndValid)

        ######  Queue Registers ######
        # Load Queue Entries
        ldq_alloc = LogicArray(em, 'ldq_alloc', 'r', self.configs.numLdqEntries)
        ldq_issue = LogicArray(em, 'ldq_issue', 'r', self.configs.numLdqEntries)
        if self.configs.ldpAddrW > 0:
            ldq_port_idx = LogicVecArray(
                em,
                'ldq_port_idx',
                'r',
                self.configs.numLdqEntries,
                self.configs.ldpAddrW,
            )
        else:
            ldq_port_idx = None
        ldq_addr_valid = LogicArray(
            em, 'ldq_addr_valid', 'r', self.configs.numLdqEntries
        )
        ldq_addr = LogicVecArray(
            em, 'ldq_addr', 'r', self.configs.numLdqEntries, self.configs.addrW
        )
        ldq_data_valid = LogicArray(
            em, 'ldq_data_valid', 'r', self.configs.numLdqEntries
        )
        if not self.configs.ldDataPass:
            ldq_data = LogicVecArray(
                em, 'ldq_data', 'r', self.configs.numLdqEntries, self.configs.dataW
            )

        # Store Queue Entries
        stq_alloc = LogicArray(em, 'stq_alloc', 'r', self.configs.numStqEntries)
        if self.configs.stResp:
            stq_exec = LogicArray(em, 'stq_exec', 'r', self.configs.numStqEntries)
        if self.configs.stpAddrW > 0:
            stq_port_idx = LogicVecArray(
                em,
                'stq_port_idx',
                'r',
                self.configs.numStqEntries,
                self.configs.stpAddrW,
            )
        else:
            stq_port_idx = None
        stq_addr_valid = LogicArray(
            em, 'stq_addr_valid', 'r', self.configs.numStqEntries
        )
        stq_addr = LogicVecArray(
            em, 'stq_addr', 'r', self.configs.numStqEntries, self.configs.addrW
        )
        if not self.configs.stDataPass:
            stq_data_valid = LogicArray(
                em, 'stq_data_valid', 'r', self.configs.numStqEntries
            )
            stq_data = LogicVecArray(
                em, 'stq_data', 'r', self.configs.numStqEntries, self.configs.dataW
            )

        # Order for load-store
        store_is_older = LogicVecArray(
            em,
            'store_is_older',
            'r',
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )

        # Pointers
        ldq_tail = LogicVec(em, 'ldq_tail', 'r', self.configs.ldqAddrW)
        ldq_head = LogicVec(em, 'ldq_head', 'r', self.configs.ldqAddrW)

        stq_tail = LogicVec(em, 'stq_tail', 'r', self.configs.stqAddrW)
        stq_head = LogicVec(em, 'stq_head', 'r', self.configs.stqAddrW)
        stq_issue = LogicVec(em, 'stq_issue', 'r', self.configs.stqAddrW)
        stq_resp = LogicVec(em, 'stq_resp', 'r', self.configs.stqAddrW)

        # Entry related signals
        # From port dispatchers
        ldq_wen = LogicArray(em, 'ldq_wen', 'w', self.configs.numLdqEntries)
        ldq_addr_wen = LogicArray(em, 'ldq_addr_wen', 'w', self.configs.numLdqEntries)
        ldq_reset = LogicArray(em, 'ldq_reset', 'w', self.configs.numLdqEntries)
        stq_wen = LogicArray(em, 'stq_wen', 'w', self.configs.numStqEntries)
        stq_addr_wen = LogicArray(em, 'stq_addr_wen', 'w', self.configs.numStqEntries)
        if not self.configs.stDataPass:
            stq_data_wen = LogicArray(
                em, 'stq_data_wen', 'w', self.configs.numStqEntries
            )
        stq_reset = LogicArray(em, 'stq_reset', 'w', self.configs.numStqEntries)
        # From Read/Write Block
        ldq_data_wen = LogicArray(em, 'ldq_data_wen', 'w', self.configs.numLdqEntries)
        ldq_issue_set = LogicArray(em, 'ldq_issue_set', 'w', self.configs.numLdqEntries)
        if self.configs.stResp:
            stq_exec_set = LogicArray(
                em, 'stq_exec_set', 'w', self.configs.numStqEntries
            )
        # Form Group Allocator
        ga_ls_order = LogicVecArray(
            em,
            'ga_ls_order',
            'w',
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )

        BitsToOH(em, ldq_head_oh, ldq_head)
        BitsToOH(em, stq_head_oh, stq_head)
        # indicates tail pointer was just updated (i.e., new stores were allocated)
        stq_tail_update = Logic(em, 'stq_tail_update', 'r')
        stq_tail_update.regInit()
        Reduce(em, stq_tail_update, num_stores, BinOp.OR)

        # Pipelining Strategy:
        # The signals are always passed through the pipeline stages (*_pcomp,
        # *_p0, *_p1). If the pipeline stage is enabled, the signal will be
        # registered (the signal type is 'r' for register). Otherwise, the
        # signal type is 'w' for wire, and the pipeline stage is effectively
        # bypassed. If the signals are registers, we need to conditionally call
        # regInit().
        pipe_comp_type = 'r' if self.configs.pipeComp else 'w'
        pipe0_type = 'r' if self.configs.pipe0 else 'w'
        pipe1_type = 'r' if self.configs.pipe1 else 'w'

        # update queue entries
        # load queue
        ldq_wen_pcomp = LogicArray(
            em, 'ldq_wen_pcomp', pipe_comp_type, self.configs.numLdqEntries
        )
        ldq_wen_p0 = LogicArray(
            em, 'ldq_wen_p0', pipe0_type, self.configs.numLdqEntries
        )
        ldq_alloc_next = LogicArray(
            em, 'ldq_alloc_next', 'w', self.configs.numLdqEntries
        )
        if self.configs.pipeComp:
            ldq_wen_pcomp.regInit()
        if self.configs.pipe0:
            ldq_wen_p0.regInit()

        for i in range(0, self.configs.numLdqEntries):
            em.add_assignment(ldq_alloc_next[i], ~ldq_reset[i] & ldq_alloc[i])
            em.add_assignment(ldq_alloc[i], ldq_wen[i] | ldq_alloc_next[i])
            em.add_assignment(ldq_wen_pcomp[i], ldq_wen[i])
            em.add_assignment(ldq_wen_p0[i], ldq_wen_pcomp[i])
            em.add_assignment(
                ldq_issue[i], ~ldq_wen_p0[i] & (ldq_issue_set[i] | ldq_issue[i])
            )
            em.add_assignment(
                ldq_addr_valid[i], ~ldq_wen[i] & (ldq_addr_wen[i] | ldq_addr_valid[i])
            )
            em.add_assignment(
                ldq_data_valid[i], ~ldq_wen[i] & (ldq_data_wen[i] | ldq_data_valid[i])
            )
        # store queue
        stq_alloc_next = LogicArray(
            em, 'stq_alloc_next', 'w', self.configs.numStqEntries
        )
        for i in range(0, self.configs.numStqEntries):
            em.add_assignment(stq_alloc_next[i], ~stq_reset[i] & stq_alloc[i])
            em.add_assignment(stq_alloc[i], stq_wen[i] | stq_alloc_next[i])
            if self.configs.stResp:
                em.add_assignment(
                    stq_exec[i], ~stq_wen[i] & (stq_exec_set[i] | stq_exec[i])
                )
            em.add_assignment(
                stq_addr_valid[i], ~stq_wen[i] & (stq_addr_wen[i] | stq_addr_valid[i])
            )
            if not self.configs.stDataPass:
                em.add_assignment(
                    stq_data_valid[i],
                    ~stq_wen[i] & (stq_data_wen[i] | stq_data_valid[i]),
                )

        # order matrix
        # store_is_older(i,j) = (not stq_reset(j) and (stq_alloc(j) or ga_ls_order(i, j)))
        #                  when ldq_wen(i)
        #                  else not stq_reset(j) and store_is_older(i, j)
        for i in range(0, self.configs.numLdqEntries):
            for j in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    (store_is_older, i, j),
                    (~Val(stq_reset, j) & (Val(stq_alloc, j) | Val(ga_ls_order, i, j)))
                    .when(Val(ldq_wen, i))
                    .else_(~Val(stq_reset, j) & Val(store_is_older, i, j)),
                )

        # pointers update
        ldq_not_empty = Logic(em, 'ldq_not_empty', 'w')
        stq_not_empty = Logic(em, 'stq_not_empty', 'w')
        Reduce(em, ldq_not_empty, ldq_alloc, BinOp.OR)
        em.add_assignment(ldq_empty, ~ldq_not_empty)
        MuxLookUp(em, stq_not_empty, stq_alloc, stq_head)
        em.add_assignment(stq_empty, ~stq_not_empty)
        em.add_assignment(empty_o, ldq_empty & stq_empty)

        WrapAdd(em, ldq_tail, ldq_tail, num_loads, self.configs.numLdqEntries)
        WrapAdd(em, stq_tail, stq_tail, num_stores, self.configs.numStqEntries)
        WrapAddConst(em, stq_issue, stq_issue, 1, self.configs.numStqEntries)
        WrapAddConst(em, stq_resp, stq_resp, 1, self.configs.numStqEntries)

        ldq_tail_oh = LogicVec(em, 'ldq_tail_oh', 'w', self.configs.numLdqEntries)
        BitsToOH(em, ldq_tail_oh, ldq_tail)
        ldq_head_next_oh = LogicVec(
            em, 'ldq_head_next_oh', 'w', self.configs.numLdqEntries
        )
        ldq_head_next = LogicVec(em, 'ldq_head_next', 'w', self.configs.ldqAddrW)
        ldq_head_sel = Logic(em, 'ldq_head_sel', 'w')
        if self.configs.headLag:
            # Update the head pointer according to the valid signal of last cycle
            CyclicPriorityMasking(em, ldq_head_next_oh, ldq_alloc, ldq_tail_oh)
            Reduce(em, ldq_head_sel, ldq_alloc, BinOp.OR)
        else:
            CyclicPriorityMasking(em, ldq_head_next_oh, ldq_alloc_next, ldq_tail_oh)
            Reduce(em, ldq_head_sel, ldq_alloc_next, BinOp.OR)
        OHToBits(em, ldq_head_next, ldq_head_next_oh)
        em.add_assignment(ldq_head, ldq_head_next.when(ldq_head_sel).else_(ldq_tail))

        stq_tail_oh = LogicVec(em, 'stq_tail_oh', 'w', self.configs.numStqEntries)
        BitsToOH(em, stq_tail_oh, stq_tail)
        stq_head_next_oh = LogicVec(
            em, 'stq_head_next_oh', 'w', self.configs.numStqEntries
        )
        stq_head_next = LogicVec(em, 'stq_head_next', 'w', self.configs.stqAddrW)
        stq_head_sel = Logic(em, 'stq_head_sel', 'w')
        if self.configs.stResp:
            if self.configs.headLag:
                # Update the head pointer according to the valid signal of last cycle
                CyclicPriorityMasking(em, stq_head_next_oh, stq_alloc, stq_tail_oh)
                Reduce(em, stq_head_sel, stq_alloc, BinOp.OR)
            else:
                CyclicPriorityMasking(em, stq_head_next_oh, stq_alloc_next, stq_tail_oh)
                Reduce(em, stq_head_sel, stq_alloc_next, BinOp.OR)
            OHToBits(em, stq_head_next, stq_head_next_oh)
            em.add_assignment(
                stq_head, stq_head_next.when(stq_head_sel).else_(stq_tail)
            )
        else:
            WrapAddConst(em, stq_head_next, stq_head, 1, self.configs.numStqEntries)
            em.add_assignment(stq_head_sel, wresp_valid_i[0])
            em.add_assignment(
                stq_head, stq_head_next.when(stq_head_sel).else_(stq_head)
            )

        # Load Queue Entries
        ldq_alloc.regInit(init=[0] * self.configs.numLdqEntries)
        ldq_issue.regInit()
        if self.configs.ldpAddrW > 0:
            ldq_port_idx.regInit(ldq_wen)
        ldq_addr_valid.regInit()
        ldq_addr.regInit(ldq_addr_wen)
        ldq_data_valid.regInit()
        if not self.configs.ldDataPass:
            ldq_data.regInit(ldq_data_wen)

        # Store Queue Entries
        stq_alloc.regInit(init=[0] * self.configs.numStqEntries)
        if self.configs.stResp:
            stq_exec.regInit()
        if self.configs.stpAddrW > 0:
            stq_port_idx.regInit(stq_wen)
        stq_addr_valid.regInit()
        stq_addr.regInit(stq_addr_wen)
        if not self.configs.stDataPass:
            stq_data_valid.regInit()
            stq_data.regInit(stq_data_wen)

        # Order for load-store
        store_is_older.regInit()

        # Pointers
        ldq_tail.regInit(init=0)
        ldq_head.regInit(init=0)

        stq_tail.regInit(init=0)
        stq_head.regInit(init=0)
        stq_issue.regInit(enable=stq_issue_en, init=0)
        stq_resp.regInit(enable=stq_resp_en, init=0)

        ######   Entity Instantiation   ######

        # Group Allocator
        lsq_submodules.group_allocator.instantiate(
            em,
            group_init_valid_i,
            group_init_ready_o,
            ldq_tail,
            ldq_head,
            ldq_empty,
            stq_tail,
            stq_head,
            stq_empty,
            ldq_wen,
            num_loads,
            ldq_port_idx,
            stq_wen,
            num_stores,
            stq_port_idx,
            ga_ls_order,
        )

        # When the condition "lsq_submodules.ptq_dispatcher_lda != None" is not true:
        # The dispatcher module will be set to None when there are zero load ports.
        # In this case, do not instantiate dispatching logic when there are zero load ports.
        # - WARNING: This logic needs more testing
        # - TODO: Also remove the load queue when there are zero load ports.
        if lsq_submodules.ptq_dispatcher_lda != None:
            # Load Address Port Dispatcher
            lsq_submodules.ptq_dispatcher_lda.instantiate(
                em,
                ldp_addr_i,
                ldp_addr_valid_i,
                ldp_addr_ready_o,
                ldq_alloc,
                ldq_addr_valid,
                ldq_port_idx,
                ldq_addr,
                ldq_addr_wen,
                ldq_head_oh,
            )

        # When the condition "lsq_submodules.qtp_dispatcher_ldd != None" is not true:
        # The dispatcher module will be set to None when there are zero load ports.
        # In this case, do not instantiate dispatching logic when there are zero load ports.
        # - WARNING: This logic needs more testing
        # - TODO: Also remove the load queue when there are zero load ports.
        if lsq_submodules.qtp_dispatcher_ldd != None and not self.configs.ldDataPass:
            # Load Data Port Dispatcher
            lsq_submodules.qtp_dispatcher_ldd.instantiate(
                em,
                ldp_data_o,
                ldp_data_valid_o,
                ldp_data_ready_i,
                ldq_alloc,
                ldq_data_valid,
                ldq_port_idx,
                ldq_data,
                ldq_reset,
                ldq_head_oh,
            )

        # Store Address Port Dispatcher
        lsq_submodules.ptq_dispatcher_sta.instantiate(
            em,
            stp_addr_i,
            stp_addr_valid_i,
            stp_addr_ready_o,
            stq_alloc,
            stq_addr_valid,
            stq_port_idx,
            stq_addr,
            stq_addr_wen,
            stq_head_oh,
        )

        # Store Data Port Dispatcher
        if not self.configs.stDataPass:
            lsq_submodules.ptq_dispatcher_std.instantiate(
                em,
                stp_data_i,
                stp_data_valid_i,
                stp_data_ready_o,
                stq_alloc,
                stq_data_valid,
                stq_port_idx,
                stq_data,
                stq_data_wen,
                stq_head_oh,
            )

        # Store Backward Port Dispatcher
        if self.configs.stResp:
            lsq_submodules.qtp_dispatcher_stb.instantiate(
                em,
                None,
                stp_exec_valid_o,
                stp_exec_ready_i,
                stq_alloc,
                stq_exec,
                stq_port_idx,
                None,
                stq_reset,
                stq_head_oh,
            )

        ###### Dependency Check ######
        load_idx_oh = LogicVecArray(
            em, 'load_idx_oh', 'w', self.configs.numLdMem, self.configs.numLdqEntries
        )
        load_en = LogicArray(em, 'load_en', 'w', self.configs.numLdMem)

        # Multiple store channels not yet implemented
        assert self.configs.numStMem == 1
        store_idx = LogicVec(em, 'store_idx', 'w', self.configs.stqAddrW)
        store_en = Logic(em, 'store_en', 'w')

        # Matrix Generation
        ld_st_conflict = LogicVecArray(
            em,
            'ld_st_conflict',
            'w',
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )
        can_bypass = LogicVecArray(
            em,
            'can_bypass',
            'w',
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )
        can_bypass_p0 = LogicVecArray(
            em,
            'can_bypass_p0',
            pipe0_type,
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )
        if self.configs.pipe0:
            can_bypass_p0.regInit(init=[0] * self.configs.numLdqEntries)

        ldq_head_oh_pcomp = LogicVec(
            em, 'ldq_head_oh_pcomp', pipe_comp_type, self.configs.numLdqEntries
        )
        ldq_alloc_pcomp = LogicArray(
            em, 'ldq_alloc_pcomp', pipe_comp_type, self.configs.numLdqEntries
        )
        ldq_addr_valid_pcomp = LogicArray(
            em, 'ldq_addr_valid_pcomp', pipe_comp_type, self.configs.numLdqEntries
        )
        stq_alloc_pcomp = LogicArray(
            em, 'stq_alloc_pcomp', pipe_comp_type, self.configs.numStqEntries
        )
        stq_addr_valid_pcomp = LogicArray(
            em, 'stq_addr_valid_pcomp', pipe_comp_type, self.configs.numStqEntries
        )
        if not self.configs.stDataPass:
            stq_data_valid_pcomp = LogicArray(
                em, 'stq_data_valid_pcomp', pipe_comp_type, self.configs.numStqEntries
            )
        stq_tail_update_pcomp = Logic(em, 'stq_tail_update_pcomp', pipe_comp_type)
        # addr_valid_pcomp is always a wire: combines other registers signals
        addr_valid_pcomp = LogicVecArray(
            em,
            'addr_valid_pcomp',
            'w',
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )
        addr_same_pcomp = LogicVecArray(
            em,
            'addr_same_pcomp',
            pipe_comp_type,
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )
        store_is_older_pcomp = LogicVecArray(
            em,
            'store_is_older_pcomp',
            pipe_comp_type,
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )

        # combinational signal indicating whether a load has already completed (assuming it is allocated), meaning the
        # data (= read response) from memory has been received
        load_completed = LogicArray(
            em, 'load_completed', 'w', self.configs.numLdqEntries
        )
        # combinational signal indicating whether a store has already completed (assuming it is allocated), meaning the
        # write response from memory has been received
        store_completed = LogicArray(
            em, 'store_completed', 'w', self.configs.numStqEntries
        )

        if self.configs.pipeComp:
            ldq_head_oh_pcomp.regInit(init=0)
            ldq_alloc_pcomp.regInit(init=[0] * self.configs.numLdqEntries)
            ldq_addr_valid_pcomp.regInit()
            stq_alloc_pcomp.regInit(init=[0] * self.configs.numStqEntries)
            stq_addr_valid_pcomp.regInit()
            if not self.configs.stDataPass:
                stq_data_valid_pcomp.regInit()
            stq_tail_update_pcomp.regInit()
            addr_same_pcomp.regInit()
            store_is_older_pcomp.regInit()

        em.add_assignment(ldq_head_oh_pcomp, ldq_head_oh)
        for i in range(0, self.configs.numLdqEntries):
            em.add_assignment((ldq_alloc_pcomp, i), Val(ldq_alloc, i))
            em.add_assignment((ldq_addr_valid_pcomp, i), Val(ldq_addr_valid, i))
        for j in range(0, self.configs.numStqEntries):
            em.add_assignment((stq_alloc_pcomp, j), Val(stq_alloc, j))
            em.add_assignment((stq_addr_valid_pcomp, j), Val(stq_addr_valid, j))
            if not self.configs.stDataPass:
                em.add_assignment((stq_data_valid_pcomp, j), Val(stq_data_valid, j))
        em.add_assignment(stq_tail_update_pcomp, stq_tail_update)
        for i in range(0, self.configs.numLdqEntries):
            for j in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    (store_is_older_pcomp, i, j), Val(store_is_older, i, j)
                )
        for i in range(0, self.configs.numLdqEntries):
            for j in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    (addr_valid_pcomp, i, j),
                    Val(ldq_addr_valid_pcomp, i) & Val(stq_addr_valid_pcomp, j),
                )
        for i in range(0, self.configs.numLdqEntries):
            for j in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    (addr_same_pcomp, i, j),
                    Bit(1).when(Val(ldq_addr, i) == Val(stq_addr, j)).else_(Bit(0)),
                )

        for i in range(self.configs.numLdqEntries):
            # No need to use pipelined ldq_data_valid here: As soon as the load entry has valid data (in the queue
            # itself, not the pipeline), the load is considered completed.
            em.add_assignment(load_completed[i], ldq_data_valid[i])
        for i in range(self.configs.numStqEntries):
            if self.configs.stResp:
                # No need to use pipelined stq_exec here: As soon as the store response has been received from memory,
                # the store is considered completed.
                em.add_assignment(store_completed[i], stq_exec[i])
            else:
                # If the store queue entry is not valid (anymore), the store has completed.
                em.add_assignment(store_completed[i], ~stq_alloc[i])

        # A load conflicts with a store when:
        # 1. The store entry is valid, and
        # 2. The store entry hasn't completed (received write response from memory), and
        # 3. The store is older than the load, and
        # 4. The address conflicts(same or invalid store address).
        # NOTE: Because we only consider non-completed stores to conflict with a load, bypass will
        # not forward from any stores which are already completed (but still allocated). However,
        # such loads only exist if store responses or pipe0 are enabled, which is not the case by
        # default.
        for i in range(0, self.configs.numLdqEntries):
            for j in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    (ld_st_conflict, i, j),
                    Val(stq_alloc_pcomp, j)
                    & ~Val(store_completed, j)
                    & Val(store_is_older_pcomp, i, j)
                    & (Val(addr_same_pcomp, i, j) | ~Val(stq_addr_valid_pcomp, j)),
                )

        # A conflicting store entry can be bypassed to a load entry when:
        # 1. The load entry is valid, and
        # 2. The load entry is not issued yet, and
        # 3. The address of the load-store pair are both valid and values the same.
        # (stDataPass implies bypass off, and keeps no data valid bit to check.)
        for i in range(0, self.configs.numLdqEntries):
            for j in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    (can_bypass_p0, i, j),
                    (
                        Val(ldq_alloc_pcomp, i)
                        if self.configs.stDataPass
                        else Val(ldq_alloc_pcomp, i) & Val(stq_data_valid_pcomp, j)
                    )
                    & Val(addr_same_pcomp, i, j)
                    & Val(addr_valid_pcomp, i, j),
                )
        for i in range(0, self.configs.numLdqEntries):
            for j in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    (can_bypass, i, j),
                    ~Val(ldq_issue, i) & Val(can_bypass_p0, i, j),
                )

        # Load

        load_conflict = LogicArray(em, 'load_conflict', 'w', self.configs.numLdqEntries)
        load_req_valid = LogicArray(
            em, 'load_req_valid', 'w', self.configs.numLdqEntries
        )
        can_load = LogicArray(em, 'can_load', 'w', self.configs.numLdqEntries)
        can_load_p0 = LogicArray(
            em, 'can_load_p0', pipe0_type, self.configs.numLdqEntries
        )
        if self.configs.pipe0:
            can_load_p0.regInit(init=[0] * self.configs.numLdqEntries)

        # The load conflicts with any store
        for i in range(0, self.configs.numLdqEntries):
            Reduce(em, load_conflict[i], ld_st_conflict[i], BinOp.OR)
        # The load is valid when the entry is valid and not yet issued, the load address should also be valid.
        # We do not need to check ldq_data_valid, since unissued load request cannot have valid data.
        for i in range(0, self.configs.numLdqEntries):
            em.add_assignment(
                load_req_valid[i], ldq_alloc_pcomp[i] & ldq_addr_valid_pcomp[i]
            )
        if self.configs.ldDataPass:
            # Load data passthrough: a port's data must reach it in program
            # order and there is one holding register per port, so a load may
            # only go to memory while it is the oldest allocated load of its
            # port -- or the second oldest while the oldest hands its data to
            # the port this cycle, so a port can still issue every cycle.
            # ld_first / ld_second are registered and updated incrementally:
            # when a port delivers, second moves up to first and the third
            # oldest (one mask over registered state) to second; a new entry
            # becomes first or second by how many loads of its port remain.
            # A group allocates each of its ports at most once and only one
            # group allocates per cycle, so a port gains at most one entry per
            # cycle. No priority mask sits on a path from circuit signals.
            # ld_first also names the entry a port's delivered data belongs to.
            assert not self.configs.gaMulti, "loadDataPassthrough needs groupMulti off"
            for g, ports in enumerate(self.configs.gaLdPortIdx):
                assert len(set(ports)) == len(ports), \
                    f"group {g} allocates a load port twice"
            n_e, n_p = self.configs.numLdqEntries, self.configs.numLdPorts
            ld_port_oh = LogicVecArray(em, 'ld_port_oh', 'w', n_e, n_p)
            ld_port_oh_next = LogicVecArray(em, 'ld_port_oh_next', 'w', n_e, n_p)
            ld_first = LogicArray(em, 'ld_first', 'r', n_e)
            ld_second = LogicArray(em, 'ld_second', 'r', n_e)
            ld_first.regInit(init=[0] * n_e)
            ld_second.regInit(init=[0] * n_e)
            if n_p > 1:
                # the port index being written this cycle (the register's input)
                ldq_port_idx_in = LogicVecArray(
                    em, 'ldq_port_idx_in', 'w', n_e, self.configs.ldpAddrW
                )
            for i in range(n_e):
                if n_p == 1:
                    em.add_assignment(ld_port_oh[i], Val(1))
                    em.add_assignment(ld_port_oh_next[i], Val(1))
                else:
                    em.add_custom_statement(CustomStatement(
                        f"\t{ldq_port_idx_in.getNameWrite(i)} <= "
                        f"{ldq_port_idx.getNameWrite(i)};\n",
                        f"assign {ldq_port_idx_in.getNameWrite(i)} = "
                        f"{ldq_port_idx.getNameWrite(i)};\n",
                    ))
                    BitsToOH(em, ld_port_oh[i], ldq_port_idx[i])
                    BitsToOH(em, ld_port_oh_next[i], ldq_port_idx_in[i])

            # first / second / third oldest of each port, as port one-hots
            ld_first_port = LogicVecArray(em, 'ld_first_port', 'w', n_e, n_p)
            ld_second_port = LogicVecArray(em, 'ld_second_port', 'w', n_e, n_p)
            ld_rest_port = LogicVecArray(em, 'ld_rest_port', 'w', n_e, n_p)
            ld_third_port = LogicVecArray(em, 'ld_third_port', 'w', n_e, n_p)
            for i in range(n_e):
                em.add_assignment(
                    ld_first_port[i], ld_port_oh[i].when(ld_first[i]).else_(Val(0))
                )
                em.add_assignment(
                    ld_second_port[i], ld_port_oh[i].when(ld_second[i]).else_(Val(0))
                )
                em.add_assignment(
                    ld_rest_port[i],
                    ld_port_oh[i]
                    .when(ldq_alloc[i] & ~ld_first[i] & ~ld_second[i])
                    .else_(Val(0)),
                )
            CyclicPriorityMasking(em, ld_third_port, ld_rest_port, ldq_head_oh)
            ld_has_first = LogicVec(em, 'ld_has_first', 'w', n_p)
            ld_has_second = LogicVec(em, 'ld_has_second', 'w', n_p)
            ld_has_third = LogicVec(em, 'ld_has_third', 'w', n_p)
            Reduce(em, ld_has_first, ld_first_port, BinOp.OR)
            Reduce(em, ld_has_second, ld_second_port, BinOp.OR)
            Reduce(em, ld_has_third, ld_third_port, BinOp.OR)

            # port handshakes (assigned with the read response below)
            ld_port_hs = LogicArray(em, 'ld_port_hs', 'w', n_p)
            ld_port_hs_vec = LogicVec(em, 'ld_port_hs_vec', 'w', n_p)
            for p in range(n_p):
                em.add_assignment((ld_port_hs_vec, p), Val(ld_port_hs, p))
            # loads of the port left after this cycle's delivery: none / one
            ld_left_none = LogicVec(em, 'ld_left_none', 'w', n_p)
            ld_left_one = LogicVec(em, 'ld_left_one', 'w', n_p)
            em.add_assignment(
                ld_left_none, ~ld_has_first | (ld_port_hs_vec & ~ld_has_second)
            )
            em.add_assignment(
                ld_left_one,
                (ld_has_first & ~ld_has_second & ~ld_port_hs_vec)
                | (ld_has_second & ld_port_hs_vec & ~ld_has_third),
            )

            ld_entry_hs = LogicArray(em, 'ld_entry_hs', 'w', n_e)
            ld_third = LogicArray(em, 'ld_third', 'w', n_e)
            ld_new_first = LogicArray(em, 'ld_new_first', 'w', n_e)
            ld_new_second = LogicArray(em, 'ld_new_second', 'w', n_e)
            ld_may_issue = LogicArray(em, 'ld_may_issue', 'w', n_e)
            for i in range(n_e):
                for name, dst, vec, oh in (
                    ('hs', ld_entry_hs, ld_port_hs_vec, ld_port_oh),
                    ('none', ld_new_first, ld_left_none, ld_port_oh_next),
                    ('one', ld_new_second, ld_left_one, ld_port_oh_next),
                ):
                    tmp = LogicVec(em, f'ld_{name}_vec_{i}', 'w', n_p)
                    em.add_assignment(tmp, oh[i] & vec)
                    Reduce(em, dst[i], tmp, BinOp.OR)
                Reduce(em, ld_third[i], ld_third_port[i], BinOp.OR)
                em.add_assignment(
                    ld_first[i],
                    (ld_first[i] & ~ld_entry_hs[i])
                    | (ld_second[i] & ld_entry_hs[i])
                    | (ldq_wen[i] & ld_new_first[i]),
                )
                em.add_assignment(
                    ld_second[i],
                    (ld_second[i] & ~ld_entry_hs[i])
                    | (ld_third[i] & ld_entry_hs[i])
                    | (ldq_wen[i] & ld_new_second[i]),
                )
                em.add_assignment(
                    ld_may_issue[i], ld_first[i] | (ld_second[i] & ld_entry_hs[i])
                )

        # Generate list for loads that does not face dependency issue
        for i in range(0, self.configs.numLdqEntries):
            if self.configs.ldDataPass:
                em.add_assignment(
                    can_load_p0[i],
                    ~load_conflict[i] & load_req_valid[i] & ld_may_issue[i],
                )
            else:
                em.add_assignment(
                    can_load_p0[i], ~load_conflict[i] & load_req_valid[i]
                )
        for i in range(0, self.configs.numLdqEntries):
            em.add_assignment(can_load[i], ~ldq_issue[i] & can_load_p0[i])

        ldq_head_oh_p0 = LogicVec(
            em, 'ldq_head_oh_p0', pipe0_type, self.configs.numLdqEntries
        )
        if self.configs.pipe0:
            ldq_head_oh_p0.regInit()
        em.add_assignment(ldq_head_oh_p0, ldq_head_oh_pcomp)

        can_load_list = []
        can_load_list.append(can_load)
        for w in range(0, self.configs.numLdMem):
            CyclicPriorityMasking(em, load_idx_oh[w], can_load_list[w], ldq_head_oh_p0)
            Reduce(em, load_en[w], can_load_list[w], BinOp.OR)
            if w + 1 != self.configs.numLdMem:
                load_idx_oh_LogicArray = LogicArray(
                    em, f'load_idx_oh_Array_{w+1}', 'w', self.configs.numLdqEntries
                )
                VecToArray(em, load_idx_oh_LogicArray, load_idx_oh[w])
                can_load_list.append(
                    LogicArray(
                        em, f'can_load_list_{w+1}', 'w', self.configs.numLdqEntries
                    )
                )
                for i in range(0, self.configs.numLdqEntries):
                    em.add_assignment(
                        can_load_list[w + 1][i],
                        ~load_idx_oh_LogicArray[i] & can_load_list[w][i],
                    )

        # Store
        # When pipelining (pipe0) is enabled, this uses look-ahead to the next store entry to reduce the critical path.
        # Both the current and next stores are checked for validity and conflicts, and the result is multiplexed "late
        # in the clock cycle" to reduce the critical path. When pipelining is disabled, only the current store entry is
        # checked, so there is no need for computing the signals for the next store entry, and for the multiplexing.

        # Store request is valid if the entry is allocated and has valid address+data.
        # With stDataPass the data travels on its own channel, so only the address counts.
        store_req_valid_arr = LogicArray(
            em, 'store_req_valid_arr', 'w', self.configs.numStqEntries
        )
        for i in range(self.configs.numStqEntries):
            if self.configs.stDataPass:
                em.add_assignment(
                    store_req_valid_arr[i], stq_alloc_pcomp[i] & stq_addr_valid_pcomp[i]
                )
            else:
                em.add_assignment(
                    store_req_valid_arr[i],
                    stq_alloc_pcomp[i]
                    & stq_addr_valid_pcomp[i]
                    & stq_data_valid_pcomp[i],
                )

        store_conflict = Logic(em, 'store_conflict', 'w')
        store_req_valid_p0 = Logic(em, 'store_req_valid_p0', pipe0_type)
        st_ld_conflict_p0 = LogicVec(
            em, 'st_ld_conflict_p0', pipe0_type, self.configs.numLdqEntries
        )
        if self.configs.pipe0:
            store_req_valid_p0.regInit(init=0)
            st_ld_conflict_p0.regInit()

        # next issue pointer (needed for look-ahead when pipelining is enabled and for stalling store issue)
        stq_issue_next = LogicVec(em, 'stq_issue_next', 'w', self.configs.stqAddrW)
        WrapAddConst(em, stq_issue_next, stq_issue, 1, self.configs.numStqEntries)

        # checks for current and next (if needed) store entry
        store_req_valid_curr = Logic(em, 'store_req_valid_curr', 'w')
        st_ld_conflict_curr = LogicVec(
            em, 'st_ld_conflict_curr', 'w', self.configs.numLdqEntries
        )
        if self.configs.pipe0:
            # with pipelining: also compute for the next entry
            store_req_valid_next = Logic(em, 'store_req_valid_next', 'w')
            st_ld_conflict_next = LogicVec(
                em, 'st_ld_conflict_next', 'w', self.configs.numLdqEntries
            )

        # validity lookup
        MuxLookUp(em, store_req_valid_curr, store_req_valid_arr, stq_issue)
        if self.configs.pipe0:
            # with pipelining: also compute for the next entry
            MuxLookUp(em, store_req_valid_next, store_req_valid_arr, stq_issue_next)

        # A store conflicts with a load when:
        # 1. The load entry is valid, and
        # 2. The load entry hasn't completed (received data from memory), and
        # 3. The load is older than the store, and
        # 4. The address conflicts(same or invalid store address).
        # Index order are reversed for store matrix.
        for i in range(self.configs.numLdqEntries):
            em.add_assignment(
                (st_ld_conflict_curr, i),
                Val(ldq_alloc_pcomp, i)
                & ~Val(load_completed, i)
                & ~Val(em.mux_index(store_is_older_pcomp[i], stq_issue))
                & (
                    Val(em.mux_index(addr_same_pcomp[i], stq_issue))
                    | ~Val(ldq_addr_valid_pcomp, i)
                ),
            )
        if self.configs.pipe0:
            # with pipelining: also compute for the next entry
            for i in range(self.configs.numLdqEntries):
                em.add_assignment(
                    (st_ld_conflict_next, i),
                    Val(ldq_alloc_pcomp, i)
                    & ~Val(load_completed, i)
                    & ~Val(em.mux_index(store_is_older_pcomp[i], stq_issue_next))
                    & (
                        Val(em.mux_index(addr_same_pcomp[i], stq_issue_next))
                        | ~Val(ldq_addr_valid_pcomp, i)
                    ),
                )

        if self.configs.pipe0:
            # with pipelining: multiplex between current and next store entry
            # Multiplex from current and next
            em.add_assignment(
                st_ld_conflict_p0,
                st_ld_conflict_next.when(stq_issue_en).else_(st_ld_conflict_curr),
            )
            em.add_assignment(
                store_req_valid_p0,
                store_req_valid_next.when(stq_issue_en).else_(store_req_valid_curr),
            )
        else:
            # without pipelining: only consider current store entry
            em.add_assignment(st_ld_conflict_p0, st_ld_conflict_curr)
            em.add_assignment(store_req_valid_p0, store_req_valid_curr)

        # Stalling Store Issue
        # For small queues relative to the memory latency, it is possible that all store entries
        # have been allocated and are in-flight to the memory. In this case, the store issue
        # pointer would wrap around and re-issue the same stores a second time. To avoid this, we
        # stall store issue once the issue pointer catches up to the tail pointer (i.e., when all
        # store entries are in-flight), and only allow store issue to proceed when the tail pointer
        # moves (indicating a store entry has been freed up and subsequently allocated again).
        store_issue_stall_p0 = Logic(em, 'store_issue_stall', 'r')
        store_issue_stall_set = Logic(em, 'store_issue_stall_set', 'w')
        store_issue_stall_reset = Logic(em, 'store_issue_stall_reset', 'w')
        store_issue_stall_p0.regInit(init=0)
        em.add_assignment(
            store_issue_stall_set,
            stq_issue_en.when(stq_issue_next == stq_tail).else_(Bit(0)),
        )
        em.add_assignment(store_issue_stall_reset, stq_tail_update_pcomp)
        em.add_assignment(
            store_issue_stall_p0,
            ~store_issue_stall_reset & (store_issue_stall_p0 | store_issue_stall_set),
        )

        # The store conflicts with any load
        Reduce(em, store_conflict, st_ld_conflict_p0, BinOp.OR)
        em.add_assignment(
            store_en,
            ~store_conflict & store_req_valid_p0 & ~store_issue_stall_p0,
        )
        em.add_assignment(store_idx, stq_issue)

        # Bypass
        bypass_idx_oh_p0 = LogicVecArray(
            em,
            'bypass_idx_oh_p0',
            pipe0_type,
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )
        bypass_en = LogicArray(em, 'bypass_en', 'w', self.configs.numLdqEntries)
        if self.configs.pipe0:
            bypass_idx_oh_p0.regInit()
        if self.configs.bypass:
            stq_last_oh = LogicVec(em, 'stq_last_oh', 'w', self.configs.numStqEntries)
            BitsToOHSub1(em, stq_last_oh, stq_tail)
            for i in range(0, self.configs.numLdqEntries):
                bypass_en_vec = LogicVec(
                    em, f'bypass_en_vec_{i}', 'w', self.configs.numStqEntries
                )
                # Search for the youngest store that is older than the load and conflicts
                CyclicPriorityMasking(
                    em, bypass_idx_oh_p0[i], ld_st_conflict[i], stq_last_oh, True
                )
                # Check if the youngest conflict store can bypass with the load
                em.add_assignment(bypass_en_vec, bypass_idx_oh_p0[i] & can_bypass[i])
                Reduce(em, bypass_en[i], bypass_en_vec, BinOp.OR)
        else:
            # bypass disabled: tie bypass signals low
            for i in range(0, self.configs.numLdqEntries):
                em.add_assignment(bypass_en[i], Bit(0))
            for i in range(0, self.configs.numLdqEntries):
                em.add_assignment(bypass_idx_oh_p0[i], Val(0))

        # Pipeline Stage 1

        # load registers (if enabled, w/ backpressure)
        load_idx_oh_p1 = LogicVecArray(
            em,
            'load_idx_oh_p1',
            pipe1_type,
            self.configs.numLdMem,
            self.configs.numLdqEntries,
        )
        load_en_p1 = LogicArray(em, 'load_en_p1', pipe1_type, self.configs.numLdMem)
        # store registers (if enabled, w/ backpressure)
        store_idx_p1 = LogicVec(em, 'store_idx_p1', pipe1_type, self.configs.stqAddrW)
        store_en_p1 = Logic(em, 'store_en_p1', pipe1_type)
        # bypass registers (if enabled, w/o backpressure)
        bypass_idx_oh_p1 = LogicVecArray(
            em,
            'bypass_idx_oh_p1',
            pipe1_type,
            self.configs.numLdqEntries,
            self.configs.numStqEntries,
        )
        bypass_en_p1 = LogicArray(
            em, 'bypass_en_p1', pipe1_type, self.configs.numLdqEntries
        )

        load_p1_ready = LogicArray(em, 'load_p1_ready', 'w', self.configs.numLdMem)
        store_p1_ready = Logic(em, 'store_p1_ready', 'w')

        if self.configs.pipe1:
            # pipeline register control signals (load_*_p1, store_*_p1)
            # This implements a pipeline register stage with backpressure and
            # with a # combinational path from output ready to input ready. We
            # are ready # for new data if either there is a handshake at the
            # output (*_hs), # or the register is currently empty (not *_en_p1).
            load_hs = LogicArray(em, 'load_hs', 'w', self.configs.numLdMem)
            for w in range(0, self.configs.numLdMem):
                em.add_assignment(load_hs[w], load_en_p1[w] & rreq_ready_i[w])
                em.add_assignment(load_p1_ready[w], load_hs[w] | ~load_en_p1[w])
            store_hs = Logic(em, 'store_hs', 'w')
            em.add_assignment(store_hs, store_en_p1 & wreq_ready_i[0])
            em.add_assignment(store_p1_ready, store_hs | ~store_en_p1)
            # register init
            load_idx_oh_p1.regInit(enable=load_p1_ready)
            load_en_p1.regInit(init=[0] * self.configs.numLdMem, enable=load_p1_ready)
            store_idx_p1.regInit(enable=store_p1_ready)
            store_en_p1.regInit(init=0, enable=store_p1_ready)
            bypass_idx_oh_p1.regInit()
            bypass_en_p1.regInit(init=[0] * self.configs.numLdqEntries)
        else:
            # non-pipelined "pseudo-control" signals
            for w in range(0, self.configs.numLdMem):
                em.add_assignment(load_p1_ready[w], rreq_ready_i[w] & load_en[w])
            em.add_assignment(store_p1_ready, wreq_ready_i[0])

        # pipeline register assignments
        for w in range(0, self.configs.numLdMem):
            em.add_assignment(load_idx_oh_p1[w], load_idx_oh[w])
            em.add_assignment(load_en_p1[w], load_en[w])
        em.add_assignment(store_idx_p1, store_idx)
        em.add_assignment(store_en_p1, store_en)
        for i in range(0, self.configs.numLdqEntries):
            em.add_assignment(bypass_idx_oh_p1[i], bypass_idx_oh_p0[i])
            em.add_assignment(bypass_en_p1[i], bypass_en[i])

        ######    Read/Write    ######
        # Read Request
        for w in range(0, self.configs.numLdMem):
            em.add_assignment(rreq_valid_o[w], load_en_p1[w])
            OHToBits(em, rreq_id_o[w], load_idx_oh_p1[w])
            Mux1H(em, rreq_addr_o[w], ldq_addr, load_idx_oh_p1[w])

        for i in range(0, self.configs.numLdqEntries):
            ldq_issue_set_vec = LogicVec(
                em, f'ldq_issue_set_vec_{i}', 'w', self.configs.numLdMem
            )
            for w in range(0, self.configs.numLdMem):
                em.add_assignment(
                    (ldq_issue_set_vec, w),
                    (Val(load_idx_oh, w, i) & Val(load_p1_ready, w))
                    | Val(bypass_en, i),
                )
            Reduce(em, ldq_issue_set[i], ldq_issue_set_vec, BinOp.OR)

        # Write Request
        em.add_assignment(wreq_valid_o[0], store_en_p1)
        em.add_assignment(wreq_id_o[0], Val(0))
        MuxLookUp(em, wreq_addr_o[0], stq_addr, store_idx_p1)
        if not self.configs.stDataPass:
            MuxLookUp(em, wreq_data_o[0], stq_data, store_idx_p1)
        em.add_assignment(stq_issue_en, store_en & store_p1_ready)

        if self.configs.stDataPass:
            # Store data passthrough
            # The data of a port's stores arrives in that port's program order,
            # so the oldest store whose data has not been sent yet tells which
            # port to forward from. stq_data_ptr walks the store queue for that
            # store, reading stq_port_idx as the port-ID queue. The entry
            # cannot be reused under the pointer: a store only completes once
            # its data has been written. Like the issue pointer, the data
            # pointer stalls once it catches up to the tail, until new stores
            # are allocated.
            stq_data_ptr = LogicVec(em, 'stq_data_ptr', 'r', self.configs.stqAddrW)
            stq_data_ptr_next = LogicVec(
                em, 'stq_data_ptr_next', 'w', self.configs.stqAddrW
            )
            stq_data_en = Logic(em, 'stq_data_en', 'w')
            stq_data_ptr.regInit(enable=stq_data_en, init=0)
            WrapAddConst(
                em, stq_data_ptr_next, stq_data_ptr, 1, self.configs.numStqEntries
            )
            em.add_assignment(stq_data_ptr, stq_data_ptr_next)

            # set: the last pending data leaves; hold until stores are allocated
            store_data_stall = Logic(em, 'store_data_stall', 'r')
            store_data_stall_set = Logic(em, 'store_data_stall_set', 'w')
            store_data_stall.regInit(init=0)
            em.add_assignment(
                store_data_stall_set,
                stq_data_en.when(stq_data_ptr_next == stq_tail).else_(Bit(0)),
            )
            em.add_assignment(
                store_data_stall,
                store_data_stall_set | (store_data_stall & ~stq_tail_update),
            )

            # The store under the pointer is waiting for its data. A tail update
            # in this cycle means the stall's entry was just (re)allocated.
            store_data_alloc = Logic(em, 'store_data_alloc', 'w')
            store_data_pending = Logic(em, 'store_data_pending', 'w')
            MuxLookUp(em, store_data_alloc, stq_alloc, stq_data_ptr)
            em.add_assignment(
                store_data_pending,
                store_data_alloc & (~store_data_stall | stq_tail_update),
            )

            store_data_port_valid = Logic(em, 'store_data_port_valid', 'w')
            if self.configs.numStPorts > 1:
                store_data_port = LogicVec(
                    em, 'store_data_port', 'w', self.configs.stpAddrW
                )
                MuxLookUp(em, store_data_port, stq_port_idx, stq_data_ptr)
                MuxLookUp(em, store_data_port_valid, stp_data_valid_i, store_data_port)
                MuxLookUp(em, wreq_data_o[0], stp_data_i, store_data_port)
            else:
                em.add_assignment(store_data_port_valid, stp_data_valid_i[0])
                em.add_assignment(wreq_data_o[0], stp_data_i[0])
            store_data_valid = Logic(em, 'store_data_valid', 'w')
            em.add_assignment(
                store_data_valid, store_data_pending & store_data_port_valid
            )
            em.add_assignment(wreq_data_valid_o[0], store_data_valid)
            em.add_assignment(stq_data_en, store_data_valid & wreq_data_ready_i[0])
            for p in range(self.configs.numStPorts):
                if self.configs.numStPorts > 1:
                    em.add_assignment(
                        stp_data_ready_o[p],
                        (store_data_pending & wreq_data_ready_i[0])
                        .when(store_data_port == Val(p, self.configs.stpAddrW))
                        .else_(Bit(0)),
                    )
                else:
                    em.add_assignment(
                        stp_data_ready_o[p], store_data_pending & wreq_data_ready_i[0]
                    )

        if self.configs.ldDataPass:
            assert self.configs.numLdMem == 1
            # The response goes to the port of the entry it answers; the port's
            # holding register keeps it while the port is not ready (memory
            # cannot be stalled). At most one load per port is in flight, so
            # the register and a new response never meet.
            resp_port_oh = LogicVec(em, 'resp_port_oh', 'w', self.configs.numLdPorts)
            if self.configs.numLdPorts == 1:
                em.add_assignment(resp_port_oh, Val(1))
            else:
                resp_port = LogicVec(em, 'resp_port', 'w', self.configs.ldpAddrW)
                MuxLookUp(em, resp_port, ldq_port_idx, rresp_id_i[0])
                BitsToOH(em, resp_port_oh, resp_port)
            ld_buf_valid = LogicArray(em, 'ld_buf_valid', 'r', self.configs.numLdPorts)
            ld_buf_data = LogicVecArray(
                em, 'ld_buf_data', 'r', self.configs.numLdPorts, self.configs.dataW
            )
            ld_resp_valid = LogicArray(
                em, 'ld_resp_valid', 'w', self.configs.numLdPorts
            )
            ld_port_valid = LogicArray(
                em, 'ld_port_valid', 'w', self.configs.numLdPorts
            )
            ld_buf_valid.regInit(init=[0] * self.configs.numLdPorts)
            ld_buf_data.regInit(enable=ld_resp_valid)
            for p in range(self.configs.numLdPorts):
                em.add_assignment(
                    ld_resp_valid[p], rresp_valid_i[0] & Val(resp_port_oh, p)
                )
                em.add_assignment(ld_port_valid[p], ld_buf_valid[p] | ld_resp_valid[p])
                em.add_assignment(ld_port_hs[p], ld_port_valid[p] & ldp_data_ready_i[p])
                em.add_assignment(ld_buf_valid[p], ld_port_valid[p] & ~ldp_data_ready_i[p])
                em.add_assignment(ld_buf_data[p], rresp_data_i[0])
                em.add_assignment(ldp_data_valid_o[p], ld_port_valid[p])
                em.add_assignment(
                    ldp_data_o[p],
                    ld_buf_data[p].when(ld_buf_valid[p]).else_(rresp_data_i[0]),
                )
            # A delivered value retires the oldest allocated load of its port.
            for i in range(self.configs.numLdqEntries):
                em.add_assignment(ldq_reset[i], ld_first[i] & ld_entry_hs[i])

        # Read Response and Bypass
        for i in range(0, self.configs.numLdqEntries):
            # check each read response channel for each load
            read_idx_oh = LogicArray(em, f'read_idx_oh_{i}', 'w', self.configs.numLdMem)
            read_valid = Logic(em, f'read_valid_{i}', 'w')
            if not self.configs.ldDataPass:
                read_data = LogicVec(em, f'read_data_{i}', 'w', self.configs.dataW)
            for w in range(0, self.configs.numLdMem):
                em.add_assignment(
                    read_idx_oh[w],
                    rresp_valid_i[w]
                    .when(rresp_id_i[w] == Val(i, self.configs.idW))
                    .else_(Bit(0)),
                )
            if not self.configs.ldDataPass:
                Mux1H(em, read_data, rresp_data_i, read_idx_oh)
            Reduce(em, read_valid, read_idx_oh, BinOp.OR)
            # multiplex from store queue data
            if self.configs.ldDataPass:
                pass  # the data went to the port; ldq_data_valid marks completion
            elif self.configs.stDataPass:
                em.add_assignment(ldq_data[i], read_data)
            else:
                bypass_data = LogicVec(em, f'bypass_data_{i}', 'w', self.configs.dataW)
                Mux1H(em, bypass_data, stq_data, bypass_idx_oh_p1[i])
                # multiplex from read and bypass data
                em.add_assignment(ldq_data[i], read_data | bypass_data)
            em.add_assignment(ldq_data_wen[i], bypass_en_p1[i] | read_valid)
        for w in range(0, self.configs.numLdMem):
            em.add_assignment(rresp_ready_o[w], Bit(1))

        # Write Response
        if self.configs.stResp:
            for i in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    stq_exec_set[i],
                    wresp_valid_i[0]
                    .when(stq_resp == Val(i, self.configs.stqAddrW))
                    .else_(Bit(0)),
                )
        else:
            for i in range(0, self.configs.numStqEntries):
                em.add_assignment(
                    stq_reset[i],
                    wresp_valid_i[0]
                    .when(stq_resp == Val(i, self.configs.stqAddrW))
                    .else_(Bit(0)),
                )
        em.add_assignment(stq_resp_en, wresp_valid_i[0])
        em.add_assignment(wresp_ready_o[0], Bit(1))

        ######   Write To File  ######
        # Write to the file
        output_str = em.get_definition_str(self.module_name)
        with open(f'{path_rtl}/{self.name}.{em.get_file_suffix()}', 'a') as file:
            file.write(output_str)

    def instantiate(self, **kwargs) -> str:
        """
        *Instantiation of LSQ is in lsq-generator.py.
        """
        pass
