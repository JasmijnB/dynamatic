import math
from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import BitsToOH, CyclicPriorityMasking, CyclicRangeFill, CyclicRightShift, MuxLookUp, OHToBits, Reduce, WrapAdd, WrapAddConst, WrapSub
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement
from custom_core_gen.configs import DependencyCheckerConfig
from custom_core_gen.generators.generator import Generator
from functools import reduce
from dataclasses import dataclass


@dataclass
class _DepArray:
    """Bundle of the signals produced by `_make_dep_array` for one dep queue."""
    array: object          # LogicArray: the boundary/marking bits
    array_next: object     # LogicVec: combinational next-state view of `array`
    head: object           # LogicVec: head pointer (ptr_width)
    tail: object           # LogicVec: tail pointer (ptr_width)
    tail_en: object        # Logic: tail push enable
    full: object           # Logic: queue full
    array_at_head: object  # Logic: boundary bit at the head slot
    mark_consumed: object  # Logic or None: mark hit an already-popped entry
    retiring_now: object   # Logic: the lone pending entry pops this same cycle
    head_idx: object       # LogicVec: head index (addr_width)
    head_oh: object        # LogicVec: one-hot of head index (n_entries)
    head_oh_next: object   # LogicVec: combinational next-state view of `head_oh`
    n_entries: int


