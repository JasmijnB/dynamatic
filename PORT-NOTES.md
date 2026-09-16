# custom-queues → cq-rebase-python: Python-generator port notes

Scope: everything under `tools/backend/lsq-generator-python/`.

Three trees are involved:

| tree | what it is |
| --- | --- |
| `$BASE` = `66accbd9` | merge-base of `main` and `custom-queues` |
| `custom-queues` (`de705a2f`) | the feature branch; carries an **older** port of the emitter structure |
| `lsq-vhdl-verilog` (`9bad2c77`) = this branch's starting point | `main` + a **newer, better** emitter structure (IR + VHDL/Verilog emitters) |

`lsq-vhdl-verilog`'s `core_gen/` is the source of truth for the IR / signals /
operators / emitters / `lsq.py` layer. Only what `custom-queues` *adds on top*
was brought across, re-expressed against the newer API.

All of `custom-queues`' files use CRLF line endings; everything ported here was
normalised to LF.

---

## 1. Inventory: `custom-queues` core_gen vs `lsq-vhdl-verilog` core_gen

Classification: **(a)** feature addition — ported; **(b)** older drift —
discarded; **(c)** cosmetic (black-style reformatting, double→single quotes,
"VHDL" re-inserted into language-agnostic docstrings) — discarded.

| file | verdict | detail |
| --- | --- | --- |
| `README.md` | (b)(c) | drops `bypassEn` docs, drops the `--hdl verilog` mentions, un-does doc improvements. Discarded; a new section describing `custom_core_gen/` + `ordering-network-generator.py` was added instead. |
| `core_gen/__init__.py` | (c) | `__all__` list reflow only. |
| `core_gen/codegen.py` | (b) | older; no custom-queues content. |
| `core_gen/configs.py` | (b) | **older LSQ `Configs`**: loses `bypass`/`bypassEn`, reverts `emptyLdAddrW`/`emptyStAddrW` to the pre-fix `numLdqEntries & (…%2==0)` form, loses the per-group "N entries support at most N-1 accesses" asserts. Discarded wholesale. `custom_core_gen` has its own `configs.py` and does not use this one. |
| `core_gen/utils.py` | (b)(c) | adds an unused `import re`, drops the module docstring. |
| `core_gen/ir.py` | **(a)** + (b) | (a) `Type.SIGNED` — **ported**. (b) `WhenElse.get_type()` raising on mixed ARITH/LOGIC branches — **discarded**, this is regression #2 from the task description and `lsq-vhdl-verilog` already fixes it by returning `Type.LOGIC`. |
| `core_gen/signals.py` | **(a)** + (c) | (a) `is_signed=` on `LogicVec`/`LogicVecArray` and `LogicVec.get_type()` → `Type.SIGNED` — **ported**. |
| `core_gen/emitters/emitter.py` | **(a)** + (b) | (a) `assigned_var_to_str` picking up `size` for a `(LogicVecArray, i)` target — **ported** (without it, `add_assignment((array, i), Val(k))` renders `k` at 1 bit). (b) removal of the abstract-method stubs, and `use_read_name=`/`to_reg_str=` — **discarded**: that is `custom-queues`' alternative rewrite of `*_reg_init` in terms of `add_assignment`, not a feature. `lsq-vhdl-verilog`'s string-based `*_reg_init` is kept, including the deliberate `if init is None: init = 0` divergence. |
| `core_gen/emitters/vhdl_emitter.py` | **(a)** + (b) | (a) `Type.SIGNED` support — **ported** (see §3). (b) `to_reg_str` refactor, removal of the per-branch `fix_type` in `when_else_to_str`, `add_comment` losing the blank-line case, `int_to_str`'s laxer `size=None` path, `print_custom_str`→`get_custom_str` rename — **discarded**. |
| `core_gen/emitters/verilog_emitter.py` | **(a)** + (b) | as above; (a) is the signed support, (b) the `to_reg_str` refactor and `int_to_str` returning a bare `str(din)` for `Type.ARITH` (deliberately changed on `lsq-vhdl-verilog` — sized Verilog literals are legal in arithmetic contexts). |
| `core_gen/operators/arithmetic.py` | (b) | `WrapAdd` referencing the undefined `out_size` and dropping the `Val(...)` wrapper — regression #2 from the task description, already fixed on `lsq-vhdl-verilog`. Discarded. (`WrapSub`'s `in_a + Val(max) - in_b` vs `Val(max) - in_b + in_a` is an equivalent reassociation.) |
| `core_gen/operators/shifts.py` | **(a)** | `CyclicRightShift` + the `left=` flag on the three rotate helpers — **ported, with a fix** (see §4). |
| `core_gen/operators/masking.py` | **(a)** | `CyclicRangeFill` — **ported**. Also `double_in <= din & din` → `din.concat(din)`: at `$BASE` that expression was a raw VHDL `&`, i.e. *concatenation*; the emitter-structure port on `lsq-vhdl-verilog` mapped it to Python `&` = `BinOp.AND`, which is a bitwise and. `custom-queues` fixed it; **the fix is carried over**. The branch is unreachable from both `lsq.py` and `custom_core_gen` today (verified: generated LSQ output is byte-identical with and without it), so this is purely a latent-bug fix. |
| `core_gen/operators/conversions.py` | (c) | docstrings only. |
| `core_gen/operators/mux.py` | (c) | docstrings only (`IntToBits`/`MaskLess` are the pre-rename names). |
| `core_gen/operators/reduction.py` | (c) | docstrings only. |
| `core_gen/operators/__init__.py` | **(a)** | exports `CyclicRightShift`, `CyclicRangeFill` — **ported**. |
| `core_gen/generators/dispatchers.py` | (c) | comment-only differences (~1200 diff lines, all reformatting). |
| `core_gen/generators/group_allocator.py` | (b) | **older**: loses the one-hot `group_init_valid` assertion (VHDL+Verilog) that `main` added. Discarded. |
| `core_gen/generators/lsq.py` | (b) | **older**: regression #1 from the task description — re-derived from a pre-rework upstream `lsq.py`, losing `pipe_comp_type`/`pipe0_type`/`pipe1_type`, `load_completed`/`store_completed`, `store_req_valid_arr`, `store_issue_stall`, `stq_tail_update`, the `configs.bypass` off-path and the p1 backpressure wiring. Discarded wholesale; **not one line was taken from it**. |
| `core_gen/generators/lsq_submodule_wrapper.py` | (c) | trailing whitespace. |
| `lsq-generator.py` | (b)(c) | `custom-queues` adds **no** custom-queues feature here: its only changes are stripped comments, a dead `lsq_wrapper_str` accumulator, lost `increase_indent()` calls, and the loss of the `io_stAddrToMC_ready` term in the store-completion condition. Discarded; `lsq-generator.py` is **unmodified** by this port. |

