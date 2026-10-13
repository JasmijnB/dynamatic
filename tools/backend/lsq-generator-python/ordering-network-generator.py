#!/usr/bin/env python3
import argparse
import os
import sys
from collections import defaultdict

# Ensure the package root is on the path when invoked from an arbitrary directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_gen.signals import Logic, LogicArray, LogicVecArray
from core_gen.emitters import Emitter, VerilogEmitter, VHDLEmitter
from core_gen.ir import Bit
from custom_core_gen.configs import OrderingNetworkConfig
from custom_core_gen.generators.structure import Structure


class OrderingNetworkWrapper:
    """Wraps the ordering-network structure module with the Dynamatic LSQ interface.

    Ordering networks always connect through a memory controller (slave mode).
    Each circuit-facing load/store port gets its own dedicated MC channel so the
    compiler can wire them into the MC's indexed ldAddr/stAddr arrays.

    config.ports_to_queue maps each port to a queue config index, and more
    than one port may map to the same queue config (e.g. once identical
    configs get deduplicated on the C++ side). On the structure
    side, ports sharing a queue_config_idx are exposed as one flattened IO
    array per config, with each port's position in that array given by
    group_slot (see structure.py); this class recomputes the same group_slot
    per port, in the same port order, to build matching port names.

    Signal name mappings (N = load index, M = store index, Q = a port's queue
    config index i.e. config.ports_to_queue[port_idx], S = that port's
    group_slot within queue config Q):
      | Wrapper IO                          | Structure port                             |
      | ----------------------------------- | ------------------------------------------ |
      | io_ldAddr_N_(bits|valid|ready)      | circ_addr_(i|valid_i|ready_o)_qQ_array_S   |
      | io_ldData_N_(bits|valid|ready)      | circ_data_(o|valid_o|ready_i)_qQ_array_S   |
      | io_stAddr_M_(bits|valid|ready)      | circ_addr_(i|valid_i|ready_o)_qQ_array_S   |
      | io_stData_M_(bits|valid|ready)      | circ_data_(i|valid_i|ready_o)_qQ_array_S   |
      | io_stDataToMC_M_bits                | mem_data_o_qQ_array_S                      |
      | io_stAddrToMC_M_bits                | mem_addr_o_qQ_array_S                      |
      | io_ldDataFromMC_N_bits              | mem_data_i_qQ_array_S                      |
      | io_ldAddrToMC_N_bits                | mem_addr_o_qQ_array_S                      |
      | io_ctrl_G_(valid|ready)             | bb_(valid_G_i|ready_G_o) for cross-BB groups; else always-ready |
    """

    def __init__(self, path_rtl: str, config: OrderingNetworkConfig):
        self.output_folder = path_rtl
        self.config = config
        self.name = config.name
        self.core_name = config.name + "_core"

        # Assume all queues share the same data/addr widths
        self.dataW = config.queues[0].data_width
        self.addrW = config.queues[0].addr_width

        # Count ports of each type and groups
        self.numLoads = sum(
            1 for p in config.ports_to_queue if config.queues[p].q_type == "load"
        )
        self.numStores = sum(
            1 for p in config.ports_to_queue if config.queues[p].q_type == "store"
        )
        self.numGroups = len(set(config.port_bb_ids))

        # Group index in io_ctrl is the BB id directly (BBs are numbered
        # 0..numGroups-1, matching the LSQ group_init convention). Only BBs that
        # take part in a cross-BB dependency checker get bb_valid/bb_ready ports
        # on the structure module (mirrors Structure._route_bb_ports); the other
        # groups stay always-ready.
        self.cross_bb_ids = set()
        for src_port, dst_port in zip(config.edge_src, config.edge_dst):
            src_bb = config.port_bb_ids[src_port]
            dst_bb = config.port_bb_ids[dst_port]
            if src_bb != dst_bb:
                self.cross_bb_ids.add(src_bb)
                self.cross_bb_ids.add(dst_bb)

    # ------------------------------------------------------------------
    # Wrapper  (kernel -> ordering-network -> MC)
    # ------------------------------------------------------------------

    def genWrapperSlave(self, em: Emitter):
        em.clock_name = "clk"
        em.reset_name = "rst"

        # ---- Group control IOs (always-accept) ----
        io_ctrl_ready = LogicArray(
            em, "io_ctrl_ready", "o", self.numGroups, dyn_comp=True
        )
        io_ctrl_valid = LogicArray(
            em, "io_ctrl_valid", "i", self.numGroups, dyn_comp=True
        )

        # ---- Per-load circuit-facing IOs ----
        io_ldAddr_ready = LogicArray(
            em, "io_ldAddr_ready", "o", self.numLoads, dyn_comp=True
        )
        io_ldAddr_valid = LogicArray(
            em, "io_ldAddr_valid", "i", self.numLoads, dyn_comp=True
        )
        io_ldAddr_bits = LogicVecArray(
            em, "io_ldAddr_bits", "i", self.numLoads, self.addrW, dyn_comp=True
        )
        io_ldData_ready = LogicArray(
            em, "io_ldData_ready", "i", self.numLoads, dyn_comp=True
        )
        io_ldData_valid = LogicArray(
            em, "io_ldData_valid", "o", self.numLoads, dyn_comp=True
        )
        io_ldData_bits = LogicVecArray(
            em, "io_ldData_bits", "o", self.numLoads, self.dataW, dyn_comp=True
        )

        # ---- Per-store circuit-facing IOs ----
        io_stAddr_ready = LogicArray(
            em, "io_stAddr_ready", "o", self.numStores, dyn_comp=True
        )
        io_stAddr_valid = LogicArray(
            em, "io_stAddr_valid", "i", self.numStores, dyn_comp=True
        )
        io_stAddr_bits = LogicVecArray(
            em, "io_stAddr_bits", "i", self.numStores, self.addrW, dyn_comp=True
        )
        io_stData_ready = LogicArray(
            em, "io_stData_ready", "o", self.numStores, dyn_comp=True
        )
        io_stData_valid = LogicArray(
            em, "io_stData_valid", "i", self.numStores, dyn_comp=True
        )
        io_stData_bits = LogicVecArray(
            em, "io_stData_bits", "i", self.numStores, self.dataW, dyn_comp=True
        )

        # ---- Per-load MC-facing IOs ----
        # Named io_ldAddrToMC_N_bits etc. (dyn_comp inserts the index before last token)
        io_ldAddrToMC_bits = LogicVecArray(
            em, "io_ldAddrToMC_bits", "o", self.numLoads, self.addrW, dyn_comp=True
        )
        io_ldAddrToMC_valid = LogicArray(
            em, "io_ldAddrToMC_valid", "o", self.numLoads, dyn_comp=True
        )
        io_ldAddrToMC_ready = LogicArray(
            em, "io_ldAddrToMC_ready", "i", self.numLoads, dyn_comp=True
        )
        io_ldDataFromMC_bits = LogicVecArray(
            em, "io_ldDataFromMC_bits", "i", self.numLoads, self.dataW, dyn_comp=True
        )
        io_ldDataFromMC_valid = LogicArray(
            em, "io_ldDataFromMC_valid", "i", self.numLoads, dyn_comp=True
        )
        io_ldDataFromMC_ready = LogicArray(
            em, "io_ldDataFromMC_ready", "o", self.numLoads, dyn_comp=True
        )

        # ---- Per-store MC-facing IOs ----
        io_stAddrToMC_bits = LogicVecArray(
            em, "io_stAddrToMC_bits", "o", self.numStores, self.addrW, dyn_comp=True
        )
        io_stAddrToMC_valid = LogicArray(
            em, "io_stAddrToMC_valid", "o", self.numStores, dyn_comp=True
        )
        io_stAddrToMC_ready = LogicArray(
            em, "io_stAddrToMC_ready", "i", self.numStores, dyn_comp=True
        )
        io_stDataToMC_bits = LogicVecArray(
            em, "io_stDataToMC_bits", "o", self.numStores, self.dataW, dyn_comp=True
        )
        io_stDataToMC_valid = LogicArray(
            em, "io_stDataToMC_valid", "o", self.numStores, dyn_comp=True
        )
        io_stDataToMC_ready = LogicArray(
            em, "io_stDataToMC_ready", "i", self.numStores, dyn_comp=True
        )

        # ---- Internal signals ----
        io_loadEn = [Logic(em, f"io_loadEn_{i}", "w") for i in range(self.numLoads)]
        # A store's write response: one cycle after the MC accepts its address.
        wresp_valid = [Logic(em, f"wresp_valid_{i}", "r") for i in range(self.numStores)]
        io_storeEn = [Logic(em, f"io_storeEn_{i}", "w") for i in range(self.numStores)]
        mem_data_valid_st = [
            Logic(em, f"mem_data_valid_st_{i}", "w") for i in range(self.numStores)
        ]

        # ---- io_ctrl: groups without a cross-BB dependency always accept; the
        # rest are driven by the structure's bb_ready ports during instantiation.
        for i in range(self.numGroups):
            if i not in self.cross_bb_ids:
                em.add_assignment(io_ctrl_ready[i], Bit(1))

        for i in range(self.numStores):
            em.add_assignment(wresp_valid[i], io_storeEn[i] & io_stAddrToMC_ready[i])
            wresp_valid[i].regInit(init=0)

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

        # ---- Group (BB) handshake: forward io_ctrl to the structure's cross-BB
        # bb_valid/bb_ready ports. io_ctrl index == BB id (see __init__).
        for bb_id in sorted(self.cross_bb_ids):
            em.add_map(f"bb_valid_{bb_id}_i", io_ctrl_valid[bb_id].getNameRead())
            em.add_map(f"bb_ready_{bb_id}_o", io_ctrl_ready[bb_id].getNameWrite())

        # Each port's structure ports (by base name, its direction the last
        # two characters) and the wrapper signal they connect to (None: open).
        def load_ports(i):
            return [
                ("circ_addr_i", io_ldAddr_bits[i]), ("circ_addr_valid_i", io_ldAddr_valid[i]),
                ("circ_addr_ready_o", io_ldAddr_ready[i]), ("circ_data_o", io_ldData_bits[i]),
                ("circ_data_valid_o", io_ldData_valid[i]),
                ("circ_data_ready_i", io_ldData_ready[i]),
                ("mem_addr_o", io_ldAddrToMC_bits[i]), ("mem_addr_valid_o", io_loadEn[i]),
                ("mem_data_i", io_ldDataFromMC_bits[i]),
                ("mem_addr_ready_i", io_ldAddrToMC_ready[i]),
                ("mem_data_valid_i", io_ldDataFromMC_valid[i]),
                ("mem_data_ready_o", io_ldDataFromMC_ready[i]), ("empty_o", None),
            ]

        def store_ports(i):
            return [
                ("circ_addr_i", io_stAddr_bits[i]), ("circ_addr_valid_i", io_stAddr_valid[i]),
                ("circ_addr_ready_o", io_stAddr_ready[i]), ("circ_data_i", io_stData_bits[i]),
                ("circ_data_valid_i", io_stData_valid[i]),
                ("circ_data_ready_o", io_stData_ready[i]),
                ("mem_addr_o", io_stAddrToMC_bits[i]), ("mem_addr_valid_o", io_storeEn[i]),
                ("mem_data_o", io_stDataToMC_bits[i]),
                ("mem_addr_ready_i", io_stAddrToMC_ready[i]),
                ("mem_data_ready_i", io_stDataToMC_ready[i]),
                ("mem_data_valid_o", mem_data_valid_st[i]),
                ("mem_exec_valid_i", wresp_valid[i]), ("mem_exec_ready_o", None),
                ("empty_o", None),
            ]

        # Position of each port within the array of ports sharing its queue
        # config (config.ports_to_queue need not be a bijection: several ports
        # may point at the same queue_config_idx). Must match structure.py's
        # group_counters exactly, so iterate ports_to_queue in the same order.
        group_counters = defaultdict(int)
        type_counters = defaultdict(int)
        for q_idx in self.config.ports_to_queue:
            group_slot = group_counters[q_idx]
            group_counters[q_idx] += 1
            q_type = self.config.queues[q_idx].q_type
            i = type_counters[q_type]
            type_counters[q_type] += 1
            for base, sig in (load_ports if q_type == "load" else store_ports)(i):
                name = f"{base}_q{q_idx}_array_{group_slot}{base[-2:]}"
                if sig is None:
                    em.add_map(name)
                else:
                    em.add_map(name, sig.getNameRead() if base.endswith("_i")
                               else sig.getNameWrite())

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

# Measurement only: move the queues' and checkers' @region sections into entities.
if os.environ.get("LSQ_SPLIT_REGIONS") == "1":
    assert args.hdl == "vhdl", "LSQ_SPLIT_REGIONS needs --hdl vhdl"
    from core_gen.split_regions import split_dir
    split_dir(args.o)
