"""Circular-buffer pointer helpers that work for ANY number of entries.

Queue and dep-array pointers are n+1 bits: the MSB is a generation bit and
the low n = ceil(log2(N)) bits are the slot, 0 .. N-1. Equal pointers mean
empty; equal slots with differing generations mean full. For a power-of-two
N this is the classic bit trick and the pointer simply counts mod 2N. For
other N the SAME layout is kept -- only the increment differs: the slot wraps
from N-1 to 0 and toggles the generation bit. So slot extraction, full and
empty are the same free bit-slices and compares for every N; only
increments and pointer differences cost extra when N is not a power of two.

Each helper emits exactly the old power-of-two expression when N is a power
of two, so power-of-two designs are unchanged.
"""

from core_gen.ir import Val, Bit
from core_gen.signals import Logic, LogicVec
from core_gen.utils import isPow2
from core_gen.operators import WrapAddConst, WrapSub


def _low(em, ptr, n):
    return Val(em.slice_var(ptr.getNameRead(), n - 1, 0))


def ptr_next(em, out, ptr, n_entries: int) -> None:
    """out = the pointer after `ptr`: slot + 1, wrapping N-1 -> 0 with a
    generation toggle."""
    if isPow2(n_entries):
        WrapAddConst(em, out, ptr, 1, 2 * n_entries)
        return
    n = ptr.size - 1
    em.use_temp()
    wrap = Logic(em, em.get_temp(f"{out.name}_wrap"), "w")
    em.add_assignment(wrap, _low(em, ptr, n) == Val(n_entries - 1, size=n))
    low_next = LogicVec(em, em.get_temp(f"{out.name}_slot"), "w", n)
    low_src = LogicVec(em, em.get_temp(f"{out.name}_slot_cur"), "w", n)
    em.add_assignment(low_src, _low(em, ptr, n))
    em.add_assignment(
        low_next, Val(0, size=n).when(wrap).else_(low_src + Val(1, size=n)))
    gen_next = Logic(em, em.get_temp(f"{out.name}_gen"), "w")
    em.add_assignment(gen_next, Val(em.index_var(ptr.getNameRead(), n)) ^ wrap)
    em.add_assignment(out, Val(gen_next).concat(low_next))


def ptr_index(em, out, ptr, n_entries: int, n: int) -> None:
    """out (n bits) = the slot of pointer `ptr`: its low bits, for every N."""
    em.add_assignment(out, _low(em, ptr, n))


def count_slot(em, out, cnt, n_entries: int, n: int) -> None:
    """out (n bits) = the slot just past `cnt` entries from slot 0, for an
    occupancy cnt in [0, N]: cnt mod N (so a full count N maps to slot 0)."""
    if isPow2(n_entries):
        em.add_assignment(out, _low(em, cnt, n))
        return
    em.add_assignment(
        out,
        Val(0, size=n).when(cnt == Val(n_entries, size=cnt.size))
        .else_(_low(em, cnt, n)))


def ptr_diff(em, out, a, b, n_entries: int) -> None:
    """out = the number of entries from pointer b up to pointer a (0 .. N)."""
    if isPow2(n_entries):
        WrapSub(em, out, a, b, 2 * n_entries)
        return
    n = a.size - 1
    em.use_temp()
    a_low = LogicVec(em, em.get_temp(f"{out.name}_a"), "w", n + 1)
    b_low = LogicVec(em, em.get_temp(f"{out.name}_b"), "w", n + 1)
    em.add_assignment(a_low, Bit(0).concat(_low(em, a, n)))
    em.add_assignment(b_low, Bit(0).concat(_low(em, b, n)))
    same_gen = Logic(em, em.get_temp(f"{out.name}_same_gen"), "w")
    em.add_assignment(
        same_gen,
        Val(em.index_var(a.getNameRead(), n)) == Val(em.index_var(b.getNameRead(), n)))
    # Same generation: a is at or after b in the slot order; otherwise a has
    # wrapped past slot N-1 once more than b.
    em.add_assignment(
        out,
        (a_low - b_low).when(same_gen).else_((a_low + Val(n_entries)) - b_low))
