"""Dependency checker: holds back a successor queue's accesses until every
predecessor access preceding them in program order has completed or is known
to target a different address.

Every scheme comes in two halves, combined in `generate`:
  - the disparity (`_<scheme>_disparity`): the state tracking how far the
    successor runs ahead of the predecessor, and from it `no_dep_pending`:
    the head successor has no pending predecessor left;
  - the window (`_same_bb_window`, `_cross_bb_window`), built on that state:
    `check_mask`, the physical predecessor slots the head successor's address
    must be compared against, plus scheme-specific extra grant conditions.
The forced-sequential mode (`forcedSequential`) builds the disparity only and
grants the head successor on `no_dep_pending` alone, without comparing
addresses: it waits for every predecessor access preceding it in program order.

Same BB (`same_bb`): the order of the two accesses is static, so a signed
up/down counter, the access disparity AD (+1 per successor access, -1 per
predecessor completion), is the head-relative index of the last predecessor
entry the head successor must check (AD < 0: nothing pending).

Different BBs: the interleaving is dynamic and arrives as BB-execution
handshakes. The scheme is chosen per checker by queue depth
(`cross_bb_scheme`, or the `depScheme` key):
  - runs: boundary bits. The first successor execution after a predecessor
    run marks the most recent predecessor entry (predecessor dep array) and
    stamps the successor entry dependent (successor dep array). One signed
    counter, the run disparity (the access disparity counted in runs), tracks
    how many marks the head successor is behind; its window ends at that
    mark. O(n_pq + n_sq) state.
  - matrix: a dependence matrix, one row of pending predecessor slots per
    queued successor. O(n_pq * n_sq) state, almost no logic; cheaper for
    shallow successor queues.
  - exact (only by `depScheme`, never picked by auto): runs plus one
    register, the window already claimed, so the head's boundary is
    resolved however many runs it is behind.

The dep arrays' pointers are shared per port (Queue._shared_dep_state). The
predecessor array lives in the queue's physical slot frame, because its slots
index the queue's addresses; the successor array lives in its queue's slot
frame too, read at the head (q_grant).

A single-entry queue (NumEntries == 1) has q_addr_width == 0: there is no slot
index, the pointer is the bare generation bit, and the lone slot is always
both head and tail. The `== 0` branches below handle that case.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from functools import reduce

from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import Reduce
from core_gen.ir import BinOp, Val, Bit, CustomStatement
from core_gen.utils import isPow2
from custom_core_gen.configs import DependencyCheckerConfig
from custom_core_gen.generators.generator import Generator


def _bits(em: Emitter, vec, n: int, bit) -> None:
    """vec[i] = bit(i) for every i < n."""
    for i in range(n):
        em.add_assignment((vec, i), bit(i))


def _any(em: Emitter, name: str, bits_name: str, n: int, bit):
    """name = the OR of bit(i) over every i < n, through the vector bits_name."""
    out = Logic(em, name, "w")
    bits = LogicVec(em, bits_name, "w", n)
    _bits(em, bits, n, bit)
    Reduce(em, out, bits, BinOp.OR)
    return out


def _head_read(slots, head_oh):
    """The slot that the one-hot head_oh selects, as an AND-OR."""
    return reduce(lambda a, b: a | b, [s & Val(head_oh, i) for i, s in enumerate(slots)])


@dataclass
class _Disparity:
    no_dep_pending: object
    ad: object = None              # same BB: the access-disparity counter
    # Cross BB: the head successor's window. A scheme tracking it whole gives
    # `window` (matrix: the head row, empty once nothing is pending); the mark
    # schemes give `window_parts`, per-bit functions ORed and masked by
    # no_dep_pending (runs:
    # the fill to the head's boundary; exact: that fill and the window
    # already claimed by an earlier successor of its run).
    window: object = None
    window_parts: list = field(default_factory=list)
    # Scheme-specific grant conditions, ANDed into the grant.
    conditions: list = field(default_factory=list)


class DependencyChecker(Generator):
    # Cross-BB schemes built on the predecessor port's shared marks.
    MARK_SCHEMES = ("runs", "exact")

    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        c = self.configs
        self.ports.clear()
        port = self._add_port
        crosses_bb = c.pq_bb != c.sq_bb
        scheme = self.cross_bb_scheme(c) if crosses_bb else "same_bb"
        if scheme == "exact" and c.forced_sequential:
            # A forced-sequential checker reads no window, only whether the
            # head's run disparity is <= 0, which `runs` already gives
            # exactly: `exact`'s claimed window would be unused.
            scheme = "runs"
        n_pq = c.pq.num_entries
        pq_ptr_width = c.pq.q_addr_width + 1
        sq_ptr_width = c.sq.q_addr_width + 1

        # Predecessor. A single-entry queue has no done index to expose
        # (Queue omits its done_ptr_o the same way).
        pq_addr_i = port(LogicVecArray(em, "pq_addr", "i", n_pq, c.pq.addr_width))
        self._pq_done_i = (port(LogicVec(em, "pq_done", "i", c.pq.q_addr_width))
                           if c.pq.q_addr_width > 0 else None)
        pq_done_en_i = port(Logic(em, "pq_done_en", "i"))
        # Only read by the same-BB scheme; cross-BB tests the end marker instead.
        self._pq_length_i = port(LogicVec(em, "pq_length", "i", pq_ptr_width))
        if crosses_bb:
            # The predecessor's end-of-allocation marker (one-hot of its first
            # unallocated slot) and whether every slot is allocated.
            self._pq_alloc_end_oh_i = port(LogicVec(em, "pq_alloc_end_oh", "i", n_pq))
            self._pq_alloc_full_i = port(Logic(em, "pq_alloc_full", "i"))
            # Dep-array state decoded once per port by the queues
            # (Queue._shared_dep_state): heads are the predecessor's q_done
            # and the successor's q_grant, tails each port's BB-execution
            # count, which advance on exactly this checker's head_en/tail_en.
            n_sq = c.sq.num_entries
            self._shared = SimpleNamespace(
                pq_head_oh=port(LogicVec(em, "pq_dep_head_oh", "i", n_pq)),
                pq_tail_oh=port(LogicVec(em, "pq_dep_tail_oh", "i", n_pq)),
                pq_empty=port(Logic(em, "pq_dep_empty", "i")),
                pq_full=port(Logic(em, "pq_dep_full", "i")),
                sq_head_oh=port(LogicVec(em, "sq_dep_head_oh", "i", n_sq)),
                sq_tail_oh=port(LogicVec(em, "sq_dep_tail_oh", "i", n_sq)),
                sq_empty=port(Logic(em, "sq_dep_empty", "i")),
                sq_full=port(Logic(em, "sq_dep_full", "i")),
            )
            if scheme == "matrix":
                # The predecessor port's shared live-slot bits.
                self._shared.live = port(LogicVec(em, "pq_live", "i", n_pq))
            if scheme in self.MARK_SCHEMES:
                # The predecessor port's shared marks towards this successor's
                # BB (Queue._mark_unit).
                self._shared.marks = port(LogicVec(em, "pq_marks", "i", n_pq))
                self._shared.mark_head = port(Logic(em, "pq_mark_head", "i"))
                self._shared.mark_sval = port(Logic(em, "sq_mark_value", "i"))
            self._pq_pending_done_i = None
        else:
            # Retired, not yet completed predecessor accesses (`_ad_counter`).
            self._shared = None
            self._pq_pending_done_i = port(LogicVec(em, "pq_pending_done", "i", pq_ptr_width))

        # Successor and outputs.
        sq_head = port(LogicVec(em, "sq_head", "i", c.sq.addr_width))
        sq_grant_en_i = port(Logic(em, "sq_grant_en", "i"))
        allow_sq_access_o = port(Logic(em, "allow_sq_access", "o"))
        allow_pq_access_o = port(Logic(em, "allow_pq_access", "o"))

        em.begin_region('dc_order')
        self._dep_empty = None  # (pq, sq), set by `_bb_handshakes` (cross-BB only)
        disparity = getattr(self, f"_{scheme}_disparity")(em, pq_done_en_i, sq_grant_en_i)
        no_dep_pending = disparity.no_dep_pending
        allow_pq, extra_sq = self._bb_execution_gates()

        if c.forced_sequential:
            # The address ports (pq_addr, sq_head, pq_length, the allocation
            # marker) stay, unused, so the structure wires every checker alike.
            em.add_comment(
                "Forced sequential: allow the successor access only once every\n"
                "\tpredecessor access preceding it in program order has completed\n"
                "\t(no address comparison)\n"
            )
            grant = extra_sq + [no_dep_pending]
        else:
            window = self._same_bb_window if scheme == "same_bb" else self._cross_bb_window
            check_mask, conditions = window(em, disparity)

            # A conflict: a pending (masked) predecessor entry with the head
            # successor's address.
            em.begin_region('dc_compare')
            conflict = _any(em, "conflict", "conflicts", n_pq, lambda i: Val(check_mask, i).when(
                Val(pq_addr_i, i) == sq_head).else_(Bit(0)))

            em.add_comment(
                "Allow the successor access if:\n"
                "\t- the scheme-specific conditions hold (the head successor's\n"
                "\t  dependency window is allocated in the predecessor queue)\n"
                "\t- AND its address conflicts with no pending predecessor entry\n"
                "\t  (trivially true when nothing is pending anymore)\n"
            )
            grant = conditions + extra_sq + [~conflict | no_dep_pending]
        em.add_assignment(allow_pq_access_o, allow_pq)
        em.add_assignment(allow_sq_access_o, reduce(lambda a, b: a & b, grant))
        self._write_to_file(em, path_rtl, out_file)

    @staticmethod
    def cross_bb_scheme(c: DependencyCheckerConfig) -> str:
        """The cross-BB scheme of a checker: `depScheme` if set, else the
        cheaper one for its queue depths. Fitted on standalone P&R of one
        checker over n_pq x n_sq in {1,2,3,4,6,8,12,16}^2 (cost LUT + FF/2):
        the matrix's n_sq x n_pq state wins while the run counter and window
        fill are the larger fixed cost, i.e. iff (n_pq - 1)(n_sq - 3) <= 4.
        This picks the cheaper scheme at all 64 measured points."""
        if c.dep_scheme != "auto":
            return c.dep_scheme
        n_pq, n_sq = c.pq.num_entries, c.sq.num_entries
        return "matrix" if (n_pq - 1) * (n_sq - 3) <= 4 else "runs"

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
        if self._dep_empty is None:
            return self._pq_underflow_gate, []
        pq_empty, sq_empty = self._dep_empty
        return ~pq_empty, [~sq_empty]

    @staticmethod
    def _count(em: Emitter, counter, delta_name: str, up, down) -> None:
        """counter += up - down. One adder; the +1 / 0 / -1 delta is built
        bitwise (two's complement) because the Verilog emitter cannot nest
        signed literals in a when-else."""
        delta = LogicVec(em, delta_name, "w", counter.size)
        em.add_assignment((delta, 0), up ^ down)
        for b in range(1, counter.size):
            em.add_assignment((delta, b), down & ~up)
        em.add_assignment(counter, counter + delta)

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

    def _same_bb_disparity(self, em: Emitter, pq_done_en_i, sq_grant_en_i) -> _Disparity:
        """AD < 0: every predecessor access before the head successor is done.
        In forced-sequential mode the grant keeps AD <= 0, so neither the
        window's allocation gate nor its overflow cap is needed."""
        ad = self._ad_counter(em, self.configs.access_disparity_width,
                              sq_grant_en_i, pq_done_en_i)
        no_dep_pending = Logic(em, "no_dep_pending", "w")
        em.add_assignment(no_dep_pending, ad < Val(0, size=self.configs.access_disparity_width))
        return _Disparity(no_dep_pending, ad=ad)

    def _same_bb_window(self, em: Emitter, disparity: _Disparity):
        """The window is the predecessor slots at head-relative index 0..AD."""
        n_pq = self.configs.pq.num_entries
        addr_width = self.configs.pq.q_addr_width
        ad_width = self.configs.access_disparity_width
        ad = disparity.ad
        pq_done_i = self._pq_done_i

        # Wide enough to hold AD in full and n_pq as a POSITIVE signed number,
        # so AD, slot indices and the queue length compare without wrapping.
        cmp_width = max(ad_width, addr_width + 2)
        ad_cmp = self._sign_extend(em, "ad_cmp", ad, cmp_width)
        # pq_length is non-negative: zero-extension keeps its value.
        pq_length_cmp = LogicVec(em, "pq_length_cmp", "w", cmp_width, is_signed=True)
        em.add_assignment(pq_length_cmp,
                          Val(0, cmp_width - addr_width - 1).concat(self._pq_length_i))

        # check_mask[j] = (j - pq_done) mod n_pq <= AD mod 2^addr_width
        # Computed per slot from pq_done and AD's low bits (a LUT or two per
        # bit) rather than as a thermometer of AD rotated by pq_done.
        # The mask only matters while the grant can fire: AD < 0 grants on
        # no_dep_pending regardless of conflicts, and AD >= n_pq fails
        # `allocated` (pq_length <= n_pq). In between, AD's low bits are AD.
        check_mask = LogicVec(em, "check_mask", "w", n_pq)
        if addr_width == 0:
            # Single entry: the lone slot is always head-relative index 0.
            em.add_assignment((check_mask, 0), Bit(1))
        else:
            ad_slice = em.slice_var(ad_cmp.getNameRead(), addr_width - 1, 0)
            # ad_cmp is `signed`; VHDL needs the slice converted explicitly.
            ad_low = (f"std_logic_vector({ad_slice})"
                      if em.get_file_suffix() == "vhd" else ad_slice)
            ad_idx = LogicVec(em, "ad_idx", "w", addr_width)
            em.add_assignment(ad_idx, Val(ad_low))
            # Head-relative index of each slot. For a power of two the
            # subtraction wraps exactly; otherwise add n_pq back when pq_done
            # lies above j.
            rel_idx = LogicVecArray(em, "rel_idx", "w", n_pq, addr_width)
            if not isPow2(n_pq):
                # One more bit holds n_pq + j < 2 n_pq; the result (< n_pq)
                # fits back into addr_width bits.
                pq_done_ext = LogicVec(em, "pq_done_ext", "w", addr_width + 1)
                em.add_assignment(pq_done_ext, Bit(0).concat(pq_done_i))
            for j in range(n_pq):
                if isPow2(n_pq):
                    em.add_assignment(rel_idx[j], Val(j, size=addr_width) - pq_done_i)
                else:
                    wide = LogicVec(em, f"rel_idx_wide_{j}", "w", addr_width + 1)
                    em.add_assignment(wide, (Val(j) - pq_done_ext).when(
                        pq_done_ext <= Val(j, size=addr_width + 1)).else_(
                            Val(n_pq + j) - pq_done_ext))
                    em.add_assignment(rel_idx[j], Val(
                        em.slice_var(wide.getNameRead(), addr_width - 1, 0)))
                em.add_assignment((check_mask, j), rel_idx[j] <= ad_idx)

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
        return check_mask, conditions

    def _ad_counter(self, em: Emitter, ad_width, sq_grant_en, pq_done_en):
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
        self._count(em, ad, "ad_delta", sq_grant_en, pq_done_en)

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
    # Cross-BB schemes
    # ===--------------------------------------------------------------------===

    def _matrix_disparity(self, em: Emitter, pq_done_en, sq_grant_en) -> _Disparity:
        """A classic dependence matrix, one row per queued successor.

        `pq_live[p]` (shared per predecessor port, Queue._shared_dep_state):
        predecessor slot p is BB-executed and not yet completed. A successor
        BB execution copies pq_live into its row: exactly the predecessor
        accesses preceding it in program order that are still pending. A
        predecessor completion clears its column in every row. The rows sit
        in the successor queue's slot frame; the head row is read at q_grant.
        No predecessor pending: the head row is zero."""
        n_pq = self.configs.pq.num_entries
        n_sq = self.configs.sq.num_entries
        shared = self._bb_handshakes(em)
        em.begin_region('dc_live')
        # Column clear: the predecessor completing this cycle.
        clear_col = LogicVec(em, "pq_clear_col", "w", n_pq)
        _bits(em, clear_col, n_pq, lambda p: pq_done_en & Val(shared.pq_head_oh, p))
        # The row a successor BB execution writes (the two BBs never execute
        # in the same cycle, so no same-cycle predecessor push to include).
        new_row = LogicVec(em, "dep_new_row", "w", n_pq)
        _bits(em, new_row, n_pq, lambda p: Val(shared.live, p) & ~Val(clear_col, p))

        em.begin_region('dc_rows')
        rows = [LogicArray(em, f"dep_row_{i}", "r", n_pq) for i in range(n_sq)]
        written = [LogicArray(em, f"dep_row_written_{i}", "w", n_pq) for i in range(n_sq)]
        for i in range(n_sq):
            for p in range(n_pq):
                em.add_assignment(written[i][p], Val(new_row, p).when(
                    Val(shared.sq_tail_oh, i) & shared.sq_tail_en).else_(
                        rows[i][p] & ~Val(clear_col, p)))
        for i in range(n_sq):
            for p in range(n_pq):
                em.add_assignment(rows[i][p], written[i][p])
            rows[i].regInit()

        em.begin_region('dc_headrow')
        head_row = LogicVec(em, "dep_head_row", "w", n_pq)
        _bits(em, head_row, n_pq, lambda p: _head_read(
            [rows[i][p] for i in range(n_sq)], shared.sq_head_oh))
        dep_pending = Logic(em, "dep_pending", "w")
        Reduce(em, dep_pending, head_row, BinOp.OR)
        no_dep_pending = Logic(em, "no_dep_pending", "w")
        em.add_assignment(no_dep_pending, ~dep_pending)
        return _Disparity(no_dep_pending, window=head_row)

    def _runs_disparity(self, em: Emitter, pq_done_en, sq_grant_en) -> _Disparity:
        """The marks and dependent bits of the mark scheme, tracked by one
        signed counter instead of a boundary register.

        A mark lands exactly when a dependent successor is stamped, both in
        program order, and predecessors complete in order: the k-th marked
        completion satisfies the k-th dependent run. So the run disparity
          dependent successors retired - marked predecessors completed
        plus the head's own dependent bit is how many runs the head successor
        is behind, its head disparity:
          <= 0: every predecessor before it has completed;
          == 1: its boundary is the first live mark from the predecessor
                head, so the window is [head, first mark];
          == k: earlier runs were granted early and their marks are still
                pending; the window ends at the k-th mark. Only the first
                `run_lookahead` marks are resolved; beyond that the head
                waits for completions, which is always safe (those accesses
                precede it in program order).
        No announce, no boundary register, no clear walk: the dependent
        bits are written once and only read at the head."""
        n_pq = self.configs.pq.num_entries
        lookahead = self.configs.run_lookahead
        shared = self._bb_handshakes(em)
        _, head_disparity_le = self._run_disparity(em, shared, pq_done_en, sq_grant_en)
        no_dep_pending = head_disparity_le(0)
        resolved = head_disparity_le(lookahead)

        em.begin_region('dc_fill')
        # Window per lookahead step: [head, i-th mark].
        fills, rest = [], shared.marks
        for i in range(1, lookahead + 1):
            fill = LogicVec(em, f"run_window_{i}", "w", n_pq)
            self._fill_to_first_set(em, fill, rest, shared.pq_head_oh, n_pq, f"run_window_{i}")
            fills.append(fill)
            if i < lookahead:
                beyond = LogicVec(em, f"run_marks_beyond_{i}", "w", n_pq)
                em.add_assignment(beyond, rest & ~fill)
                rest = beyond
        window = fills[0]
        for i in range(2, lookahead + 1):
            sel = LogicVec(em, f"run_window_sel_{i}", "w", n_pq)
            em.add_assignment(sel, window.when(head_disparity_le(i - 1)).else_(fills[i - 1]))
            window = sel
        return _Disparity(no_dep_pending, window_parts=[lambda i: Val(window, i)],
                          conditions=[resolved])

    def _exact_disparity(self, em: Emitter, pq_done_en, sq_grant_en) -> _Disparity:
        """`runs` without its lookahead limit, by one register per checker:
        the claimed window (n_pq bits, predecessor slot frame).

        When a dependent successor retires with a head disparity >= 1, its
        window [predecessor head, boundary] is latched into the claimed
        window; predecessor completions clear it slot by slot. Successors
        retire in order, so the claimed window always runs from the
        predecessor head to the boundary of the most recent dependent run
        that left, and every mark inside it is dead (its run has left).
        Hence, while the head disparity is >= 1:
          a dependent head: its boundary is the first mark from the
              predecessor head outside the claimed window (the oldest
              unretired dependent run's), so its window is the fill up to
              that mark, however many runs it is behind;
          a non-dependent head: it continues the run that claimed the window
              (no dependent successor retired since), so its window is the
              claimed one.
        The fill reads registered state only, as in `runs`, and the only
        event that moves the claimed window is a dependent successor
        retiring: no announce of a new head is needed."""
        n_pq = self.configs.pq.num_entries
        shared = self._bb_handshakes(em)
        head_dependent, head_disparity_le = self._run_disparity(
            em, shared, pq_done_en, sq_grant_en)
        no_dep_pending = head_disparity_le(0)

        em.begin_region('dc_claim')
        claim_window = LogicArray(em, "claim_window", "r", n_pq)
        em.begin_region('dc_fill')
        live_marks = LogicVec(em, "exact_marks", "w", n_pq)
        _bits(em, live_marks, n_pq, lambda i: Val(shared.marks, i) & ~claim_window[i])
        fill = LogicVec(em, "exact_fill", "w", n_pq)
        self._fill_to_first_set(em, fill, live_marks, shared.pq_head_oh, n_pq, "exact_fill")
        # The claimed window covers a head that continues its run; a head
        # starting a new run (dependent) adds the fill, which extends it.
        unclaimed = LogicVec(em, "exact_unclaimed", "w", n_pq)
        _bits(em, unclaimed, n_pq, lambda i: Val(fill, i) & head_dependent)

        em.begin_region('dc_claim')
        take = Logic(em, "claim_take", "w")
        em.add_assignment(take, sq_grant_en & head_dependent & ~no_dep_pending)
        for i in range(n_pq):
            # (Separate wires: Vivado rejects a when-else nested in an
            # expression, a VHDL-2019 construct.)
            kept = Logic(em, f"claim_kept_{i}", "w")
            em.add_assignment(kept, Val(fill, i).when(take).else_(claim_window[i]))
            em.add_assignment(claim_window[i],
                              kept & ~(pq_done_en & Val(shared.pq_head_oh, i)))
        claim_window.regInit()
        return _Disparity(no_dep_pending, window_parts=[
            lambda i: Val(unclaimed, i), lambda i: claim_window[i]])

    def _run_disparity(self, em: Emitter, shared, pq_done_en, sq_grant_en):
        """The successor's dependent bits and the run disparity, shared by
        `runs` and `exact`. Returns the head successor's dependent bit and
        head_disparity_le(k): whether its head disparity (the run disparity
        plus that bit) is at most k."""
        n_pq, n_sq = self.configs.pq.num_entries, self.configs.sq.num_entries
        head_dependent = self._succ_bits(em, n_sq, shared)

        em.begin_region('dc_counter')
        # In [-n_sq, n_pq]: a satisfied run waits in the successor queue, a
        # run that left early holds a live mark.
        width = max(n_sq, n_pq + 1).bit_length() + 1
        run_disparity = LogicVec(em, "run_disparity", "r", width, is_signed=True)
        run_disparity.regInit(init=0)
        succ_retire = Logic(em, "run_succ_retire", "w")
        em.add_assignment(succ_retire, sq_grant_en & head_dependent)
        pred_retire = Logic(em, "run_pred_retire", "w")
        em.add_assignment(pred_retire, pq_done_en & shared.mark_head)
        self._count(em, run_disparity, "run_delta", succ_retire, pred_retire)
        # Compared without an adder: run disparity + bit <= k  <=>
        # run disparity <= k - bit.
        run_disparity_le = {}

        def le(k):
            # (Comparisons get their own wires: the Verilog emitter cannot
            # nest signed literals in a when-else.)
            if k not in run_disparity_le:
                run_disparity_le[k] = Logic(
                    em, f"run_disparity_le_{k}".replace("-", "m"), "w")
                em.add_assignment(run_disparity_le[k], run_disparity <= Val(k, size=width))
            return run_disparity_le[k]

        def head_disparity_le(k):
            out = Logic(em, f"head_disparity_le_{k}", "w")
            em.add_assignment(out, le(k - 1).when(head_dependent).else_(le(k)))
            return out
        return head_dependent, head_disparity_le

    def _cross_bb_window(self, em: Emitter, disparity: _Disparity):
        """[predecessor head, boundary] while a dependence is pending. It is
        a contiguous run from the predecessor head (completions leave in
        order), so it is unallocated exactly when the end-of-allocation
        marker lies in it, unless the queue is full."""
        em.begin_region('dc_window')
        n_pq = self.configs.pq.num_entries
        check_mask = disparity.window
        if check_mask is None:
            check_mask = LogicVec(em, "check_mask", "w", n_pq)
            _bits(em, check_mask, n_pq, lambda i: reduce(
                lambda a, b: a | b, [part(i) for part in disparity.window_parts])
                & ~disparity.no_dep_pending)
        end_in_window = _any(em, "ad_end_in_window", "ad_end_in_window_bits", n_pq,
                             lambda i: Val(check_mask, i) & Val(self._pq_alloc_end_oh_i, i))
        allocated = Logic(em, "corresponding_entry_allocated", "w")
        em.add_assignment(allocated, self._pq_alloc_full_i | ~end_in_window)
        return check_mask, [allocated] + disparity.conditions

    def _bb_handshakes(self, em: Emitter):
        """The shared per-port dep state plus this checker's BB-execution
        handshakes: a BB may execute only while every dep array on it has
        room, so each side is ready while its array is not full."""
        shared = self._shared
        em.begin_region('dc_ptrs')
        shared.sq_tail_en = self._make_bb_ports(em, "sq", ~shared.sq_full)
        shared.pq_tail_en = self._make_bb_ports(em, "pq", ~shared.pq_full)
        self._dep_empty = (shared.pq_empty, shared.sq_empty)
        return shared

    def _make_bb_ports(self, em: Emitter, prefix: str, ready):
        valid_i = self._add_port(Logic(em, f"{prefix}_bb_valid", "i"))
        ready_o = self._add_port(Logic(em, f"{prefix}_bb_ready", "o"))
        executed = Logic(em, f"{prefix}_bb_executed", "w")
        em.add_assignment(ready_o, ready)
        em.add_assignment(executed, valid_i & ready)
        return executed

    def _succ_bits(self, em: Emitter, n: int, shared):
        """The successor's dependent (run-start) bits, in the successor
        queue's slot frame: a BB execution writes the shared mark unit's
        value at the tail slot, overwriting whatever a retired entry left
        there. Returns the head's bit."""
        em.begin_region('dc_sbits')
        array = LogicArray(em, "sq_array", "r", n)
        written = LogicArray(em, "sq_array_written", "w", n)
        for i in range(n):
            em.add_assignment(written[i], shared.mark_sval.when(
                Val(shared.sq_tail_oh, i) & shared.sq_tail_en).else_(array[i]))
        for i in range(n):
            em.add_assignment(array[i], written[i])
        array.regInit()
        head = Logic(em, "sq_array_head", "w")
        em.add_assignment(head, _head_read([array[i] for i in range(n)], shared.sq_head_oh))
        return head

    @staticmethod
    def _fill_to_first_set(em: Emitter, dout, bits, pivot, n: int, name: str) -> None:
        """dout = every slot cyclically from the one-hot `pivot` up to and
        including the first set bit of `bits` (all ones if none), with ONE
        doubled subtract: x ^ (x - pivot) flips exactly the bits from the
        pivot up to the first set bit. A wrap borrows into the upper copy."""
        if n == 1:
            em.add_assignment(dout, Val(1, size=1))
            return
        dbl = LogicVec(em, f"{name}_dbl", "w", 2 * n)
        piv = LogicVec(em, f"{name}_piv", "w", 2 * n)
        for i in range(2 * n):
            em.add_assignment((dbl, i), Val(bits, i % n))
            em.add_assignment((piv, i), Val(pivot, i) if i < n else Bit(0))
        diff = LogicVec(em, f"{name}_diff", "w", 2 * n)
        em.add_assignment(diff, dbl ^ (dbl - piv))
        _bits(em, dout, n, lambda i: Val(diff, i) | Val(diff, i + n))
