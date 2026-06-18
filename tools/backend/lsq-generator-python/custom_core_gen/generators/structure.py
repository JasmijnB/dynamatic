#! /usr/bin/env python3

from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.ir import BinOp, Bit, reduce_bin
from custom_core_gen.configs import OrderingNetworkConfig
from custom_core_gen.generators.queue import Queue
from custom_core_gen.generators.dependency_checker import DependencyChecker
from custom_core_gen.generators.generator import Generator
import enum
from copy import copy
from collections import defaultdict
from functools import reduce

DC_TO_PQ_MAP = {
    "pq_addr_i": "q_addr_o",
    "pq_done_i": "done_ptr_o",
    "pq_done_en_i": "done_en_o",
    "pq_length_i": "length_o",
    "allow_pq_access_o": "allow_access_i",
}


DC_TO_SQ_MAP = {
    "sq_head_i": "queue_head_o",
    "sq_access_en_i": "access_en_o",
    "allow_sq_access_o": "allow_access_i",
}

# DC ports that are handled by structure.py's BB routing rather than the queue maps.
CROSS_BB_DC_PORTS = {"pq_bb_valid_i", "pq_bb_ready_o", "sq_bb_valid_i", "sq_bb_ready_o"}


def get_global_queue_ports(queue_def):
    dc_ports = set(DC_TO_SQ_MAP.values()) | set(DC_TO_PQ_MAP.values())
    queue_ports = set(queue_def.get_ports().keys())
    return queue_ports - dc_ports


def make_connecting_signal(signal, em, appended_name=""):
    c_signal = copy(signal)
    c_signal.em = em
    c_signal.name = em.get_temp(f"{signal.name}{"_" if appended_name != "" else ''}{appended_name}")
    em.use_temp()
    c_signal.type = "w"
    c_signal.force_reg = False
    c_signal.signalInit()

    return c_signal


class QueueInstance:
    def __init__(self, q_def, num: int, q_type: str):
        self.q_def = q_def
        self.num = num
        self.q_type = q_type
        self.port_vars = defaultdict(list)
        self.successors = []

    def init_port_vars(self, universal_vars, group_slot):
        # group_slot is this queue's position within its config's universal
        # arrays (those are sized per config). It is only an array offset, not
        # the queue's identity: self.num is globally unique across all queues.
        for port, signal in universal_vars.items():
            self.port_vars[port] = [signal[group_slot]]

    def instantiate(self, em, defaults):
        # transform port_vars
        port_signals = {}

        # load the defaults into the port vars
        for port_name in defaults:
            if port_name not in self.port_vars or len(self.port_vars[port_name]) == 0:
                port_signals[port_name] = defaults[port_name]

        for port_name, signal_list in self.port_vars.items():
            # if no signals are connected, connect to the defaults if it exists
            if len(signal_list) == 0:
                raise Exception(
                    f"No signal mapped to port {port_name} of queue {self.num} and no default provided"
                )
            elif len(signal_list) == 1:
                out = signal_list[0]
            else:
                out = make_connecting_signal(signal_list[0], em)
                # reduce the signal list with an and (since in order to allow access/alloc, all predecessors must allow access/alloc)
                em.add_assignment(out, reduce_bin(BinOp.AND, signal_list))

            port_signals[port_name] = out

        self.q_def.instantiate(em, port_signals, f"queue_{self.num}_{self.q_type}")


class DependencyCheckerInstance:
    def __init__(
        self,
        dp_def,
        pred: QueueInstance,
        succ: QueueInstance,
        pq_bb: int,
        sq_bb: int,
    ):
        self.pred = pred
        self.succ = succ
        self.ports = dp_def.get_ports()
        self.port_vars = {}
        self.dp_def = dp_def
        # BBs of this specific edge. The dp_def is shared across all edges with
        # the same (pred queue, succ queue) pair, so its config's pq_bb/sq_bb
        # only reflect the first such edge; the BB handshake routing must use
        # these per-instance values instead.
        self.pq_bb = pq_bb
        self.sq_bb = sq_bb

    def connect_ports(self, port_name, map, queue, em):
        signal_type = self.ports[port_name].type
        # if the the queue output port already has a temporary signal connected, just directly connect to it
        if signal_type == "i" and queue.port_vars[map[port_name]]:
            self.port_vars[port_name] = queue.port_vars[map[port_name]][0]
        # otherwise, create a new temporary signal and connect the queue port to it
        else:
            c_signal = make_connecting_signal(self.ports[port_name], em, queue.num)
            self.port_vars[port_name] = c_signal
            queue.port_vars[map[port_name]].append(c_signal)

    def init_port_vars(self, em):
        for port_name in DC_TO_PQ_MAP.keys():
            self.connect_ports(port_name, DC_TO_PQ_MAP, self.pred, em)

        for port_name in DC_TO_SQ_MAP.keys():
            self.connect_ports(port_name, DC_TO_SQ_MAP, self.succ, em)

    def instantate(self, em):
        self.dp_def.instantiate(
            em, self.port_vars, f"dp_q{self.pred.num}_q{self.succ.num}"
        )