class DependencyChecker(Generator):
    def __init__(self, name: str, suffix: str, configs: DependencyCheckerConfig):
        super().__init__(name, suffix, configs)

    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        assert self.configs.dep_entry_ratio == 1, (
            "dep_entry_ratio must be 1: the dep array sizing in this generator "
            "(n_pq_entries/n_sq_entries = pq/sq.num_entries) doesn't scale by it"
        )
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

        n_pq_entries = self.configs.pq.num_entries
        check_mask = LogicVec(em, "check_mask", "w", n_pq_entries)

        if not crosses_bb:
            access_disparity = self._ad_same_group(em, ad_width, sq_access_en_i, pq_done_en_i)
            # Same-BB: AD is an integer count of pending predecessor accesses to
            # check. Build a head-relative run of 1s (bit i set iff i < AD) and
            # rotate it into the physical-entry frame by the done pointer.
            ones = LogicVec(em, "ones", "w", n_pq_entries)
            for i in range(n_pq_entries):
                em.add_assignment(
                    (ones, i),
                    Bit(1).when(Val(i, size=ad_width) < access_disparity).else_(Bit(0)),
                )
            CyclicRightShift(em, check_mask, ones, pq_done_i)
        else:
            # Cross-BB: a small two-state machine drives the disparity (see
            # `_disparity_cross`). It returns the integer count (for the
            # allocation/sent gates) plus the AD boundary one-hot and the dep-head
            # one-hot. The window to check is every entry from the dep head INDEX
            # (which is also the PQ queue start index, i.e. already the physical
            # frame) up to and including the AD boundary - filled directly with
            # CyclicRangeFill, no done-pointer rotation needed. In the NEGATIVE
            # state there is nothing to check, so the mask is forced to 0.
            ad_oh, pq_head_oh, is_positive, access_disparity = self._ad_cross_group(
                em, ad_width, pq_done_en_i, sq_access_en_i
            )
            span = LogicVec(em, "check_span", "w", n_pq_entries)
            CyclicRangeFill(em, span, ad_oh, pq_head_oh)
            for i in range(n_pq_entries):
                em.add_assignment(
                    (check_mask, i),
                    Val(span, i).when(is_positive).else_(Bit(0)),
                )

        conflicts = LogicVec(em, "conflicts", "w", self.configs.pq.num_entries)
        for i in range(self.configs.pq.num_entries):
            em.add_assignment(
                (conflicts, i),
                Val(check_mask, i).when(Val(pq_addr_i, i) == sq_head).else_(Bit(0)),
            )

        Reduce(em, conflict, conflicts, BinOp.OR)

        # Bring both operands to cmp_width bits so the equality check is type-safe.
        # pq_length_i is non-negative so it gets zero-extended; access_disparity is
        # sign-extended via resize when signed (same-BB), zero-extended when
        # unsigned (cross-BB, where it can never be negative).
        ad_width_actual = access_disparity.size
        ad_is_signed = not crosses_bb
        cmp_width = max(ad_width_actual, pq_ptr_width)

        pq_length_as_cmp = LogicVec(
            em, "pq_length_as_cmp", "w", cmp_width, is_signed=True
        )
        pq_pad = cmp_width - pq_ptr_width
        if pq_pad > 0:
            em.add_assignment(pq_length_as_cmp, Val(0, pq_pad).concat(pq_length_i))
        else:
            em.add_assignment(pq_length_as_cmp, pq_length_i)

        ad_pad = cmp_width - ad_width_actual
        if ad_pad > 0:
            ad_as_cmp = LogicVec(em, "ad_as_cmp", "w", cmp_width, is_signed=True)
            if ad_is_signed:
                em.add_custom_statement(
                    CustomStatement(
                        f"{ad_as_cmp.getNameWrite()} <= resize({access_disparity.getNameRead()}, {cmp_width});",
                        f"assign {ad_as_cmp.getNameWrite()} = {access_disparity.getNameRead()};",
                    )
                )
            else:
                em.add_assignment(ad_as_cmp, Val(0, ad_pad).concat(access_disparity))
        else:
            ad_as_cmp = access_disparity

        corresponding_entry_sent = Logic(em, "corresponding_entry_sent", "w")
        corresponding_entry_allocated = Logic(em, "corresponding_entry_allocated", "w")
        em.add_assignment(
            corresponding_entry_allocated, (pq_length_as_cmp >= ad_as_cmp)
        )
        em.add_assignment(
            corresponding_entry_sent,
            access_disparity <= Val(0, size=ad_width_actual),
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

        conditions = [
            corresponding_entry_allocated,
            ~conflict | corresponding_entry_sent,
        ]

        if ad_is_signed:
            # ad is a signed number, so the max value it can take is 2^(n-1) - 1
            max_ad_val = (1 << (ad_width_actual - 1)) - 1
            # corresponding_entry_allocated requires pq_length_i >= access_disparity, and a
            # successor access bumps the disparity once more, so the largest value the
            # register can reach is num_entries + 1 (access_disparity <= pq_length_i and
            # pq_length_i <= num_entries, plus one final increment). If max_ad_val can hold
            # that, the allocation condition alone prevents overflow and no explicit cap is
            # needed; otherwise add the cap.
            if max_ad_val <= self.configs.pq.num_entries:
                conditions.append(
                    access_disparity <= Val(max_ad_val, size=ad_width_actual)
                )

        em.add_assignment(
            allow_sq_access_o,
            reduce(lambda a, b: a & b, conditions)
        )

        self._write_to_file(em, path_rtl, out_file)

    # ===----------------------------------------------------------------------===
    # Access-disparity computation
    # ===----------------------------------------------------------------------===

    def _ad_same_group(self, em: Emitter, ad_width, sq_access_en, pq_done_en):
        """Same-BB disparity: a simple signed up/down counter. +1 per successor
        access, -1 per predecessor completion."""
        access_disparity = LogicVec(em, "access_disparity", "r", ad_width, is_signed=True)
        # If the successor port executes sequentially before the predecessor port,
        # initialise the disparity to 0 (the pred already executed once, nothing to
        # check); otherwise 1 (one P access still to check).
        ad_initial_value = 1 if not self.configs.succ_can_execute_once else 0
        access_disparity.regInit(init=ad_initial_value)

        inc_ad = LogicVec(em, "inc_access_disparity", "w", ad_width, is_signed=True)
        dec_ad = LogicVec(em, "dec_access_disparity", "w", ad_width, is_signed=True)
        em.add_assignment(inc_ad, Val(1).when(sq_access_en).else_(Val(0)))
        em.add_assignment(dec_ad, Val(1).when(pq_done_en).else_(Val(0)))
        em.add_assignment(access_disparity, (access_disparity + inc_ad) - dec_ad)
        return access_disparity

    def _ad_cross_group(self, em: Emitter, ad_width, pq_done_en, sq_access_en):
        """Cross-BB disparity, driven by a two-state machine.

        NEGATIVE state (an unsigned `credit` counter, init 0): the successor
        stream is ahead of (or level with) the predecessor stream, so nothing is
        checked (disparity = 0). `pq_done_en` at a marked head increments the
        credit, a successor at a marked SQ head decrements it. When the credit is
        already 0 and a successor decrement arrives with no matching increment,
        the predecessor stream has just produced a dependency the successor must
        wait on: switch to the POSITIVE state.

        POSITIVE state (an `ad_oh` one-hot pointing at the awaited predecessor
        dependency boundary in the PQ dep array): the window the successor must
        check is every marked predecessor entry from the dep head up to and
        including `ad_oh`. On `s_next_head` (a new successor becomes the head) the
        boundary is re-searched from the head. When `pq_done_en` pops the very
        entry `ad_oh` points at, that dependency is satisfied: return to the
        NEGATIVE state with credit reset to 0.

        The returned `access_disparity` is the head-relative count of pending
        predecessor entries to check: 0 in NEGATIVE, (distance head->ad_oh)+1 in
        POSITIVE. That integer feeds the shared check-mask/conflict block."""
        n_pq = self.configs.pq.num_entries
        pq_addr_width = math.ceil(math.log2(n_pq))
        n_sq = self.configs.sq.num_entries
        sq_addr_width = math.ceil(math.log2(n_sq))

        pq, sq, sq_bb_executed = self._build_dep_arrays(em, pq_done_en, sq_access_en)

        # --- State registers ---
        is_positive = Logic(em, "ad_is_positive", "r")
        credit = LogicVec(em, "ad_credit", "r", ad_width)

        # --- s_next_head: a new successor head has arrived ---
        # Essentially `sq_access_en` (a send advances the access head to the next
        # successor), EXCEPT when that send empties the queue: then there is no new
        # head yet, so the pulse is deferred until the queue is repopulated by the
        # next successor BB execution. `awaiting_head` is a sticky flag that
        # remembers a send-into-empty so the deferred pulse fires from registered
        # state (avoiding the same-cycle push race that hid the original signal).
        s_next_head = self._make_s_next_head(em, n_sq, sq, sq_access_en, sq_bb_executed)

        # --- Resolution head (the "third" SQ pointer) ---
        # The SQ access head advances when the successor's memory access is
        # *allowed* - which is exactly what this checker gates, so it cannot be
        # used to decide whether a successor depends on a predecessor. A separate
        # resolution pointer walks the committed successors one at a time, deciding
        # each one's dependency *before* any access. It advances only when the
        # current successor is resolved (see `res_advance`). The dependency bit is
        # read from the SQ dep array at this pointer.
        sq_res_head = LogicVec(em, "sq_res_head", "r", sq_addr_width)
        s_is_first = Logic(em, "s_is_first", "w")
        MuxLookUp(em, s_is_first, sq.array_next, sq_res_head)
        
        go_to_next_p = Logic(em, "go_to_next_p", "w")
        em.add_assignment(go_to_next_p, s_next_head & s_is_first)

        # --- NEGATIVE-state credit events ---
        # A predecessor completing at a marked boundary head banks a credit; a
        # dependent successor arriving at the resolution head (s_next_head) spends
        # one. pq.retiring_now also banks a credit: the PpSs race where the lone
        # pending predecessor (p) retires the very cycle a successor's mark would
        # have landed on it - p doesn't yet know S is coming, so it grants a free
        # pass instead of leaving a literal (and unreachable, since the head has
        # already passed it) mark for S to depend on.
        neg_incr = Logic(em, "ad_neg_incr", "w")
        neg_decr = Logic(em, "ad_neg_decr", "w")
        em.add_assignment(
            neg_incr,
            ~is_positive & pq_done_en & (pq.array_at_head | self.pq_mark_raced_retire),
        )
        em.add_assignment(neg_decr, ~is_positive & go_to_next_p)

        # --- POSITIVE-state boundary search (from the dep head) ---
        # Search the next-state PQ array so a boundary marked this same cycle is
        # visible immediately (no stale read). The first marked entry at/after the
        # head is the dependency boundary the current successor must wait for. 
        # the search finds nothing the awaited predecessor already completed (it
        # retired before the successor marked it), so there is nothing to wait on.

        # Disposition of a dependent successor arriving in NEGATIVE (`neg_decr`):
        #   * a banked credit covers it (a predecessor got ahead)  -> spend credit;
        #   * else a pending boundary exists                       -> go POSITIVE;
        #   * else (no credit, no boundary)                        -> already
        #     satisfied (predecessor retired unmarked), a no-op.
        go_positive = Logic(em, "ad_go_positive", "w")
        em.add_assignment(
            go_positive,
            neg_decr & (credit == Val(0, ad_width)) & ~neg_incr,
        )

        # --- POSITIVE-state AD one-hot register ---
        # Latched on entry to POSITIVE, and re-searched to the next boundary
        # whenever a new successor head arrives while already POSITIVE (the jump).
        # Always anchored on pq.head_oh_next (not pq.head_oh): a predecessor pop
        # can land on the very same cycle ad_oh would otherwise track/search from
        # the head, and pq.head_oh itself only reflects the head as of the start
        # of the cycle (pre-edge). Using the stale pre-pop head as pivot can
        # re-find/re-track a boundary slot the head has already passed this
        # cycle, stranding ad_oh behind the head forever. ad_oh must never lag
        # the head - it has to move the instant the head does.
        ad_oh = LogicVec(em, "ad_oh", "r", n_pq)

        # CyclicPriorityMasking's search is INCLUSIVE of its pivot bit: if the
        # pivot position is itself marked in pq_array, the search immediately
        # returns the pivot, never advancing further. That is correct on entry
        # into POSITIVE (go_positive: the head itself may legitimately be the
        # boundary), but wrong on the re-search "jump" (a later successor
        # needs the NEXT marked entry after the one ad_oh already points at,
        # not ad_oh itself again - ad_oh's own slot is always still marked at
        # this point, since pq.array entries are only ever overwritten much
        # later when the tail wraps back around, not cleared on consumption).
        # So the jump case must pivot from one slot past ad_oh, or a later
        # successor that needs a later boundary gets stuck re-finding the
        # earlier one forever.
        ad_oh_idx = LogicVec(em, "ad_oh_idx", "w", pq_addr_width)
        OHToBits(em, ad_oh_idx, ad_oh)
        ad_oh_idx_plus1 = LogicVec(em, "ad_oh_idx_plus1", "w", pq_addr_width)
        WrapAddConst(em, ad_oh_idx_plus1, ad_oh_idx, 1, n_pq)
        ad_oh_plus1 = LogicVec(em, "ad_oh_plus1", "w", n_pq)
        BitsToOH(em, ad_oh_plus1, ad_oh_idx_plus1)

        search_pivot = LogicVec(em, "ad_search_pivot", "w", n_pq)
        em.add_assignment(search_pivot, pq.head_oh_next.when(go_positive).else_(ad_oh_plus1))
        ad_oh_next = LogicVec(em, "ad_oh_next", "w", n_pq)
        CyclicPriorityMasking(em, ad_oh_next, self.pq_array_next, search_pivot)

        em.add_assignment(ad_oh, pq.head_oh_next
                          .when(~go_positive & ~is_positive).else_(
                              ad_oh_next.when(go_to_next_p).else_(ad_oh)))
        ad_oh.regInit(init=1)

        # --- Consume: pop reaches the awaited boundary -> back to NEGATIVE ---
        # Uses the *current* head (pre-pop), not head_oh_next: this checks
        # whether the entry about to be popped this cycle (pq_done_en) is the one
        # ad_oh is waiting on, so it must compare against where the head IS, not
        # where it's about to go.
        head_is_ad = Logic(em, "ad_head_is_ad", "w")
        em.add_assignment(head_is_ad, pq.head_oh == ad_oh)
        consume = Logic(em, "ad_consume", "w")
        em.add_assignment(consume, is_positive & pq_done_en & head_is_ad)

        # --- Resolution-head advance ---
        # One new successor head per `s_next_head` pulse, so the resolution pointer
        # simply steps once per pulse.
        sq_res_head_next = LogicVec(em, "sq_res_head_next", "w", sq_addr_width)
        WrapAddConst(em, sq_res_head_next, sq_res_head, 1, n_sq)
        em.add_assignment(sq_res_head, sq_res_head_next)
        sq_res_head.regInit(init=0, enable=s_next_head)

        # --- State register updates ---
        is_positive_next = Logic(em, "ad_is_positive_next", "w")
        em.add_assignment(
            is_positive_next,
            Bit(0).when(consume).else_(Bit(1).when(go_positive).else_(is_positive)),
        )
        em.add_assignment(is_positive, is_positive_next)
        is_positive.regInit(init=0)

        # Credit banks marked-predecessor completions and is spent only when a
        # dependent successor is actually covered (`spend_credit`); a decrement that
        # goes POSITIVE or is a no-op does NOT touch the credit, so it never
        # underflows. Reset to 0 when consuming back into NEGATIVE.
        credit_inc = LogicVec(em, "ad_credit_inc", "w", ad_width)
        credit_dec = LogicVec(em, "ad_credit_dec", "w", ad_width)
        em.add_assignment(credit_inc, Val(1).when(neg_incr).else_(Val(0)))
        em.add_assignment(credit_dec, Val(1).when(neg_decr).else_(Val(0)))
        credit_updated = LogicVec(em, "ad_credit_updated", "w", ad_width)
        em.add_assignment(credit_updated, (credit + credit_inc) - credit_dec)
        # go_positive fires exactly when credit==0 and neg_decr has nothing to
        # spend (see go_positive's definition above), so credit_updated would
        # underflow on this very cycle; force the reset to 0 immediately using
        # go_positive itself rather than waiting a cycle for is_positive to catch
        # up (which would let the underflowed value land in the register first).
        em.add_assignment(
            credit,
            Val(0, size=ad_width).when(is_positive | go_positive).else_(credit_updated),
        )
        credit.regInit(init=0)

        # --- Disparity count: head -> ad_oh inclusive distance, +1 ---
        # Unsigned: the cross-BB disparity is never negative (NEGATIVE state reads
        # as 0, not a negative count), so it only needs pq_addr_width + 1 bits
        # (max value n_pq, when the boundary sits one slot behind the head).
        head_to_ad = LogicVec(em, "ad_head_to_ad", "w", pq_addr_width)
        WrapSub(em, head_to_ad, ad_oh_idx, pq.head_idx, n_pq)
        head_to_ad_wide = LogicVec(em, "ad_head_to_ad_wide", "w", pq_addr_width + 1)
        em.add_assignment(head_to_ad_wide, Bit(0).concat(head_to_ad))
        access_disparity_count = LogicVec(em, "ad_disparity_count", "w", pq_addr_width + 1)
        em.add_assignment(access_disparity_count, head_to_ad_wide + Val(1))
        access_disparity = LogicVec(em, "access_disparity", "w", pq_addr_width + 1)
        em.add_assignment(
            access_disparity,
            access_disparity_count.when(is_positive).else_(Val(0, size=pq_addr_width + 1)),
        )

        # POSITIVE -> the window [head .. ad_oh] inclusive (count); NEGATIVE -> 0.
        return ad_oh, pq.head_oh, is_positive, access_disparity

    def _make_bb_ports(self, em: Emitter, prefix: str, ready):
        valid_i = self._add_port(Logic(em, f"{prefix}_bb_valid", "i"))
        ready_o = self._add_port(Logic(em, f"{prefix}_bb_ready", "o"))
        executed = Logic(em, f"{prefix}_bb_executed", "w")
        em.add_assignment(ready_o, ready)
        em.add_assignment(executed, valid_i & ready)
        return executed

    def _make_dep_array(self, em: Emitter, prefix: str, n_entries: int, head_en, write_tuple: Logic, mark_en=None):
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

        # Same-cycle race: the lone pending entry (tail-1 == head) is both the
        # mark's target AND being popped (head_en) this very cycle. The mark
        # write would otherwise win unconditionally and plant a stale '1' into
        # the slot the head is leaving behind this same edge - a boundary bit
        # nothing will ever clear (it sits behind the head until the tail wraps
        # all the way back around), permanently stranding any AD search that
        # later latches onto it. This is the PpSs case where P retires (p) not
        # yet knowing a successor (S) is coming: p should grant a free pass
        # (bank a credit) instead of leaving a literal mark for S to depend on.
        retiring_now = Logic(em, f"{prefix}_dep_retiring_now", "w")
        em.add_assignment(retiring_now, head_en & ~empty & (head_next == tail))

        mark_already_consumed = None
        if mark_en is not None:
            # Predecessor marking scheme: a push (tail_en) writes 0 (this P is not
            # yet depended on). A successor execution (mark_en) overwrites the
            # most-recent entry, tail-1, with 1 - a definitive, per-successor
            # dependency boundary - without advancing the tail. If the array is
            # already empty at the mark, or its lone entry retires this same
            # cycle (retiring_now), tail-1 is/becomes invalid as a mark target
            # (the predecessor finished before, or in lockstep with, the
            # successor): suppress the stray write and signal an extra
            # decrement instead.
            mark_valid = Logic(em, f"{prefix}_mark_valid", "w")
            em.add_assignment(mark_valid, mark_en & ~empty & ~retiring_now)
            mark_already_consumed = Logic(em, f"{prefix}_mark_consumed", "w")
            em.add_assignment(mark_already_consumed, mark_en & (empty | retiring_now))
            self.pq_mark_raced_retire = Logic(em, f"{prefix}_mark_raced_retire", "w")
            em.add_assignment(self.pq_mark_raced_retire, mark_en & retiring_now)

        # `array_next` mirrors the register write equation combinationally so a
        # value written this cycle is visible the same cycle (the next-state view).
        # It avoids the stale read where a bit set on the push/mark edge is not yet
        # in the register when something else reads it that very cycle. A
        # LogicArray (not a LogicVec) so it can feed MuxLookUp the same way `array`
        # itself does.
        array_next = LogicArray(em, f"{prefix}_array_next", "w", n_entries)
        if mark_en is not None:
            for i in range(n_entries):
                next_bit = (Bit(0).when(Val(tail_oh, i) & tail_en)
                    .else_(Bit(1).when(Val(tail_oh, (i + 1) % n_entries) & mark_valid)
                    .else_(array[i])))
                em.add_assignment(array[i], next_bit)
                em.add_assignment(array_next[i], next_bit)
            self.pq_array_next = array_next
        elif isinstance(write_tuple, tuple):
            prev_val, curr_val = write_tuple
            for i in range(n_entries):
                next_bit = (curr_val.when(Val(tail_oh, i) & tail_en)
                    .else_((prev_val).when(Val(tail_oh, (i + 1) % n_entries) & tail_en)
                    .else_(array[i])))
                em.add_assignment(array[i], next_bit)
                em.add_assignment(array_next[i], next_bit)
        else:
            for i in range(n_entries):
                next_bit = write_tuple.when(Val(tail_oh, i) & tail_en).else_(array[i])
                em.add_assignment(array[i], next_bit)
                em.add_assignment(array_next[i], next_bit)

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

        # One-hot of the head index, used by the cross-BB state machine to anchor
        # the AD one-hot and to detect when a pop consumes the AD boundary.
        head_oh = LogicVec(em, f"{prefix}_dep_head_oh", "w", n_entries)
        BitsToOH(em, head_oh, head_idx)

        # Same-cycle next-state view of head_oh: head_idx/head_oh are built from
        # the registered head (pre-edge), so a pop landing this very cycle
        # (head_en) is invisible to them until the next edge. Anything that needs
        # "where the head is, accounting for a pop happening right now" (e.g. the
        # AD search pivot) must use this instead, or it searches/tracks from a
        # head position that's already one pop stale.
        head_idx_next = LogicVec(em, f"{prefix}_dep_head_idx_next", "w", addr_width)
        em.add_assignment(
            head_idx_next,
            Val(em.slice_var(head_next.getNameRead(), addr_width - 1, 0)).when(head_en).else_(head_idx),
        )
        head_oh_next = LogicVec(em, f"{prefix}_dep_head_oh_next", "w", n_entries)
        BitsToOH(em, head_oh_next, head_idx_next)

        return _DepArray(
            array=array, array_next=array_next, head=head, tail=tail, tail_en=tail_en,
            full=full, array_at_head=array_at_head, mark_consumed=mark_already_consumed,
            retiring_now=retiring_now,
            head_idx=head_idx, head_oh=head_oh, head_oh_next=head_oh_next, n_entries=n_entries,
        )

    def _make_s_next_head(self, em: Emitter, n_sq_entries, sq, sq_access_en, sq_bb_executed):
        """A pulse marking that a new successor head has become available.

        It coincides with `sq_access_en` (the send that advances the access head),
        except when that send empties the queue: there is no new head yet, so the
        pulse is deferred until the next successor BB execution repopulates the
        queue. `awaiting_head` is a sticky register that records "a send drained
        the queue"; the deferred pulse then fires from this registered state on the
        next push, sidestepping the same-cycle push race (head!=tail is already
        true the cycle the push happens, which hid the original combinational
        signal)."""
        sq_dep_addr_width = math.ceil(math.log2(n_sq_entries))
        sq_dep_head = sq.head
        sq_dep_tail = sq.tail

        sq_not_empty = Logic(em, "sq_dep_not_empty", "w")
        em.add_assignment(
            sq_not_empty,
            Val(sq_dep_head.getNameRead()) != Val(sq_dep_tail.getNameRead()),
        )

        sq_dep_head_after_pop = LogicVec(
            em, "sq_dep_head_after_pop", "w", sq_dep_addr_width + 1
        )
        WrapAddConst(em, sq_dep_head_after_pop, sq_dep_head, 1, n_sq_entries)
        sq_not_empty_after_pop = Logic(em, "sq_dep_not_empty_after_pop", "w")
        em.add_assignment(
            sq_not_empty_after_pop,
            sq_not_empty
            & (Val(sq_dep_head_after_pop.getNameRead()) != Val(sq_dep_tail.getNameRead())),
        )

        # A send that leaves the queue empty (not_empty now, empty after the pop)
        # has no new head to offer yet.
        send_into_empty = Logic(em, "sq_send_into_empty", "w")
        em.add_assignment(send_into_empty, sq_access_en & sq_not_empty & ~sq_not_empty_after_pop)

        # Sticky: set on send-into-empty, cleared once the deferred pulse fires.
        # Initialised to 1: at reset the queue is empty, so the first successor BB
        # execution is itself a "new head from empty" and must pulse s_next_head.
        awaiting_head = Logic(em, "sq_awaiting_head", "r")
        deferred_pulse = Logic(em, "sq_deferred_head", "w")
        em.add_assignment(deferred_pulse, awaiting_head & sq_bb_executed)
        em.add_assignment(
            awaiting_head,
            Bit(1).when(send_into_empty).else_(Bit(0).when(deferred_pulse).else_(awaiting_head)),
        )
        awaiting_head.regInit(init=1)

        s_next_head = Logic(em, "s_next_head", "w")
        em.add_assignment(
            s_next_head,
            (sq_access_en & sq_not_empty_after_pop) | deferred_pulse,
        )
        return s_next_head

    def _build_dep_arrays(self, em: Emitter, pq_done_en, sq_access_en):
        """Instantiate the predecessor/successor dependency arrays and the few
        derived signals the cross-BB state machine consumes. Returns the two
        `_DepArray` bundles plus `s_next_head` (next-cycle a new SQ head exists).

        The PQ array marks a definitive per-successor dependency boundary (a 1)
        on the most-recent predecessor entry each time a successor BB executes;
        the SQ array tracks which successors must actually check (0 = went
        before any predecessor, so no dependency)."""
        # dep_entry_ratio temporarily hardcoded to 1 (see generate()).
        n_pq_entries = self.configs.pq.num_entries
        n_sq_entries = self.configs.sq.num_entries

        sq_write_value = Logic(em, "sq_write_value", "w")

        # Successor dep array first, so sq_bb_executed is available to mark the
        # predecessor array's dependency boundaries.
        sq = self._make_dep_array(em, "sq", n_sq_entries, sq_access_en, sq_write_value)
        sq_bb_executed = self._make_bb_ports(em, "sq", ~sq.full)

        pq_executed_last = Logic(em, "pq_executed_last", "r")
        sq_executed_last = Logic(em, "sq_executed_last", "r")

        # Predecessor dep array: pushes write 0; the first successor BB execution
        # after a predecessor marks the most-recent predecessor entry as a
        # definitive (per-successor) boundary.
        first_sq_bb = Logic(em, "first_sq_bb")
        em.add_assignment(first_sq_bb, ~sq_executed_last & sq_bb_executed)
        pq = self._make_dep_array(em, "pq", n_pq_entries, pq_done_en, None, mark_en=first_sq_bb)
        pq_bb_executed = self._make_bb_ports(em, "pq", ~pq.full)

        em.add_assignment(pq.tail_en, pq_bb_executed)
        em.add_assignment(sq.tail_en, sq_bb_executed)

        # TODO: What if the BBs execute at the same time?
        # -> For now not possible
        em.add_assignment(pq_executed_last, ~sq_bb_executed & (pq_bb_executed | pq_executed_last))
        em.add_assignment(sq_executed_last, ~pq_bb_executed & (sq_bb_executed | sq_executed_last))

        # At reset no predecessor has executed yet, so a successor that genuinely
        # goes first must stamp its dep entry with 0 (sq_write_value = pq_executed_last)
        # and keep its free pass, rather than be marked dependent on a predecessor
        # that never ran. Likewise, if the predecessor dep array was already empty
        # at the mark (pq.mark_consumed: the awaited predecessor retired before
        # this successor's BB executed), there is nothing left to wait on either,
        # so the successor must NOT be stamped dependent.
        pq_executed_last.regInit(init=0)
        sq_executed_last.regInit(init=1)
        em.add_assignment(sq_write_value, pq_executed_last & ~pq.mark_consumed)

        self.dep_full = Logic(em, "dep_full", "w")
        em.add_assignment(self.dep_full, pq.full | sq.full)

        return pq, sq, sq_bb_executed

