"""Dependency checker: holds back a successor queue's accesses until every
predecessor access preceding them in program order has completed or is known
to target a different address.

Both schemes produce the same three things, combined in `generate`:
  - `check_mask`: the physical predecessor slots the head successor's address
    must be compared against,
  - `no_dep_pending`: the head successor has no pending predecessor left,
  - scheme-specific extra grant conditions.

Same BB (`_same_bb_check`): the order of the two accesses is static, so a
signed up/down counter, the access disparity AD (+1 per successor access, -1
per predecessor completion), is the head-relative index of the last
predecessor entry the head successor must check (AD < 0: nothing pending).

Different BBs (`_cross_bb_check`): the interleaving is dynamic and arrives as
BB-execution handshakes. The first successor execution after a predecessor
run marks the most recent predecessor entry as its boundary (predecessor dep
array: WHERE) and stamps the successor entry dependent (successor dep array:
WHEN). A boundary register `ad_oh` jumps from mark to mark, one dependent
successor at a time, with its target precomputed from registered state.

The dep arrays' pointers are shared per port (Queue._shared_dep_state). The
predecessor array lives in the queue's physical slot frame, because its slots
index the queue's addresses; the successor array is head-anchored (only the
successor's head address is ever compared, so no frame has to match).

A single-entry queue (NumEntries == 1) has q_addr_width == 0: there is no slot
index, the pointer is the bare generation bit, and the lone slot is always
both head and tail. The `== 0` branches below handle that case.
"""

import math
from dataclasses import dataclass
from functools import reduce

from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import (
    BitsToOH, CyclicPriorityMasking, CyclicRangeFill, MuxLookUp, Reduce,
)
from core_gen.ir import BinOp, Val, Bit, CustomStatement
from core_gen.utils import isPow2
from custom_core_gen.configs import DependencyCheckerConfig
from custom_core_gen.generators.generator import Generator
from custom_core_gen.generators.ptr_utils import ptr_next, ptr_index, ptr_diff, count_slot


def _bits(em: Emitter, vec, n: int, bit) -> None:
    """vec[i] = bit(i) for every i < n."""
    for i in range(n):
        em.add_assignment((vec, i), bit(i))


@dataclass
class _DepArray:
    array: object          # registered bits
    tail_en: object        # push enable, driven by the caller
    tail_oh: object        # one-hot of the push slot
    full: object
    empty: object
    not_empty: object
    array_at_head: object
    head_oh: object
    head_oh_next: object   # head one-hot, accounting for a pop this cycle
    mark_consumed: object = None  # predecessor: the mark could not land
    cnt: object = None            # successor: occupancy


