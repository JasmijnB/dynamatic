from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import *
from core_gen.configs import Configs
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, reduce_bin
from custom_core_gen.generators.generator import Generator


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

        # Address buffer (both types)
        q_addr = LogicVecArray(
            em, "q_addr", "r", self.configs.num_entries, self.configs.addr_width
        )
        for i in range(self.configs.num_entries):
            em.add_assignment(
                q_addr[i],
                circ_addr_i.when(Val(q_tail_oh, i) & alloc_en).else_(q_addr[i]),
            )
        q_addr.regInit()

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
        can_issue = Logic(em, "can_issue", "w")

        em.add_assignment(
            can_issue,
            ((q_issue == q_head) & head_en) | (q_issue != q_head) | q_full_w_issue,
        )
        issue_req = can_issue | bypass_active if self.configs.bypass else can_issue
        em.add_assignment(issue_en, issue_req & mem_addr_ready_i)

        em.add_assignment(mem_addr_valid_o, issue_req)

        # In bypass mode route circ_addr_i directly; otherwise use the queue buffer
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
        )

        self._write_to_file(em, path_rtl, out_file)

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
            em.add_assignment(q_tail_idx, Val(em.slice_var(q_tail.getNameRead(), n - 1, 0)))
            BitsToOH(em, q_tail_oh, q_tail_idx)

            q_issue_sel = LogicVec(em, "q_issue_sel", "w", n)
            em.add_assignment(
                q_issue_sel, Val(em.slice_var(q_issue.getNameRead(), n - 1, 0))
            )

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
            WrapAddConst(em, pt_next, pt, 1, self.configs.num_entries)
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

            em.add_assignment(done_ptr_o, Val(em.slice_var(q_done.getNameRead(), n - 1, 0)))
            em.add_assignment(
                alloc_ptr_o, Val(em.slice_var(q_tail.getNameRead(), n - 1, 0))
            )
            em.add_assignment(head_ptr_o, Val(em.slice_var(q_head.getNameRead(), n - 1, 0)))

        # Number of entries from done to tail (allocated but not yet complete).
        length_o = self._add_port(LogicVec(em, "length", "o", ptr_width))
        em.add_assignment(length_o, q_tail - q_done)

        done_en_o = self._add_port(Logic(em, "done_en", "o"))
        access_en_o = self._add_port(Logic(em, "access_en", "o"))

        em.add_assignment(done_en_o, done_en)
        em.add_assignment(access_en_o, head_en)

        if self.configs.is_succ:
            queue_head_w = LogicVec(em, "queue_head_w", "w", self.configs.addr_width)
            if n == 0:
                # Single entry: it is always the head.
                em.add_assignment(queue_head_w, q_addr[0])
            else:
                q_head_sel = LogicVec(em, "q_head_sel", "w", n)
                em.add_assignment(
                    q_head_sel, Val(em.slice_var(q_head.getNameRead(), n - 1, 0))
                )
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
        output_str = em.get_definition_str(self.module_name)
        path = (
            out_file
            if out_file is not None
            else f"{path_rtl}/{self.name}.{em.get_file_suffix()}"
        )
        with open(path, "a") as file:
            file.write(output_str)
