import math
from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import (
    BitsToOH,
    CyclicPriorityMasking,
    CyclicRangeFill,
    CyclicRightShift,
    MuxLookUp,
    OHToBits,
    Reduce,
    WrapAddConst,
    WrapSub,
)
from core_gen.ir import BinOp, Val, Bit, CustomStatement
from custom_core_gen.configs import DependencyCheckerConfig
from custom_core_gen.generators.generator import Generator
from functools import reduce
from dataclasses import dataclass


@dataclass
class _DepArray:
    """Bundle of the signals produced by `_make_dep_array` for one dep queue."""
    array: object          # LogicArray: the mark/dependent bits
    head: object           # LogicVec: head pointer (ptr_width)
    tail: object           # LogicVec: tail pointer (ptr_width)
    tail_en: object        # Logic: tail push enable
    tail_oh: object        # LogicVec: one-hot of the tail index (n_entries)
    full: object           # Logic: queue full
    empty: object          # Logic: queue empty (head == tail)
    array_at_head: object  # Logic: bit at the head slot
    mark_consumed: object  # Logic or None: mark hit an already-popped entry
    head_idx: object       # LogicVec: head index (addr_width)
    head_oh: object        # LogicVec: one-hot of head index (n_entries)
    head_oh_next: object   # LogicVec: head one-hot, accounting for a pop this cycle
    n_entries: int


