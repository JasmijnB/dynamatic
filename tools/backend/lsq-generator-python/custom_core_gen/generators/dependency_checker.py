from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import CyclicRightShift, MuxLookUp, Reduce
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, Type
from custom_core_gen.configs import DependencyCheckerConfig
from custom_core_gen.generators.generator import Generator


class DependencyChecker(Generator):
    def __init__(self, name: str, suffix: str, configs: DependencyCheckerConfig):
        super().__init__(name, suffix, configs)

    def generate(self, em: Emitter, path_rtl) -> None:
        self.ports.clear()

        pq_ptr_width = self.configs.pq.q_addr_width + 1

        ######  Queue Inputs ######
        # ===[ predecessor ]===
        pq_addr_i = self._add_port(LogicVecArray(
            em, "pq_addr", "i", self.configs.pq.num_entries, self.configs.pq.addr_width
        ))
        pq_done_i    = self._add_port(LogicVec(em, "pq_done",    "i", self.configs.pq.q_addr_width))
        pq_done_en_i = self._add_port(Logic   (em, "pq_send_en", "i"))
        pq_length_i  = self._add_port(LogicVec(em, "pq_length",  "i", pq_ptr_width))

        # ====[ successor ]===
        sq_addr_i = self._add_port(LogicVecArray(
            em, "sq_addr", "i", self.configs.sq.num_entries, self.configs.sq.addr_width
        ))
        self._add_port(         LogicVec(em, "sq_tail",      "i", self.configs.sq.q_addr_width))
        sq_head_i      = self._add_port(LogicVec(em, "sq_head",      "i", self.configs.sq.q_addr_width))
        sq_access_en_i = self._add_port(Logic   (em, "sq_access_en", "i"))

        ######  Outputs ######
        allow_sq_access_o = self._add_port(Logic(em, "allow_sq_access", "o"))

        tail_offset      = LogicVec(em, "tail_offset",      "w", pq_ptr_width)
        access_disparity = LogicVec(em, "access_disparity", "r", self.configs.access_disparity_width, is_signed=True)

        conflict = Logic(em, "conflict", "w")

        em.add_assignment(access_disparity, access_disparity - pq_done_en_i + sq_access_en_i)
        access_disparity.regInit()

        check_mask = LogicVec(em, "check_mask", "w", self.configs.pq.num_entries)
        ones = LogicVec(em, "ones", "w", self.configs.pq.num_entries)
        # generate access_disparity 1's for the check mask
        for i in range(self.configs.pq.num_entries):
            em.add_assignment((ones, i), Bit(1).when(Val(str(i)) <= access_disparity).else_(Bit(0)))
        CyclicRightShift(em, check_mask, ones, pq_done_i)

        tail_address = LogicVec(em, "tail_address", "w", self.configs.sq.addr_width)
        MuxLookUp(em, tail_address, sq_addr_i, sq_head_i)

        conflicts = LogicVec(em, "conflicts", "w", self.configs.pq.num_entries)
        for i in range(self.configs.pq.num_entries):
            em.add_assignment((conflicts, i), Val(check_mask, i) & (Val(pq_addr_i, i) == tail_address))

        Reduce(em, conflict, conflicts, BinOp.OR)

        # tail_offset = how many PQ entries are allocated but not yet matched by SQ accesses
        em.add_assignment(tail_offset, pq_length_i - access_disparity)

        em.add_comment("Allow access if:"
                    "\t- The tail position is not 0 (i.e. the predecessor has allocated the corresponding entry for this access)"
                    "\t- AND, there is either: "
                    "       - no conflict  "
                    "       - if the access disparity is negative, so the tail has moved past any preceding accesses from the predecessor"
                    "\t- AND, the access disparity offset is not maxed out"
                    )

        max_ad_val = (1 << (self.configs.access_disparity_width - 1)) - 1
        em.add_assignment(allow_sq_access_o, (tail_offset != Val(0, pq_ptr_width)) & (~conflict | (access_disparity < Val(0))) & (access_disparity <= Val(max_ad_val)))

        self._write_to_file(em, path_rtl)
