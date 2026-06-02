#!/usr/bin/env python3
import argparse
import os
import sys

# Ensure the package root is on the path when invoked from an arbitrary directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_gen.signals import Logic, LogicVec, LogicArray, LogicVecArray
from core_gen.emitters import Emitter, VerilogEmitter, VHDLEmitter
from core_gen.ir import Bit, CustomStatement
from custom_core_gen.configs import OrderingNetworkConfig
from custom_core_gen.generators.structure import Structure


class OrderingNetworkWrapper:
    """Wraps the ordering-network structure module with the Dynamatic LSQ interface.

    Ordering networks always connect through a memory controller (slave mode).
    Each circuit-facing load/store port gets its own dedicated MC channel so the
    compiler can wire them into the MC's indexed ldAddr/stAddr arrays.

    Signal name mappings (N = load index, M = store index):
      | Wrapper IO                          | Structure port                             |
      | ----------------------------------- | ------------------------------------------ |
      | io_ldAddr_N_(bits|valid|ready)      | circ_addr_(i|valid_i|ready_o)_q0_array_N   |
      | io_ldData_N_(bits|valid|ready)      | circ_data_(o|valid_o|ready_i)_q0_array_N   |
      | io_stAddr_M_(bits|valid|ready)      | circ_addr_(i|valid_i|ready_o)_q1_array_M   |
      | io_stData_M_(bits|valid|ready)      | circ_data_(i|valid_i|ready_o)_q1_array_M   |
      | io_stDataToMC_M_bits                | mem_data_o_q1_array_M                      |
      | io_stAddrToMC_M_bits                | mem_addr_o_q1_array_M                      |
      | io_ldDataFromMC_N_bits              | mem_data_i_q0_array_N                      |
      | io_ldAddrToMC_N_bits                | mem_addr_o_q0_array_N                      |
      | io_ctrl_G_(ready|valid)             | (always-ready, not forwarded to structure)  |
    """

    def __init__(self, path_rtl: str, config: OrderingNetworkConfig):
        self.output_folder = path_rtl
        self.config = config
        self.name = config.name
        self.core_name = config.name + "_core"

        # Assume all queues share the same data/addr/id widths
        self.dataW = config.queues[0].data_width
        self.addrW = config.queues[0].addr_width
        self.idW   = config.queues[0].id_width

        # Find the queue config index for load and store
        self.ld_q_idx = next(i for i, q in enumerate(config.queues) if q.q_type == "load")
        self.st_q_idx = next(i for i, q in enumerate(config.queues) if q.q_type == "store")

        # Count ports of each type and groups
        self.numLoads  = sum(1 for p in config.ports_to_queue if config.queues[p].q_type == "load")
        self.numStores = sum(1 for p in config.ports_to_queue if config.queues[p].q_type == "store")
        self.numGroups = len(set(config.port_bb_ids))
        assert self.numGroups == 1, "The ordering network only supports a single group for now"
        assert self.numLoads == 1, "The ordering network only supports a single load port for now"
        assert self.numStores == 1, "The ordering network only supports a single store port for now"

    # ------------------------------------------------------------------
    # Helper: build the mangled structure-module port name
    # ------------------------------------------------------------------

    def _sp(self, base: str, q_idx: int, within_group: int, direction: str) -> str:
        """Return the flattened structure-module port name.

        direction is '_i' for inputs, '_o' for outputs.
        """
        return f"{base}_q{q_idx}_array_{within_group}{direction}"

    # ------------------------------------------------------------------
    # Wrapper  (kernel -> ordering-network -> MC)
    # ------------------------------------------------------------------

    def genWrapperSlave(self, em: Emitter):
        em.clock_name = "clk"
        em.reset_name = "rst"

        # ---- Group control IOs (always-accept) ----
        io_ctrl_ready = LogicArray(em, "io_ctrl_ready", "o", self.numGroups, dyn_comp=True)
        LogicArray(em, "io_ctrl_valid", "i", self.numGroups, dyn_comp=True)

        # ---- Per-load circuit-facing IOs ----
        io_ldAddr_ready = LogicArray   (em, "io_ldAddr_ready", "o", self.numLoads,             dyn_comp=True)
        io_ldAddr_valid = LogicArray   (em, "io_ldAddr_valid", "i", self.numLoads,             dyn_comp=True)
        io_ldAddr_bits  = LogicVecArray(em, "io_ldAddr_bits",  "i", self.numLoads, self.addrW, dyn_comp=True)
        io_ldData_ready = LogicArray   (em, "io_ldData_ready", "i", self.numLoads,             dyn_comp=True)
        io_ldData_valid = LogicArray   (em, "io_ldData_valid", "o", self.numLoads,             dyn_comp=True)
        io_ldData_bits  = LogicVecArray(em, "io_ldData_bits",  "o", self.numLoads, self.dataW, dyn_comp=True)

        # ---- Per-store circuit-facing IOs ----
        io_stAddr_ready = LogicArray   (em, "io_stAddr_ready", "o", self.numStores,             dyn_comp=True)
        io_stAddr_valid = LogicArray   (em, "io_stAddr_valid", "i", self.numStores,             dyn_comp=True)
        io_stAddr_bits  = LogicVecArray(em, "io_stAddr_bits",  "i", self.numStores, self.addrW, dyn_comp=True)
        io_stData_ready = LogicArray   (em, "io_stData_ready", "o", self.numStores,             dyn_comp=True)
        io_stData_valid = LogicArray   (em, "io_stData_valid", "i", self.numStores,             dyn_comp=True)
        io_stData_bits  = LogicVecArray(em, "io_stData_bits",  "i", self.numStores, self.dataW, dyn_comp=True)

        # ---- Per-load MC-facing IOs ----
        # Named io_ldAddrToMC_N_bits etc. (dyn_comp inserts the index before last token)
        io_ldAddrToMC_bits    = LogicVecArray(em, "io_ldAddrToMC_bits",    "o", self.numLoads, self.addrW, dyn_comp=True)
        io_ldAddrToMC_valid   = LogicArray   (em, "io_ldAddrToMC_valid",   "o", self.numLoads,             dyn_comp=True)
        io_ldAddrToMC_ready   = LogicArray   (em, "io_ldAddrToMC_ready",   "i", self.numLoads,             dyn_comp=True)
        io_ldDataFromMC_bits  = LogicVecArray(em, "io_ldDataFromMC_bits",  "i", self.numLoads, self.dataW, dyn_comp=True)
        io_ldDataFromMC_valid = LogicArray   (em, "io_ldDataFromMC_valid", "i", self.numLoads,             dyn_comp=True)
        io_ldDataFromMC_ready = LogicArray   (em, "io_ldDataFromMC_ready", "o", self.numLoads,             dyn_comp=True)

        # ---- Per-store MC-facing IOs ----
        io_stAddrToMC_bits  = LogicVecArray(em, "io_stAddrToMC_bits",  "o", self.numStores, self.addrW, dyn_comp=True)
        io_stAddrToMC_valid = LogicArray   (em, "io_stAddrToMC_valid", "o", self.numStores,             dyn_comp=True)
        io_stAddrToMC_ready = LogicArray   (em, "io_stAddrToMC_ready", "i", self.numStores,             dyn_comp=True)
        io_stDataToMC_bits  = LogicVecArray(em, "io_stDataToMC_bits",  "o", self.numStores, self.dataW, dyn_comp=True)
        io_stDataToMC_valid = LogicArray   (em, "io_stDataToMC_valid", "o", self.numStores,             dyn_comp=True)
        io_stDataToMC_ready = LogicArray   (em, "io_stDataToMC_ready", "i", self.numStores,             dyn_comp=True)

        # ---- Per-load internal signals ----
        io_loadEn   = [Logic(em, f"io_loadEn_{i}",  "w")                  for i in range(self.numLoads)]
        wresp_valid = [Logic(em, f"wresp_valid_{i}", "w", force_reg=True) for i in range(self.numStores)]
        io_storeEn  = [Logic(em, f"io_storeEn_{i}", "w")                  for i in range(self.numStores)]
        mem_data_valid_st = [Logic(em, f"mem_data_valid_st_{i}", "w")              for i in range(self.numStores)]

        empty_ld = [Logic(em, f"empty_ld_{i}", "w") for i in range(self.numLoads)]
        empty_st = [Logic(em, f"empty_st_{i}", "w") for i in range(self.numStores)]

        # ---- io_ctrl: always accept ----
        for i in range(self.numGroups):
            em.add_assignment(io_ctrl_ready[i], Bit(1))

        # ---- Per-store register processes ----
        for i in range(self.numStores):
            em.add_comment(f"Process for wresp_valid_{i}")
            em.add_statement(em.get_reg_init_str())
            em.increase_indent()
            em.add_custom_statement(CustomStatement("if rst = '1' then", "if (rst) begin"))
            em.increase_indent()
            em.add_assignment(wresp_valid[i], Bit(0), in_process=True)
            em.decrease_indent()
            em.add_custom_statement(CustomStatement("elsif rising_edge(clk) then", "end\nelse begin"))
            em.increase_indent()
            em.add_custom_statement(CustomStatement(
                f"if {io_storeEn[i].getNameRead()} = '1' and {io_stAddrToMC_ready[i].getNameRead()} = '1' then",
                f"if ({io_storeEn[i].getNameRead()} && {io_stAddrToMC_ready[i].getNameRead()}) begin",
            ))
            em.increase_indent()
            em.add_assignment(wresp_valid[i], Bit(1), in_process=True)
            em.decrease_indent()
            em.add_custom_statement(CustomStatement("else", "end\nelse begin"))
            em.increase_indent()
            em.add_assignment(wresp_valid[i], Bit(0), in_process=True)
            em.decrease_indent()
            em.add_custom_statement(CustomStatement("end if;", "end"))
            em.decrease_indent()
            em.add_custom_statement(CustomStatement("end if;", "end"))
            em.decrease_indent()
            em.add_custom_statement(CustomStatement("end process;", "end"))

        # ---- Signal assignments ----
        em.add_comment("Signal Assignments")
        for i in range(self.numLoads):
            em.add_assignment(io_ldAddrToMC_valid[i], io_loadEn[i])
        for i in range(self.numStores):
            em.add_assignment(io_stAddrToMC_valid[i], io_storeEn[i])
            em.add_assignment(io_stDataToMC_valid[i], mem_data_valid_st[i])

        # ---- Instantiate structure module ----
        em.add_comment("Instantiate the ordering network structure")
        em.start_instantiation(self.core_name)
        em.add_map("rst", "rst")
        em.add_map("clk", "clk")

        ld_counter = 0
        st_counter = 0
        for q_idx in self.config.ports_to_queue:
            q_type = self.config.queues[q_idx].q_type
            if q_type == "load":
                i = ld_counter
                ld_counter += 1
                em.add_map(self._sp("circ_addr_i",       q_idx, i, "_i"), io_ldAddr_bits [i].getNameRead())
                em.add_map(self._sp("circ_addr_valid_i", q_idx, i, "_i"), io_ldAddr_valid[i].getNameRead())
                em.add_map(self._sp("circ_addr_ready_o", q_idx, i, "_o"), io_ldAddr_ready[i].getNameWrite())
                em.add_map(self._sp("circ_data_o",       q_idx, i, "_o"), io_ldData_bits [i].getNameWrite())
                em.add_map(self._sp("circ_data_valid_o", q_idx, i, "_o"), io_ldData_valid[i].getNameWrite())
                em.add_map(self._sp("circ_data_ready_i", q_idx, i, "_i"), io_ldData_ready[i].getNameRead())
                em.add_map(self._sp("mem_addr_o",        q_idx, i, "_o"), io_ldAddrToMC_bits   [i].getNameWrite())
                em.add_map(self._sp("mem_addr_valid_o",  q_idx, i, "_o"), io_loadEn            [i].getNameRead())
                em.add_map(self._sp("mem_data_i",        q_idx, i, "_i"), io_ldDataFromMC_bits [i].getNameRead())
                em.add_map(self._sp("mem_addr_ready_i",  q_idx, i, "_i"), io_ldAddrToMC_ready  [i].getNameRead())
                em.add_map(self._sp("mem_data_valid_i",  q_idx, i, "_i"), io_ldDataFromMC_valid[i].getNameRead())
                em.add_map(self._sp("mem_data_ready_o",  q_idx, i, "_o"), io_ldDataFromMC_ready[i].getNameWrite())
                em.add_map(self._sp("empty_o",           q_idx, i, "_o"), empty_ld[i].getNameRead())

            elif q_type == "store":
                i = st_counter
                st_counter += 1
                em.add_map(self._sp("circ_addr_i",       q_idx, i, "_i"), io_stAddr_bits [i].getNameRead())
                em.add_map(self._sp("circ_addr_valid_i", q_idx, i, "_i"), io_stAddr_valid[i].getNameRead())
                em.add_map(self._sp("circ_addr_ready_o", q_idx, i, "_o"), io_stAddr_ready[i].getNameWrite())
                em.add_map(self._sp("circ_data_i",       q_idx, i, "_i"), io_stData_bits [i].getNameRead())
                em.add_map(self._sp("circ_data_valid_i", q_idx, i, "_i"), io_stData_valid[i].getNameRead())
                em.add_map(self._sp("circ_data_ready_o", q_idx, i, "_o"), io_stData_ready[i].getNameWrite())
                em.add_map(self._sp("mem_addr_o",       q_idx, i, "_o"), io_stAddrToMC_bits[i].getNameWrite())
                em.add_map(self._sp("mem_addr_valid_o", q_idx, i, "_o"), io_storeEn        [i].getNameRead())
                em.add_map(self._sp("mem_data_o",       q_idx, i, "_o"), io_stDataToMC_bits[i].getNameWrite())
                em.add_map(self._sp("mem_addr_ready_i", q_idx, i, "_i"), io_stAddrToMC_ready[i].getNameRead())
                em.add_map(self._sp("mem_data_ready_i", q_idx, i, "_i"), io_stDataToMC_ready[i].getNameRead())
                em.add_map(self._sp("mem_data_valid_o", q_idx, i, "_o"), mem_data_valid_st[i].getNameWrite())
                em.add_map(self._sp("mem_exec_valid_i", q_idx, i, "_i"), wresp_valid[i].getNameRead())
                em.add_map(self._sp("mem_exec_ready_o", q_idx, i, "_o"))
                em.add_map(self._sp("empty_o",          q_idx, i, "_o"), empty_st[i].getNameRead())

        em.complete_instantiation()

        output_str = em.get_definition_str(self.name)
        with open(f"{self.output_folder}/{self.name}.{em.get_file_suffix()}", "w") as f:
            f.write(output_str)


# ===----------------------------------------------------------------------===#
# Argument parsing
# ===----------------------------------------------------------------------===#

parser = argparse.ArgumentParser()
parser.add_argument("-o", required=True, help="Output directory")
parser.add_argument("-c", required=True, help="Path to JSON config file")
parser.add_argument("--hdl", required=True, help="HDL target (vhdl/verilog)")
args = parser.parse_args()

if args.hdl == "verilog":
    em = VerilogEmitter()
elif args.hdl == "vhdl":
    em = VHDLEmitter()
else:
    raise ValueError(f"Unsupported HDL target: {args.hdl}")

config = OrderingNetworkConfig.from_json(args.c)

# Generate the structure core (named {name}_core to avoid clash with the wrapper)
structure = Structure(name=config.name + "_core", suffix="", configs=config)
structure.generate_from_json(em.new(), config, args.o)

# Generate the wrapper (ordering networks always connect through an MC)
wrapper = OrderingNetworkWrapper(args.o, config)
wrapper.genWrapperSlave(em)