class DependencyChecker(Generator):
    """Gates the successor queue's memory accesses until every predecessor
    access that precedes them in program order has either completed or is
    known to target a different address.

    In the default (out-of-order) mode both schemes produce the same three
    things, combined in `generate`:
      - `check_mask`: the physical predecessor slots whose addresses the
        successor at the head must be compared against,
      - `no_dep_pending`: the head successor has no pending predecessor left,
      - scheme-specific extra grant conditions.

    With `configs.forced_sequential` the address comparison is dropped
    entirely and the grant is `no_dep_pending` alone: the successor waits
    until every predecessor access preceding it in program order has
    completed, even if the addresses would not have conflicted. WHERE the
    pending predecessors sit then no longer matters, so all boundary
    tracking reduces to counting: same-BB keeps only the disparity counter
    (`_same_bb_sequential_check`), cross-BB keeps the dep arrays' mark and
    dependent bits but replaces the whole boundary/announce machinery with
    a single go-token counter (`_cross_bb_sequential_check`).

    The scheme depends on whether the two queues belong to the same BB:

      * Same BB (`_same_bb_check`): the relative order of the two accesses is
        static, so a single signed up/down counter (`access_disparity`)
        tracks how many predecessor accesses the head successor still has to
        check.

      * Different BBs (`_cross_bb_check`): the interleaving of the two BBs is
        dynamic and communicated through BB-execution handshakes. Dependency
        boundaries are marked as bits in the predecessor dep array ("where to
        jump"), dependent successors as bits in the successor dep array
        ("when to jump"); a boundary register walks the marks one dependent
        successor at a time, with its jump target precomputed combinationally
        from the registered bits. See `_cross_bb_check` for details.
    """

    def __init__(self, name: str, suffix: str, configs: DependencyCheckerConfig):
        super().__init__(name, suffix, configs)

    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        assert self.configs.dep_entry_ratio == 1, (
            "dep_entry_ratio must be 1: the dep array sizing in this generator "
            "(n_pq_entries/n_sq_entries = pq/sq.num_entries) doesn't scale by it"
        )
        self.ports.clear()
        crosses_bb = self.configs.pq_bb != self.configs.sq_bb

        n_pq_entries = self.configs.pq.num_entries
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
        # Omitted when the pq has a single entry (q_addr_width == 0): there is
        # no index to expose (Queue._generate_observable_ports omits the
        # matching done_ptr_o the same way), and the only consumer
        # (_same_bb_check's rotation) is a no-op on a width-1 array anyway.
        pq_done_i = (
            self._add_port(LogicVec(em, "pq_done", "i", self.configs.pq.q_addr_width))
            if self.configs.pq.q_addr_width > 0
            else None
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

        # Set by `_build_dep_arrays` (cross-BB schemes only); stays None for the
        # same-BB schemes, which track order with a counter and own no arrays.
        self._dep_arrays = None

        if self.configs.forced_sequential:
            # Forced-sequential mode: no addresses are compared (pq_addr,
            # pq_done, pq_length and sq_head stay unused, like pq_done in the
            # cross-BB scheme); the grant is `no_dep_pending` alone.
            if crosses_bb:
                no_dep_pending = self._cross_bb_sequential_check(
                    em, pq_done_en_i, sq_access_en_i
                )
            else:
                no_dep_pending = self._same_bb_sequential_check(
                    em, pq_done_en_i, sq_access_en_i
                )
            em.add_comment(
                "Forced-sequential: allow the successor access only once every\n"
                "\tpredecessor access preceding it in program order has completed.\n"
            )
            allow_pq, extra_sq = self._bb_execution_gates()
            em.add_assignment(allow_pq_access_o, allow_pq)
            em.add_assignment(
                allow_sq_access_o,
                reduce(lambda a, b: a & b, [no_dep_pending] + extra_sq),
            )
            self._write_to_file(em, path_rtl, out_file)
            return

        if crosses_bb:
            check_mask, no_dep_pending, conditions = self._cross_bb_check(
                em, pq_done_en_i, sq_access_en_i, pq_length_i
            )
        else:
            check_mask, no_dep_pending, conditions = self._same_bb_check(
                em, pq_done_i, pq_done_en_i, sq_access_en_i, pq_length_i
            )

        # A conflict is a pending (masked) predecessor entry whose address
        # matches the head successor's.
        conflict = Logic(em, "conflict", "w")
        conflicts = LogicVec(em, "conflicts", "w", n_pq_entries)
        for i in range(n_pq_entries):
            em.add_assignment(
                (conflicts, i),
                Val(check_mask, i).when(Val(pq_addr_i, i) == sq_head).else_(Bit(0)),
            )
        Reduce(em, conflict, conflicts, BinOp.OR)

        em.add_comment(
            "Allow the successor access if:\n"
            "\t- the scheme-specific conditions hold (the head successor's\n"
            "\t  dependency window is allocated in the predecessor queue)\n"
            "\t- AND its address conflicts with no pending predecessor entry\n"
            "\t  (trivially true when nothing is pending anymore)\n"
        )
        allow_pq, extra_sq = self._bb_execution_gates()
        em.add_assignment(allow_pq_access_o, allow_pq)
        em.add_assignment(
            allow_sq_access_o,
            reduce(
                lambda a, b: a & b,
                conditions + extra_sq + [~conflict | no_dep_pending],
            ),
        )

        self._write_to_file(em, path_rtl, out_file)

    def _bb_execution_gates(self):
        """Gates that keep each dep array's pops matched to its pushes.

        An entry is pushed when the BB executes and popped when the port's queue
        retires (`sq_access_en`) or completes (`pq_done_en`) an access. Those are
        separate handshakes: the BB ctrl token is back-pressured by *every* dep
        array on that BB's side (`structure._route_bb_ports` ANDs their readies),
        while a queue's address path is back-pressured only by its own depth. So
        one full array can stall the ctrl token while the other ports' queues
        keep retiring addresses, popping arrays that are already empty and
        running the head past the tail. At NumEntries == 1 the pointer is a bare
        generation bit, making `empty` and `full` exact complements, so a single
        such pop latches `full` high forever: `bb_ready` drops and the BB never
        executes again.

        Gating the queue's `allow_access_i` on its own dep array being non-empty
        closes the window - the queue cannot retire an access whose BB execution
        has not been recorded yet. It holds back both pop sources at once, since
        `allow_access_i` feeds `head_en` and `can_issue`/`done_en` follow the
        head.

        Returns (allow_pq, extra_sq_conditions).
        """
        if self._dep_arrays is None:
            # Same-BB schemes own no dep arrays: order is tracked by a counter,
            # there is no BB handshake, and nothing can underflow.
            return Bit(1), []
        pq, sq = self._dep_arrays
        return ~pq.empty, [~sq.empty]

    # ===----------------------------------------------------------------------===
    # Same-BB scheme
    # ===----------------------------------------------------------------------===

    def _same_bb_check(self, em: Emitter, pq_done_i, pq_done_en_i, sq_access_en_i, pq_length_i):
        """Same-BB scheme: both accesses come from one BB, so their relative
        order is static. A signed up/down counter (+1 per successor access,
        -1 per predecessor completion) yields the head-relative index of the
        last pending predecessor access to check (count - 1; negative means
        nothing left to check)."""
        n_pq = self.configs.pq.num_entries
        ad_width = self.configs.access_disparity_width
        pq_ptr_width = self.configs.pq.q_addr_width + 1

        access_disparity = self._ad_counter(em, ad_width, sq_access_en_i, pq_done_en_i)

        # Width at which entry indices, the queue length and the signed
        # disparity can all be compared without wrapping: it must represent
        # n_pq (the largest queue length) as a POSITIVE signed number and
        # hold ad_width in full.
        cmp_width = max(ad_width, pq_ptr_width + 1)

        ad_cmp = LogicVec(em, "ad_cmp", "w", cmp_width, is_signed=True)
        em.add_custom_statement(
            CustomStatement(
                f"{ad_cmp.getNameWrite()} <= resize({access_disparity.getNameRead()}, {cmp_width});",
                f"assign {ad_cmp.getNameWrite()} = {access_disparity.getNameRead()};",
            )
        )

        # pq_length is non-negative, so zero-extension keeps its value.
        pq_length_cmp = LogicVec(em, "pq_length_cmp", "w", cmp_width, is_signed=True)
        em.add_assignment(
            pq_length_cmp, Val(0, cmp_width - pq_ptr_width).concat(pq_length_i)
        )

        # Head-relative run of 1s (bit i set iff i <= AD), rotated into the
        # physical-entry frame by the done pointer. Comparing at cmp_width
        # keeps indices >= 2^(ad_width-1) from being misread as negative.
        ones = LogicVec(em, "ones", "w", n_pq)
        for i in range(n_pq):
            em.add_assignment(
                (ones, i),
                Bit(1).when(Val(i, size=cmp_width) <= ad_cmp).else_(Bit(0)),
            )
        check_mask = LogicVec(em, "check_mask", "w", n_pq)
        if pq_done_i is None:
            # n_pq == 1: rotating a single-entry array by any amount is a
            # no-op, and there is no done-pointer index to rotate by anyway.
            em.add_assignment(check_mask, ones)
        else:
            CyclicRightShift(em, check_mask, ones, pq_done_i)

        # AD < 0: every predecessor access this successor could depend on has
        # already completed.
        no_dep_pending = Logic(em, "no_dep_pending", "w")
        em.add_assignment(no_dep_pending, access_disparity < Val(0, size=ad_width))

        # The predecessor queue must have allocated up to and including the
        # last entry to check: pq_length > AD (AD is count - 1).
        corresponding_entry_allocated = Logic(em, "corresponding_entry_allocated", "w")
        em.add_assignment(corresponding_entry_allocated, pq_length_cmp > ad_cmp)

        conditions = [corresponding_entry_allocated]

        # AD is signed, so the largest value it can represent is 2^(n-1) - 1.
        max_ad_val = (1 << (ad_width - 1)) - 1
        # corresponding_entry_allocated requires pq_length > AD, and pq_length
        # <= num_entries, so after the post-grant increment AD stays <=
        # num_entries. If ad_width can represent that, the allocation
        # condition alone prevents overflow; otherwise cap explicitly. The
        # cap must be strict (AD < max) because the grant it gates increments
        # AD once more.
        if max_ad_val < n_pq:
            ad_not_maxed = Logic(em, "ad_not_maxed", "w")
            em.add_assignment(
                ad_not_maxed, access_disparity < Val(max_ad_val, size=ad_width)
            )
            conditions.append(ad_not_maxed)

        return check_mask, no_dep_pending, conditions

    def _ad_counter(self, em: Emitter, ad_width, sq_access_en, pq_done_en):
        """Signed up/down counter: +1 per successor access, -1 per predecessor
        completion. Holds the head-relative index of the last pending
        predecessor access to check (count - 1)."""
        access_disparity = LogicVec(em, "access_disparity", "r", ad_width, is_signed=True)
        # If the successor port executes sequentially before the predecessor
        # port, initialise the disparity to -1 (the pred already executed
        # once, nothing to check); otherwise 0 (one P access still to check,
        # at index 0).
        ad_initial_value = 0 if not self.configs.succ_can_execute_once else -1
        access_disparity.regInit(init=ad_initial_value)

        inc_ad = LogicVec(em, "inc_access_disparity", "w", ad_width, is_signed=True)
        dec_ad = LogicVec(em, "dec_access_disparity", "w", ad_width, is_signed=True)
        em.add_assignment(inc_ad, Val(1).when(sq_access_en).else_(Val(0)))
        em.add_assignment(dec_ad, Val(1).when(pq_done_en).else_(Val(0)))
        em.add_assignment(access_disparity, (access_disparity + inc_ad) - dec_ad)
        return access_disparity

    def _same_bb_sequential_check(self, em: Emitter, pq_done_en_i, sq_access_en_i):
        """Forced-sequential same-BB scheme: only the disparity counter
        remains. The head successor is granted exactly when AD < 0, i.e.
        every predecessor access it could depend on has completed. Compared
        to `_same_bb_check` there is no check window (no addresses are
        compared), no allocation gate (no predecessor entry is ever
        inspected), and no overflow cap (the grant this gates keeps AD <= 0
        even after its own increment)."""
        ad_width = self.configs.access_disparity_width
        access_disparity = self._ad_counter(em, ad_width, sq_access_en_i, pq_done_en_i)
        no_dep_pending = Logic(em, "no_dep_pending", "w")
        em.add_assignment(no_dep_pending, access_disparity < Val(0, size=ad_width))
        return no_dep_pending

    # ===----------------------------------------------------------------------===
    # Cross-BB scheme
    # ===----------------------------------------------------------------------===

    def _cross_bb_check(self, em: Emitter, pq_done_en, sq_access_en, pq_length_i):
        """Cross-BB scheme: boundary marks (P bits) plus dependent bits
        (S bits), resolved one successor at a time by a jumping boundary
        register whose target is precomputed from registered state.

        Program order between the two queues is set by the order of their BB
        executions (one handshake per access at dep_entry_ratio == 1). The
        state is:

          - P mark bits (`pq_array`): WHERE to jump. The first successor BB
            execution after a predecessor run marks the most-recent
            predecessor entry as that successor's dependency boundary.
          - S dependent bits (`sq_array`): WHEN to jump. A successor is
            stamped dependent at its BB execution iff a predecessor went
            before it and its mark landed. The bit is CLEARED in place if the
            boundary pops before the successor is announced as head
            (satisfaction clear) - the S bits themselves record who is still
            waiting, replacing the old NEGATIVE-state credit counter. The
            walk pivots at head+1, NOT at the head: the head entry has by
            construction already been announced (every entry is announced
            exactly once, as it becomes head), so its dependency lives in
            ad_oh/boundary_valid and its S bit is dead - a stale 1 when it
            was announced dependent. Skipping it keeps the walk aligned
            one-for-one with the pending marks; pivoting at the head instead
            makes the walk go off by one (e.g. PSPSPSPS then pppp: p2's
            satisfaction must land on S2, not be swallowed by the
            announced-but-undrained S1's stale bit), the last successor's
            bit is never cleared, and its announce latches a stale mark of
            an already-popped entry - deadlock via the allocated gate.
          - `ad_oh` + `boundary_valid`: the boundary of the successor
            currently gated at the head. `boundary_valid` = "ad_oh points at
            a pending mark the head successor must respect"; it survives an
            early (conflict-free) grant so later same-run successors keep
            checking the same window, and clears when the pop consumes the
            boundary.

        Announcement of a new head (`pop_announce` / `deferred_announce`)
        reads the new head's S bit and either claims the next boundary
        (jump) or leaves the state alone. The jump target is PRECOMPUTED
        every cycle from the REGISTERED mark bits with a pivot chosen by the
        REGISTERED boundary_valid (next mark after ad_oh when a boundary is
        resolved, first mark from the head otherwise), so the grant enters
        the state update only as a final mux select. The old design instead
        derived the search pivot from the grant itself, putting the whole
        priority-search cone in series with the allow/grant cone; that
        serial chain was the critical path.

        Corner cases and how they are absorbed:
          - boundary satisfied before its successor arrives: satisfaction
            clear (no credit counter);
          - mark racing the lone pending predecessor's retirement, or landing
            on an empty array: the mark is suppressed and the successor is
            stamped independent at the write side (`sq_write_value`);
          - push into an empty successor queue: announced on the push cycle
            with the in-flight dependent bit, and the landing mark's slot
            (pq tail-1) is the boundary directly - no search needed;
          - pop consuming the boundary while the next dependent successor is
            announced: the jump wins, claiming the next mark (pivot ad_oh+1
            already excludes the consumed slot).
        """
        n_pq = self.configs.pq.num_entries
        pq_addr_width = math.ceil(math.log2(n_pq))
        pq_ptr_width = self.configs.pq.q_addr_width + 1
        n_sq = self.configs.sq.num_entries

        (pq, sq, sq_bb_executed, sq_write_value, sq_clear_bits) = (
            self._build_dep_arrays(em, pq_done_en, sq_access_en)
        )

        # --- Boundary state ---
        boundary_valid = Logic(em, "boundary_valid", "r")
        ad_oh = LogicVec(em, "ad_oh", "r", n_pq)

        # --- Announce pulses: a new successor head becomes available ---
        pop_announce, deferred_announce, sq_not_empty = self._make_announce_pulses(
            em, n_sq, sq, sq_access_en, sq_bb_executed
        )

        # One-hot of the SQ dep slot after the head: the pivot for both the
        # satisfaction-clear walk and the announce read.
        sq_head_plus1_oh = LogicVec(em, "sq_head_plus1_oh", "w", n_sq)
        for i in range(n_sq):
            em.add_assignment((sq_head_plus1_oh, i), Val(sq.head_oh, (i - 1) % n_sq))

        # --- Satisfaction clear ---
        # A predecessor popping at a marked slot while no boundary is resolved
        # (boundary_valid = 0) satisfies the oldest QUEUED, NOT-YET-ANNOUNCED
        # dependent successor before it ever reaches the head: clear its S
        # bit, the first set bit at/after head+1. The pivot skips the head
        # slot deliberately: the head has by construction already been
        # announced (its dependency lives in ad_oh/boundary_valid), so its S
        # bit is dead - a stale 1 if it was announced dependent - and letting
        # the walk swallow it would shift every subsequent satisfaction onto
        # the wrong successor. Zeros (independent or already-satisfied
        # successors) are skipped by the priority search itself. (With
        # boundary_valid = 1 a marked pop is either the awaited boundary - the
        # consume below - or an already-departed successor's mark, and must
        # clear nothing.)
        marked_pop = Logic(em, "marked_pop", "w")
        em.add_assignment(marked_pop, pq_done_en & pq.array_at_head)
        s_clear_en = Logic(em, "sq_clear_en", "w")
        em.add_assignment(s_clear_en, marked_pop & ~boundary_valid & sq_not_empty)
        s_clear_oh = LogicVec(em, "sq_clear_oh", "w", n_sq)
        CyclicPriorityMasking(em, s_clear_oh, sq.array, sq_head_plus1_oh)
        for i in range(n_sq):
            em.add_assignment(
                (sq_clear_bits, i), s_clear_en & Val(s_clear_oh, i)
            )

        # --- Announced entry's dependent bit ---
        # Pop-announce: the incoming head is the entry at head+1 (the pop is
        # implied), read from the REGISTERED S array - a pop-announced entry
        # was pushed at least a cycle earlier (a push landing this cycle goes
        # through the deferred path instead). A satisfaction clear landing on
        # that very entry this same cycle must win: suppress the bit.
        announced_dep_bits = LogicVec(em, "announced_dep_bits", "w", n_sq)
        for i in range(n_sq):
            em.add_assignment(
                (announced_dep_bits, i),
                (Val(sq.array, i) & Val(sq_head_plus1_oh, i))
                & ~(s_clear_en & Val(s_clear_oh, i)),
            )
        announced_dep = Logic(em, "announced_dep", "w")
        Reduce(em, announced_dep, announced_dep_bits, BinOp.OR)

        # --- Jump events ---
        # Deferred announce (push into an empty queue): if the pushed
        # successor is dependent, its mark is landing at pq tail-1 THIS cycle
        # (the same BB execution writes both), so the boundary is that slot.
        jump_deferred = Logic(em, "ad_jump_deferred", "w")
        em.add_assignment(jump_deferred, deferred_announce & sq_write_value)
        jump_pop = Logic(em, "ad_jump_pop", "w")
        em.add_assignment(jump_pop, pop_announce & announced_dep)
        jump = Logic(em, "ad_jump", "w")
        em.add_assignment(jump, jump_deferred | jump_pop)

        # --- Precomputed jump target: registered marks, registered pivot ---
        # If a boundary is currently resolved, the next dependent successor
        # waits on the NEXT mark after it (the pivot must skip ad_oh's own
        # slot - it is still set); otherwise on the first mark at/after the
        # head (head_oh_next, so a pop landing this cycle cannot leave the
        # pivot behind the head).
        ad_oh_plus1 = LogicVec(em, "ad_oh_plus1", "w", n_pq)
        for i in range(n_pq):
            em.add_assignment((ad_oh_plus1, i), Val(ad_oh, (i - 1) % n_pq))

        search_pivot = LogicVec(em, "ad_search_pivot", "w", n_pq)
        em.add_assignment(
            search_pivot, ad_oh_plus1.when(boundary_valid).else_(pq.head_oh_next)
        )
        ad_oh_cand = LogicVec(em, "ad_oh_cand", "w", n_pq)
        CyclicPriorityMasking(em, ad_oh_cand, pq.array, search_pivot)

        # The mark landing this cycle for a deferred announce: pq tail-1.
        pq_tail_m1_oh = LogicVec(em, "pq_tail_m1_oh", "w", n_pq)
        for i in range(n_pq):
            em.add_assignment((pq_tail_m1_oh, i), Val(pq.tail_oh, (i + 1) % n_pq))

        em.add_assignment(
            ad_oh,
            pq_tail_m1_oh.when(jump_deferred).else_(
                ad_oh_cand.when(jump_pop).else_(ad_oh)
            ),
        )
        ad_oh.regInit(init=1)

        # --- Consume: the pop reaches the awaited boundary ---
        # Compares against the CURRENT head (pre-pop): the entry being popped
        # this cycle is the one ad_oh may be waiting on.
        head_is_ad = Logic(em, "ad_head_is_ad", "w")
        em.add_assignment(head_is_ad, pq.head_oh == ad_oh)
        consume = Logic(em, "ad_consume", "w")
        em.add_assignment(consume, boundary_valid & pq_done_en & head_is_ad)

        # A jump wins over a simultaneous consume: the consumed boundary is
        # released while the incoming successor claims the next one.
        em.add_assignment(
            boundary_valid,
            Bit(1).when(jump).else_(Bit(0).when(consume).else_(boundary_valid)),
        )
        boundary_valid.regInit(init=0)

        # --- Grant-path outputs: registered sources only ---
        # The window to check is every entry from the head up to and
        # including the boundary.
        span = LogicVec(em, "check_span", "w", n_pq)
        CyclicRangeFill(em, span, ad_oh, pq.head_oh)
        check_mask = LogicVec(em, "check_mask", "w", n_pq)
        for i in range(n_pq):
            em.add_assignment(
                (check_mask, i), Val(span, i).when(boundary_valid).else_(Bit(0))
            )

        no_dep_pending = Logic(em, "no_dep_pending", "w")
        em.add_assignment(no_dep_pending, ~boundary_valid)

        # The window's addresses are only meaningful once the predecessor
        # queue has allocated them: the head -> ad_oh distance (the index of
        # the window's last entry) must be below pq_length. Only meaningful
        # when a boundary is resolved.
        head_to_ad_ext = LogicVec(em, "ad_head_to_ad_ext", "w", pq_ptr_width)
        if pq_addr_width == 0:
            # n_pq == 1: the only physical slot is simultaneously head and
            # boundary, so the distance between them is always 0.
            em.add_assignment(head_to_ad_ext, Val(0, pq_ptr_width))
        else:
            ad_oh_idx = LogicVec(em, "ad_oh_idx", "w", pq_addr_width)
            OHToBits(em, ad_oh_idx, ad_oh)
            head_to_ad = LogicVec(em, "ad_head_to_ad", "w", pq_addr_width)
            WrapSub(em, head_to_ad, ad_oh_idx, pq.head_idx, n_pq)

            em.add_assignment(
                head_to_ad_ext, Val(0, pq_ptr_width - pq_addr_width).concat(head_to_ad)
            )
        corresponding_entry_allocated = Logic(em, "corresponding_entry_allocated", "w")
        em.add_assignment(
            corresponding_entry_allocated,
            ~boundary_valid | (pq_length_i > head_to_ad_ext),
        )

        return check_mask, no_dep_pending, [corresponding_entry_allocated]

    def _cross_bb_sequential_check(self, em: Emitter, pq_done_en, sq_access_en):
        """Forced-sequential cross-BB scheme: the out-of-order scheme's dep
        arrays plus a single token counter - no boundary register, no
        searches, no satisfaction-clear walk.

        The dep arrays are reused as-is (`_build_dep_arrays`): a mark on a
        predecessor entry means "last predecessor before some dependent
        successor"; a successor's dependent bit means "first successor of
        its run - the only one of the run that has to wait" (later
        successors of the same run share its boundary and sit behind it in
        the queue anyway). Since no addresses are compared, WHERE those
        marks sit never matters - and because predecessors complete in
        program order while successors fire in program order, the k-th mark
        to pop always belongs to the k-th dependent successor to reach the
        head. So the pairing needs no positional tracking at all:

          - a marked predecessor popping produces one go-token
            (every predecessor of that boundary has now completed),
          - the dependent successor at the head fires iff a token is
            available, consuming it; independent successors pass freely.

        Tokens absorb boundaries that complete before their successor
        reaches the head - the job of the out-of-order scheme's
        satisfaction-clear walk - so the S bits are never cleared in place
        and simply pop with their entry. At most one token per queued
        dependent successor can be outstanding (marks and dependent stamps
        are created one-for-one), which bounds the counter at n_sq."""
        n_sq = self.configs.sq.num_entries
        token_width = self.configs.sq.q_addr_width + 1  # holds 0 .. n_sq

        pq, sq, _, _, _ = self._build_dep_arrays(
            em, pq_done_en, sq_access_en, with_clears=False
        )

        marked_pop = Logic(em, "marked_pop", "w")
        em.add_assignment(marked_pop, pq_done_en & pq.array_at_head)
        dependent_fire = Logic(em, "dependent_fire", "w")
        em.add_assignment(dependent_fire, sq_access_en & sq.array_at_head)

        tokens = LogicVec(em, "boundary_tokens", "r", token_width)
        inc_tokens = LogicVec(em, "inc_boundary_tokens", "w", token_width)
        dec_tokens = LogicVec(em, "dec_boundary_tokens", "w", token_width)
        em.add_assignment(inc_tokens, Val(1).when(marked_pop).else_(Val(0)))
        em.add_assignment(dec_tokens, Val(1).when(dependent_fire).else_(Val(0)))
        em.add_assignment(tokens, (tokens + inc_tokens) - dec_tokens)
        tokens.regInit(init=0)

        sq_not_empty = Logic(em, "sq_dep_not_empty", "w")
        em.add_assignment(
            sq_not_empty,
            Val(sq.head.getNameRead()) != Val(sq.tail.getNameRead()),
        )

        # Grant: the head successor's dep entry must exist (its BB has
        # executed - also shields the registered array/pointer reads from
        # stale slots on a push into an empty queue), and it must be
        # independent or have its boundary already completed.
        tokens_available = Logic(em, "tokens_available", "w")
        em.add_assignment(tokens_available, tokens != Val(0, size=token_width))
        no_dep_pending = Logic(em, "no_dep_pending", "w")
        em.add_assignment(
            no_dep_pending, sq_not_empty & (~sq.array_at_head | tokens_available)
        )
        return no_dep_pending

    def _make_bb_ports(self, em: Emitter, prefix: str, ready):
        valid_i = self._add_port(Logic(em, f"{prefix}_bb_valid", "i"))
        ready_o = self._add_port(Logic(em, f"{prefix}_bb_ready", "o"))
        executed = Logic(em, f"{prefix}_bb_executed", "w")
        em.add_assignment(ready_o, ready)
        em.add_assignment(executed, valid_i & ready)
        return executed

    def _make_dep_array(self, em: Emitter, prefix: str, n_entries: int, head_en,
                        write_value=None, mark_en=None, clear_bits=None):
        addr_width = math.ceil(math.log2(n_entries))
        ptr_width = addr_width + 1

        array = LogicArray(em, f"{prefix}_array", "r", n_entries)

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

        tail_oh = LogicVec(em, f"{prefix}_dep_tail_oh", "w", n_entries)
        if addr_width == 0:
            # Single-entry array (n_entries == 1): no physical index exists,
            # the lone slot is always both tail and head.
            em.add_assignment(tail_oh, Val(1, size=1))
        else:
            tail_idx = LogicVec(em, f"{prefix}_dep_tail_idx", "w", addr_width)
            em.add_assignment(tail_idx, Val(em.slice_var(tail.getNameRead(), addr_width - 1, 0)))
            BitsToOH(em, tail_oh, tail_idx)

        # Empty before the push (head == tail). When empty there is no
        # most-recent entry, so a mark must be suppressed: otherwise it lands
        # on slot tail-1, which wraps behind the head, planting a stray '1'
        # outside the active [head, tail) window.
        empty = Logic(em, f"{prefix}_dep_empty", "w")
        em.add_assignment(empty,
            Val(em.slice_var(tail.getNameRead(), ptr_width - 1, 0))
            == Val(em.slice_var(head.getNameRead(), ptr_width - 1, 0)))

        # Same-cycle race: the lone pending entry (tail-1 == head) is both the
        # mark's target AND being popped (head_en) this very cycle. The mark
        # write would otherwise win unconditionally and plant a stale '1' into
        # the slot the head is leaving behind this same edge - a boundary bit
        # nothing would ever clear. This is the PpSs case where P retires (p)
        # not yet knowing a successor (S) is coming: the completion itself
        # satisfies S, so no mark (and no dependent stamp) must be left.
        retiring_now = Logic(em, f"{prefix}_dep_retiring_now", "w")
        em.add_assignment(retiring_now, head_en & ~empty & (head_next == tail))

        mark_already_consumed = None
        if mark_en is not None:
            # Predecessor marking scheme: a push (tail_en) writes 0 (this P is
            # not yet depended on). A successor execution (mark_en) overwrites
            # the most-recent entry, tail-1, with 1 - a definitive,
            # per-successor dependency boundary - without advancing the tail.
            # If the array is already empty at the mark, or its lone entry
            # retires this same cycle (retiring_now), tail-1 is/becomes
            # invalid as a mark target (the predecessor finished before, or in
            # lockstep with, the successor): suppress the stray write; the
            # successor is stamped independent instead (see `sq_write_value`).
            mark_valid = Logic(em, f"{prefix}_mark_valid", "w")
            em.add_assignment(mark_valid, mark_en & ~empty & ~retiring_now)
            mark_already_consumed = Logic(em, f"{prefix}_mark_consumed", "w")
            em.add_assignment(mark_already_consumed, mark_en & (empty | retiring_now))

            for i in range(n_entries):
                em.add_assignment(
                    array[i],
                    Bit(0).when(Val(tail_oh, i) & tail_en)
                    .else_(Bit(1).when(Val(tail_oh, (i + 1) % n_entries) & mark_valid)
                    .else_(array[i])))
        else:
            # Successor dependent bits: a push stamps `write_value`; a set
            # `clear_bits` bit (see `_cross_bb_check`: satisfaction clear or
            # announce handoff) zeroes a queued successor's bit. Push and
            # clear never collide: the clears target the [head, tail) window,
            # the push writes at the tail.
            for i in range(n_entries):
                cleared = array[i]
                if clear_bits is not None:
                    cleared = Bit(0).when(Val(clear_bits, i)).else_(array[i])
                em.add_assignment(
                    array[i],
                    write_value.when(Val(tail_oh, i) & tail_en).else_(cleared))
        array.regInit()

        full = Logic(em, f"{prefix}_dep_full", "w")
        tail_msb = Val(em.index_var(tail.getNameRead(), addr_width))
        head_msb = Val(em.index_var(head.getNameRead(), addr_width))
        if addr_width == 0:
            # No low bits to compare (the pointer is pure generation bit).
            em.add_assignment(full, tail_msb != head_msb)
        else:
            tail_low = Val(em.slice_var(tail.getNameRead(), addr_width - 1, 0))
            head_low = Val(em.slice_var(head.getNameRead(), addr_width - 1, 0))
            em.add_assignment(full, (tail_msb != head_msb) & (tail_low == head_low))

        # One-hot of the head index, used to anchor the boundary search, the
        # check window and the satisfaction clear.
        head_oh = LogicVec(em, f"{prefix}_dep_head_oh", "w", n_entries)
        array_at_head = Logic(em, f"{prefix}_array_at_head", "w")
        head_oh_next = LogicVec(em, f"{prefix}_dep_head_oh_next", "w", n_entries)
        if addr_width == 0:
            # Single-entry array: the lone slot is always the head, and stays
            # the head even "accounting for a pop happening right now" - there
            # is nowhere else for it to point. No index exists to hand back
            # (callers needing it, e.g. the cross-BB boundary search, must
            # special-case addr_width == 0 themselves).
            head_idx = None
            em.add_assignment(array_at_head, array[0])
            em.add_assignment(head_oh, Val(1, size=1))
            em.add_assignment(head_oh_next, Val(1, size=1))
        else:
            head_idx = LogicVec(em, f"{prefix}_dep_head_idx", "w", addr_width)
            em.add_assignment(head_idx, Val(em.slice_var(head.getNameRead(), addr_width - 1, 0)))
            MuxLookUp(em, array_at_head, array, head_idx)
            BitsToOH(em, head_oh, head_idx)

            # Same-cycle next-state view of head_oh: head_idx/head_oh are built
            # from the registered head (pre-edge), so a pop landing this very
            # cycle (head_en) is invisible to them until the next edge. Anything
            # that needs "where the head is, accounting for a pop happening right
            # now" (e.g. the boundary search pivot) must use this instead.
            head_idx_next = LogicVec(em, f"{prefix}_dep_head_idx_next", "w", addr_width)
            em.add_assignment(
                head_idx_next,
                Val(em.slice_var(head_next.getNameRead(), addr_width - 1, 0)).when(head_en).else_(head_idx),
            )
            BitsToOH(em, head_oh_next, head_idx_next)

        return _DepArray(
            array=array, head=head, tail=tail, tail_en=tail_en, tail_oh=tail_oh,
            full=full, empty=empty, array_at_head=array_at_head,
            mark_consumed=mark_already_consumed,
            head_idx=head_idx, head_oh=head_oh, head_oh_next=head_oh_next,
            n_entries=n_entries,
        )

    def _make_announce_pulses(self, em: Emitter, n_sq_entries, sq, sq_access_en, sq_bb_executed):
        """Two pulses marking that a new successor head has become available,
        plus the registered not-empty flag.

        `pop_announce`: the grant pops the current head and the queue still
        holds a next entry - that entry was pushed at least a cycle ago, so
        its dependent bit is readable from the registered S array.

        `deferred_announce`: a send drained the queue (or it is empty after
        reset), so there is no new head yet; the pulse fires on the push that
        repopulates the queue. `awaiting_head` is a sticky register that
        remembers the send-into-empty. The pushed entry's dependent bit is
        the in-flight `sq_write_value`, and its mark (if any) is landing at
        pq tail-1 this same cycle."""
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

        # A send that leaves the queue empty (not_empty now, empty after the
        # pop) has no new head to offer yet.
        send_into_empty = Logic(em, "sq_send_into_empty", "w")
        em.add_assignment(send_into_empty, sq_access_en & sq_not_empty & ~sq_not_empty_after_pop)

        # Sticky: set on send-into-empty, cleared once the deferred pulse
        # fires. Initialised to 1: at reset the queue is empty, so the first
        # successor BB execution is itself a "new head from empty".
        awaiting_head = Logic(em, "sq_awaiting_head", "r")
        deferred_announce = Logic(em, "sq_deferred_announce", "w")
        em.add_assignment(deferred_announce, awaiting_head & sq_bb_executed)
        em.add_assignment(
            awaiting_head,
            Bit(1).when(send_into_empty).else_(Bit(0).when(deferred_announce).else_(awaiting_head)),
        )
        awaiting_head.regInit(init=1)

        pop_announce = Logic(em, "sq_pop_announce", "w")
        em.add_assignment(pop_announce, sq_access_en & sq_not_empty_after_pop)

        return pop_announce, deferred_announce, sq_not_empty

    def _build_dep_arrays(self, em: Emitter, pq_done_en, sq_access_en, with_clears=True):
        """Instantiate the predecessor/successor dependency arrays and the few
        derived signals the cross-BB scheme consumes.

        The PQ array marks a definitive per-successor dependency boundary (a
        1) on the most-recent predecessor entry each time a successor BB
        execution follows a predecessor run; the SQ array tracks which
        QUEUED, NOT-YET-ANNOUNCED successors still have a pending boundary
        (0 = went before any predecessor, never had one, satisfied while
        queued, or already announced - handed off to ad_oh)."""
        # dep_entry_ratio temporarily hardcoded to 1 (see generate()).
        n_pq_entries = self.configs.pq.num_entries
        n_sq_entries = self.configs.sq.num_entries

        sq_write_value = Logic(em, "sq_write_value", "w")
        # Per-bit satisfaction clears, driven by `_cross_bb_check` (which owns
        # the announce/satisfaction logic). The forced-sequential scheme needs
        # none: its token counter absorbs early boundary completions.
        sq_clear_bits = (
            LogicVec(em, "sq_clear_bits", "w", n_sq_entries) if with_clears else None
        )

        # Successor dep array first, so sq_bb_executed is available to mark
        # the predecessor array's dependency boundaries.
        sq = self._make_dep_array(
            em, "sq", n_sq_entries, sq_access_en,
            write_value=sq_write_value, clear_bits=sq_clear_bits,
        )
        sq_bb_executed = self._make_bb_ports(em, "sq", ~sq.full)

        pq_executed_last = Logic(em, "pq_executed_last", "r")
        sq_executed_last = Logic(em, "sq_executed_last", "r")

        # Predecessor dep array: pushes write 0; the first successor BB
        # execution after a predecessor marks the most-recent predecessor
        # entry as a definitive (per-successor) boundary.
        first_sq_bb = Logic(em, "first_sq_bb")
        em.add_assignment(first_sq_bb, ~sq_executed_last & sq_bb_executed)
        pq = self._make_dep_array(em, "pq", n_pq_entries, pq_done_en, mark_en=first_sq_bb)
        pq_bb_executed = self._make_bb_ports(em, "pq", ~pq.full)

        # Published for `generate`'s access gates (see `_dep_arrays`).
        self._dep_arrays = (pq, sq)

        em.add_assignment(pq.tail_en, pq_bb_executed)
        em.add_assignment(sq.tail_en, sq_bb_executed)

        # TODO: What if the BBs execute at the same time?
        # -> For now not possible
        em.add_assignment(pq_executed_last, ~sq_bb_executed & (pq_bb_executed | pq_executed_last))
        em.add_assignment(sq_executed_last, ~pq_bb_executed & (sq_bb_executed | sq_executed_last))

        # At reset no predecessor has executed yet, so a successor that
        # genuinely goes first must stamp its dep entry with 0 and keep its
        # free pass, rather than be marked dependent on a predecessor that
        # never ran. Likewise, if the mark could not land (pq.mark_consumed:
        # the awaited predecessor retired before or in lockstep with this
        # successor's BB execution), there is nothing left to wait on either.
        pq_executed_last.regInit(init=0)
        sq_executed_last.regInit(init=1)
        em.add_assignment(sq_write_value, pq_executed_last & ~pq.mark_consumed)

        return pq, sq, sq_bb_executed, sq_write_value, sq_clear_bits
