"""Move the `-- @region <name>` sections of generated VHDL into their own entities.

Measurement-only (LSQ_SPLIT_REGIONS=1, see Emitter.begin_region): the emitter
writes flat architectures, so a hierarchical utilization report can only
attribute area to modules the generator already instantiates. This pass turns
every region of an architecture into an entity of its own, instantiated in
place, without changing a single statement: each concurrent assignment,
process and instance keeps its text, and every register block of the
architecture's register process moves with the region that was current when
its regInit() was called. The result is the same circuit with more hierarchy.

A region's ports follow from what it reads and writes: it gets an input for
every signal it reads but does not drive, and an output for every signal it
drives that something outside it reads (or that is a port of the enclosing
entity). Inside the new entity the boundary signals keep their names and are
connected to `p_`-prefixed ports by plain assignments; clk and rst are passed
port to port, so the clock reaches every register without an extra delta
cycle. A signal driven from two regions (e.g. different bits of one vector)
is an error: the markers have to keep each signal inside one region.

Text-based on purpose, and only valid for the VHDL this emitter writes:
one statement per `;`, `signal <name> : <type>;` declarations, `entity
work.<name>` instantiations, and register processes made of top-level
`if ... end if;` blocks.
"""

import re
import sys
from pathlib import Path

MARKER = re.compile(r"^\s*-- @region (\S+)\s*$")
IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
PORT_DECL = re.compile(r"^\s*(\w+)\s*:\s*(in|out|inout)\s+(.+?);?\s*$")
SIGNAL_DECL = re.compile(r"^\s*signal\s+(\w+)\s*:\s*(.+?);\s*$")
INST_HEAD = re.compile(r"^\s*(\w+)\s*:\s*entity\s+work\.(\w+)\s*$")
MAP_ENTRY = re.compile(r"^\s*(\w+)\s*=>\s*(.+?),?\s*$")
LHS = re.compile(r"^\s*(\w+)\s*(\([^;]*?\))?\s*<=")
CLOCKS = ("clk", "rst")
LIBRARY = ("library IEEE;\nuse IEEE.std_logic_1164.all;\n"
           "use IEEE.numeric_std.all;\n\n")


def _strip_literals(text):
    text = re.sub(r"--.*", "", text)
    text = re.sub(r'"[^"]*"', '""', text)
    return re.sub(r"'.'", "''", text)


class Item:
    """One body element: its text, region, and the signals it reads/writes."""

    def __init__(self, kind, lines, region):
        self.kind, self.lines, self.region = kind, lines, region
        self.reads, self.writes = set(), set()


def _entity_ports(text):
    """{entity: [(name, dir, type)]} for every entity in `text`."""
    out = {}
    for m in re.finditer(r"^entity (\w+) is\n(.*?)^end entity;", text, re.M | re.S):
        ports = []
        for line in m.group(2).splitlines():
            pm = PORT_DECL.match(line)
            if pm:
                ports.append((pm.group(1), pm.group(2), pm.group(3).strip()))
        out[m.group(1)] = ports
    return out


def _parse_body(lines):
    """Split an architecture body into items, tagging each with its region."""
    items, region, i = [], None, 0
    while i < len(lines):
        line = lines[i]
        m = MARKER.match(line)
        if m:
            region = None if m.group(1) == "-" else m.group(1)
            i += 1
            continue
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("--"):
            items.append(Item("comment", [line], region))
            i += 1
            continue
        if INST_HEAD.match(line):
            j = i
            while lines[j].strip() != ");":
                j += 1
            items.append(Item("inst", lines[i:j + 1], region))
            i = j + 1
            continue
        if re.match(r"^\s*process\b", line):
            j = i
            while not lines[j].strip().startswith("end process"):
                j += 1
            block = lines[i:j + 1]
            if any(MARKER.match(b) for b in block):
                items.extend(_split_reg_process(block, region))
                # the process's last marker carries on into the statements after it
                for b in block:
                    mm = MARKER.match(b)
                    if mm:
                        region = None if mm.group(1) == "-" else mm.group(1)
            else:
                items.append(Item("process", block, region))
            i = j + 1
            continue
        j = i
        while not _strip_literals(lines[j]).rstrip().endswith(";"):
            j += 1
        items.append(Item("stmt", lines[i:j + 1], region))
        i = j + 1
    return items


def _split_reg_process(block, region):
    """The register process -> one 'reg' item per top-level if-block."""
    # block[0] = "process (...) is", block[1] = "begin", block[-1] = "end process;"
    assert block[1].strip() == "begin", block[:2]
    items, cur, depth = [], [], 0
    for line in block[2:-1]:
        m = MARKER.match(line)
        if m and depth == 0:
            region = None if m.group(1) == "-" else m.group(1)
            continue
        s = _strip_literals(line).strip()
        if not s:
            continue
        cur.append(line)
        if re.match(r"^if\b", s) and s.endswith("then"):
            depth += 1
        elif s.startswith("end if"):
            depth -= 1
        if depth == 0:
            items.append(Item("reg", cur, region))
            cur = []
    assert not cur and depth == 0, "unbalanced register process"
    return items


def _analyse(item, known, entity_ports):
    if item.kind == "comment":
        return
    if item.kind == "inst":
        target = INST_HEAD.match(item.lines[0]).group(2)
        dirs = {n: d for n, d, _ in entity_ports.get(target, [])}
        for line in item.lines[1:]:
            m = MAP_ENTRY.match(line)
            if not m:
                continue
            names = {t for t in IDENT.findall(_strip_literals(m.group(2))) if t in known}
            if dirs.get(m.group(1)) == "out":
                item.writes |= names
            else:
                item.reads |= names
        return
    for line in item.lines:
        text = _strip_literals(line)
        m = LHS.match(text)
        if m and (item.kind == "stmt" or not text.strip().startswith("if")):
            item.writes.add(m.group(1))
            text = text[m.end():] + (m.group(2) or "")
        item.reads |= {t for t in IDENT.findall(text) if t in known}
    item.writes &= known


