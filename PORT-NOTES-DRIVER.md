# Port notes: driver / backend-config / test-harness slice

Rebase of the `custom-queues` branch onto current `main`
(worktree `cq-driver`, branch `cq-rebase-driver`, which starts from
`lsq-vhdl-verilog` = main + reworked Python LSQ generator).

Merge base with main: `66accbd9` ("$BASE"). Reference diffs were taken with
`git diff 66accbd9 custom-queues -- <path>`.

These changes **cannot be compiled** here: they reference C++/pass/RTL symbols
that a parallel agent is porting under `include/` and `lib/`. See
"Cross-boundary symbols" at the bottom.

---

## `data/rtl-config-vhdl.json`

**custom-queues added.** A second `handshake.lsq` RTL component entry, placed
*before* each existing one, guarded by a new boolean RTL parameter
`isOrderingNetwork` constrained with `"eq": true`. That entry runs
`ordering-network-generator.py` instead of `lsq-generator.py`. It also added an
explicit `--hdl vhdl` to the existing LSQ generator command lines.

**How main drifted.** The file still has the same *two* `handshake.lsq`
entries (line ~60 and line ~377 on main; the second is effectively dead since
both have identical match criteria — `name` + `hdl` — so the first always
wins). Everything around them changed, but the entries themselves are byte
identical to `$BASE`.

**Resolution.** Inserted the ordering-network entry before *both* LSQ entries,
matching custom-queues, and added the explicit (no-op, it is the generator's
default) `--hdl vhdl` to both LSQ generator commands.

**Judgement calls.**
- custom-queues' *second* ordering-network entry pointed at
  `$DYNAMATIC/tools/backend/ordering-network-generator-python/ordering-network-generator.py`.
  That directory does not exist on `custom-queues` (`git ls-tree -r custom-queues
  tools/backend/` shows only `lsq-generator-python/`). It is a typo in a dead
  entry. I normalized both entries to
  `tools/backend/lsq-generator-python/ordering-network-generator.py`.
- custom-queues' second ordering-network entry used
  `io-map: [{ "clk": "clock" }, { "rst": "reset" }, ...]`, copied from the LSQ
  entry. The ordering-network generator emits `clk`/`rst` ports
  (`ordering-network-generator.py`: `em.clock_name = "clk"` / `em.reset_name =
  "rst"`, `em.add_map("clk", "clk")`), whereas `lsq-generator.py` emits
  `clock`/`reset`. I normalized both ordering-network entries to
  `{ "clk": "clk" }, { "rst": "rst" }`, i.e. custom-queues' *first* entry's
  mapping, which is the correct one.
- I kept the duplicated (dead) second entry rather than deduplicating, to stay
  close to both branches and avoid an unrelated cleanup.

## `data/rtl-config-verilog.json`

**custom-queues added.** The same ordering-network entry (with `--hdl verilog`
and `hdl: "verilog"`), plus two fixes to the existing Verilog LSQ entry: add
`--hdl verilog` to the generator command, and change the entry's `"hdl"` from
`"vhdl"` to `"verilog"`.

**How main drifted.** Main (via `9bad2c77`, the LSQ-generator rework already on
this branch) already added `--hdl verilog` to the generator command, but left
`"hdl": "vhdl"` on the entry.

**Resolution.** Added the ordering-network entry; kept main's already-correct
generator command; applied the remaining `"hdl": "vhdl"` -> `"hdl": "verilog"`
fix.

**Judgement call / flag for review.** The `"hdl"` field only drives the file
extension the backend expects (`getHDLExtension` in `lib/Support/RTL/RTL.cpp`:
`vhdl` -> `.vhd`, `verilog` -> `.v`). Since the generator is invoked with
`--hdl verilog` it writes `.v`, so `"verilog"` is right and main's `"vhdl"`
looks like a leftover. I have not been able to run the flow to confirm.

## `tools/dynamatic/dynamatic.cpp`

**custom-queues added.** A `use-ordering-network` frontend flag on the
`compile` command, forwarded as an extra positional argument to `compile.sh`.

**How main drifted.** The `Compile` command grew several new flags/options
(`k-induction`, `speculation`, `enable-short-circuit`, `enable-duplication`,
`calculate-path-delays`, `instrument-ii`), so the `execCmd` argument list went
from 13 to 19 positional arguments.

**Resolution.** Added `USE_ORDERING_NETWORK` as the last flag constant, its
`addFlag` registration last, and `useOrderingNetwork` as positional argument
**20** (custom-queues used 14). Re-wrapped the `execCmd` call the way
`clang-format` wants it.

## `tools/dynamatic/scripts/compile.sh`

**custom-queues added.**
1. `USE_ORDERING_NETWORK=${14}` plus a `REPLACE_MEM_IFACES` variable that
   selects `--handshake-replace-memory-interfaces=use-ordering-network=true`
   vs. the bare pass.
2. Substituted that variable into the two `dynamatic-opt` invocations
   (straight-to-queue branch and normal branch).