class Structure(Generator):
    def __init__(self, name: str, suffix: str, configs):
        self.name = name
        self.module_name = name + suffix
        self.configs = configs

    def generate_from_json(self, em, config: OrderingNetworkConfig, out_path):
        out_file = f"{out_path}/{self.name}.{em.get_file_suffix()}"

        # Derive pred/succ roles from graph topology
        pred_ports = set(config.edge_src)
        succ_ports = set(config.edge_dst)

        # One Queue def per port instance; is_pred/is_succ are set from the graph
        queue_defs = {}  # port_idx -> Queue
        for port_idx, queue_config_idx in enumerate(config.ports_to_queue):
            q_config = copy(config.queues[queue_config_idx])
            q_config.is_pred = port_idx in pred_ports
            q_config.is_succ = port_idx in succ_ports
            q_def = Queue(
                name=f"{self.name}_queue_{port_idx}", suffix="", configs=q_config
            )
            q_def.generate(em.new(), path_rtl=out_path, out_file=out_file)
            queue_defs[port_idx] = q_def

        # One DependencyChecker def per unique (src_queue_idx, dst_queue_idx) pair
        dc_def_map = {}
        for edge_idx, (src_port, dst_port) in enumerate(
            zip(config.edge_src, config.edge_dst)
        ):
            src_queue_idx = config.ports_to_queue[src_port]
            dst_queue_idx = config.ports_to_queue[dst_port]
            key = (src_queue_idx, dst_queue_idx)
            if key not in dc_def_map:
                dc_config = config.dependency_checkers[edge_idx]
                dc_config.pq = config.queues[src_queue_idx]
                dc_config.sq = config.queues[dst_queue_idx]
                dc_config.pq_bb = config.port_bb_ids[src_port]
                dc_config.sq_bb = config.port_bb_ids[dst_port]
                dc_def = DependencyChecker(
                    name=f"{self.name}_dependency_checker_{edge_idx}",
                    suffix="",
                    configs=dc_config
                )
                dc_def.generate(em.new(), path_rtl=out_path, out_file=out_file)
                dc_def_map[key] = dc_def

        # Validate port maps using graph-derived pred/succ defs
        pred_queue_def = queue_defs[next(iter(pred_ports))]
        succ_queue_def = queue_defs[next(iter(succ_ports))]
        any_dc_def = next(iter(dc_def_map.values()))

        pq_ports = set(pred_queue_def.ports.keys())
        sq_ports = set(succ_queue_def.ports.keys())
        dc_ports = set(any_dc_def.ports.keys())

        pq_keys, pq_vals = set(DC_TO_PQ_MAP.keys()), set(DC_TO_PQ_MAP.values())
        sq_keys, sq_vals = set(DC_TO_SQ_MAP.keys()), set(DC_TO_SQ_MAP.values())

        assert (
            pq_keys <= dc_ports
        ), f"DC_TO_PQ_MAP keys not in DC ports:      {pq_keys - dc_ports}"
        assert (
            pq_vals <= pq_ports
        ), f"DC_TO_PQ_MAP values not in pred ports:  {pq_vals - pq_ports}"
        assert (
            sq_keys <= dc_ports
        ), f"DC_TO_SQ_MAP keys not in DC ports:      {sq_keys - dc_ports}"
        assert (
            sq_vals <= sq_ports
        ), f"DC_TO_SQ_MAP values not in succ ports:  {sq_vals - sq_ports}"
        assert dc_ports <= (pq_keys | sq_keys | CROSS_BB_DC_PORTS), \
            f"DC ports not fully mapped: {dc_ports - (pq_keys | sq_keys | CROSS_BB_DC_PORTS)}"

        # Count ports per queue config
        group_sizes = defaultdict(int)
        for queue_config_idx in config.ports_to_queue:
            group_sizes[queue_config_idx] += 1

        # Build universal port arrays per queue config.
        # Use the first port instance for each config group as the representative def.
        representative_defs = {}
        for port_idx, queue_config_idx in enumerate(config.ports_to_queue):
            if queue_config_idx not in representative_defs:
                representative_defs[queue_config_idx] = queue_defs[port_idx]

        universal_vars = {}
        for queue_config_idx, q_def in representative_defs.items():
            group_size = group_sizes[queue_config_idx]
            q_port_map = q_def.get_ports()
            global_q_ports = get_global_queue_ports(q_def)
            universal_vars[queue_config_idx] = {}
            for port in global_q_ports:
                signal = q_port_map[port]
                if type(signal) == LogicVec:
                    universal_vars[queue_config_idx][port] = LogicVecArray(
                        em,
                        f"{port}_q{queue_config_idx}_array",
                        signal.type,
                        group_size,
                        signal.size,
                    )
                elif type(signal) == Logic:
                    universal_vars[queue_config_idx][port] = LogicArray(
                        em, f"{port}_q{queue_config_idx}_array", signal.type, group_size
                    )
                else:
                    raise Exception(
                        f"Unsupported signal type {type(signal)} for port {port}"
                    )

        # Create one QueueInstance per port. Each queue gets a globally unique
        # index (its port index) for naming; the per-config array slot is a
        # separate local offset used only to index the universal arrays.
        queue_instances = {}
        group_counters = defaultdict(int)
        for port_idx, queue_config_idx in enumerate(config.ports_to_queue):
            queue_type = config.queues[queue_config_idx].q_type
            group_slot = group_counters[queue_config_idx]
            group_counters[queue_config_idx] += 1
            q_instance = QueueInstance(queue_defs[port_idx], port_idx, queue_type)
            q_instance.init_port_vars(universal_vars[queue_config_idx], group_slot)
            queue_instances[port_idx] = q_instance

        # Create DependencyCheckerInstances
        dp_checkers = []
        for edge_idx, (src_port, dst_port) in enumerate(
            zip(config.edge_src, config.edge_dst)
        ):
            src_queue_idx = config.ports_to_queue[src_port]
            dst_queue_idx = config.ports_to_queue[dst_port]
            key = (src_queue_idx, dst_queue_idx)
            dp_checker = DependencyCheckerInstance(
                dc_def_map[key],
                queue_instances[src_port],
                queue_instances[dst_port],
                config.port_bb_ids[src_port],
                config.port_bb_ids[dst_port],
            )
            dp_checker.init_port_vars(em)
            dp_checkers.append(dp_checker)

        self._route_bb_ports(em, dp_checkers)

        defaults = {}
        defaults["allow_alloc_i"] = Logic(em, "allow_alloc_default", "w")
        defaults["allow_access_i"] = Logic(em, "allow_access_default", "w")
        em.add_assignment(defaults["allow_alloc_i"], Bit(1))
        em.add_assignment(defaults["allow_access_i"], Bit(1))

        for queue in queue_instances.values():
            queue.instantiate(em, defaults)
        for dp_checker in dp_checkers:
            dp_checker.instantate(em)

        self._write_to_file(em, out_path, out_file)

    def _route_bb_ports(self, em: Emitter, dp_checkers: list) -> None:
        """Create structure-level BB handshake ports and wire them to cross-BB DCs.

        For each distinct BB ID that appears in a cross-BB DC, one bb_valid_{x}
        input and bb_ready_{x} output are added to the structure module.
        The valid forwarded to each DC is gated by all *other* DCs' ready signals
        for the same BB so that all DCs record the execution atomically.
        """
        bb_to_dc_ports = defaultdict(list)  # bb_id -> [(dp_checker, prefix)]
        for dp_checker in dp_checkers:
            if dp_checker.pq_bb != dp_checker.sq_bb:
                bb_to_dc_ports[dp_checker.pq_bb].append((dp_checker, "pq"))
                bb_to_dc_ports[dp_checker.sq_bb].append((dp_checker, "sq"))

        dc_ready_wires = {}  # (id(dp_checker), prefix) -> Logic "w" wire
        for bb_id, pairs in sorted(bb_to_dc_ports.items()):
            for dp_checker, prefix in pairs:
                wire = Logic(em, f"dc_{dp_checker.pred.num}_{dp_checker.succ.num}_{prefix}_bb_ready", "w")
                dc_ready_wires[(id(dp_checker), prefix)] = wire
                dp_checker.port_vars[f"{prefix}_bb_ready_o"] = wire

        for bb_id, pairs in sorted(bb_to_dc_ports.items()):
            bb_valid = Logic(em, f"bb_valid_{bb_id}", "i")
            bb_ready = Logic(em, f"bb_ready_{bb_id}", "o")
            all_readies = [dc_ready_wires[(id(dp), pfx)] for dp, pfx in pairs]

            em.add_assignment(bb_ready, reduce(lambda x, y: x & y, all_readies))

            for i, (dp_checker, prefix) in enumerate(pairs):
                others = [all_readies[j] for j in range(len(pairs)) if j != i]
                if others:
                    masked = Logic(em, f"bb_{bb_id}_{prefix}_{dp_checker.pred.num}_{dp_checker.succ.num}_valid", "w")
                    factor = bb_valid
                    for w in others:
                        factor = factor & w
                    em.add_assignment(masked, factor)
                    dp_checker.port_vars[f"{prefix}_bb_valid_i"] = masked
                else:
                    dp_checker.port_vars[f"{prefix}_bb_valid_i"] = bb_valid
