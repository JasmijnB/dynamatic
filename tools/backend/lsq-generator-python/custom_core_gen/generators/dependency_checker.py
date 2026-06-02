from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import CyclicRightShift, MuxLookUp, Reduce
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement
from custom_core_gen.configs import DependencyCheckerConfig
from custom_core_gen.generators.generator import Generator


class DependencyChecker(Generator):
    def __init__(self, name: str, suffix: str, configs: DependencyCheckerConfig):
        super().__init__(name, suffix, configs)

    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        self.ports.clear()

        pq_ptr_width = self.configs.pq.q_addr_width + 1

        ######  Queue Inputs ######
        # ===[ predecessor ]===
        pq_addr_i = self._add_port(LogicVecArray(
            em, "pq_addr", "i", self.configs.pq.num_entries, self.configs.pq.addr_width
        ))
        pq_done_i    = self._add_port(LogicVec(em, "pq_done",    "i", self.configs.pq.q_addr_width))
        pq_done_en_i = self._add_port(Logic   (em, "pq_done_en", "i"))
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
        allow_pq_access_o = self._add_port(Logic(em, "allow_pq_access", "o"))
        # TODO: Only allow predecessor access when the access disparity bit cannot overflow
        em.add_assignment(allow_pq_access_o, Bit(1)) 

        ad_width = self.configs.access_disparity_width
        access_disparity = LogicVec(em, "access_disparity", "r", ad_width, is_signed=True)

        conflict = Logic(em, "conflict", "w")
        
        inc_ad = LogicVec(em, "inc_access_disparity", "w", self.configs.access_disparity_width, is_signed=True)
        dec_ad = LogicVec(em, "dec_access_disparity", "w", self.configs.access_disparity_width, is_signed=True)
        em.add_assignment(inc_ad, Val(1).when(sq_access_en_i).else_(Val(0)))
        em.add_assignment(dec_ad, Val(1).when(pq_done_en_i).else_(Val(0)))
        em.add_assignment(access_disparity, (access_disparity + inc_ad) - dec_ad)

        # if the successor port executes sequentially before the predcessor port, 
        # initialise the access disparity to -1 (implying the pred already executed once)
        ad_initial_value = 0 if not self.configs.succ_can_execute_once else -1
        access_disparity.regInit(init=ad_initial_value)

        check_mask = LogicVec(em, "check_mask", "w", self.configs.pq.num_entries)
        ones = LogicVec(em, "ones", "w", self.configs.pq.num_entries)
        # generate access_disparity 1's for the check mask
        for i in range(self.configs.pq.num_entries):
            em.add_assignment((ones, i), Bit(1).when(Val(i, size=ad_width) <= access_disparity).else_(Bit(0)))
        CyclicRightShift(em, check_mask, ones, pq_done_i)

        tail_address = LogicVec(em, "tail_address", "w", self.configs.sq.addr_width)
        MuxLookUp(em, tail_address, sq_addr_i, sq_head_i)

        conflicts = LogicVec(em, "conflicts", "w", self.configs.pq.num_entries)
        for i in range(self.configs.pq.num_entries):
            em.add_assignment((conflicts, i), Val(check_mask, i).when(Val(pq_addr_i, i) == tail_address).else_(Bit(0)))

        Reduce(em, conflict, conflicts, BinOp.OR)

        # Bring both operands to cmp_width bits so the equality check is type-safe.
        # pq_length_i is non-negative so it gets zero-extended;
        # access_disparity is signed so it gets sign-extended via resize when needed.
        cmp_width = max(self.configs.access_disparity_width, pq_ptr_width)

        pq_length_as_cmp = LogicVec(em, "pq_length_as_cmp", "w", cmp_width, is_signed=True)
        pq_pad = cmp_width - pq_ptr_width
        if pq_pad > 0:
            em.add_assignment(pq_length_as_cmp, Val(0, pq_pad).concat(pq_length_i))
        else:
            em.add_assignment(pq_length_as_cmp, pq_length_i)

        ad_pad = cmp_width - self.configs.access_disparity_width
        if ad_pad > 0:
            ad_as_cmp = LogicVec(em, "ad_as_cmp", "w", cmp_width, is_signed=True)
            em.add_custom_statement(CustomStatement(
                f"{ad_as_cmp.getNameWrite()} <= resize({access_disparity.getNameRead()}, {cmp_width});",
                f"assign {ad_as_cmp.getNameWrite()} = {access_disparity.getNameRead()};",
            ))
        else:
            ad_as_cmp = access_disparity


        corresponding_entry_sent = Logic(em, "corresponding_entry_sent", "w")
        corresponding_entry_allocated = Logic(em, "corresponding_entry_allocated", "w")
        em.add_assignment(corresponding_entry_allocated, (pq_length_as_cmp != ad_as_cmp))
        em.add_assignment(corresponding_entry_sent, access_disparity < Val(0, size=ad_width))

        em.add_comment("Allow access if:"
                    "\t- The access disparity has reached the pq_length "
                    "(i.e. the predecessor has allocated the corresponding entry for this access)\n"
                    "\t- AND, there is either: \n"
                    "       - no conflict  \n"
                    "       - if the access disparity is negative (i.e. the corresponding entry has been complete)\n"
                    "\t- AND, the access disparity is not maxed out\n"
                    )
        max_ad_val = (1 << (self.configs.access_disparity_width - 1)) - 1
        em.add_assignment(allow_sq_access_o,
            corresponding_entry_allocated & (~conflict | corresponding_entry_sent) & (access_disparity <= Val(max_ad_val, size=ad_width)))

        self._write_to_file(em, path_rtl, out_file)