3. A new `export_mem_dot` helper (calls `export-dot --mem-dep`, then `dot
   -Tpng`) and a call to it next to `export_dot`.

**How main drifted.**
- The argument list grew to 19 positionals.
- Main already renamed `--handshake-analyze-lsq-usage` to
  `--handshake-deactivate-mem-dependencies` in both invocations (upstreamed as
  part of #926), so custom-queues' half of that rename was already there.
- custom-queues' diff also deleted three explanatory comments inside
  `export_dot`; main kept them.

**Resolution.** `USE_ORDERING_NETWORK=${20}`. Only the
`--handshake-replace-memory-interfaces` token was replaced by
`$REPLACE_MEM_IFACES`; main's `--handshake-deactivate-mem-dependencies` was
left as is. Kept main's comments in `export_dot` and gave `export_mem_dot` the
same comment style (the comment deletions in custom-queues were incidental).

## `tools/export-dot/export-dot.cpp`

**custom-queues added.** A `--mem-dep` boolean option and a
`getMemDepDOTGraph()` that renders the memory dependency graph: one node per
`handshake::LoadOp`/`StoreOp` clustered by basic block, one edge per
`MemDependenceAttr`, labelled `RAW/WAR/WAW/RAR d=<distance> l=<loopDepth>`.
It also renamed the `handshake::LSQOp` case in `getPrettyNodeLabel` to
`handshake::MemOrderingUnitOp`.

**How main drifted.** Essentially not at all in the touched regions — the
includes, the option block, the `TypeSwitch` in `getPrettyNodeLabel`, and
`main()`'s tail are all unchanged from `$BASE`. The port is effectively
verbatim.

**Verified against main's APIs (read-only):**
`DOTGraph::Builder::{addNode,addEdge,addSubgraph,getRoot}` and
`WithAttributes::addAttr` signatures in `include/dynamatic/Support/DOT.h`
match; `getDialectAttr<Attr>` in `include/dynamatic/Support/Attribute.h`
matches; `MemDependenceAttr::{getDstAccess,getLoopDepth,getDistance}` and
`MemDependenceArrayAttr::getDependencies` exist in
`include/dynamatic/Dialect/Handshake/HandshakeAttributes.td`;
`NameAnalysis::getOp(StringRef)` and `getUniqueName(Operation*)` exist;
`getLogicBB(Operation*)` returns `std::optional<unsigned>`.
`clang-format --style=file` reports the file clean.

## `tools/integration/util.cpp`, `tools/integration/util.h`

**custom-queues added.**
- `bool useOrderingNetwork` on `IntegrationTestData` and the
  `--use-ordering-network` flag in the generated `.dyn` script.
- In `runSpecIntegrationTest`: `--handshake-analyze-lsq-usage` ->
  `--handshake-deactivate-mem-dependencies`, and two extra `runSubprocess`
  calls producing `<name>_mem_dep.dot` / `.png`.

**How main drifted.** **Both files were deleted upstream** by commit
`89235aec` ("[ci] separate output directory for different fixtures", #964),
which folded their contents into `TEST_SUITE.cpp` as an `IntegrationTest`
struct with a `run()` member (replacing the free `runIntegrationTest`), and
added a `testName` field used to give each fixture its own output directory.
Separately, `runSpecIntegrationTest` was removed entirely by `fdb165e6`
("[Speculation] Switch to automatic buffering and normal CI flow", #923) —
speculation now goes through the normal `compile`/`simulate` flow.

**Resolution.** The two files are *not* recreated. The `useOrderingNetwork`
field and the `--use-ordering-network` flag were re-expressed on main's
`IntegrationTest` struct in `TEST_SUITE.cpp`. The two `runSpecIntegrationTest`
changes were **dropped as obsolete**:
- the pass rename is already in `compile.sh` on main;
- the mem-dep DOT/PNG export is now produced for *every* test by the
  `export_mem_dot` call added to `compile.sh`, which is the path all fixtures
  (including spec) take.

## `tools/integration/TEST_SUITE.cpp`

**custom-queues added.** `OrderingNetworkFixture` / `OrderingNetworkMemoryFixture`
classes, their `TEST_P` bodies, and two `INSTANTIATE_TEST_SUITE_P` blocks (29
misc benchmarks with dependency edges; 15 memory benchmarks containing an LSQ).

**How main drifted.** Heavy restructuring (see above): `IntegrationTestData` +
`runIntegrationTest(config)` became `struct IntegrationTest` + `config.run()`,
with new fields `testName`, `useSpeculation`, `useDuplication`, `clockPeriod`,
`instrumentII`, a `simReportPath()` helper, an `IIMonitorFixture`, and
`BaseFixture::getVerboseOutdirSuffix()` which every fixture now passes as
`.testName`.

**Resolution.** Re-expressed in main's structure:
- `useOrderingNetwork` added to `IntegrationTest` (placed after
  `useDuplication`, before `milpSolver`, so the designated initializers in the
  new `TEST_P` bodies stay in declaration order);
- `--use-ordering-network` appended to the `compile` line in
  `IntegrationTest::run()`;
- both fixtures declared next to `VerifyInvariantsFixture`;
- both `TEST_P` bodies use `config.run()` instead of
  `runIntegrationTest(config)` and gained `.testName = getVerboseOutdirSuffix()`
  so they get their own output directory (main's convention; without it
  `IntegrationTest::run()` asserts `testName.size() > 0`);
- the two benchmark lists are copied verbatim, placed inside the file's
  `// clang-format off` region just before the `DYNAMATIC_ENABLE_CBC` block.

**Notes.**
- All 29 misc and 15 memory benchmark directories named in the lists still
  exist under `integration-test/` on main (checked).
- ctest cases are auto-discovered via `gtest_discover_tests`, so no CMake or
  `.github/workflows/ci.yml` registration is needed. The test bodies are named
  `basic`, not `*_NoCI`, so these 44 new tests **will run in CI**
  (`run-ci-integration-tests` excludes only `_NoCI`). That matches
  custom-queues' intent, but it roughly doubles the memory-test CI time; worth
  a deliberate decision before merging.

## `integration-test/{if_convert,loop_path,nested_loop,single_loop}/cf.mlir`

**No change needed.** custom-queues' 2-line change was the
`#handshake<deps[["store0", 0, 0]]>` -> `#handshake<deps[{dstAccess : ...,
isActive : true}]>` assembly-syntax update. Main upstreamed exactly this in
`3a11eeb8` (#926).

- `integration-test/if_convert/cf.mlir` and `integration-test/loop_path/cf.mlir`
  are **byte-identical** to `custom-queues`' versions
  (`diff <(git show custom-queues:<path>) <path>` -> no output).
- `integration-test/nested_loop/cf.mlir` and
  `integration-test/single_loop/cf.mlir` **no longer exist on main**; those
  directories now only hold the `.c`/`.h`/`results.md` files.

---

## Cross-boundary symbols this slice depends on

Nothing in this slice compiles or runs until the `include/`+`lib/`+python
slices land. Exact dependencies:

| Symbol / name | Kind | Used by | Owner |
| --- | --- | --- | --- |
| `handshake::MemOrderingUnitOp` (rename of `handshake::LSQOp`) | MLIR op C++ class | `tools/export-dot/export-dot.cpp`, `getPrettyNodeLabel` TypeSwitch case | dialect agent (`include/dynamatic/Dialect/Handshake/HandshakeOps.td`) |
| `use-ordering-network` option on the `handshake-replace-memory-interfaces` pass (`bool`, default `false`) | pass option | `tools/dynamatic/scripts/compile.sh` (`$REPLACE_MEM_IFACES`) | transforms agent (`include/dynamatic/Transforms/Passes.td`, `lib/Transforms/HandshakeReplaceMemoryInterfaces.cpp`) |
| RTL parameter `isOrderingNetwork` (boolean) on `handshake.lsq` external modules | HW-lowering-emitted RTL parameter | `data/rtl-config-vhdl.json`, `data/rtl-config-verilog.json` (`"eq": true` match) | HandshakeToHW agent (`lib/Conversion/HandshakeToHW/HandshakeToHW.cpp`, `addBoolean("isOrderingNetwork", ...)`) |
| `tools/backend/lsq-generator-python/ordering-network-generator.py` (`-o`, `-c`, `--hdl` required) | script | both `data/rtl-config-*.json` generator commands | python-generator agent |

Symbols listed in the task brief that this slice **does not** use, and
therefore does not depend on: `handshake::MemOrderingKind`,
`MemInterfaceAttr`'s `group`/`orderingKind`, `OrderingNetworkInfo`,
`QueueConfig`, `DependencyCheckerConfig`.

## Verification actually run

| Check | Command | Result |
| --- | --- | --- |
| VHDL RTL config is valid JSON | `python3 -m json.tool data/rtl-config-vhdl.json` | passed |
| Verilog RTL config is valid JSON | `python3 -m json.tool data/rtl-config-verilog.json` | passed |
| compile.sh syntax | `bash -n tools/dynamatic/scripts/compile.sh` | passed |
| formatting | `clang-format --style=file <file> \| diff - <file>` on the three C++ files | `export-dot.cpp` and `TEST_SUITE.cpp` clean; `dynamatic.cpp` clean apart from 4 pre-existing `){}` vs `) {}` hunks caused by the local clang-format version, untouched by this port |
| benchmark dirs referenced by the new fixtures exist | `test -d integration-test/<name>` for all 44 | all present |

**Not verified:** nothing was compiled or executed (no build is possible until
the `include/`/`lib/` slice lands), so the C++ additions are reviewed by
reading only. The `--hdl`/`"hdl"` change in `rtl-config-verilog.json` and the
`clk`/`rst` io-map normalisation in `rtl-config-vhdl.json` are reasoned from
the generator sources, not observed end to end.
