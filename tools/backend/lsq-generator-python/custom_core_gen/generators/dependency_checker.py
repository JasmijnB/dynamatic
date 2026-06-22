import math
from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import BitsToOH, CyclicPriorityMasking, CyclicRightShift, MuxLookUp, OHToBits, Reduce, WrapAddConst, WrapSub
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement
from custom_core_gen.configs import DependencyCheckerConfig
from custom_core_gen.generators.generator import Generator
from functools import reduce


class DependencyChecker(Generator):
    def __init__(self, name: str, suffix: str, configs: DependencyCheckerConfig):
        super().__init__(name, suffix, configs)

    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        self.ports.clear()
        crosses_bb = self.configs.pq_bb != self.configs.sq_bb

        pq_ptr_width = self.configs.pq.q_addr_width + 1
        
        ######  Queue Inputs ######
        # ===[ predecessor ]===
        pq_addr_i = self._add_port(
            LogicVecArray(
                em,
                "pq_addr",
                "i",
                self.configs.pq.num_entries,
                self.configs.pq.addr_width,
            )
        )
        pq_done_i = self._add_port(
            LogicVec(em, "pq_done", "i", self.configs.pq.q_addr_width)
        )
        pq_done_en_i = self._add_port(Logic(em, "pq_done_en", "i"))
        pq_length_i = self._add_port(LogicVec(em, "pq_length", "i", pq_ptr_width))

        # ====[ successor ]===
        sq_head = self._add_port(
            LogicVec(em, "sq_head", "i", self.configs.sq.addr_width)
        )
        sq_access_en_i = self._add_port(Logic(em, "sq_access_en", "i"))

        ######  Outputs ######
        allow_sq_access_o = self._add_port(Logic(em, "allow_sq_access", "o"))
        allow_pq_access_o = self._add_port(Logic(em, "allow_pq_access", "o"))
        # TODO: Only allow predecessor access when the access disparity bit cannot overflow
        em.add_assignment(allow_pq_access_o, Bit(1))

        ad_width = self.configs.access_disparity_width

        conflict = Logic(em, "conflict", "w")

        if not crosses_bb:
            access_disparity = LogicVec(
                em, "access_disparity", "r", ad_width, is_signed=True
            )
            # if the successor port executes sequentially before the predcessor port,
            # initialise the access disparity to -1 (implying the pred already executed once)
            ad_initial_value = 0 if not (self.configs.succ_can_execute_once) else -1
            access_disparity.regInit(init=ad_initial_value)
            inc_ad = LogicVec(
                em,
                "inc_access_disparity",
                "w",
                self.configs.access_disparity_width,
                is_signed=True,
            )
            dec_ad = LogicVec(
                em,
                "dec_access_disparity",
                "w",
                self.configs.access_disparity_width,
                is_signed=True,
            )
            em.add_assignment(inc_ad, Val(1).when(sq_access_en_i).else_(Val(0)))
            em.add_assignment(dec_ad, Val(1).when(pq_done_en_i).else_(Val(0)))
            em.add_assignment(access_disparity, (access_disparity + inc_ad) - dec_ad)
        else:
            access_disparity = LogicVec(
                em, "access_disparity", "w", ad_width, is_signed=True
            )
            access_disparity_base = LogicVec(
                em, "access_disparity_base", "r", ad_width, is_signed=True
            )
            access_disparity_base.regInit(init=-1)


            inc_ad = LogicVec( em, "inc_access_disparity", "w", self.configs.access_disparity_width, is_signed=True)
            dec_ad = LogicVec(em, "dec_access_disparity", "w", self.configs.access_disparity_width, is_signed=True)
            ad_decr = LogicVec(em, "ad_decr", "w", ad_width, is_signed=True)
            ad_incr = LogicVec(em, "ad_incr", "w", ad_width, is_signed=True)

            pq_array_at_head, sq_array_at_head, new_ad_bits, pq_dep_addr_width, undo_inc = \
                self.generate_dep_arrays(em, pq_done_en_i, sq_access_en_i, ad_incr, access_disparity_base)

            em.add_assignment(dec_ad, Val(1).when(pq_done_en_i & (pq_array_at_head | (access_disparity_base > Val(0, size=ad_width)))).else_(Val(0)))

            # `dec_ad` decrements eagerly on a predecessor done, reading the head's
            # boundary bit. When the head is the latest P that bit is only a
            # placeholder, so the decrement is speculative; `undo_val` adds the 1
            # back if a later predecessor proves that P was not its group's last.
            undo_val = LogicVec(em, "ad_undo_val", "w", ad_width, is_signed=True)
            em.add_assignment(undo_val, Val(1).when(undo_inc).else_(Val(0)))
            em.add_assignment(ad_decr, (access_disparity_base - dec_ad) + undo_val)
            
            em.add_assignment(ad_incr, ad_decr + Val(1))
            


            ad_final = LogicVec(em, "ad_final_val", "w", ad_width, is_signed=True)
            em.add_assignment(ad_final, ad_incr.when(sq_array_at_head).else_(ad_decr))

            pad = ad_width - pq_dep_addr_width
            if pad > 0:
                ad_jumped = LogicVec(em, "pq_new_ad_widened", "w", ad_width, is_signed=True)
                # new_ad is a head-relative cyclic distance in a dep array sized
                # dep_entry_ratio (2x) larger than the queue, so a boundary that lies
                # behind the head reads as a two's-complement negative value
                # (e.g. -1 == all-ones). It must be sign-extended into the wider
                # signed access_disparity: zero-extension would turn -1 into +31,
                # so corresponding_entry_sent (access_disparity < 0) never fires and
                # the successor deadlocks waiting for a predecessor that already retired.
                em.add_custom_statement(CustomStatement(
                    f"{ad_jumped.getNameWrite()} <= resize(signed({new_ad_bits.getNameRead()}), {ad_width});",
                    f"assign {ad_jumped.getNameWrite()} = $signed({new_ad_bits.getNameRead()});",
                ))
            else:
                ad_jumped = new_ad_bits

            ad_next = LogicVec(em, "ad_next", "w", ad_width, is_signed=True)
            em.add_assignment(ad_next, ad_final.when(ad_final < Val(0, size=ad_width)).else_(ad_jumped))
            em.add_assignment(access_disparity, ad_next)

            em.add_assignment(access_disparity_base, access_disparity.when(sq_access_en_i).else_(ad_decr))


        check_mask = LogicVec(em, "check_mask", "w", self.configs.pq.num_entries)
        ones = LogicVec(em, "ones", "w", self.configs.pq.num_entries)
        # generate access_disparity 1's for the check mask
        for i in range(self.configs.pq.num_entries):
            em.add_assignment(
                (ones, i),
                Bit(1).when(Val(i, size=ad_width) <= access_disparity).else_(Bit(0)),
            )
        CyclicRightShift(em, check_mask, ones, pq_done_i)

        conflicts = LogicVec(em, "conflicts", "w", self.configs.pq.num_entries)
        for i in range(self.configs.pq.num_entries):
            em.add_assignment(
                (conflicts, i),
                Val(check_mask, i).when(Val(pq_addr_i, i) == sq_head).else_(Bit(0)),
            )

        Reduce(em, conflict, conflicts, BinOp.OR)

        # Bring both operands to cmp_width bits so the equality check is type-safe.
        # pq_length_i is non-negative so it gets zero-extended;
        # access_disparity is signed so it gets sign-extended via resize when needed.
        cmp_width = max(self.configs.access_disparity_width, pq_ptr_width)

        pq_length_as_cmp = LogicVec(
            em, "pq_length_as_cmp", "w", cmp_width, is_signed=True
        )
        pq_pad = cmp_width - pq_ptr_width
        if pq_pad > 0:
            em.add_assignment(pq_length_as_cmp, Val(0, pq_pad).concat(pq_length_i))
        else:
            em.add_assignment(pq_length_as_cmp, pq_length_i)

        ad_pad = cmp_width - self.configs.access_disparity_width
        if ad_pad > 0:
            ad_as_cmp = LogicVec(em, "ad_as_cmp", "w", cmp_width, is_signed=True)
            em.add_custom_statement(
                CustomStatement(
                    f"{ad_as_cmp.getNameWrite()} <= resize({access_disparity.getNameRead()}, {cmp_width});",
                    f"assign {ad_as_cmp.getNameWrite()} = {access_disparity.getNameRead()};",
                )
            )
        else:
            ad_as_cmp = access_disparity

        corresponding_entry_sent = Logic(em, "corresponding_entry_sent", "w")
        corresponding_entry_allocated = Logic(em, "corresponding_entry_allocated", "w")
        em.add_assignment(
            corresponding_entry_allocated, (pq_length_as_cmp > ad_as_cmp)
        )
        em.add_assignment(
            corresponding_entry_sent, access_disparity < Val(0, size=ad_width)
        )

        em.add_comment(
            "Allow access if:"
            "\t- The predecessor queue length has reached the queue length "
            "(i.e. the predecessor has allocated the corresponding entry for this access)\n"
            "\t- AND, there is either: \n"
            "       - no conflict  \n"
            "       - if the access disparity is negative (i.e. the corresponding entry has been complete)\n"
            "\t- AND, the access disparity is not maxed out\n"
        )
        # ad is a signed number, so the max value it can take is 2^(n-1) - 1
        max_ad_val = (1 << (self.configs.access_disparity_width - 1)) - 1

        conditions = [
            corresponding_entry_allocated,
            ~conflict | corresponding_entry_sent,
        ]
        
        # if the max access disparity is larger or equal to to the queue size, 
        # we don't need to check for overflows as the first condition already guarantees 
        # that the access disparity cannot exceed the max
        # because access_disparity <= pq_length_i and pq_length_i <= num_entries, so access_disparity <= num_entries
        if max_ad_val < self.configs.pq.num_entries:
            conditions.append(access_disparity <= Val(max_ad_val, size=ad_width))

        em.add_assignment(
            allow_sq_access_o,
            reduce(lambda a, b: a & b, conditions)
        )

        self._write_to_file(em, path_rtl, out_file)
        
    def _make_bb_ports(self, em: Emitter, prefix: str, ready):
        valid_i = self._add_port(Logic(em, f"{prefix}_bb_valid", "i"))
        ready_o = self._add_port(Logic(em, f"{prefix}_bb_ready", "o"))
        executed = Logic(em, f"{prefix}_bb_executed", "w")
        em.add_assignment(ready_o, ready)
        em.add_assignment(executed, valid_i & ready)
        return executed

    def _make_dep_array(self, em: Emitter, prefix: str, n_entries: int, head_en, write_tuple: Logic):
        addr_width = math.ceil(math.log2(n_entries))
        ptr_width = addr_width + 1

        array = LogicArray(em, f"{prefix}_array", "r", n_entries)
        array.regInit()

        head = LogicVec(em, f"{prefix}_dep_head", "r", ptr_width)
        head_next = LogicVec(em, f"{prefix}_dep_head_next", "w", ptr_width)
        WrapAddConst(em, head_next, head, 1, n_entries)
        em.add_assignment(head, head_next)
        head.regInit(init=0, enable=head_en)

        tail = LogicVec(em, f"{prefix}_dep_tail", "r", ptr_width)
        tail_next = LogicVec(em, f"{prefix}_dep_tail_next", "w", ptr_width)
        tail_en = Logic(em, f"{prefix}_dep_tail_en", "w")
        WrapAddConst(em, tail_next, tail, 1, n_entries)
        em.add_assignment(tail, tail_next)
        tail.regInit(init=0, enable=tail_en)

        tail_idx = LogicVec(em, f"{prefix}_dep_tail_idx", "w", addr_width)
        em.add_assignment(tail_idx, Val(em.slice_var(tail.getNameRead(), addr_width - 1, 0)))
        tail_oh = LogicVec(em, f"{prefix}_dep_tail_oh", "w", n_entries)
        BitsToOH(em, tail_oh, tail_idx)

        # Empty before the push (head == tail). When empty there is no previous
        # entry, so the prev_val write to tail-1 must be suppressed: otherwise it
        # wraps to slot n-1 (behind the head), planting a stray '1' outside the
        # active [head, tail) window that the priority search would escape to.
        empty = Logic(em, f"{prefix}_dep_empty", "w")
        em.add_assignment(empty,
            Val(em.slice_var(tail.getNameRead(), ptr_width - 1, 0))
            == Val(em.slice_var(head.getNameRead(), ptr_width - 1, 0)))

        if isinstance(write_tuple, tuple):
            prev_val, curr_val = write_tuple
            for i in range(n_entries):
                em.add_assignment(array[i], curr_val.when(Val(tail_oh, i) & tail_en)
                                  .else_((prev_val).when(Val(tail_oh, (i + 1) % n_entries) & tail_en)
                                  .else_(array[i])))
        else:
            for i in range(n_entries):
                em.add_assignment(array[i], write_tuple.when(Val(tail_oh, i) & tail_en).else_(array[i]))

        array.regInit()

        full = Logic(em, f"{prefix}_dep_full", "w")
        tail_msb = Val(em.index_var(tail.getNameRead(), addr_width))
        head_msb = Val(em.index_var(head.getNameRead(), addr_width))
        tail_low = Val(em.slice_var(tail.getNameRead(), addr_width - 1, 0))
        head_low = Val(em.slice_var(head.getNameRead(), addr_width - 1, 0))
        em.add_assignment(full, (tail_msb != head_msb) & (tail_low == head_low))

        head_idx = LogicVec(em, f"{prefix}_dep_head_idx", "w", addr_width)

        em.add_assignment(head_idx, Val(em.slice_var(head.getNameRead(), addr_width - 1, 0)))
        array_at_head = Logic(em, f"{prefix}_array_at_head", "w")
        MuxLookUp(em, array_at_head, array, head_idx)

        return array, head, tail, tail_en, full, array_at_head

    def generate_dep_arrays(self, em: Emitter, pq_done_en, sq_access_en, access_disparity, access_disparity_base):
        n_pq_entries = self.configs.pq.num_entries * self.configs.dep_entry_ratio
        n_sq_entries = self.configs.sq.num_entries * self.configs.dep_entry_ratio

        
        pq_write_value = Logic(em, "pq_write_value", "w")
        sq_write_value = Logic(em, "sq_write_value", "w")
        
        pq_array, pq_dep_head, pq_dep_tail, pq_dep_tail_en, pq_dep_full, pq_array_at_head = \
            self._make_dep_array(em, "pq", n_pq_entries, pq_done_en, (pq_write_value, Bit(1)))
        sq_array, sq_dep_head, sq_dep_tail, sq_dep_tail_en, sq_dep_full, sq_array_at_head = \
            self._make_dep_array(em, "sq", n_sq_entries, sq_access_en, sq_write_value)

        pq_bb_executed = self._make_bb_ports(em, "pq", ~pq_dep_full)
        sq_bb_executed = self._make_bb_ports(em, "sq", ~sq_dep_full)
        
        em.add_assignment(pq_dep_tail_en, pq_bb_executed)
        em.add_assignment(sq_dep_tail_en, sq_bb_executed)
        
        # TODO: What if the BBs execute at the same time? 
        # -> For now not possible
        
        pq_executed_last = Logic(em, "pq_executed_last", "r")
        sq_executed_last = Logic(em, "sq_executed_last", "r")
        
        em.add_assignment(pq_executed_last, ~sq_bb_executed & (pq_bb_executed | pq_executed_last))
        em.add_assignment(sq_executed_last, ~pq_bb_executed & (sq_bb_executed | sq_executed_last))
        
        # At reset no predecessor has executed yet, so a successor that genuinely
        # goes first must stamp its dep entry with 0 (sq_write_value = pq_executed_last)
        # and keep its free pass, rather than be marked dependent on a predecessor
        # that never ran.
        pq_executed_last.regInit(init=0)
        sq_executed_last.regInit(init=1)
        
        em.add_assignment(pq_write_value, sq_executed_last)
        em.add_assignment(sq_write_value, pq_executed_last)

        pq_dep_addr_width = math.ceil(math.log2(n_pq_entries))

        # The successor dep queue is empty when its head and tail pointers coincide;
        # an empty queue has no head entry, so it must not raise the disparity.
        sq_not_empty = Logic(em, "sq_dep_not_empty", "w")
        em.add_assignment(
            sq_not_empty,
            Val(sq_dep_head.getNameRead()) != Val(sq_dep_tail.getNameRead()),
        )

        ad_idx = LogicVec(em, "ad_idx", "w", pq_dep_addr_width)
        # access_disparity is `signed` in VHDL; slicing it yields `signed`, not
        # `std_logic_vector`, so an explicit cast is required on the VHDL side.
        em.add_custom_statement(CustomStatement(
            f"{ad_idx.getNameWrite()} <= std_logic_vector({access_disparity.getNameRead()}({pq_dep_addr_width - 1} downto 0));",
            f"assign {ad_idx.getNameWrite()} = {access_disparity.getNameRead()}[{pq_dep_addr_width - 1}:0];",
        ))
        ad_oh = LogicVec(em, "ad_oh", "w", n_pq_entries)
        BitsToOH(em, ad_oh, ad_idx)

        pq_masked = LogicVec(em, "pq_masked", "w", n_pq_entries)
        CyclicPriorityMasking(em, pq_masked, pq_array, ad_oh)

        # Absolute position (within the dep array) of the next dependency boundary.
        new_ad_abs = LogicVec(em, "new_ad_abs", "w", pq_dep_addr_width)
        OHToBits(em, new_ad_abs, pq_masked)

        # Make it relative to the queue start (the dep array head), i.e. the cyclic
        # distance from the head to the boundary. access_disparity is consumed as a
        # count from the queue's done pointer, so it must be head-relative, not an
        # absolute dep-array index.
        pq_dep_head_idx = LogicVec(em, "pq_dep_head_idx_rel", "w", pq_dep_addr_width)
        em.add_assignment(pq_dep_head_idx,
            Val(em.slice_var(pq_dep_head.getNameRead(), pq_dep_addr_width - 1, 0)))

        new_ad = LogicVec(em, "new_ad", "w", pq_dep_addr_width)
        WrapSub(em, new_ad, new_ad_abs, pq_dep_head_idx, n_pq_entries)

        self.dep_full = Logic(em, "dep_full", "w")
        em.add_assignment(self.dep_full, pq_dep_full | sq_dep_full)

        # --- speculative-decrement undo ---------------------------------------
        # The eager decrement (in `generate`) reads pq_array_at_head. When the head
        # is the most-recent P that bit is still a placeholder '1', so the
        # decrement is a guess that this P is the last of its group. We only learn
        # the truth once the next item executes:
        #   * a successor (S) executes  -> the P really was last, keep it.
        #   * another predecessor (P) executes first -> it was not last, undo.
        # The decrement must stay eager (not deferred): a predecessor done has to
        # cancel the successor's disparity increment in the same window, otherwise
        # the succ-can-execute-once loop deadlocks.
        ad_width = self.configs.access_disparity_width
        ptr_width = pq_dep_addr_width + 1

        # head is the most-recent P iff advancing it by one reaches the tail
        # (exactly one entry occupied); only then is pq_array_at_head a placeholder.
        pq_dep_head_plus1 = LogicVec(em, "pq_dep_head_plus1", "w", ptr_width)
        WrapAddConst(em, pq_dep_head_plus1, pq_dep_head, 1, n_pq_entries)
        pq_head_is_latest = Logic(em, "pq_head_is_latest", "w")
        em.add_assignment(
            pq_head_is_latest,
            Val(pq_dep_head_plus1.getNameRead()) == Val(pq_dep_tail.getNameRead()),
        )

        # In the positive-lead regime every done legitimately decrements, so those
        # are never speculative.
        ad_base_positive = Logic(em, "ad_base_positive", "w")
        em.add_assignment(
            ad_base_positive, access_disparity_base > Val(0, size=ad_width)
        )

        # A done on the placeholder (latest P, no S since it) is speculative.
        spec_dec_set = Logic(em, "spec_dec_set", "w")
        em.add_assignment(
            spec_dec_set,
            pq_done_en & pq_head_is_latest & ~sq_executed_last & ~ad_base_positive,
        )
        # At most one speculative decrement is outstanding: the moment another item
        # executes it is resolved. Held until then.
        spec_dec_pending = Logic(em, "spec_dec_pending", "r")
        em.add_assignment(
            spec_dec_pending,
            ~(pq_bb_executed | sq_bb_executed) & (spec_dec_pending | spec_dec_set),
        )
        spec_dec_pending.regInit(init=0)

        # Undo when the next executed item is a predecessor (no S in between): the
        # speculative P was not the last of its group.
        undo_inc = Logic(em, "ad_undo_inc", "w")
        em.add_assignment(
            undo_inc,
            (spec_dec_pending | spec_dec_set) & pq_bb_executed & ~sq_bb_executed,
        )

        return pq_array_at_head, sq_array_at_head, new_ad, pq_dep_addr_width, undo_inc

