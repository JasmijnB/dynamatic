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

        if crosses_bb:
            access_disparity = self._cross_bb_disparity(
                em, pq_done_en_i, sq_access_en_i, ad_width
            )
        else:
            access_disparity = self._same_bb_disparity(
                em, sq_access_en_i, pq_done_en_i, ad_width
            )

        self._emit_allow(
            em, access_disparity, pq_addr_i, pq_done_i, sq_head, pq_length_i,
            pq_ptr_width, ad_width, allow_sq_access_o,
        )

        self._write_to_file(em, path_rtl, out_file)

    # ===----------------------------------------------------------------------===
    # Access disparity (same BB)
    # ===----------------------------------------------------------------------===
    def _same_bb_disparity(self, em: Emitter, sq_access_en_i, pq_done_en_i, ad_width):
        """Same-BB disparity is a plain signed up/down counter: +1 per successor
        access, -1 per predecessor done."""
        access_disparity = LogicVec(
            em, "access_disparity", "r", ad_width, is_signed=True
        )
        # if the successor port executes sequentially before the predcessor port,
        # initialise the access disparity to 0 (implying the pred already executed once,
        # so there is nothing to check); otherwise 1 (one P access still to check)
        ad_initial_value = 1 if not (self.configs.succ_can_execute_once) else 0
        access_disparity.regInit(init=ad_initial_value)
        inc_ad = LogicVec(em, "inc_access_disparity", "w", ad_width, is_signed=True)
        dec_ad = LogicVec(em, "dec_access_disparity", "w", ad_width, is_signed=True)
        em.add_assignment(inc_ad, Val(1).when(sq_access_en_i).else_(Val(0)))
        em.add_assignment(dec_ad, Val(1).when(pq_done_en_i).else_(Val(0)))
        em.add_assignment(access_disparity, (access_disparity + inc_ad) - dec_ad)
        return access_disparity

    # ===----------------------------------------------------------------------===
    # Access disparity (cross BB): two-mode FSM
    # ===----------------------------------------------------------------------===
    def _cross_bb_disparity(self, em: Emitter, pq_done_en_i, sq_access_en_i, ad_width):
        """Two explicit modes replace the old signed-counter-with-sign-tricks:
          * queue mode (a pending predecessor group lies ahead): the boundary is a
            one-hot bit `ad_oh` in the predecessor dep array; the live disparity is
            the head-relative distance to it (+1). As predecessors retire the head
            walks toward the bit; when a `pq_done_en` lands on the bit the boundary
            is consumed and we drop to counting mode.
          * counting mode (the successor is ahead): an unsigned credit counter
            `ad_credit` tracks completed-but-unmatched predecessor groups. The
            successor may always issue; each boundary-predecessor done adds a credit,
            each successor access spends one, and exhausting the credit on a
            successor access (with a real pending boundary) flips back to queue mode.
        """
        n_pq = self.configs.pq.num_entries * self.configs.dep_entry_ratio
        n_sq = self.configs.sq.num_entries * self.configs.dep_entry_ratio
        pq_addr_w = math.ceil(math.log2(n_pq))
        sq_addr_w = math.ceil(math.log2(n_sq))
        sq_ptr_w = sq_addr_w + 1

        (pq_array, pq_dep_head, pq_dep_tail, sq_dep_head, sq_dep_tail,
         pq_array_at_head, _, sq_bb_executed, _) = \
            self.generate_dep_arrays(em, pq_done_en_i, sq_access_en_i)

        # ---- state ----
        counting_mode = Logic(em, "counting_mode", "r")
        ad_credit = LogicVec(em, "ad_credit", "r", ad_width)   # unsigned
        ad_oh = LogicVec(em, "ad_oh", "r", n_pq)               # one-hot boundary

        access_disparity, ad_dist, pq_head_oh = self._cross_bb_geometry(
            em, ad_oh, counting_mode, pq_dep_head, n_pq, pq_addr_w, ad_width
        )

        events = self._cross_bb_events(
            em, ad_dist, pq_addr_w, counting_mode, pq_done_en_i, sq_access_en_i,
            pq_array_at_head, ad_credit, pq_dep_head, pq_dep_tail,
            sq_dep_head, sq_dep_tail, sq_bb_executed, n_sq, sq_ptr_w, ad_width,
        )

        self._cross_bb_next_state(
            em, counting_mode, ad_credit, ad_oh, events,
            pq_array, pq_head_oh, n_pq, ad_width,
        )

        return access_disparity

    def _cross_bb_geometry(self, em, ad_oh, counting_mode, pq_dep_head, n_pq, pq_addr_w, ad_width):
        """Queue-mode geometry and the live `access_disparity` wire: 0 in counting
        mode, the head-relative distance to the boundary bit (+1) in queue mode.
        This is a pure function of the state registers (no `sq_access_en`
        dependence) so it cannot form a combinational loop with the queue's
        allow/access handshake."""
        pq_head_idx = LogicVec(em, "pq_dc_head_idx", "w", pq_addr_w)
        em.add_assignment(pq_head_idx,
            Val(em.slice_var(pq_dep_head.getNameRead(), pq_addr_w - 1, 0)))
        pq_head_oh = LogicVec(em, "pq_dc_head_oh", "w", n_pq)
        BitsToOH(em, pq_head_oh, pq_head_idx)

        ad_abs = LogicVec(em, "ad_oh_idx", "w", pq_addr_w)
        OHToBits(em, ad_abs, ad_oh)
        ad_dist = LogicVec(em, "ad_dist", "w", pq_addr_w)
        WrapSub(em, ad_dist, ad_abs, pq_head_idx, n_pq)

        qmode_ad = LogicVec(em, "qmode_ad", "w", ad_width, is_signed=True)
        qpad = ad_width - pq_addr_w
        if qpad > 0:
            em.add_assignment(qmode_ad, Val(0, qpad).concat(ad_dist) + Val(1))
        else:
            em.add_assignment(qmode_ad, ad_dist + Val(1))

        access_disparity = LogicVec(em, "access_disparity", "w", ad_width, is_signed=True)
        em.add_assignment(access_disparity,
            Val(0, size=ad_width).when(counting_mode).else_(qmode_ad))
        return access_disparity, ad_dist, pq_head_oh

    def _cross_bb_events(self, em, ad_dist, pq_addr_w, counting_mode, pq_done_en_i,
                         sq_access_en_i, pq_array_at_head, ad_credit,
                         pq_dep_head, pq_dep_tail, sq_dep_head, sq_dep_tail,
                         sq_bb_executed, n_sq, sq_ptr_w, ad_width):
        """Decode the per-cycle events that drive the next state: boundary consume,
        credit inc/dec, genuine new successor head (vs. a drain), and the
        counting->queue transition."""
        # boundary consumed: head sits on the bit (dist == 0) and a predecessor retires
        at_boundary = Logic(em, "ad_at_boundary", "w")
        em.add_assignment(at_boundary, ad_dist == Val(0, size=pq_addr_w))
        consume = Logic(em, "ad_consume", "w")
        em.add_assignment(consume, ~counting_mode & pq_done_en_i & at_boundary)

        credit_inc = Logic(em, "ad_credit_inc", "w")
        em.add_assignment(credit_inc, pq_done_en_i & pq_array_at_head)
        credit_dec = Logic(em, "ad_credit_dec", "w")
        em.add_assignment(credit_dec, sq_access_en_i)
        credit_zero = Logic(em, "ad_credit_zero", "w")
        em.add_assignment(credit_zero, ad_credit == Val(0, size=ad_width))

        # Successor dep-queue occupancy: tell a genuine new head from an access that
        # merely drains the queue (no head behind it).
        sq_head_p1 = LogicVec(em, "sq_dc_head_p1", "w", sq_ptr_w)
        WrapAddConst(em, sq_head_p1, sq_dep_head, 1, n_sq)
        sq_empty = Logic(em, "sq_dc_empty", "w")
        em.add_assignment(sq_empty, sq_dep_head == sq_dep_tail)
        sq_pop_empties = Logic(em, "sq_dc_pop_empties", "w")
        em.add_assignment(sq_pop_empties, sq_head_p1 == sq_dep_tail)
        new_head_pop = Logic(em, "sq_new_head_pop", "w")
        em.add_assignment(new_head_pop,
            sq_access_en_i & ~sq_empty & ~sq_pop_empties)
        new_head_refill = Logic(em, "sq_new_head_refill", "w")
        em.add_assignment(new_head_refill, sq_empty & sq_bb_executed)

        pq_has_boundary = Logic(em, "pq_dc_has_boundary", "w")
        em.add_assignment(pq_has_boundary, pq_dep_head != pq_dep_tail)

        # counting -> queue: a successor needs to wait (no credit left) and there is
        # a real pending predecessor boundary.
        count_to_queue = Logic(em, "count_to_queue", "w")
        em.add_assignment(count_to_queue,
            counting_mode & pq_has_boundary & credit_zero & ~credit_inc
            & (sq_access_en_i | new_head_refill))

        return (consume, credit_inc, credit_dec, credit_zero,
                new_head_pop, new_head_refill, count_to_queue)

    def _cross_bb_next_state(self, em, counting_mode, ad_credit, ad_oh, events,
                             pq_array, pq_head_oh, n_pq, ad_width):
        """Drive the three state registers (mode, credit counter, boundary bit)."""
        (consume, credit_inc, credit_dec, credit_zero,
         new_head_pop, new_head_refill, count_to_queue) = events

        # ---- mode ----
        counting_next = Logic(em, "counting_mode_next", "w")
        em.add_assignment(counting_next,
            (~count_to_queue).when(counting_mode).else_(consume))
        em.add_assignment(counting_mode, counting_next)
        counting_mode.regInit(init=1)

        # ---- credit counter ----
        staying_count = Logic(em, "ad_credit_keep", "w")
        em.add_assignment(staying_count, counting_mode & ~count_to_queue)
        credit_inc_v = LogicVec(em, "ad_credit_inc_v", "w", ad_width)
        credit_dec_v = LogicVec(em, "ad_credit_dec_v", "w", ad_width)
        em.add_assignment(credit_inc_v,
            Val(1, size=ad_width).when(credit_inc).else_(Val(0, size=ad_width)))
        em.add_assignment(credit_dec_v,
            Val(1, size=ad_width).when(credit_dec).else_(Val(0, size=ad_width)))
        credit_sum = LogicVec(em, "ad_credit_sum", "w", ad_width)
        em.add_assignment(credit_sum, (ad_credit + credit_inc_v) - credit_dec_v)
        # The only way credit + inc - dec underflows is credit == 0, dec, ~inc,
        # which is exactly the credit-exhausted case -> next credit is 0 anyway.
        credit_underflow = Logic(em, "ad_credit_underflow", "w")
        em.add_assignment(credit_underflow, credit_zero & credit_dec & ~credit_inc)
        credit_next = LogicVec(em, "ad_credit_next", "w", ad_width)
        em.add_assignment(credit_next,
            Val(0, size=ad_width).when(~staying_count | credit_underflow).else_(credit_sum))
        em.add_assignment(ad_credit, credit_next)
        ad_credit.regInit(init=0)

        # ---- one-hot boundary bit ----
        # `next_oh` marches one boundary forward for a back-to-back queue pop;
        # `first_oh` (first pending P from the head) is used on every re-entry /
        # refill, where the head may have advanced past the old bit.
        first_oh = LogicVec(em, "ad_first_oh", "w", n_pq)
        CyclicPriorityMasking(em, first_oh, pq_array, pq_head_oh)
        ad_oh_rot = LogicVec(em, "ad_oh_rot", "w", n_pq)
        for i in range(n_pq):
            em.add_assignment((ad_oh_rot, i), Val(ad_oh, (i - 1) % n_pq))
        next_oh = LogicVec(em, "ad_next_oh", "w", n_pq)
        CyclicPriorityMasking(em, next_oh, pq_array, ad_oh_rot)

        load_first = Logic(em, "ad_load_first", "w")
        em.add_assignment(load_first, count_to_queue | new_head_refill)
        load_next = Logic(em, "ad_load_next", "w")
        em.add_assignment(load_next, ~counting_mode & new_head_pop & ~consume)
        ad_oh_next = LogicVec(em, "ad_oh_next", "w", n_pq)
        em.add_assignment(ad_oh_next,
            first_oh.when(load_first).else_(next_oh.when(load_next).else_(ad_oh)))
        em.add_assignment(ad_oh, ad_oh_next)
        ad_oh.regInit(init=0)

    # ===----------------------------------------------------------------------===
    # Conflict check and allow output (shared by both modes)
    # ===----------------------------------------------------------------------===
    def _emit_allow(self, em, access_disparity, pq_addr_i, pq_done_i, sq_head,
                    pq_length_i, pq_ptr_width, ad_width, allow_sq_access_o):
        conflict = Logic(em, "conflict", "w")

        check_mask = LogicVec(em, "check_mask", "w", self.configs.pq.num_entries)
        ones = LogicVec(em, "ones", "w", self.configs.pq.num_entries)
        # generate access_disparity 1's for the check mask
        # AD counts how many P accesses to check, so bit i is set iff i < AD
        # (AD = 0 checks no entries, AD = n checks entries 0..n-1).
        for i in range(self.configs.pq.num_entries):
            em.add_assignment(
                (ones, i),
                Bit(1).when(Val(i, size=ad_width) < access_disparity).else_(Bit(0)),
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
            corresponding_entry_allocated, (pq_length_as_cmp >= ad_as_cmp)
        )
        em.add_assignment(
            corresponding_entry_sent, access_disparity <= Val(0, size=ad_width)
        )

        em.add_comment(
            "Allow access if:"
            "\t- The predecessor queue length has reached the queue length "
            "(i.e. the predecessor has allocated the corresponding entry for this access)\n"
            "\t- AND, there is either: \n"
            "       - no conflict  \n"
            "       - if the access disparity is zero or negative (i.e. the corresponding entry has been complete)\n"
            "\t- AND, the access disparity is not maxed out\n"
        )
        # ad is a signed number, so the max value it can take is 2^(n-1) - 1
        max_ad_val = (1 << (self.configs.access_disparity_width - 1)) - 1

        conditions = [
            corresponding_entry_allocated,
            ~conflict | corresponding_entry_sent,
        ]

        # corresponding_entry_allocated requires pq_length_i >= access_disparity, and a
        # successor access bumps the disparity once more, so the largest value the
        # register can reach is num_entries + 1 (access_disparity <= pq_length_i and
        # pq_length_i <= num_entries, plus one final increment). If max_ad_val can hold
        # that, the allocation condition alone prevents overflow and no explicit cap is
        # needed; otherwise add the cap.
        if max_ad_val <= self.configs.pq.num_entries:
            conditions.append(access_disparity <= Val(max_ad_val, size=ad_width))

        em.add_assignment(
            allow_sq_access_o,
            reduce(lambda a, b: a & b, conditions)
        )

    def _make_bb_ports(self, em: Emitter, prefix: str, ready):
        valid_i = self._add_port(Logic(em, f"{prefix}_bb_valid", "i"))
        ready_o = self._add_port(Logic(em, f"{prefix}_bb_ready", "o"))
        executed = Logic(em, f"{prefix}_bb_executed", "w")
        em.add_assignment(ready_o, ready)
        em.add_assignment(executed, valid_i & ready)
        return executed

    def _make_dep_array(self, em: Emitter, prefix: str, n_entries: int, head_en,
                        write_tuple: Logic, with_array_at_head: bool = True):
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

        array_at_head = None
        if with_array_at_head:
            head_idx = LogicVec(em, f"{prefix}_dep_head_idx", "w", addr_width)
            em.add_assignment(head_idx, Val(em.slice_var(head.getNameRead(), addr_width - 1, 0)))
            array_at_head = Logic(em, f"{prefix}_array_at_head", "w")
            MuxLookUp(em, array_at_head, array, head_idx)

        return array, head, tail, tail_en, full, array_at_head

    def generate_dep_arrays(self, em: Emitter, pq_done_en, sq_access_en):
        n_pq_entries = self.configs.pq.num_entries * self.configs.dep_entry_ratio
        n_sq_entries = self.configs.sq.num_entries * self.configs.dep_entry_ratio

        pq_write_value = Logic(em, "pq_write_value", "w")
        sq_write_value = Logic(em, "sq_write_value", "w")

        # The successor dep array is only used for its head/tail pointers (occupancy
        # / back-pressure), so it does not need the `array_at_head` lookup.
        pq_array, pq_dep_head, pq_dep_tail, pq_dep_tail_en, pq_dep_full, pq_array_at_head = \
            self._make_dep_array(em, "pq", n_pq_entries, pq_done_en, (pq_write_value, Bit(1)))
        sq_array, sq_dep_head, sq_dep_tail, sq_dep_tail_en, sq_dep_full, _ = \
            self._make_dep_array(em, "sq", n_sq_entries, sq_access_en, sq_write_value,
                                 with_array_at_head=False)

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

        # A predecessor entry is marked '1' (a group boundary) on its previous slot
        # when a successor executed since the last predecessor; the current slot is
        # always written '1' as a provisional latest-P placeholder.
        em.add_assignment(pq_write_value, sq_executed_last)
        em.add_assignment(sq_write_value, pq_executed_last)

        pq_dep_addr_width = math.ceil(math.log2(n_pq_entries))

        self.dep_full = Logic(em, "dep_full", "w")
        em.add_assignment(self.dep_full, pq_dep_full | sq_dep_full)

        return (pq_array, pq_dep_head, pq_dep_tail, sq_dep_head, sq_dep_tail,
                pq_array_at_head, pq_bb_executed, sq_bb_executed, pq_dep_addr_width)
