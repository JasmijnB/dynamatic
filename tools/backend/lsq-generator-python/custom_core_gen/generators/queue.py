from types import SimpleNamespace

from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import *
from core_gen.ir import BinOp, Val, Bit
from custom_core_gen.generators.generator import Generator
from custom_core_gen.generators.ptr_utils import (
    ptr_next, ptr_index, ptr_diff, ptr_full, slot_one_hot)


class Queue(Generator):
    def generate(self, em: Emitter, path_rtl, out_file: str = None) -> None:
        self.ports.clear()
        c = self.configs
        port = self._add_port
        is_store = c.q_type == "store"

        # IOs - common
        empty_o = port(Logic(em, "empty", "o"))
        circ_addr_i = port(LogicVec(em, "circ_addr", "i", c.addr_width))
        circ_addr_valid_i = port(Logic(em, "circ_addr_valid", "i"))
        circ_addr_ready_o = port(Logic(em, "circ_addr_ready", "o"))

        mem_addr_valid_o = port(Logic(em, "mem_addr_valid", "o"))
        mem_addr_ready_i = port(Logic(em, "mem_addr_ready", "i"))
        mem_addr_o = port(LogicVec(em, "mem_addr", "o", c.addr_width))

        # IOs - type-specific
        if is_store:
            circ_data_i = port(LogicVec(em, "circ_data", "i", c.data_width))
            circ_data_valid_i = port(Logic(em, "circ_data_valid", "i"))
            circ_data_ready_o = port(Logic(em, "circ_data_ready", "o"))
            mem_data_o = port(LogicVec(em, "mem_data", "o", c.data_width))
            mem_data_valid_o = port(Logic(em, "mem_data_valid", "o"))
            mem_data_ready_i = port(Logic(em, "mem_data_ready", "i"))
            mem_exec_valid_i = port(Logic(em, "mem_exec_valid", "i"))
            mem_exec_ready_o = port(Logic(em, "mem_exec_ready", "o"))
            if c.st_resp:
                circ_exec_valid_o = port(Logic(em, "circ_exec_valid", "o"))
                circ_exec_ready_i = port(Logic(em, "circ_exec_ready", "i"))
        else:
            mem_data_valid_i = port(Logic(em, "mem_data_valid", "i"))
            mem_data_ready_o = port(Logic(em, "mem_data_ready", "o"))
            mem_data_i = port(LogicVec(em, "mem_data", "i", c.data_width))
            circ_data_o = port(LogicVec(em, "circ_data", "o", c.data_width))
            circ_data_valid_o = port(Logic(em, "circ_data_valid", "o"))
            circ_data_ready_i = port(Logic(em, "circ_data_ready", "i"))

        allow_access_i = port(Logic(em, "allow_access", "i"))

        # Shared pointer infrastructure
        em.begin_region('q_ctrl')
        q = self._setup_pointers(em)

        # Per-entry write enable: the allocated entry is at q_alloc. Kept as its own
        # signal (in queue control) so the address registers below hold only the
        # addresses.
        q_addr_wen = LogicVec(em, "q_addr_wen", "w", c.num_entries)
        for i in range(c.num_entries):
            em.add_assignment((q_addr_wen, i), Val(q.alloc_oh, i) & q.alloc_en)

        # Address buffer (both types)
        em.begin_region('q_addr')
        q_addr = LogicVecArray(em, "q_addr", "r", c.num_entries, c.addr_width)
        for i in range(c.num_entries):
            em.add_assignment(q_addr[i], circ_addr_i.when(Val(q_addr_wen, i)).else_(q_addr[i]))
        q_addr.regInit()

        em.begin_region('q_ctrl')
        em.add_assignment(empty_o, q.empty)

        # Bypass is an optional fast path that lets an incoming address skip the
        # queue when it is empty. When disabled the queue always routes through
        # its buffer.
        bypass_addr_valid = bypass_active = None
        if c.bypass:
            # bypass_addr_valid: queue is empty with no pending issues and a valid address.
            # Deliberately excludes allow_access_i to break the combinatorial loop that
            # would form through queue_head_o → dep-checker → allow_access_i → bypass_active.
            bypass_addr_valid = Logic(em, "bypass_addr_valid", "w")
            em.add_assignment(bypass_addr_valid,
                              q.empty & (q.issue == q.grant) & circ_addr_valid_i)

            # bypass_active additionally requires allow_access_i; used for all control paths.
            bypass_active = Logic(em, "bypass_active", "w")
            em.add_assignment(bypass_active, bypass_addr_valid & allow_access_i)

        # Allocation
        can_alloc = Logic(em, "can_alloc", "w")
        em.add_assignment(can_alloc, ~q.full)
        em.add_assignment(circ_addr_ready_o, can_alloc)
        em.add_assignment(q.alloc_en, circ_addr_valid_i & can_alloc)

        # Grant: also fires when bypass completes (memory accepts)
        grant_now = ~q.empty & allow_access_i
        if c.bypass:
            grant_now = grant_now | (bypass_active & mem_addr_ready_i)
        em.add_assignment(q.grant_en, grant_now)

        # Issue
        em.begin_region('q_issue')
        can_issue = Logic(em, "can_issue", "w")
        em.add_assignment(
            can_issue,
            ((q.issue == q.grant) & q.grant_en) | (q.issue != q.grant) | q.full_w_issue,
        )
        issue_req = can_issue | bypass_active if c.bypass else can_issue
        em.add_assignment(q.issue_en, issue_req & mem_addr_ready_i)
        em.add_assignment(mem_addr_valid_o, issue_req)

        # In bypass mode route circ_addr_i directly; otherwise use the queue buffer
        mem_addr_from_queue = LogicVec(em, "mem_addr_from_queue", "w", c.addr_width)
        if q.issue_sel is None:
            # Single-entry queue: the only entry is always the one to issue.
            em.add_assignment(mem_addr_from_queue, q_addr[0])
        else:
            MuxLookUp(em, mem_addr_from_queue, q_addr, q.issue_sel)
        if c.bypass:
            em.add_assignment(mem_addr_o, circ_addr_i.when(bypass_active).else_(mem_addr_from_queue))
        else:
            em.add_assignment(mem_addr_o, mem_addr_from_queue)

        if is_store:
            em.add_assignment(mem_data_o, circ_data_i)
            em.add_assignment(mem_data_valid_o, circ_data_valid_i)
            em.add_assignment(circ_data_ready_o, mem_data_ready_i)

            if c.st_resp:
                em.add_assignment(circ_exec_valid_o, mem_exec_valid_i)
                em.add_assignment(mem_exec_ready_o, circ_exec_ready_i)
                em.add_assignment(q.done_en, mem_exec_valid_i & circ_exec_ready_i)
            else:
                em.add_assignment(mem_exec_ready_o, Bit(1))
                em.add_assignment(q.done_en, mem_exec_valid_i)
        else:
            em.add_assignment(mem_data_ready_o, circ_data_ready_i)
            em.add_assignment(circ_data_o, mem_data_i)
            em.add_assignment(circ_data_valid_o, mem_data_valid_i)
            em.add_assignment(q.done_en, mem_data_valid_i & circ_data_ready_i)

        em.begin_region('q_ctrl')
        self._observable_ports(em, q_addr, q, bypass_addr_valid, circ_addr_i)
        if c.shares_dep_state:
            self._shared_dep_state(em, q.done, q.grant, q.done_en)

        self._write_to_file(em, path_rtl, out_file)

    def _out(self, em: Emitter, name: str, sig) -> None:
        """An output port `name` driven by `sig`, of the same shape."""
        o = self._add_port(LogicVec(em, name, "o", sig.size) if isinstance(sig, LogicVec)
                           else Logic(em, name, "o"))
        em.add_assignment(o, sig)

    def _shared_dep_state(self, em: Emitter, q_done, q_grant, done_en) -> None:
        """Dep-array state that every cross-BB dependency checker of this
        port would otherwise keep a private copy of, computed once here.

        A checker's predecessor dep array pops on this queue's completions
        and its successor dep array on this queue's retirements, both from 0:
        their heads ARE q_done and q_grant. Both arrays push once per BB
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

        if c.is_pred:
            # Predecessor role: head = q_done, tail = BB executions.
            head_oh = slot_one_hot(em, "dep_p_head_oh", "dep_p_head_oh_idx", q_done, n, aw)
            tail_oh = slot_one_hot(em, "dep_p_tail_oh", "dep_p_tail_oh_idx", bb_tail, n, aw)
            empty = Logic(em, "dep_p_empty", "w")
            em.add_assignment(empty, q_done == bb_tail)
            full = Logic(em, "dep_p_full", "w")
            ptr_full(em, full, bb_tail, q_done, aw)
            for name, sig in (("dep_p_head_oh", head_oh), ("dep_p_tail_oh", tail_oh),
                              ("dep_p_empty", empty), ("dep_p_full", full)):
                self._out(em, name, sig)
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
            # Successor role: occupancy = BB executions - q_grant.
            em.begin_region('dc_ptrs')
            cnt = LogicVec(em, "dep_s_cnt", "w", ptr_width)
            ptr_diff(em, cnt, bb_tail, q_grant, n)
            empty = Logic(em, "dep_s_empty", "w")
            em.add_assignment(empty, cnt == Val(0, size=ptr_width))
            full = Logic(em, "dep_s_full", "w")
            em.add_assignment(full, cnt == Val(n, size=ptr_width))
            # The successor arrays live in the queue's physical slot frame:
            # written at the BB-execution tail, read at q_grant.
            head_oh = slot_one_hot(em, "dep_s_head_oh", "dep_s_head_idx", q_grant, n, aw)
            tail_oh = slot_one_hot(em, "dep_s_tail_oh", "dep_s_tail_idx", bb_tail, n, aw)
            for name, sig in (("dep_s_head_oh", head_oh), ("dep_s_tail_oh", tail_oh),
                              ("dep_s_empty", empty), ("dep_s_full", full)):
                self._out(em, name, sig)

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

    # ===----------------------------------------------------------------------===
    # Shared helpers
    # ===----------------------------------------------------------------------===

    def _setup_pointers(self, em: Emitter):
        """
        Declare the circular-buffer pointers, in program order
        alloc -> grant -> issue -> done, their look-ahead wires, the
        alloc one-hot, and the full/empty status signals.

        When num_entries is a power of 2, uses the bit-flip trick: pointers are
        q_addr_width+1 bits wide and full/empty are purely combinatorial wires.
        The MSB acts as a generation bit so that equal pointers (all bits) means
        empty, and equal lower bits with differing MSBs means full.

        Returns a namespace of the pointer registers (alloc, grant, issue,
        done), their enable wires (alloc_en, ...: driven by the caller),
        alloc_oh, full, empty, full_w_issue, and issue_sel (q_issue's slot
        for MuxLookUp, None for a single entry).
        """
        n = self.configs.q_addr_width
        n_entries = self.configs.num_entries
        ptr_width = n + 1
        q = SimpleNamespace()
        names = ("alloc", "grant", "issue", "done")
        for name in names:
            setattr(q, name, LogicVec(em, f"q_{name}", "r", ptr_width))
        nexts = {name: LogicVec(em, f"q_{name}_next", "w", ptr_width) for name in names}

        # With n == 0 (single entry) the lone entry is always the one to
        # allocate and to issue.
        q.alloc_oh = slot_one_hot(em, "q_alloc_oh", "q_alloc_idx", q.alloc, n_entries, n)
        q.issue_sel = None
        if n > 0:
            q.issue_sel = LogicVec(em, "q_issue_sel", "w", n)
            ptr_index(em, q.issue_sel, q.issue, n_entries, n)

        for name in names:
            setattr(q, f"{name}_en", Logic(em, f"{name}_en", "w"))
        q.full = Logic(em, "q_full", "w")
        q.empty = Logic(em, "q_empty", "w")
        q.full_w_issue = Logic(em, "q_full_w_issue", "w")

        for name in ("done", "issue", "alloc", "grant"):
            ptr_next(em, nexts[name], getattr(q, name), n_entries)
            em.add_assignment(getattr(q, name), nexts[name])
        for name in ("done", "issue", "alloc", "grant"):
            getattr(q, name).regInit(init=0, enable=getattr(q, f"{name}_en"))

        # Full between alloc and done, empty when alloc meets grant (all bits,
        # generation included), and full between issue and grant.
        ptr_full(em, q.full, q.alloc, q.done, n)
        em.add_assignment(q.empty, q.alloc == q.grant)
        ptr_full(em, q.full_w_issue, q.issue, q.grant, n)
        return q

    def _observable_ports(self, em: Emitter, q_addr, q, bypass_active, circ_addr_i) -> None:
        """Output ports exposing queue state to the dependency checkers."""
        c = self.configs
        n = c.q_addr_width
        ptr_width = n + 1

        q_addr_out_o = self._add_port(
            LogicVecArray(em, "q_addr", "o", c.num_entries, c.addr_width))
        for i in range(c.num_entries):
            em.add_assignment(q_addr_out_o[i], q_addr[i])

        # With n == 0 (single-entry queue) there is no index to expose: the
        # pointer register is pure generation bit, so the port would be
        # zero-width. Omit it (DependencyChecker omits pq_done_i the same way).
        if n > 0:
            done_ptr_o = self._add_port(LogicVec(em, "done_ptr", "o", n))
            ptr_index(em, done_ptr_o, q.done, c.num_entries, n)

        # Number of entries from done to alloc (allocated but not yet complete).
        length_o = self._add_port(LogicVec(em, "length", "o", ptr_width))
        ptr_diff(em, length_o, q.alloc, q.done, c.num_entries)

        # Number of entries from done to grant: accesses granted (let through
        # by allow_access_i) but not yet completed. A same-BB dependency
        # checker gates this queue's retirement on it, so that the completions
        # already in flight cannot underflow its disparity counter.
        pending_done_o = self._add_port(LogicVec(em, "pending_done", "o", ptr_width))
        ptr_diff(em, pending_done_o, q.grant, q.done, c.num_entries)

        self._out(em, "done_en", q.done_en)
        self._out(em, "grant_en", q.grant_en)

        # The END of the allocated addresses, as a marker a cross-BB checker
        # can test against its check window: alloc_end_oh is the one-hot of
        # the first unallocated slot (q_alloc), alloc_full says every slot is
        # allocated (q_alloc then aliases the done slot). A boundary is out of
        # bounds -- its address not known yet -- exactly when this marker lies
        # inside the window [head, boundary], so no distance arithmetic is
        # needed. Both come from registered state and are shared by every
        # checker on this queue.
        self._out(em, "alloc_end_oh", q.alloc_oh)
        self._out(em, "alloc_full", q.full)

        if c.is_succ:
            em.begin_region('q_head_mux')
            queue_head_w = LogicVec(em, "queue_head_w", "w", c.addr_width)
            if n == 0:
                # Single entry: it is always the head.
                em.add_assignment(queue_head_w, q_addr[0])
            else:
                q_grant_sel = LogicVec(em, "q_grant_sel", "w", n)
                ptr_index(em, q_grant_sel, q.grant, c.num_entries, n)
                MuxLookUp(em, queue_head_w, q_addr, q_grant_sel)
            queue_head_o = self._add_port(LogicVec(em, "queue_head", "o", c.addr_width))
            if bypass_active is not None:
                em.add_assignment(queue_head_o, circ_addr_i.when(bypass_active).else_(queue_head_w))
            else:
                em.add_assignment(queue_head_o, queue_head_w)
