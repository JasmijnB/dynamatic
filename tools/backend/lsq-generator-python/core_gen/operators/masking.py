from core_gen.emitters import Emitter
from core_gen.ir import Val


def CyclicPriorityMasking(em: Emitter, dout, din, base, reverse=False) -> str:
    """
    Parameters:
        dout (LogicVecArray, LogicArray, LogicVec):
            Destination to write the masked result.
            One youngest or oldest bit set to '1' and the other to '0' per each Array
        din  (LogicVecArray, LogicArray, LogicVec):
            Input data to be masked.
        base (LogicVec):
            Binary pivot index for the rotation mask.
        reverse (bool, optional):
            Choose direction of masking.
            False -> Find the oldest   (Searching direction: base to MSB -> LSB to base)
            True  -> Find the youngest (Searching direction: base to LSB -> MSB to base)

    Example:
        1. din1 = 010110     2. din2 = 100100   3. din3 = 000110
           base = 001000        base = 001000      base = 001000
           reverse = False      reverse = True     reverse = False

           dout1= 010000        dout2= 000100      dout3= 00001
           (base to MSB)        (base to LSB)      (base to MSB -> LSB to base)

    Behavior (with the Example 1):
        double_in            = 010110 010110
        base                 = 000000 001000
        double_in - base     = 010110 001110
        ~(double_in - base)  = 101001 110001
        double_out           = double_in & ~(double_in - base)
                             = 000000 010000
        dout                 = 000000 | 010000
                             = 010000

    Example (LogicVecArray din):
        1. din = 010
                 000
                 100
                 010
           base = 001
           reverse = False

           priority masking -> (0th col) [0010] with base = 1 -> 0010
           priority masking -> (1st col) [1001] with base = 1 -> 0001
           priority masking -> (2nd col) [0000] with base = 1 -> 0000

           -> dout = 000
                     000
                     100
                     010
    """

    em.add_comment("Priority Masking Begin")
    em.add_comment(f"CyclicPriorityMask({dout.name}, {din.name}, {base.name})")
    em.use_temp()
    from core_gen.signals import LogicVecArray, LogicVec, LogicArray

    if isinstance(din, LogicVecArray):
        assert reverse == False
        for i in range(0, din.size):
            size = din.length
            double_in = LogicVec(em, em.get_temp(f"double_in_{i}"), "w", size * 2)
            for j in range(0, size):
                em.add_assignment((double_in, j), Val(din, j, i))
                em.add_assignment((double_in, j + size), Val(din, j, i))
            double_out = LogicVec(em, em.get_temp(f"double_out_{i}"), "w", size * 2)
            # TODO: Double check whether the brackets are correct
            em.add_assignment(
                double_out, double_in & ~(double_in - (Val(0, size).concat(base)))
            )
            for j in range(0, size):
                em.add_assignment(
                    (dout, j, i), Val(double_out, j) | Val(double_out, j + size)
                )
    else:
        if reverse:
            if isinstance(din, LogicArray):
                size = din.length
            else:
                size = din.size
            double_in = LogicVec(em, em.get_temp("double_in"), "w", size * 2)
            for i in range(0, size):
                em.add_assignment((double_in, i), Val(din, size - 1 - i))
                em.add_assignment((double_in, i + size), Val(din, size - 1 - i))
            base_rev = LogicVec(em, em.get_temp("base_rev"), "w", size)
            for i in range(0, size):
                em.add_assignment((base_rev, i), Val(base, size - 1 - i))
            double_out = LogicVec(em, em.get_temp("double_out"), "w", size * 2)
            em.add_assignment(
                double_out, double_in & ~(double_in - (Val(0, size).concat(base_rev)))
            )
            for i in range(0, size):
                em.add_assignment(
                    (dout, size - 1 - i), Val(double_out, i) | Val(double_out, i + size)
                )
        else:
            if isinstance(din, LogicArray):
                size = din.length
                double_in = LogicVec(em, em.get_temp("double_in"), "w", size * 2)
                for i in range(0, size):
                    em.add_assignment((double_in, i), Val(din, i))
                    em.add_assignment((double_in, i + size), Val(din, i))
            else:
                size = din.size
                double_in = LogicVec(em, em.get_temp("double_in"), "w", size * 2)
                em.add_assignment(double_in, din.concat(din))
            double_out = LogicVec(em, em.get_temp("double_out"), "w", size * 2)
            em.add_assignment(
                double_out, double_in & ~(double_in - (Val(0, size).concat(base)))
            )
            if isinstance(dout, LogicVec):
                # TODO: Have indexing function
                em.add_assignment(
                    dout,
                    Val(em.slice_var(f"{double_out.getNameRead()}", size - 1, 0))
                    | Val(
                        em.slice_var(f"{double_out.getNameRead()}", 2 * size - 1, size)
                    ),
                )
            else:
                for i in range(0, size):
                    em.add_assignment(
                        (dout, i), Val(double_out, i) | Val(double_out, i + size)
                    )
    em.add_comment("Priority Masking End\n")


def CyclicRangeFill(em: Emitter, dout, start, end) -> None:
    """
    Fill every bit between two one-hot markers, going rightward (towards the LSB)
    from `start` down to `end`, inclusive and cyclically (wrapping past the LSB
    back to the MSB when `end` sits above `start`).

    Parameters:
        dout  (LogicVec): Destination for the filled range.
        start (LogicVec): One-hot marker for the (inclusive) high end of the run.
        end   (LogicVec): One-hot marker for the (inclusive) low end of the run.

    This mirrors CyclicPriorityMasking's doubled-vector subtract, but ORs the run
    back in instead of isolating the first bit:
        double_in  = {start, start}
        double_out = double_in | (double_in - {0, end})
        dout[i]    = double_out[i] | double_out[i + size]

    Example (size = 5):
        start = 00100   end = 00001  ->  dout = 00111
        start = 00001   end = 00100  ->  dout = 11101   (wraps past the LSB)
        start = 00100   end = 00100  ->  dout = 00100   (single bit)
    """
    em.add_comment("Cyclic Range Fill Begin")
    em.add_comment(f"CyclicRangeFill({dout.name}, {start.name}, {end.name})")
    em.use_temp()
    from core_gen.signals import LogicVec

    size = start.size
    # Doubled copy of `start` so the subtraction can wrap past the LSB.
    double_in = LogicVec(em, em.get_temp("range_double_in"), "w", size * 2)
    for i in range(0, size):
        em.add_assignment((double_in, i), Val(start, i))
        em.add_assignment((double_in, i + size), Val(start, i))
    # Subtracting the (zero-extended) `end` marker turns the run between the two
    # markers into ones; ORing `double_in` back in re-adds the `start` bit itself.
    double_out = LogicVec(em, em.get_temp("range_double_out"), "w", size * 2)
    em.add_assignment(
        double_out, double_in | (double_in - (Val(0, size).concat(end)))
    )
    # Fold the two halves back together so the wrap-around bits land correctly.
    em.add_assignment(
        dout,
        Val(em.slice_var(f"{double_out.getNameRead()}", size - 1, 0))
        | Val(em.slice_var(f"{double_out.getNameRead()}", 2 * size - 1, size)),
    )
    em.add_comment("Cyclic Range Fill End\n")
