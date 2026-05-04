from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import CyclicRightShift, MuxLookUp, Reduce
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, Type
from custom_core_gen.configs import DependencyCheckerConfig


class DependencyChecker:
    def __init__(self, name: str, suffix: str, configs: DependencyCheckerConfig):
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
            lq_corVal(0)e.generate(...)

        """

        self.name = name
        self.module_name = name + suffix
        self.configs = configs
        
        
    def generate(self, em: Emitter, lsq_submodules, path_rtl) -> None:
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

        ######  Queue Inputs ######
        # ===[ predecessor ]===
        pq_addr_i = LogicVecArray(
            em, "pq_addr", "i", self.configs.pq.num_entries, self.configs.pq.addr_width
        )
        pq_tail_i = LogicVec(em, "pq_tail", "i", self.configs.pq.q_addr_width)
        pq_done_i = LogicVec(em, "pq_done", "i", self.configs.pq.q_addr_width)
        pq_send_en_i = Logic(em, "pq_send_en", "i")
        pq_alloc_en_i = Logic(em, "pq_alloc_en", "i")
        
        # ====[ successor ]===
        sq_addr_i = LogicVecArray(
            em, "sq_addr", "i", self.configs.sq.num_entries, self.configs.sq.addr_width
        )
        sq_tail_i = LogicVec(em, "sq_tail", "i", self.configs.sq.q_addr_width)
        sq_head_i = LogicVec(em, "sq_head", "i", self.configs.sq.q_addr_width)
        sq_send_en_i = Logic(em, "sq_alloc_en", "i")
        
        ######  Outputs ######
        allow_pq_alloc_o = Logic(em, "allow_pq_alloc", "o")
        allow_sq_acces_o = Logic(em, "allow_access", "o")
        
        tail_position = LogicVec(em, "alloc_disparity", "w", self.configs.tail_position_width)                
        access_disparity = LogicVec(em, "access_disparity", "w", self.configs.access_disparity_width, is_signed=True)
        
        conflict = Logic(em, "no_conflict", "w")
        
        em.add_assignment(access_disparity, access_disparity - pq_send_en_i + sq_send_en_i)
        
        check_mask = LogicVec(em, "check_mask", "w", self.configs.pq.num_entries)
        ones = LogicVec(em, "ones", "w", self.configs.pq.num_entries)
        # generate access_disparity 1's for the check mask
        # TODO: access_disparity must be an integer as it can be negative!! check for this
        # ugly fix for signed handling, by default it is unsigned so we just directly print the integers
        for i in range(self.configs.pq.num_entries):
            em.add_assignment((ones, i), Bit(1).when(Val(str(i)) <= access_disparity).else_(Bit(0)))
        CyclicRightShift(em, check_mask, ones, pq_done_i)
        
        head_address = LogicVec(em, "head_address", "w", self.configs.sq.addr_width)
        MuxLookUp(em, head_address, sq_addr_i, sq_head_i)
        
        conflicts = LogicVec(em, "conflicts", "w", self.configs.pq.num_entries)
        for i in range(self.configs.pq.num_entries):
            em.add_assignment((conflicts, i), Val(check_mask, i) & (Val(sq_addr_i, i) == head_address))
            
        Reduce(em, conflict, conflicts, BinOp.OR)
        
        hp_width = self.configs.tail_position_width
        em.add_assignment(tail_position, tail_position + pq_alloc_en_i - sq_send_en_i)
        em.add_assignment(allow_sq_acces_o, (tail_position != Val(0, hp_width)) & (~conflict | (access_disparity < Val(0))))
        em.add_assignment(allow_pq_alloc_o, (tail_position != Val((1 << self.configs.tail_position_width) - 1, hp_width)))
        
        # TODO: checks for overflow of access_disparity
        # TODO: Proper integer handling in the emitter (is wrong right now)
        # TODO: Look into the shifter, seems to be using one-hot representation?
        
        # Write to the file
        output_str = em.get_definition_str(self.module_name)
        with open(f"{path_rtl}/{self.name}.{em.get_file_suffix()}", "a") as file:
            file.write(output_str)
            
    def instantiate(self, **kwargs) -> str:
        """
        *Instantiation of LoadQueue is in lsq-generator.py.
        """
        pass