class DependencyChecker(Generator):
    def __init__(self, name: str, suffix: str, configs: DependencyCheckerConfig):
        super().__init__(name, suffix, configs)

    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        c = self.configs
        # The dep arrays are sized pq/sq.num_entries, which does not scale by it.
        assert c.dep_entry_ratio == 1, "dep_entry_ratio must be 1"
        self.ports.clear()
        port = self._add_port
        crosses_bb = c.pq_bb != c.sq_bb
        n_pq = c.pq.num_entries
        pq_ptr_width = c.pq.q_addr_width + 1
        sq_ptr_width = c.sq.q_addr_width + 1

        # Predecessor. A single-entry queue has no done index to expose
        # (Queue omits its done_ptr_o the same way).
        pq_addr_i = port(LogicVecArray(em, "pq_addr", "i", n_pq, c.pq.addr_width))
        pq_done_i = (port(LogicVec(em, "pq_done", "i", c.pq.q_addr_width))
                     if c.pq.q_addr_width > 0 else None)
        pq_done_en_i = port(Logic(em, "pq_done_en", "i"))
        # Only read by the same-BB scheme; cross-BB tests the end marker instead.
        pq_length_i = port(LogicVec(em, "pq_length", "i", pq_ptr_width))
        if crosses_bb:
            # The predecessor's end-of-allocation marker (one-hot of its first
            # unallocated slot) and whether every slot is allocated.
            self._pq_alloc_end_oh_i = port(LogicVec(em, "pq_alloc_end_oh", "i", n_pq))
            self._pq_alloc_full_i = port(Logic(em, "pq_alloc_full", "i"))
            # Shared dep-array pointers, owned by the queues: heads are the
            # predecessor's q_done and the successor's q_head, tails each
            # port's BB-execution count. They advance on exactly this
            # checker's head_en / tail_en.
            self._shared_ptrs = {
                "pq_head": port(LogicVec(em, "pq_dep_head", "i", pq_ptr_width)),
                "pq_tail": port(LogicVec(em, "pq_dep_tail", "i", pq_ptr_width)),
                "sq_head": port(LogicVec(em, "sq_dep_head", "i", sq_ptr_width)),
                "sq_tail": port(LogicVec(em, "sq_dep_tail", "i", sq_ptr_width)),
            }
            self._pq_pending_done_i = None
        else:
            # Retired, not yet completed predecessor accesses (`_ad_counter`).
            self._shared_ptrs = None
            self._pq_pending_done_i = port(LogicVec(em, "pq_pending_done", "i", pq_ptr_width))

        # Successor and outputs.
        sq_head = port(LogicVec(em, "sq_head", "i", c.sq.addr_width))
        sq_access_en_i = port(Logic(em, "sq_access_en", "i"))
        allow_sq_access_o = port(Logic(em, "allow_sq_access", "o"))
        allow_pq_access_o = port(Logic(em, "allow_pq_access", "o"))

        self._dep_arrays = None  # set by `_build_dep_arrays` (cross-BB only)
        if crosses_bb:
            check_mask, no_dep_pending, conditions = self._cross_bb_check(
                em, pq_done_en_i, sq_access_en_i)
        else:
            check_mask, no_dep_pending, conditions = self._same_bb_check(
                em, pq_done_i, pq_done_en_i, sq_access_en_i, pq_length_i)

        # A conflict: a pending (masked) predecessor entry with the head
        # successor's address.
        conflict = Logic(em, "conflict", "w")
        conflicts = LogicVec(em, "conflicts", "w", n_pq)
        _bits(em, conflicts, n_pq, lambda i: Val(check_mask, i).when(
            Val(pq_addr_i, i) == sq_head).else_(Bit(0)))
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
        em.add_assignment(allow_sq_access_o, reduce(
            lambda a, b: a & b, conditions + extra_sq + [~conflict | no_dep_pending]))
        self._write_to_file(em, path_rtl, out_file)

    def _bb_execution_gates(self):
        """(allow_pq, extra_sq): gates on each queue's retirement.

        Cross-BB: a dep-array entry is pushed by the BB token and popped by
        the queue's address path, which are back-pressured separately (the
        token by every dep array of the BB, the address only by its queue).
        Without a gate a queue can retire an access whose BB execution is not
        recorded yet, popping an empty array; at NumEntries == 1, where empty
        and full are complements, that latches `full` and the BB deadlocks.
        So each queue may only retire while its dep array is non-empty.

        Same-BB: no arrays; the AD counter's underflow guard (`_ad_counter`).
        """
        if self._dep_arrays is None:
            return self._pq_underflow_gate, []
        pq, sq = self._dep_arrays
        return ~pq.empty, [~sq.empty]

    @staticmethod
    def _sign_extend(em: Emitter, name: str, sig, width: int):
        out = LogicVec(em, name, "w", width, is_signed=True)
        em.add_custom_statement(CustomStatement(
            f"{out.getNameWrite()} <= resize({sig.getNameRead()}, {width});",
            f"assign {out.getNameWrite()} = {sig.getNameRead()};"))
        return out

    # ===--------------------------------------------------------------------===
    # Same-BB scheme
    # ===--------------------------------------------------------------------===

    def _same_bb_check(self, em: Emitter, pq_done_i, pq_done_en_i, sq_access_en_i,
                       pq_length_i):
        """The window is the predecessor slots at head-relative index 0..AD."""
        n_pq = self.configs.pq.num_entries
        n = self.configs.pq.q_addr_width
        ad_width = self.configs.access_disparity_width
        ad = self._ad_counter(em, ad_width, sq_access_en_i, pq_done_en_i)

        # Wide enough to hold AD in full and n_pq as a POSITIVE signed number,
        # so AD, slot indices and the queue length compare without wrapping.
        cmp_width = max(ad_width, n + 2)
        ad_cmp = self._sign_extend(em, "ad_cmp", ad, cmp_width)
        # pq_length is non-negative: zero-extension keeps its value.
        pq_length_cmp = LogicVec(em, "pq_length_cmp", "w", cmp_width, is_signed=True)
        em.add_assignment(pq_length_cmp, Val(0, cmp_width - n - 1).concat(pq_length_i))

        no_dep_pending = Logic(em, "no_dep_pending", "w")
        em.add_assignment(no_dep_pending, ad < Val(0, size=ad_width))

        # check_mask[j] = AD >= 0 and (j - pq_done) mod n_pq <= min(AD, n_pq - 1)
        # Computed per slot from pq_done and a saturated AD (a LUT or two per
        # bit) rather than as a thermometer of AD rotated by pq_done.
        check_mask = LogicVec(em, "check_mask", "w", n_pq)
        if n == 0:
            # Single entry: the lone slot is always head-relative index 0.
            em.add_assignment((check_mask, 0), ~no_dep_pending)
        else:
            # AD saturated into [0, n_pq - 1]: AD can reach n_pq (see
            # ad_not_maxed) and must then still select the whole queue.
            # Negative AD is masked by no_dep_pending instead.
            ad_slice = em.slice_var(ad_cmp.getNameRead(), n - 1, 0)
            # ad_cmp is `signed`; VHDL needs the slice converted explicitly.
            ad_low = (f"std_logic_vector({ad_slice})"
                      if em.get_file_suffix() == "vhd" else ad_slice)
            ad_sat = LogicVec(em, "ad_sat", "w", n)
            em.add_assignment(ad_sat, Val((1 << n) - 1, size=n).when(
                ad_cmp >= Val(n_pq - 1, size=cmp_width)).else_(Val(ad_low)))
            # Head-relative index of each slot. For a power of two the n-bit
            # subtraction wraps exactly; otherwise add n_pq back when pq_done
            # lies above j.
            rel_idx = LogicVecArray(em, "rel_idx", "w", n_pq, n)
            if not isPow2(n_pq):
                # n + 1 bits hold n_pq + j < 2 n_pq; the result (< n_pq) fits
                # back into n bits.
                pq_done_ext = LogicVec(em, "pq_done_ext", "w", n + 1)
                em.add_assignment(pq_done_ext, Bit(0).concat(pq_done_i))
            for j in range(n_pq):
                if isPow2(n_pq):
                    em.add_assignment(rel_idx[j], Val(j, size=n) - pq_done_i)
                else:
                    wide = LogicVec(em, f"rel_idx_wide_{j}", "w", n + 1)
                    em.add_assignment(wide, (Val(j) - pq_done_ext).when(
                        pq_done_ext <= Val(j, size=n + 1)).else_(Val(n_pq + j) - pq_done_ext))
                    em.add_assignment(
                        rel_idx[j], Val(em.slice_var(wide.getNameRead(), n - 1, 0)))
                em.add_assignment((check_mask, j), ~no_dep_pending & (rel_idx[j] <= ad_sat))

        # The predecessor must have allocated the window's last entry:
        # pq_length > AD (AD is count - 1).
        allocated = Logic(em, "corresponding_entry_allocated", "w")
        em.add_assignment(allocated, pq_length_cmp > ad_cmp)
        conditions = [allocated]
        # Overflow: `allocated` needs AD < pq_length <= n_pq, so after the
        # grant's increment AD <= n_pq. If the signed width cannot hold n_pq,
        # cap explicitly; strictly (AD < max), since the gated grant
        # increments AD once more.
        max_ad = (1 << (ad_width - 1)) - 1
        if max_ad < n_pq:
            ad_not_maxed = Logic(em, "ad_not_maxed", "w")
            em.add_assignment(ad_not_maxed, ad < Val(max_ad, size=ad_width))
            conditions.append(ad_not_maxed)
        return check_mask, no_dep_pending, conditions

    def _ad_counter(self, em: Emitter, ad_width, sq_access_en, pq_done_en):
        """The access disparity, plus the predecessor-retire underflow guard.

        AD is kept in range by back-pressure, never by clamping: a clamped
        counter loses count, so after surplus predecessor completions a
        successor would check a predecessor access that comes LATER in
        program order, which deadlocks with a reverse same-BB edge. The top
        is capped by the grant's `ad_not_maxed`. At the bottom a completion
        cannot be refused (a store completion is a registered pulse from the
        memory interface), so the gate sits one step earlier, on the
        predecessor's retirement: with pending = retired, not yet completed,
        a retire is allowed only while AD - pending > min. Completions lower
        both sides alike and successor accesses only raise AD, so
        AD - pending >= min is invariant, and with pending >= 0 AD >= min."""
        # A sign bit plus one value bit, to hold both -1 and 0.
        assert ad_width is not None and ad_width >= 2, (
            f"AccessDisparityWidth must be >= 2 for a signed counter, got {ad_width}")
        ad = LogicVec(em, "access_disparity", "r", ad_width, is_signed=True)
        # 0: one predecessor access to check, at index 0. -1 when the
        # successor precedes the predecessor in the BB (nothing to check yet).
        ad.regInit(init=-1 if self.configs.succ_can_execute_once else 0)
        # One adder; the +1 / 0 / -1 delta is built bitwise (two's complement)
        # because the Verilog emitter cannot nest signed literals in a when-else.
        delta = LogicVec(em, "ad_delta", "w", ad_width)
        em.add_assignment((delta, 0), sq_access_en ^ pq_done_en)
        for b in range(1, ad_width):
            em.add_assignment((delta, b), pq_done_en & ~sq_access_en)
        em.add_assignment(ad, ad + delta)

        # Guard at a width holding AD - pending without wrapping
        # (AD in [min, max], pending in [0, n_pq]).
        pending = self._pq_pending_done_i
        guard_width = max(ad_width, pending.size + 1) + 1
        ad_ext = self._sign_extend(em, "ad_guard_ext", ad, guard_width)
        pending_ext = LogicVec(em, "pq_pending_ext", "w", guard_width, is_signed=True)
        em.add_assignment(pending_ext, Val(0, guard_width - pending.size).concat(pending))
        # A signed wire: the Verilog emitter takes signedness from signal
        # operands only, not from sub-expressions.
        diff = LogicVec(em, "ad_minus_pending", "w", guard_width, is_signed=True)
        em.add_assignment(diff, ad_ext - pending_ext)
        self._pq_underflow_gate = Logic(em, "pq_retire_no_underflow", "w")
        em.add_assignment(self._pq_underflow_gate,
                          diff > Val(-(1 << (ad_width - 1)), size=guard_width))
        return ad

    # ===--------------------------------------------------------------------===
    # Cross-BB scheme
    # ===--------------------------------------------------------------------===

    def _cross_bb_check(self, em: Emitter, pq_done_en, sq_access_en):
        """State: predecessor marks (WHERE to jump), successor dependent bits
        (WHEN to jump), and `ad_oh` + `boundary_valid`, the boundary of the
        successor at the head. `boundary_valid` survives an early
        (conflict-free) grant, so later successors of the same run keep
        checking the same window, and clears when the boundary pops.

        Every successor is announced exactly once, as it becomes head
        (`pop_announce`, or `deferred_announce` for a push into an empty
        queue). If dependent, `ad_oh` jumps to its boundary: the next mark
        after the current one, or the first from the head. The jump target
        is precomputed every cycle from REGISTERED marks and a pivot chosen
        by the REGISTERED boundary_valid, so the grant only drives a final
        mux select (deriving the pivot from the grant was the critical path).

        A marked predecessor completion while no boundary is resolved
        satisfies the oldest queued dependent successor before it reaches the
        head: its bit is cleared in place (satisfaction clear)."""
        n_pq = self.configs.pq.num_entries
        n_sq = self.configs.sq.num_entries
        pq, sq, sq_bb_executed, sq_write_value, sq_clear_bits = (
            self._build_dep_arrays(em, pq_done_en, sq_access_en))

        boundary_valid = Logic(em, "boundary_valid", "r")
        ad_oh = LogicVec(em, "ad_oh", "r", n_pq)
        pop_announce, deferred_announce, sq_not_empty = self._make_announce_pulses(
            em, n_sq, sq, sq_access_en, sq_bb_executed)

        # The slot after the successor head (the constant slot 1, anchored):
        # pivot of the clear walk and of the announce read.
        sq_head_plus1_oh = LogicVec(em, "sq_head_plus1_oh", "w", n_sq)
        _bits(em, sq_head_plus1_oh, n_sq, lambda i: Val(sq.head_oh, (i - 1) % n_sq))

        # Satisfaction clear: the first set bit at/after head+1. The head is
        # skipped on purpose: it has already been announced, so its
        # dependency lives in ad_oh and its bit is dead (a stale 1 if it was
        # dependent). Walking from the head would swallow that stale bit and
        # shift every later satisfaction onto the wrong successor (e.g.
        # PSPSPSPS then pppp), leaving the last one to latch a stale mark and
        # deadlock. With boundary_valid a marked pop is the awaited boundary
        # (consumed below) or a departed successor's mark: it clears nothing.
        marked_pop = Logic(em, "marked_pop", "w")
        em.add_assignment(marked_pop, pq_done_en & pq.array_at_head)
        s_clear_en = Logic(em, "sq_clear_en", "w")
        em.add_assignment(s_clear_en, marked_pop & ~boundary_valid & sq_not_empty)
        s_clear_oh = LogicVec(em, "sq_clear_oh", "w", n_sq)
        # No wrap needed: the live window is slots 0..cnt-1 with nothing stale
        # above, and the only slot a cyclic walk would add is the dead head.
        self._first_set_from(em, s_clear_oh, sq.array, sq_head_plus1_oh, n_sq,
                             "sq_clear_search")
        _bits(em, sq_clear_bits, n_sq, lambda i: s_clear_en & Val(s_clear_oh, i))

        # Dependent bit of the pop-announced entry (head+1), from the
        # registered array: it was pushed at least a cycle ago (a same-cycle
        # push takes the deferred path). A same-cycle clear of it wins.
        announced_dep_bits = LogicVec(em, "announced_dep_bits", "w", n_sq)
        _bits(em, announced_dep_bits, n_sq, lambda i: (
            Val(sq.array, i) & Val(sq_head_plus1_oh, i)) & ~(s_clear_en & Val(s_clear_oh, i)))
        announced_dep = Logic(em, "announced_dep", "w")
        Reduce(em, announced_dep, announced_dep_bits, BinOp.OR)

        # Jumps. A deferred announce's mark lands at predecessor tail-1 this
        # very cycle (one BB execution writes both), so that slot is the
        # boundary directly, with no search.
        jump_deferred = Logic(em, "ad_jump_deferred", "w")
        em.add_assignment(jump_deferred, deferred_announce & sq_write_value)
        jump_pop = Logic(em, "ad_jump_pop", "w")
        em.add_assignment(jump_pop, pop_announce & announced_dep)
        jump = Logic(em, "ad_jump", "w")
        em.add_assignment(jump, jump_deferred | jump_pop)

        # Jump target. With a boundary resolved: the next mark after ad_oh
        # (skipping ad_oh's own slot, which is still set). Otherwise: the
        # first mark from the head, using head_oh_next so a pop this cycle
        # cannot leave the pivot behind the head.
        ad_oh_plus1 = LogicVec(em, "ad_oh_plus1", "w", n_pq)
        _bits(em, ad_oh_plus1, n_pq, lambda i: Val(ad_oh, (i - 1) % n_pq))
        search_pivot = LogicVec(em, "ad_search_pivot", "w", n_pq)
        em.add_assignment(search_pivot, ad_oh_plus1.when(boundary_valid).else_(pq.head_oh_next))
        ad_oh_cand = LogicVec(em, "ad_oh_cand", "w", n_pq)
        CyclicPriorityMasking(em, ad_oh_cand, pq.array, search_pivot)
        pq_tail_m1_oh = LogicVec(em, "pq_tail_m1_oh", "w", n_pq)
        _bits(em, pq_tail_m1_oh, n_pq, lambda i: Val(pq.tail_oh, (i + 1) % n_pq))
        em.add_assignment(ad_oh, pq_tail_m1_oh.when(jump_deferred).else_(
            ad_oh_cand.when(jump_pop).else_(ad_oh)))
        ad_oh.regInit(init=1)

        # The awaited boundary pops (compared with the pre-pop head). A
        # same-cycle jump wins: the consumed boundary is released while the
        # incoming successor claims the next one (ad_oh+1 already skips it).
        head_is_ad = Logic(em, "ad_head_is_ad", "w")
        em.add_assignment(head_is_ad, pq.head_oh == ad_oh)
        consume = Logic(em, "ad_consume", "w")
        em.add_assignment(consume, boundary_valid & pq_done_en & head_is_ad)
        em.add_assignment(boundary_valid, Bit(1).when(jump).else_(
            Bit(0).when(consume).else_(boundary_valid)))
        boundary_valid.regInit(init=0)

        # Window [head, ad_oh]. The allocated slots run from the head up to
        # the end-of-allocation marker, so the boundary is unallocated exactly
        # when the marker lies in the window, unless the queue is full (the
        # marker then aliases the head). This replaces a head-to-boundary
        # distance compared with pq_length.
        span = LogicVec(em, "check_span", "w", n_pq)
        CyclicRangeFill(em, span, ad_oh, pq.head_oh)
        check_mask = LogicVec(em, "check_mask", "w", n_pq)
        _bits(em, check_mask, n_pq, lambda i: Val(span, i).when(boundary_valid).else_(Bit(0)))
        no_dep_pending = Logic(em, "no_dep_pending", "w")
        em.add_assignment(no_dep_pending, ~boundary_valid)
        end_bits = LogicVec(em, "ad_end_in_window_bits", "w", n_pq)
        _bits(em, end_bits, n_pq, lambda i: Val(span, i) & Val(self._pq_alloc_end_oh_i, i))
        end_in_window = Logic(em, "ad_end_in_window", "w")
        Reduce(em, end_in_window, end_bits, BinOp.OR)
        allocated = Logic(em, "corresponding_entry_allocated", "w")
        em.add_assignment(allocated, ~boundary_valid | self._pq_alloc_full_i | ~end_in_window)
        return check_mask, no_dep_pending, [allocated]

    def _make_bb_ports(self, em: Emitter, prefix: str, ready):
        valid_i = self._add_port(Logic(em, f"{prefix}_bb_valid", "i"))
        ready_o = self._add_port(Logic(em, f"{prefix}_bb_ready", "o"))
        executed = Logic(em, f"{prefix}_bb_executed", "w")
        em.add_assignment(ready_o, ready)
        em.add_assignment(executed, valid_i & ready)
        return executed

    def _build_dep_arrays(self, em: Emitter, pq_done_en, sq_access_en):
        """A successor's dependent bit is 0 if it went before any predecessor,
        its mark could not land, it was satisfied while queued, or it has
        been announced (handed off to ad_oh)."""
        n_pq = self.configs.pq.num_entries
        n_sq = self.configs.sq.num_entries
        sq_write_value = Logic(em, "sq_write_value", "w")
        # Driven by `_cross_bb_check`'s satisfaction clear.
        sq_clear_bits = LogicVec(em, "sq_clear_bits", "w", n_sq)
        # Successor array first: its BB execution marks the predecessor array.
        sq = self._succ_dep_array(em, n_sq, sq_access_en, sq_write_value, sq_clear_bits)
        sq_bb_executed = self._make_bb_ports(em, "sq", ~sq.full)
        pq_executed_last = Logic(em, "pq_executed_last", "r")
        sq_executed_last = Logic(em, "sq_executed_last", "r")
        first_sq_bb = Logic(em, "first_sq_bb")
        em.add_assignment(first_sq_bb, ~sq_executed_last & sq_bb_executed)
        pq = self._pred_dep_array(em, n_pq, pq_done_en, first_sq_bb)
        pq_bb_executed = self._make_bb_ports(em, "pq", ~pq.full)
        self._dep_arrays = (pq, sq)
        em.add_assignment(pq.tail_en, pq_bb_executed)
        em.add_assignment(sq.tail_en, sq_bb_executed)
        # Assumes the two BBs never execute in the same cycle.
        em.add_assignment(pq_executed_last, ~sq_bb_executed & (pq_bb_executed | pq_executed_last))
        em.add_assignment(sq_executed_last, ~pq_bb_executed & (sq_bb_executed | sq_executed_last))
        # At reset no predecessor has run: a successor that goes first is
        # independent (and does not count as "first after a predecessor").
        pq_executed_last.regInit(init=0)
        sq_executed_last.regInit(init=1)
        # Dependent iff a predecessor ran before and its mark could land.
        em.add_assignment(sq_write_value, pq_executed_last & ~pq.mark_consumed)
        return pq, sq, sq_bb_executed, sq_write_value, sq_clear_bits

    def _pred_dep_array(self, em: Emitter, n: int, head_en, mark_en) -> _DepArray:
        """Boundary marks, in the queue's physical slot frame (head = q_done,
        tail = the port's BB-execution count). A push writes 0; a successor
        BB execution marks the most recent entry (tail-1) without advancing
        the tail."""
        addr_width = math.ceil(math.log2(n))  # slot index width, 0 if n == 1
        ptr_width = addr_width + 1
        array = LogicArray(em, "pq_array", "r", n)
        tail_en = Logic(em, "pq_dep_tail_en", "w")
        head_next = LogicVec(em, "pq_dep_head_next", "w", ptr_width)
        head, tail = self._shared_ptrs["pq_head"], self._shared_ptrs["pq_tail"]
        ptr_next(em, head_next, head, n)

        tail_oh = LogicVec(em, "pq_dep_tail_oh", "w", n)
        if addr_width == 0:
            em.add_assignment(tail_oh, Val(1, size=1))
        else:
            tail_idx = LogicVec(em, "pq_dep_tail_idx", "w", addr_width)
            ptr_index(em, tail_idx, tail, n, addr_width)
            BitsToOH(em, tail_oh, tail_idx)
        empty = Logic(em, "pq_dep_empty", "w")
        em.add_assignment(empty, Val(em.slice_var(tail.getNameRead(), ptr_width - 1, 0))
                          == Val(em.slice_var(head.getNameRead(), ptr_width - 1, 0)))
        # The mark cannot land if the array is empty (tail-1 would wrap behind
        # the head) or its lone entry retires this same cycle (the mark would
        # plant a 1 in the slot the head leaves, which nothing clears). The
        # predecessor then finished before, or in lockstep with, the
        # successor, so `mark_consumed` stamps the successor independent.
        retiring_now = Logic(em, "pq_dep_retiring_now", "w")
        em.add_assignment(retiring_now, head_en & ~empty & (head_next == tail))
        mark_valid = Logic(em, "pq_mark_valid", "w")
        em.add_assignment(mark_valid, mark_en & ~empty & ~retiring_now)
        mark_consumed = Logic(em, "pq_mark_consumed", "w")
        em.add_assignment(mark_consumed, mark_en & (empty | retiring_now))
        for i in range(n):
            em.add_assignment(array[i], Bit(0).when(Val(tail_oh, i) & tail_en).else_(
                Bit(1).when(Val(tail_oh, (i + 1) % n) & mark_valid).else_(array[i])))
        array.regInit()

        # Full: same slot, other generation (for every n, see ptr_utils).
        full = Logic(em, "pq_dep_full", "w")
        tail_msb = Val(em.index_var(tail.getNameRead(), addr_width))
        head_msb = Val(em.index_var(head.getNameRead(), addr_width))
        if addr_width == 0:
            em.add_assignment(full, tail_msb != head_msb)
        else:
            tail_low = Val(em.slice_var(tail.getNameRead(), addr_width - 1, 0))
            head_low = Val(em.slice_var(head.getNameRead(), addr_width - 1, 0))
            em.add_assignment(full, (tail_msb != head_msb) & (tail_low == head_low))

        head_oh = LogicVec(em, "pq_dep_head_oh", "w", n)
        array_at_head = Logic(em, "pq_array_at_head", "w")
        head_oh_next = LogicVec(em, "pq_dep_head_oh_next", "w", n)
        if addr_width == 0:
            em.add_assignment(array_at_head, array[0])
            em.add_assignment(head_oh, Val(1, size=1))
            em.add_assignment(head_oh_next, Val(1, size=1))
        else:
            head_idx = LogicVec(em, "pq_dep_head_idx", "w", addr_width)
            ptr_index(em, head_idx, head, n, addr_width)
            MuxLookUp(em, array_at_head, array, head_idx)
            BitsToOH(em, head_oh, head_idx)
            # head_oh is built from the registered head, so a pop this cycle
            # is invisible to it until the next edge; head_oh_next is not.
            head_next_idx = LogicVec(em, "pq_dep_head_next_idx", "w", addr_width)
            ptr_index(em, head_next_idx, head_next, n, addr_width)
            head_idx_next = LogicVec(em, "pq_dep_head_idx_next", "w", addr_width)
            em.add_assignment(head_idx_next, head_next_idx.when(head_en).else_(head_idx))
            BitsToOH(em, head_oh_next, head_idx_next)
        not_empty = Logic(em, "pq_dep_not_empty", "w")
        em.add_assignment(not_empty, ~empty)
        return _DepArray(array, tail_en, tail_oh, full, empty, not_empty,
                         array_at_head, head_oh, head_oh_next, mark_consumed=mark_consumed)

    def _succ_dep_array(self, em: Emitter, n: int, head_en, write_value,
                        clear_bits) -> _DepArray:
        """Dependent bits, head-anchored: slot 0 is the head and the array
        shifts down on a pop; the occupancy is the shared bb_tail - q_head.
        Writes land at their registered slot (a push at cnt, the clears as
        given) and are then shifted, so no stale bit sits above cnt. A push
        and a clear never collide: clears hit live entries, the push the
        first free slot."""
        addr_width = math.ceil(math.log2(n))  # slot index width, 0 if n == 1
        ptr_width = addr_width + 1
        cnt = LogicVec(em, "sq_dep_cnt", "w", ptr_width)
        ptr_diff(em, cnt, self._shared_ptrs["sq_tail"], self._shared_ptrs["sq_head"], n)
        array = LogicArray(em, "sq_array", "r", n)
        tail_en = Logic(em, "sq_dep_tail_en", "w")
        empty = Logic(em, "sq_dep_empty", "w")
        em.add_assignment(empty, cnt == Val(0, size=ptr_width))
        not_empty = Logic(em, "sq_dep_not_empty", "w")
        em.add_assignment(not_empty, ~empty)
        full = Logic(em, "sq_dep_full", "w")
        em.add_assignment(full, cnt == Val(n, size=ptr_width))

        # One-hot of slot cnt mod n (full aliases slot 0; there is no push then).
        tail_oh = LogicVec(em, "sq_dep_tail_oh", "w", n)
        if addr_width == 0:
            em.add_assignment(tail_oh, Val(1, size=1))
        else:
            tail_idx = LogicVec(em, "sq_dep_tail_idx", "w", addr_width)
            count_slot(em, tail_idx, cnt, n, addr_width)
            BitsToOH(em, tail_oh, tail_idx)
        retiring_now = Logic(em, "sq_dep_retiring_now", "w")
        em.add_assignment(retiring_now, head_en & (cnt == Val(1, size=ptr_width)))

        written = LogicArray(em, "sq_array_written", "w", n)
        for i in range(n):
            em.add_assignment(written[i], write_value.when(Val(tail_oh, i) & tail_en).else_(
                Bit(0).when(Val(clear_bits, i)).else_(array[i])))
        # A pop shifts a 0 in at the top.
        for i in range(n):
            above = written[i + 1] if i + 1 < n else Bit(0)
            em.add_assignment(array[i], above.when(head_en).else_(written[i]))
        array.regInit()

        array_at_head = Logic(em, "sq_array_at_head", "w")
        em.add_assignment(array_at_head, array[0])
        head_oh = LogicVec(em, "sq_dep_head_oh", "w", n)
        em.add_assignment(head_oh, Val(1, size=n))
        # The head accounting for a pop this cycle, in the registered frame:
        # slot 1 on a pop, else slot 0.
        head_oh_next = LogicVec(em, "sq_dep_head_oh_next", "w", n)
        if addr_width == 0:
            em.add_assignment(head_oh_next, Val(1, size=1))
        else:
            em.add_assignment((head_oh_next, 0), ~head_en)
            em.add_assignment((head_oh_next, 1), head_en)
            for i in range(2, n):
                em.add_assignment((head_oh_next, i), Bit(0))
        return _DepArray(array, tail_en, tail_oh, full, empty, not_empty,
                         array_at_head, head_oh, head_oh_next, cnt=cnt)

    @staticmethod
    def _first_set_from(em: Emitter, dout, bits, pivot, n: int, name: str) -> None:
        """dout = one-hot of the first set bit of `bits` at or above the
        one-hot `pivot`, or 0 if none: the non-cyclic form of
        CyclicPriorityMasking, bits & ~(bits - pivot)."""
        packed = LogicVec(em, f"{name}_bits", "w", n)
        _bits(em, packed, n, lambda i: Val(bits, i))
        em.add_assignment(dout, packed & ~(packed - pivot))

    def _make_announce_pulses(self, em: Emitter, n_sq, sq, sq_access_en, sq_bb_executed):
        """`pop_announce`: a successor retires and leaves a next entry, whose
        dependent bit is in the registered array. `deferred_announce`: the
        retire drained the queue (or it is empty after reset), so the push
        that repopulates it announces the new head, with the in-flight
        `sq_write_value` as its dependent bit."""
        sq_not_empty = sq.not_empty
        # At least two entries: one is left behind when the head pops.
        not_empty_after_pop = Logic(em, "sq_dep_not_empty_after_pop", "w")
        em.add_assignment(not_empty_after_pop, sq_not_empty & (
            sq.cnt != Val(1, size=math.ceil(math.log2(n_sq)) + 1)))
        send_into_empty = Logic(em, "sq_send_into_empty", "w")
        em.add_assignment(send_into_empty, sq_access_en & sq_not_empty & ~not_empty_after_pop)
        # Sticky: remembers a drain until the next push. Starts at 1, since at
        # reset the first successor BB execution is also a new head from empty.
        awaiting_head = Logic(em, "sq_awaiting_head", "r")
        deferred_announce = Logic(em, "sq_deferred_announce", "w")
        # send_into_empty counts directly, not only through the register: a
        # push in the same cycle as the drain is the new head right now.
        # Waiting for the register would skip its announce, and it would be
        # granted with the conflict check masked off (the missed-announce bug).
        em.add_assignment(deferred_announce, (awaiting_head | send_into_empty) & sq_bb_executed)
        # The announce consumes the pending state, so it wins.
        em.add_assignment(awaiting_head, Bit(0).when(deferred_announce).else_(
            Bit(1).when(send_into_empty).else_(awaiting_head)))
        awaiting_head.regInit(init=1)
        pop_announce = Logic(em, "sq_pop_announce", "w")
        em.add_assignment(pop_announce, sq_access_en & not_empty_after_pop)
        return pop_announce, deferred_announce, sq_not_empty