def split_architecture(text, name, entity_ports, library):
    """Rewrite entity `name`'s architecture in `text`; returns new text."""
    arch_re = re.compile(
        rf"^architecture arch of {name} is\n(.*?)^begin\n(.*?)^end architecture;\n",
        re.M | re.S)
    am = arch_re.search(text)
    decl_text, body = am.group(1), am.group(2)
    if not any(MARKER.match(l) for l in body.splitlines()):
        return text, []

    decls = {}
    preamble = []
    for line in decl_text.splitlines():
        m = SIGNAL_DECL.match(line)
        if m:
            decls[m.group(1)] = m.group(2).strip()
        else:
            preamble.append(line)
    ports = {n: (d, t) for n, d, t in entity_ports[name]}
    known = set(decls) | set(ports)

    items = _parse_body(body.splitlines())
    for it in items:
        _analyse(it, known, entity_ports)

    regions = []
    for it in items:
        if it.region is not None and it.region not in regions:
            regions.append(it.region)

    # Who drives each signal: one region (None = the enclosing module).
    driver = {}
    for it in items:
        for w in it.writes:
            if w in driver and driver[w] != it.region:
                raise ValueError(
                    f"{name}: {w} is driven from regions {driver[w]!r} and {it.region!r}")
            driver[w] = it.region

    new_entities, inst_lines = [], []
    for r in regions:
        mine = [it for it in items if it.region == r]
        others = [it for it in items if it.region != r]
        w_r = set().union(*(it.writes for it in mine))
        rd_r = set().union(*(it.reads for it in mine))
        rd_out = set().union(*(it.reads for it in others)) | {
            p for p, (d, _) in ports.items() if d == "out"}
        used = w_r | rd_r
        inputs = sorted(n for n in rd_r - w_r if n not in CLOCKS)
        outputs = sorted(n for n in w_r & rd_out if n not in CLOCKS)
        clocks = [c for c in CLOCKS if c in used]

        def typ(n):
            return decls[n] if n in decls else ports[n][1]

        sub = f"{name}_r_{r}"
        port_lines = [f"\t\t{c} : in std_logic" for c in clocks]
        port_lines += [f"\t\tp_{n} : in {typ(n)}" for n in inputs]
        port_lines += [f"\t\tp_{n} : out {typ(n)}" for n in outputs]
        ent = [library.rstrip("\n"), "", f"entity {sub} is", "\tport("]
        ent.append(";\n".join(port_lines))
        ent += ["\t);", "end entity;", "", f"architecture arch of {sub} is"]
        ent += preamble
        ent += [f"\tsignal {n} : {typ(n)};" for n in sorted(used) if n not in CLOCKS]
        ent.append("begin")
        ent += [f"\t{n} <= p_{n};" for n in inputs]
        ent += [f"\tp_{n} <= {n};" for n in outputs]
        for it in mine:
            if it.kind != "reg":
                ent += it.lines
        regs = [it for it in mine if it.kind == "reg"]
        if regs:
            ent += ["\tprocess (clk, rst) is", "\tbegin"]
            for it in regs:
                ent += it.lines
            ent.append("\tend process;")
        ent += ["end architecture;", ""]
        new_entities.append("\n".join(ent) + "\n")

        maps = [f"\t\t\t{c} => {c}" for c in clocks]
        maps += [f"\t\t\tp_{n} => {n}" for n in inputs + outputs]
        inst_lines += [f"\tr_{r} : entity work.{sub}", "\t\tport map(",
                       ",\n".join(maps), "\t\t);"]

    body_out = []
    for it in items:
        if it.region is None and it.kind != "reg":
            body_out += it.lines
    body_out += inst_lines
    regs = [it for it in items if it.region is None and it.kind == "reg"]
    if regs:
        body_out += ["\tprocess (clk, rst) is", "\tbegin"]
        for it in regs:
            body_out += it.lines
        body_out.append("\tend process;")
    new_arch = (f"architecture arch of {name} is\n{decl_text}begin\n"
                + "\n".join(body_out) + "\nend architecture;\n")
    text = text[:am.start()] + new_arch + text[am.end():]
    # The region entities must be analysed before the entity that uses them.
    ent_start = re.search(rf"^entity {name} is$", text, re.M).start()
    insert_at = ent_start
    if text[ent_start - len(library):ent_start] == library:
        insert_at = ent_start - len(library)
    text = text[:insert_at] + "".join(new_entities) + text[insert_at:]
    return text, regions


def split_file(path: Path):
    text = path.read_text()
    if "-- @region" not in text:
        return []
    library = LIBRARY
    entity_ports = _entity_ports(text)
    done = []
    for name in list(entity_ports):
        text, regions = split_architecture(text, name, entity_ports, library)
        if regions:
            done.append((name, regions))
            entity_ports = _entity_ports(text)
    path.write_text(text)
    return done


def split_dir(out_dir):
    """Split every generated VHDL file in `out_dir` (no-op without markers)."""
    report = []
    for p in sorted(Path(out_dir).glob("*.vhd")):
        report += split_file(p)
    return report


if __name__ == "__main__":
    for name, regions in split_dir(sys.argv[1]):
        print(name, "->", ", ".join(regions))
