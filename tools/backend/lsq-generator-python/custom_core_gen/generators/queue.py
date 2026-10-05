from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import *
from core_gen.configs import Configs
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, reduce_bin
from custom_core_gen.generators.generator import Generator
from custom_core_gen.generators.ptr_utils import ptr_next, ptr_index, ptr_diff, count_slot
from core_gen.utils import isPow2


class Queue(Generator):
    def __init__(self, name: str, suffix: str, configs: Configs):
        super().__init__(name, suffix, configs)

    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        self.ports.clear()
        is_store = self.configs.q_type == "store"

        # IOs - common
        empty_o = self._add_port(Logic(em, "empty", "o"))
        circ_addr_i = self._add_port(
            LogicVec(em, "circ_addr", "i", self.configs.addr_width)
        )
        circ_addr_valid_i = self._add_port(Logic(em, "circ_addr_valid", "i"))
        circ_addr_ready_o = self._add_port(Logic(em, "circ_addr_ready", "o"))

        mem_addr_valid_o = self._add_port(Logic(em, "mem_addr_valid", "o"))
        mem_addr_ready_i = self._add_port(Logic(em, "mem_addr_ready", "i"))
        mem_addr_o = self._add_port(
            LogicVec(em, "mem_addr", "o", self.configs.addr_width)
        )

        # IOs - type-specific
        if is_store:
            circ_data_i = self._add_port(
                LogicVec(em, "circ_data", "i", self.configs.data_width)
            )
            circ_data_valid_i = self._add_port(Logic(em, "circ_data_valid", "i"))
            circ_data_ready_o = self._add_port(Logic(em, "circ_data_ready", "o"))
            mem_data_o = self._add_port(
                LogicVec(em, "mem_data", "o", self.configs.data_width)
            )
            mem_data_valid_o = self._add_port(Logic(em, "mem_data_valid", "o"))
            mem_data_ready_i = self._add_port(Logic(em, "mem_data_ready", "i"))
            mem_exec_valid_i = self._add_port(Logic(em, "mem_exec_valid", "i"))
            mem_exec_ready_o = self._add_port(Logic(em, "mem_exec_ready", "o"))
            if self.configs.st_resp:
                circ_exec_valid_o = self._add_port(Logic(em, "circ_exec_valid", "o"))
                circ_exec_ready_i = self._add_port(Logic(em, "circ_exec_ready", "i"))
        else:
            mem_data_valid_i = self._add_port(Logic(em, "mem_data_valid", "i"))
            mem_data_ready_o = self._add_port(Logic(em, "mem_data_ready", "o"))
            mem_data_i = self._add_port(
                LogicVec(em, "mem_data", "i", self.configs.data_width)
            )
            circ_data_o = self._add_port(
                LogicVec(em, "circ_data", "o", self.configs.data_width)
            )
            circ_data_valid_o = self._add_port(Logic(em, "circ_data_valid", "o"))
            circ_data_ready_i = self._add_port(Logic(em, "circ_data_ready", "i"))

        allow_access_i = self._add_port(Logic(em, "allow_access", "i"))

        # Shared pointer infrastructure
        em.begin_region('q_ctrl')
        (
            q_done,
            q_issue,
            q_tail,
            q_head,
            q_tail_oh,
            done_en,
            issue_en,
            head_en,
            alloc_en,
            q_full,
            q_empty,
            q_full_w_issue,
            q_issue_sel,
        ) = self._setup_pointers(em)

        # Per-entry write enable: the allocated entry is the tail. Kept as its own
        # signal (in queue control) so the address registers below hold only the
        # addresses.
        q_addr_wen = LogicVec(em, "q_addr_wen", "w", self.configs.num_entries)
        for i in range(self.configs.num_entries):
            em.add_assignment((q_addr_wen, i), Val(q_tail_oh, i) & alloc_en)

        # Address buffer (both types)
        em.begin_region('q_addr')
        q_addr = LogicVecArray(
            em, "q_addr", "r", self.configs.num_entries, self.configs.addr_width
        )
        for i in range(self.configs.num_entries):
            em.add_assignment(
                q_addr[i],
                circ_addr_i.when(Val(q_addr_wen, i)).else_(q_addr[i]),
            )
        q_addr.regInit()

        em.begin_region('q_ctrl')
        em.add_assignment(empty_o, q_empty)

        # Bypass is an optional fast path that lets an incoming address skip the
        # queue when it is empty. When disabled the queue always routes through
        # its buffer.
        if self.configs.bypass:
            # bypass_addr_valid: queue is empty with no pending issues and a valid address.
            # Deliberately excludes allow_access_i to break the combinatorial loop that
            # would form through queue_head_o → dep-checker → allow_access_i → bypass_active.
            bypass_addr_valid = Logic(em, "bypass_addr_valid", "w")
            em.add_assignment(
                bypass_addr_valid,
                q_empty & (q_issue == q_head) & circ_addr_valid_i,
            )

            # bypass_active additionally requires allow_access_i; used for all control paths.
            bypass_active = Logic(em, "bypass_active", "w")
            em.add_assignment(bypass_active, bypass_addr_valid & allow_access_i)
        else:
            bypass_addr_valid = None
            bypass_active = None

        # Allocation
        can_alloc = Logic(em, "can_alloc", "w")
        em.add_assignment(can_alloc, ~q_full)
        em.add_assignment(circ_addr_ready_o, can_alloc)
        em.add_assignment(alloc_en, circ_addr_valid_i & can_alloc)

        # Retirement: also fires when bypass completes (memory accepts)
        head_retire = ~q_empty & allow_access_i
        if self.configs.bypass:
            head_retire = head_retire | (bypass_active & mem_addr_ready_i)
        em.add_assignment(head_en, head_retire)

        # Issue
        em.begin_region('q_issue')
        can_issue = Logic(em, "can_issue", "w")

        em.add_assignment(
            can_issue,
            ((q_issue == q_head) & head_en) | (q_issue != q_head) | q_full_w_issue,
        )
        issue_req = can_issue | bypass_active if self.configs.bypass else can_issue
        em.add_assignment(issue_en, issue_req & mem_addr_ready_i)

        em.add_assignment(mem_addr_valid_o, issue_req)

        # In bypass mode route circ_addr_i directly; otherwise use the queue buffer
        em.begin_region('q_issue')
        mem_addr_from_queue = LogicVec(
            em, "mem_addr_from_queue", "w", self.configs.addr_width
        )
        if q_issue_sel is None:
            # Single-entry queue: the only entry is always the one to issue.
            em.add_assignment(mem_addr_from_queue, q_addr[0])
        else:
            MuxLookUp(em, mem_addr_from_queue, q_addr, q_issue_sel)
        if self.configs.bypass:
            em.add_assignment(
                mem_addr_o, circ_addr_i.when(bypass_active).else_(mem_addr_from_queue)
            )
        else:
            em.add_assignment(mem_addr_o, mem_addr_from_queue)

        if is_store:
            em.add_assignment(mem_data_o, circ_data_i)
            em.add_assignment(mem_data_valid_o, circ_data_valid_i)
            em.add_assignment(circ_data_ready_o, mem_data_ready_i)

            if self.configs.st_resp:
                em.add_assignment(circ_exec_valid_o, mem_exec_valid_i)
                em.add_assignment(mem_exec_ready_o, circ_exec_ready_i)
                em.add_assignment(done_en, mem_exec_valid_i & circ_exec_ready_i)
            else:
                em.add_assignment(mem_exec_ready_o, Bit(1))
                em.add_assignment(done_en, mem_exec_valid_i)
        else:
            em.add_assignment(mem_data_ready_o, circ_data_ready_i)
            em.add_assignment(circ_data_o, mem_data_i)
            em.add_assignment(circ_data_valid_o, mem_data_valid_i)
            em.add_assignment(done_en, mem_data_valid_i & circ_data_ready_i)

        em.begin_region('q_ctrl')
        self._generate_observable_ports(
            em,
            q_addr,
            q_done,
            q_tail,
            q_head,
            done_en,
            alloc_en,
            head_en,
            bypass_addr_valid,
            circ_addr_i,
            alloc_end_oh=q_tail_oh,
            alloc_full=q_full,
        )
        if self.configs.shares_dep_state:
            self._shared_dep_state(em, q_done, q_head, done_en)

        self._write_to_file(em, path_rtl, out_file)

    def _shared_dep_state(self, em: Emitter, q_done, q_head, done_en) -> None:
        """Dep-array state that every cross-BB dependency checker of this
        port would otherwise keep a private copy of, computed once here.

        A checker's predecessor dep array pops on this queue's completions
        and its successor dep array on this queue's retirements, both from 0:
        their heads ARE q_done and q_head. Both arrays push once per BB
        execution of this port, atomically across every checker on the BB
        (structure._route_bb_ports), so all their tails are one BB-execution
        counter, kept here and advanced by bb_exec_i. The decodes of these
        pointers (one-hots, empty/full) and, per successor BB, the boundary
        marks (`_mark_unit`) depend on nothing else, so they are built here
        once and exported to every checker of the port.
        """
        em.begin_region('dc_ptrs')
        c = self.configs
        n, aw = c.num_entries, c.q_addr_width
        ptr_width = aw + 1
        bb_exec_i = self._add_port(Logic(em, "bb_exec", "i"))
        bb_tail = LogicVec(em, "q_bb_tail", "r", ptr_width)
        bb_tail_next = LogicVec(em, "q_bb_tail_next", "w", ptr_width)
        ptr_next(em, bb_tail_next, bb_tail, n)
        em.add_assignment(bb_tail, bb_tail_next)
        bb_tail.regInit(init=0, enable=bb_exec_i)

        def one_hot(name, ptr):
            oh = LogicVec(em, name, "w", n)
            if aw == 0:
                em.add_assignment(oh, Val(1, size=1))
            else:
                idx = LogicVec(em, f"{name}_idx", "w", aw)
                ptr_index(em, idx, ptr, n, aw)
                BitsToOH(em, oh, idx)
            return oh

        def out(name, sig):
            o = self._add_port(type(sig)(em, name, "o", sig.size) if isinstance(sig, LogicVec)
                               else Logic(em, name, "o"))
            em.add_assignment(o, sig)

        if c.is_pred:
            # Predecessor role: head = q_done, tail = BB executions.
            head_oh = one_hot("dep_p_head_oh", q_done)
            tail_oh = one_hot("dep_p_tail_oh", bb_tail)
            empty = Logic(em, "dep_p_empty", "w")
            em.add_assignment(empty, q_done == bb_tail)
            full = Logic(em, "dep_p_full", "w")
            tail_msb = Val(em.index_var(bb_tail.getNameRead(), aw))
            head_msb = Val(em.index_var(q_done.getNameRead(), aw))
            if aw == 0:
                em.add_assignment(full, tail_msb != head_msb)
            else:
                tail_low = Val(em.slice_var(bb_tail.getNameRead(), aw - 1, 0))
                head_low = Val(em.slice_var(q_done.getNameRead(), aw - 1, 0))
                em.add_assignment(full, (tail_msb != head_msb) & (tail_low == head_low))
            for name, sig in (("dep_p_head_oh_o", head_oh), ("dep_p_tail_oh_o", tail_oh),
                              ("dep_p_empty_o", empty), ("dep_p_full_o", full)):
                out(name.removesuffix("_o"), sig)
            if c.needs_live:
                # BB-executed, not yet completed slots: what a successor BB
                # execution copies into a dependence-matrix row.
                em.begin_region('dc_live')
                live = LogicArray(em, "dep_p_live", "r", n)
                for i in range(n):
                    em.add_assignment(live[i], (live[i] | (bb_exec_i & Val(tail_oh, i)))
                                      & ~(done_en & Val(head_oh, i)))
                live.regInit()
                live_o = self._add_port(LogicVec(em, "dep_p_live", "o", n))
                for i in range(n):
                    em.add_assignment((live_o, i), live[i])
            for bb in c.mark_bbs:
                self._mark_unit(em, bb, bb_exec_i, q_done, bb_tail, head_oh, tail_oh,
                                empty, done_en)
        if c.is_succ:
            # Successor role: occupancy = BB executions - q_head.
            em.begin_region('dc_ptrs')
            cnt = LogicVec(em, "dep_s_cnt", "w", ptr_width)
            ptr_diff(em, cnt, bb_tail, q_head, n)
            empty = Logic(em, "dep_s_empty", "w")
            em.add_assignment(empty, cnt == Val(0, size=ptr_width))
            full = Logic(em, "dep_s_full", "w")
            em.add_assignment(full, cnt == Val(n, size=ptr_width))
            # Slot cnt mod n, for a head-anchored (shifting) successor array.
            tail_oh = LogicVec(em, "dep_s_tail_oh", "w", n)
            if aw == 0:
                em.add_assignment(tail_oh, Val(1, size=1))
            else:
                idx = LogicVec(em, "dep_s_tail_idx", "w", aw)
                count_slot(em, idx, cnt, n, aw)
                BitsToOH(em, tail_oh, idx)
            for name, sig in (("dep_s_tail_oh", tail_oh), ("dep_s_empty", empty),
                              ("dep_s_full", full)):
                out(name, sig)

    def _mark_unit(self, em: Emitter, bb, bb_exec_i, q_done, bb_tail, head_oh, tail_oh,
                   empty, done_en) -> None:
        """Boundary marks of this predecessor port towards successor BB `bb`,
        in the queue's physical slot frame: the first execution of `bb` after
        a run of this port's BB marks the most recent entry (tail-1). Shared
        by every run-scheme checker from this port into `bb`.

        Exports the marks, whether the head entry is marked, and the dependent
        bit a successor execution of `bb` stamps this cycle: 1 iff this port's
        BB ran since the last execution of `bb` and the mark could land. It
        cannot land if the array is empty (tail-1 would wrap behind the head)
        or its lone entry completes this same cycle (the mark would plant a 1
        in the slot the head leaves); the successor is then independent."""
        em.begin_region('dc_marks')
        n, aw = self.configs.num_entries, self.configs.q_addr_width
        s_exec = self._add_port(Logic(em, f"succ_bb_exec_bb{bb}", "i"))
        p_last = Logic(em, f"mk{bb}_pred_executed_last", "r")
        s_last = Logic(em, f"mk{bb}_succ_executed_last", "r")
        # Assumes the two BBs never execute in the same cycle.
        em.add_assignment(p_last, ~s_exec & (bb_exec_i | p_last))
        em.add_assignment(s_last, ~bb_exec_i & (s_exec | s_last))
        # At reset no predecessor has run: a successor that goes first is
        # independent (and does not count as "first after a predecessor").
        p_last.regInit(init=0)
        s_last.regInit(init=1)
        first_s = Logic(em, f"mk{bb}_first_succ", "w")
        em.add_assignment(first_s, ~s_last & s_exec)
        head_next = LogicVec(em, f"mk{bb}_head_next", "w", aw + 1)
        ptr_next(em, head_next, q_done, n)
        retiring_now = Logic(em, f"mk{bb}_retiring_now", "w")
        em.add_assignment(retiring_now, done_en & ~empty & (head_next == bb_tail))
        mark_valid = Logic(em, f"mk{bb}_mark_valid", "w")
        em.add_assignment(mark_valid, first_s & ~empty & ~retiring_now)
        mark_consumed = Logic(em, f"mk{bb}_mark_consumed", "w")
        em.add_assignment(mark_consumed, first_s & (empty | retiring_now))
        marks = LogicArray(em, f"mk{bb}_marks", "r", n)
        for i in range(n):
            em.add_assignment(marks[i], Bit(0).when(Val(tail_oh, i) & bb_exec_i).else_(
                Bit(1).when(Val(tail_oh, (i + 1) % n) & mark_valid).else_(marks[i])))
        marks.regInit()
        marks_o = self._add_port(LogicVec(em, f"marks_bb{bb}", "o", n))
        for i in range(n):
            em.add_assignment((marks_o, i), marks[i])
        at_head = LogicVec(em, f"mk{bb}_at_head_bits", "w", n)
        for i in range(n):
            em.add_assignment((at_head, i), marks[i] & Val(head_oh, i))
        mark_head_o = self._add_port(Logic(em, f"mark_head_bb{bb}", "o"))
        Reduce(em, mark_head_o, at_head, BinOp.OR)
        sval_o = self._add_port(Logic(em, f"mark_sval_bb{bb}", "o"))
        em.add_assignment(sval_o, p_last & ~mark_consumed)

    def _add_alloc_end_ports(self, em: Emitter, alloc_end_oh, alloc_full) -> None:
        """The END of the allocated addresses, as a marker a cross-BB checker
        can test against its check window: alloc_end_oh is the one-hot of the
        first unallocated slot (the tail), alloc_full says every slot is
        allocated (the tail then aliases the done slot). A boundary is out of
        bounds -- its address not known yet -- exactly when this marker lies
        inside the window [head, boundary], so no distance arithmetic is
        needed. Both come from registered state and are shared by every
        checker on this queue."""
        end_o = self._add_port(
            LogicVec(em, "alloc_end_oh", "o", self.configs.num_entries))
        em.add_assignment(end_o, alloc_end_oh)
        full_o = self._add_port(Logic(em, "alloc_full", "o"))
        em.add_assignment(full_o, alloc_full)

    # ===----------------------------------------------------------------------===
    # Shared helpers
    # ===----------------------------------------------------------------------===

    def _setup_pointers(self, em: Emitter):
        """
        Declare the circular-buffer pointers (done/issue/tail/head), their
        look-ahead wires, the tail one-hot, and the full/empty status signals.

        When num_entries is a power of 2, uses the bit-flip trick: pointers are
        q_addr_width+1 bits wide and full/empty are purely combinatorial wires.
        The MSB acts as a generation bit so that equal pointers (all bits) means
        empty, and equal lower bits with differing MSBs means full.

        alloc_en, issue_en, and head_en (head-retirement enable) are declared as
        wires; the caller is responsible for assigning logic to each of them.

        Returns:
            q_done, q_issue, q_tail, q_head — pointer registers
            q_tail_oh                       — one-hot of q_tail index
            done_en, issue_en, head_en, alloc_en — enable wires (to be driven by caller)
            q_full, q_empty, q_full_w_issue — status signals
            q_issue_sel                     — lower bits of q_issue for MuxLookUp
        """
        n = self.configs.q_addr_width
        ptr_width = n + 1

        q_tail = LogicVec(em, "q_tail", "r", ptr_width)
        q_head = LogicVec(em, "q_head", "r", ptr_width)
        q_issue = LogicVec(em, "q_issue", "r", ptr_width)
        q_done = LogicVec(em, "q_done", "r", ptr_width)

        q_tail_next = LogicVec(em, "q_tail_next", "w", ptr_width)
        q_head_next = LogicVec(em, "q_head_next", "w", ptr_width)
        q_issue_next = LogicVec(em, "q_issue_next", "w", ptr_width)
        q_done_next = LogicVec(em, "q_done_next", "w", ptr_width)

        q_tail_oh = LogicVec(em, "q_tail_oh", "w", self.configs.num_entries)

        if n == 0:
            # Single-entry queue (NumEntries == 1): there is no physical index
            # to compute, the lone entry is always "tail" and always "issue".
            em.add_assignment(q_tail_oh, Val(1, size=1))
            q_issue_sel = None
        else:
            # BitsToOH and MuxLookUp need the physical index (lower n bits only)
            q_tail_idx = LogicVec(em, "q_tail_idx", "w", n)
            ptr_index(em, q_tail_idx, q_tail, self.configs.num_entries, n)
            BitsToOH(em, q_tail_oh, q_tail_idx)

            q_issue_sel = LogicVec(em, "q_issue_sel", "w", n)
            ptr_index(em, q_issue_sel, q_issue, self.configs.num_entries, n)

        alloc_en = Logic(em, "alloc_en", "w")
        head_en = Logic(em, "head_en", "w")
        issue_en = Logic(em, "issue_en", "w")
        done_en = Logic(em, "done_en", "w")

        q_full = Logic(em, "q_full", "w")
        q_empty = Logic(em, "q_empty", "w")
        q_full_w_issue = Logic(em, "q_full_w_issue", "w")

        for pt, pt_next in [
            (q_done, q_done_next),
            (q_issue, q_issue_next),
            (q_tail, q_tail_next),
            (q_head, q_head_next),
        ]:
            ptr_next(em, pt_next, pt, self.configs.num_entries)
            em.add_assignment(pt, pt_next)

        q_done.regInit(init=0, enable=done_en)
        q_issue.regInit(init=0, enable=issue_en)
        q_tail.regInit(init=0, enable=alloc_en)
        q_head.regInit(init=0, enable=head_en)

        # Full between tail and done: lower bits match but generation bits differ.
        # With n == 0 (single-entry queue) the pointer IS the generation bit -
        # there are no low bits to compare, so equality is trivial.
        tail_msb = Val(em.index_var(q_tail.getNameRead(), n))
        done_msb = Val(em.index_var(q_done.getNameRead(), n))
        if n == 0:
            em.add_assignment(q_full, tail_msb != done_msb)
        else:
            # Same slot, other generation (for every N, see ptr_utils).
            tail_low = Val(em.slice_var(q_tail.getNameRead(), n - 1, 0))
            done_low = Val(em.slice_var(q_done.getNameRead(), n - 1, 0))
            em.add_assignment(q_full, (tail_msb != done_msb) & (tail_low == done_low))

        # Empty: all bits (including generation) equal
        em.add_assignment(q_empty, q_tail == q_head)

        # Full between issue and head: same lower bits, different generation
        issue_msb = Val(em.index_var(q_issue.getNameRead(), n))
        head_msb = Val(em.index_var(q_head.getNameRead(), n))
        if n == 0:
            em.add_assignment(q_full_w_issue, issue_msb != head_msb)
        else:
            issue_low = Val(em.slice_var(q_issue.getNameRead(), n - 1, 0))
            head_low = Val(em.slice_var(q_head.getNameRead(), n - 1, 0))
            em.add_assignment(
                q_full_w_issue, (issue_msb != head_msb) & (issue_low == head_low)
            )

        return (
            q_done,
            q_issue,
            q_tail,
            q_head,
            q_tail_oh,
            done_en,
            issue_en,
            head_en,
            alloc_en,
            q_full,
            q_empty,
            q_full_w_issue,
            q_issue_sel,
        )

    def _generate_observable_ports(
        self,
        em: Emitter,
        q_addr,
        q_done,
        q_tail,
        q_head,
        done_en,
        alloc_en,
        head_en,
        bypass_active=None,
        circ_addr_i=None,
        alloc_end_oh=None,
        alloc_full=None,
    ) -> None:
        """Add output ports that expose internal queue state for observation."""
        n = self.configs.q_addr_width
        ptr_width = n + 1

        q_addr_out_o = self._add_port(
            LogicVecArray(
                em, "q_addr", "o", self.configs.num_entries, self.configs.addr_width
            )
        )
        for i in range(self.configs.num_entries):
            em.add_assignment(q_addr_out_o[i], q_addr[i])

        # With n == 0 (single-entry queue) there is no index to expose: the
        # pointer register is pure generation bit, so these ports would be
        # zero-width. Omit them; nothing downstream needs a done/alloc/head
        # index into a 1-entry buffer (DependencyChecker.pq_done_i is omitted
        # the same way, see DependencyChecker.generate).
        if n > 0:
            done_ptr_o = self._add_port(LogicVec(em, "done_ptr", "o", n))
            alloc_ptr_o = self._add_port(LogicVec(em, "alloc_ptr", "o", n))
            head_ptr_o = self._add_port(LogicVec(em, "head_ptr", "o", n))

            ptr_index(em, done_ptr_o, q_done, self.configs.num_entries, n)
            ptr_index(em, alloc_ptr_o, q_tail, self.configs.num_entries, n)
            ptr_index(em, head_ptr_o, q_head, self.configs.num_entries, n)

        # Number of entries from done to tail (allocated but not yet complete).
        length_o = self._add_port(LogicVec(em, "length", "o", ptr_width))
        ptr_diff(em, length_o, q_tail, q_done, self.configs.num_entries)

        # Number of entries from done to head: accesses retired (let through
        # by allow_access_i) but not yet completed. A same-BB dependency
        # checker gates this queue's retirement on it, so that the completions
        # already in flight cannot underflow its disparity counter.
        pending_done_o = self._add_port(LogicVec(em, "pending_done", "o", ptr_width))
        ptr_diff(em, pending_done_o, q_head, q_done, self.configs.num_entries)

        done_en_o = self._add_port(Logic(em, "done_en", "o"))
        access_en_o = self._add_port(Logic(em, "access_en", "o"))

        em.add_assignment(done_en_o, done_en)
        em.add_assignment(access_en_o, head_en)

        self._add_alloc_end_ports(em, alloc_end_oh, alloc_full)

        if self.configs.is_succ:
            em.begin_region('q_head_mux')
            queue_head_w = LogicVec(em, "queue_head_w", "w", self.configs.addr_width)
            if n == 0:
                # Single entry: it is always the head.
                em.add_assignment(queue_head_w, q_addr[0])
            else:
                q_head_sel = LogicVec(em, "q_head_sel", "w", n)
                ptr_index(em, q_head_sel, q_head, self.configs.num_entries, n)
                MuxLookUp(em, queue_head_w, q_addr, q_head_sel)
            queue_head_o = self._add_port(
                LogicVec(em, "queue_head", "o", self.configs.addr_width)
            )
            if bypass_active is not None and circ_addr_i is not None:
                em.add_assignment(
                    queue_head_o,
                    circ_addr_i.when(bypass_active).else_(queue_head_w),
                )
            else:
                em.add_assignment(queue_head_o, queue_head_w)

    def _write_to_file(self, em: Emitter, path_rtl: str, out_file: str = None):
        em.begin_region(None)
        output_str = em.get_definition_str(self.module_name)
        path = (
            out_file
            if out_file is not None
            else f"{path_rtl}/{self.name}.{em.get_file_suffix()}"
        )
        with open(path, "a") as file:
            file.write(output_str)
