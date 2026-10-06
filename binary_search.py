#!/usr/bin/env python3
"""Find the smallest queue sizes of one benchmark's memory unit by binary search.

Run it from the root of a built dynamatic checkout:

    cd dynamatic
    python3 binary_search.py atax                     # ordering network
    python3 binary_search.py gemver --unit lsq        # classic LSQ
    python3 binary_search.py histogram --cycle-tol 0.01 --sizes 1 2 4 8 16

The benchmark (integration-test/<name>) is compiled once. Every configuration
then reuses that compile: write-hdl emits the memory unit's JSON config, this
script rewrites the queue depths in it, regenerates the unit's VHDL with the
generator in tools/backend/lsq-generator-python, and simulates. The netlist
around the unit does not depend on the depths, so nothing else is recompiled.

The search: simulate with every queue at its largest depth (the baseline), then
binary-search each queue in turn for the smallest depth whose cycle count stays
within --cycle-tol of the baseline, keeping the depths already found for the
earlier queues and the maximum for the later ones. A configuration that fails
to simulate counts as too small. It prints the depth found per queue and the
cycle count there.

Memory units:
  on   the ordering network (compile --use-ordering-network). Next to the
       queue depths it also searches the AccessDisparityWidth of every
       dependency checker whose two ports are in the same basic block, from 2
       up to the compiler's width (--no-ad-search keeps the compiler's).
  lsq  the classic load-store queue: one load and one store queue per LSQ.
       A queue must be deeper than the largest number of its accesses in one
       basic block, and at least 2.

Everything is written under bsearch/<benchmark>-<unit>/ (one directory per
configuration, with its log in run.log); a rerun reuses what is already there.
Simulation needs ModelSim/Questa, the compile Gurobi (as for dynamatic itself).
"""

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
DYNAMATIC = ROOT / "bin" / "dynamatic"
GENERATORS = ROOT / "tools" / "backend" / "lsq-generator-python"
GENERATOR = {"on": GENERATORS / "ordering-network-generator.py",
             "lsq": GENERATORS / "lsq-generator.py"}
TESTS = ROOT / "integration-test"
OUT = ROOT / "bsearch"

COMPILE = """set-dynamatic-path {root}
set-src {src}
set-clock-period {cp}
compile --buffer-algorithm {buffers}{flags} --milp-solver gurobi
exit
"""
WRITE_HDL = "set-dynamatic-path {root}\nset-src {src}\nwrite-hdl\nexit\n"
SIMULATE = "set-dynamatic-path {root}\nset-src {src}\nsimulate\nexit\n"


# ===----------------------------------------------------------------------===
# Running dynamatic
# ===----------------------------------------------------------------------===