**Net result: `core_gen/generators/` and `lsq-generator.py` are untouched.**

## 2. Files ported wholesale (new)

* `custom_core_gen/` — `configs.py`, `generators/{generator,queue,dependency_checker,structure}.py`,
  and the whole `testbenches/` tree (5 testbenches, their `generate.py`,
  `*-config.json` and `*_tb.sv`, plus `test.py` and `utils.sv`).
* `ordering-network-generator.py` — top-level CLI (`-o`, `-c`, `--hdl`).

`custom_core_gen` depends only on `core_gen.{emitters,signals,operators,ir}` —
never on `core_gen.generators` — so the drifted `lsq.py`/`group_allocator.py`
were genuinely not needed.

## 3. API adaptations made

1. **`Type.SIGNED` / `is_signed`** (`ir.py`, `signals.py`, both emitters).
   `custom_core_gen/generators/dependency_checker.py` declares six signed
   vectors (`access_disparity`, `inc/dec_access_disparity`, `ad_cmp`,
   `pq_length_cmp`). Ported onto the newer emitters as:
   * `vhdl_emitter.add_assignment` / `verilog_emitter.add_assignment` now take
     the target's type from `out.get_type()` instead of hard-coding
     `Type.LOGIC`;
   * `vhdl_emitter.fix_type` gained `SIGNED←LOGIC` (`signed(x)`) and
     `LOGIC←SIGNED` (`std_logic_vector(x)`) conversions;
   * both `bin_to_str`s promote `Type.ARITH` to `Type.SIGNED` when the
     assignment context or either operand is signed;
   * `logicvec_signal_init` emits `signed(N downto 0)` / `wire signed [N:0]`;
   * `int_to_str` renders `to_signed(v, n)` (VHDL) / a bare decimal (Verilog)
     in a signed context.
2. **Signed `WhenElse` (VHDL only, new fix).** `lsq-vhdl-verilog`'s
   `WhenElse.get_type()` deliberately returns `Type.LOGIC` and lets
   `when_else_to_str` convert each branch individually, because a VHDL
   conditional expression cannot sit inside a type conversion. With a signed
   target that produced the illegal `signed("01" when c else "00")`.
   `when_else_to_str` now honours a `Type.SIGNED` context per branch, and
   `add_assignment` skips the outer `fix_type` for a `WhenElse`. Output is now
   `to_signed(1, 2) when c else to_signed(0, 2)`. `custom-queues` got this for
   free from its (otherwise broken) `WhenElse.get_type()`; this achieves the
   same result without reintroducing that regression.
3. **Signed register reset values.** `*_reg_init` used
   `int_to_str(init, size)`, which raises for a negative `init`. A signed
   `access_disparity` resets to `-1` when `succCanExecuteOnce` is set, so both
   emitters gained `reset_value_str()` (`to_signed(-1, n)` / `-1`).
4. **`assigned_var_to_str` size for `(LogicVecArray, i)`** — see inventory.
5. **`generator.py` missing import.** `Generator._generate_master_interface`
   uses `CustomStatement` but the file never imported it (a latent `NameError`
   on `custom-queues` too). `from core_gen.ir import CustomStatement` added.
6. **`structure.py` nondeterministic port order.** `get_global_queue_ports()`
   returned a `set`, and the caller's iteration order decided the structure
   module's IO declaration order — so two runs of the same config emitted
   different (functionally equivalent) files. Now returns a list in the
   queue's own port order. Pre-existing on `custom-queues`; fixed here because
   it makes generated output unreproducible.

## 4. `CyclicRightShift` and the dependency checker's `check_mask`

`custom-queues`' `CyclicRightShift` did **not** propagate its `left=False` flag
through the recursion, so only the topmost rotate layer went right and every
lower layer went left. That is not a right rotate — but the topmost layer
rotates by `n/2`, and `+n/2 ≡ -n/2 (mod n)`, so the composition was in fact
**exactly a cyclic left rotate** for every distance.

This port:

* implements `CyclicRightShift` as a real right rotate (the flag is propagated);
* changes the one call site — `DependencyChecker._same_bb_check`'s `check_mask`
  — from `CyclicRightShift` to `CyclicLeftShift`. A left rotate is what that
  code's own comment asks for ("head-relative index `i` lives at physical entry
  `(pq_done + i) % n`", i.e. `check_mask[j] = ones[j - pq_done]`), and it
  reproduces `custom-queues`' generated RTL **byte-for-byte** in that region.

Keeping the naive port instead would have silently changed the emitted mask
(and does: the structure_simple testbench fails with a literal right rotate,
and passes with the left rotate — confirmed by simulation, see §6).

`CyclicRightShift` now has no in-tree caller. It is kept as the correct
counterpart of `CyclicLeftShift` and because it is part of the operator surface
`custom-queues` added.

## 5. Known limitations carried over (not regressions of this port)

* `QueueConfig` asserts `NumEntries` is a power of two — ordering-network
  queues cannot be non-power-of-2. (The **LSQ** generator does support
  non-power-of-2 queues; that path is exercised in §6.)
* `DependencyChecker.generate` asserts `dep_entry_ratio == 1`; a config with
  `"depEntryRatio": 2` raises `AssertionError: dep_entry_ratio must be 1: the
  dep array sizing in this generator … doesn't scale by it`. Deliberate and
  pre-existing.
* A testbench `generate.py` that clears `out/` with `Path.unlink()` fails with
  `IsADirectoryError` if a simulator left a `work/` directory there.
  `testbenches/test.py` handles this; the standalone scripts do not.
* LSQ queue size 1 is rejected upstream (`group 0: too many loads (1) for load
  queue with 1 entries!`) — an assertion that exists on `lsq-vhdl-verilog`
  before this port. Size-1 *ordering-network* queues do work.

## 6. Verification performed

Interpreter: `/home/bookelma/master-thesis/venv/bin/python` (3.12.3).
Run from `tools/backend/lsq-generator-python/`.

```
python -m compileall -q core_gen custom_core_gen ordering-network-generator.py lsq-generator.py
    -> clean

PYTHONPATH=$PWD python custom_core_gen/testbenches/{load_queue,store_queue,
    dependency_checker,structure_simple,structure_cross_group}/generate.py
    -> all 5 generate .v and .vhd without exceptions

python -m pytest custom_core_gen/testbenches/test.py -q     (with Questa on PATH)
    -> 15 passed   (5 x generate, 5 x Verilog sim, 5 x VHDL sim)

python ordering-network-generator.py -o OUT -c CFG --hdl {vhdl,verilog}
    for both testbench structure-config.json files and for 7 synthetic configs:
      size-1 queues (same-BB and cross-BB), succCanExecuteOnce, forcedSequential
      (same-BB and cross-BB), 16/8-entry queues, depEntryRatio=2
    -> all generate except depEntryRatio=2, which hits the documented assert

python lsq-generator.py --config-file CFG --hdl {vhdl,verilog}
    queue sizes 16/16, 5/3 (non-power-of-2), 2/2, 3/7, 9/5   -> all generate

vcom -2008 / vlog on every file generated above  -> zero errors

LSQ non-regression: generated 16/16, 5/3 and 2/2 LSQs (both HDLs) with
lsq-vhdl-verilog, then again after every step of this port
    -> byte-identical every time (`diff -r` clean)
```