def run(cmd: list, log: Path, timeout_min: float) -> int:
    """Run `cmd` from the dynamatic root, appending its output to `log`. On a
    timeout the whole process group is killed: the simulator is a grandchild
    of dynamatic and would otherwise keep running."""
    with open(log, "a") as f:
        f.write(f"\n$ {' '.join(map(str, cmd))}\n")
        f.flush()
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            return proc.wait(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            f.write(f"\n[TIMEOUT after {timeout_min} min]\n")
            return 124


def frontend(script: str, work: Path, name: str, log: Path, timeout_min: float,
             **fields) -> int:
    """Run one dynamatic frontend script on work/<name>.c."""
    path = work / "frontend.dyn"
    path.write_text(script.format(root=ROOT, src=work / f"{name}.c", **fields))
    return run([DYNAMATIC, "--run", path], log, timeout_min)


def unit_jsons(hdl: Path, unit: str) -> list:
    """The memory unit's JSON configs in out/hdl, in a stable order. Both units
    are emitted as handshake_lsq*.json; the ordering network's say so."""
    want = unit == "on"
    return [p for p in sorted(hdl.glob("handshake_lsq*.json"))
            if bool(json.loads(p.read_text()).get("isOrderingNetwork")) == want]


# ===----------------------------------------------------------------------===
# The search space: one coordinate per queue (and per same-BB checker's AD width)
# ===----------------------------------------------------------------------===

def describe(hdl: Path, unit: str, sizes: list, search_ad: bool):
    """[(label, json name, field, index, legal values)] per coordinate."""
    coords = []
    for path in unit_jsons(hdl, unit):
        data = json.loads(path.read_text())
        if unit == "lsq":
            for kind, counts in (("load", data.get("numLoads")),
                                 ("store", data.get("numStores"))):
                lowest = max(2, max(counts or [0]) + 1)
                coords.append((f"{path.name} {kind} queue", path.name, kind, None,
                               [s for s in sizes if s >= lowest]))
            continue
        for i, q in enumerate(data["queues"]):
            coords.append((f"{path.name} queue {i} ({q['QueueType']})", path.name,
                           "queue", i, list(sizes)))
        if not search_ad:
            continue
        bbs = data["portBBIds"]
        for i, (src, dst, dc) in enumerate(zip(data["edgeSrc"], data["edgeDst"],
                                               data["dependencyCheckers"])):
            if bbs[src] == bbs[dst]:  # only a same-BB checker has the counter
                coords.append((f"{path.name} checker {i} AccessDisparityWidth",
                               path.name, "ad", i,
                               list(range(2, int(dc["AccessDisparityWidth"]) + 1))))
    return coords


def resize(hdl: Path, unit: str, coords: list, key: tuple, bypass: bool) -> list:
    """Write `key` into the unit's JSONs in out/hdl; return the edited files."""
    paths = unit_jsons(hdl, unit)
    datas = {p.name: json.loads(p.read_text()) for p in paths}
    for (_, name, field, i, _), value in zip(coords, key):
        data = datas[name]
        if field == "queue":
            data["queues"][i]["NumEntries"] = value
        elif field == "ad":
            data["dependencyCheckers"][i]["AccessDisparityWidth"] = value
        else:
            data["fifoDepth_L" if field == "load" else "fifoDepth_S"] = value
    if unit == "lsq":
        for data in datas.values():
            data["fifoDepth"] = max(data["fifoDepth_L"], data["fifoDepth_S"])
            # The generator asserts idW >= ceil(log2(load depth)).
            data["indexWidth"] = math.ceil(math.log2(data["fifoDepth_L"]))
            if not bypass:
                data["bypassEn"] = 0
    for p in paths:
        p.write_text(json.dumps(datas[p.name], indent=2))
    return paths


# ===----------------------------------------------------------------------===
# One configuration
# ===----------------------------------------------------------------------===

def simulate(args, base: Path, coords: list, key: tuple):
    """Cycle count of one configuration, or None if it did not simulate."""
    tag = "-".join(map(str, key))
    work = OUT / f"{args.benchmark}-{args.unit}" / tag
    result = work / "cycles.txt"
    if result.exists():
        text = result.read_text().strip()
        return int(text) if text.isdigit() else None
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(base, work, ignore=shutil.ignore_patterns(
        "out", "*.dyn", "run.log"))
    shutil.copytree(base / "out" / "comp", work / "out" / "comp",
                    ignore=shutil.ignore_patterns("*.png"))
    log, hdl = work / "run.log", work / "out" / "hdl"

    cycles = None
    for _ in range(2):  # the simulator occasionally hangs; one retry
        shutil.rmtree(work / "out" / "hdl", ignore_errors=True)
        shutil.rmtree(work / "out" / "sim", ignore_errors=True)
        if frontend(WRITE_HDL, work, args.benchmark, log, args.sim_timeout):
            break
        # write-hdl emits the JSONs at the compiler's depths: resize, regenerate.
        # The LSQ generator emits VHDL only and rejects --hdl.
        hdl_flag = ["--hdl", "vhdl"] if args.unit == "on" else []
        if any(run([sys.executable, GENERATOR[args.unit], "-o", hdl, "-c", p,
                    *hdl_flag], log, 10)
               for p in resize(hdl, args.unit, coords, key, not args.no_bypass)):
            break
        if frontend(SIMULATE, work, args.benchmark, log, args.sim_timeout) == 0:
            report = work / "out" / "sim" / "report.txt"
            match = report.exists() and re.search(r"Latency\s*=\s*(\d+)",
                                                  report.read_text())
            cycles = int(match.group(1)) if match else None
            break
    result.write_text(f"{'' if cycles is None else cycles}\n")
    if not args.keep_out:
        shutil.rmtree(work / "out", ignore_errors=True)
    return cycles


# ===----------------------------------------------------------------------===
# Driver
# ===----------------------------------------------------------------------===

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("benchmark", help="integration-test name, e.g. atax")
    parser.add_argument("--unit", choices=["on", "lsq"], default="on",
                        help="memory unit to size (default: on)")
    parser.add_argument("--cycle-tol", type=float, default=0.05, metavar="FRAC",
                        help="allowed cycle increase over the all-max baseline, "
                             "as a fraction (default: 0)")
    parser.add_argument("--sizes", type=int, nargs="+", metavar="SIZE",
                        default=list(range(1, 17)),
                        help="depths a queue may take (default: 1..16)")
    parser.add_argument("--no-ad-search", action="store_true",
                        help="on: keep the compiler's AccessDisparityWidths")
    parser.add_argument("--no-bypass", action="store_true",
                        help="lsq: turn off store-to-load forwarding")
    parser.add_argument("--buffer-algorithm", default="fpl22",
                        help="default: fpl22")
    parser.add_argument("--clock-period", type=float, default=8.0, metavar="NS",
                        help="buffer-placement target (default: 8)")
    parser.add_argument("--no-ftd", action="store_true",
                        help="compile without --fast-token-delivery")
    parser.add_argument("--sim-timeout", type=float, default=15, metavar="MIN",
                        help="per write-hdl/simulation timeout (default: 15)")
    parser.add_argument("--keep-out", action="store_true",
                        help="keep each configuration's out/ directory")
    args = parser.parse_args()

    if not DYNAMATIC.exists() or not (TESTS / args.benchmark).is_dir():
        parser.error(f"run from a built dynamatic root (need {DYNAMATIC} and "
                     f"{TESTS / args.benchmark})")
    sizes = sorted(set(args.sizes))

    # Compile once.
    base = OUT / f"{args.benchmark}-{args.unit}" / "base"
    hdl = base / "out" / "hdl"
    if not unit_jsons(hdl, args.unit):
        print(f"Compiling {args.benchmark} ...", flush=True)
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        for f in [*(TESTS / args.benchmark).glob("*.c"),
                  *(TESTS / args.benchmark).glob("*.h")]:
            shutil.copy(f, base)
        flags = ("" if args.no_ftd else " --fast-token-delivery") \
            + (" --use-ordering-network" if args.unit == "on" else "")
        log = base / "run.log"
        if (frontend(COMPILE, base, args.benchmark, log, 60, cp=args.clock_period,
                     buffers=args.buffer_algorithm, flags=flags)
                or frontend(WRITE_HDL, base, args.benchmark, log, args.sim_timeout)):
            sys.exit(f"compile failed, see {log}")
        if not unit_jsons(hdl, args.unit):
            sys.exit(f"{args.benchmark} has no {args.unit} memory unit")

    coords = describe(hdl, args.unit, sizes, not args.no_ad_search)
    empty = [label for label, *_, legal in coords if not legal]
    if empty:
        sys.exit(f"no legal depth among {sizes} for: {', '.join(empty)}")
    print(f"{args.benchmark}, {args.unit}: {len(coords)} coordinates, "
          f"cycle tolerance {args.cycle_tol:.1%}")
    for label, *_, legal in coords:
        print(f"  {label}: {legal}")
    print(flush=True)

    # Coordinate-wise binary search from the all-max point.
    cache: dict = {}

    def cycles_of(key, what):
        """Simulate `key` (once), printing what is being tried and the outcome."""
        key = tuple(key)
        tag = "-".join(map(str, key))
        print(f"    {what:<12} [{tag}] ... ", end="", flush=True)
        if key in cache:
            print("(seen before) ", end="")
        else:
            cache[key] = simulate(args, base, coords, key)
        cycles = cache[key]
        print("FAILED" if cycles is None else f"{cycles} cycles", end="",
              flush=True)
        return cycles

    current = [legal[-1] for *_, legal in coords]
    print("Baseline, every coordinate at its maximum:")
    baseline = cycles_of(current, "all-max")
    print(flush=True)
    if baseline is None:
        sys.exit("the all-max configuration did not simulate; see its run.log "
                 f"under {OUT}")
    budget = math.floor(baseline * (1 + args.cycle_tol))
    print(f"Budget: {budget} cycles\n", flush=True)
    for i, (label, *_, legal) in enumerate(coords):
        print(f"[{i + 1}/{len(coords)}] {label}: searching {legal[0]}..{legal[-1]}",
              flush=True)
        lo, hi = 0, len(legal) - 1  # legal[hi] is known to meet the budget
        while lo < hi:
            mid = (lo + hi) // 2
            cycles = cycles_of(current[:i] + [legal[mid]] + current[i + 1:],
                               f"try {legal[mid]}")
            if cycles is not None and cycles <= budget:
                hi = mid
                print("  -> fits, go smaller", flush=True)
            else:
                lo = mid + 1
                print("  -> too small, go larger", flush=True)
        current[i] = legal[lo]
        print(f"    => {legal[lo]}\n", flush=True)

    width = max(len(label) for label, *_ in coords)
    print(f"{args.benchmark}, {args.unit}: {len(cache)} simulations")
    for (label, *_, legal), value in zip(coords, current):
        print(f"  {label:<{width}}  {value:>3}   (max {legal[-1]})")
    print(f"  cycles {cache[tuple(current)]}   (all-max baseline {baseline})")


if __name__ == "__main__":
    main()
