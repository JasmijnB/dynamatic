#! /usr/bin/env python3

from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, Type, reduce_bin
from custom_core_gen.configs import QueueConfig, DependencyCheckerConfig
from custom_core_gen.generators.queue import Queue
from custom_core_gen.generators.dependency_checker import DependencyChecker
from custom_core_gen.generators.generator import Generator
import pydot
import enum
from copy import copy
from collections import defaultdict

DC_TO_PQ_MAP = {
    "pq_addr_i":      "q_addr_o",
    "pq_done_i":    "done_ptr_o",
    "pq_send_en_i":     "done_en_o",
    "pq_alloc_en_i":    "alloc_en_o",
    "allow_pq_alloc_o": "allow_alloc_i",
}


DC_TO_SQ_MAP = {
    "sq_addr_i":       "q_addr_o",
    "sq_tail_i":    "alloc_ptr_o",
    "sq_head_i":     "head_ptr_o",
    "sq_access_en_i":    "access_en_o",
    "allow_sq_access_o": "allow_access_i",
}

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

class QueueInstance(): 
    def __init__(self, q_def, num: int, q_type: str):
        self.q_def = q_def
        self.num = num
        self.q_type = q_type
        self.port_vars = defaultdict(list)
        self.successors = []
        
    def init_port_vars(self, universal_vars):
        for (port, signal) in universal_vars.items():
            self.port_vars[port] = [signal[self.num]]
            
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
                    raise Exception(f"No signal mapped to port {port_name} of queue {self.num} and no default provided")
            elif len(signal_list) == 1:
                out = signal_list[0]
            else:
                out = make_connecting_signal(signal_list[0], em)
                # reduce the signal list with an and (since in order to allow access/alloc, all predecessors must allow access/alloc)
                em.add_assignment(out, reduce_bin(BinOp.AND, signal_list))

            port_signals[port_name] = out
                
        self.q_def.instantiate(em, port_signals, f"queue_{self.num}_{self.q_type}")
            

class DependencyCheckerInstance():
    def __init__(self, dp_def, pred: QueueInstance, succ: QueueInstance):
        self.pred = pred
        self.succ = succ
        self.ports = dp_def.get_ports()
        self.port_vars = {}
        self.dp_def = dp_def

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
        self.dp_def.instantiate(em, self.port_vars)
            

class Structure(Generator):
    def __init__(self, name: str, suffix: str, configs):
        self.name = name
        self.module_name = name + suffix
        self.configs = configs
        
    def generate_from_dot(self, em, dot_path, out_path, configs):
        graphs = pydot.graph_from_dot_file(dot_path)
        graph = graphs[0]

        def strip_quotes(s):
            return s.strip('"\'')

        def get_config_id(attrs):
            return int(strip_quotes(str(attrs.get('config_id', '0'))))

        def node_config_id(node_name):
            nodes = graph.get_node(node_name)
            return get_config_id(nodes[0].get_attributes()) if nodes else 0

        # Collect edges as (src, dst, dc_config_id) in graph order
        edge_triples = [
            (strip_quotes(e.get_source()), strip_quotes(e.get_destination()), get_config_id(e.get_attributes()))
            for e in graph.get_edges()
        ]

        # Collect queue nodes in first-seen order, paired with their config IDs
        seen = {}
        for src, dst, _ in edge_triples:
            for name in [src, dst]:
                if name not in seen:
                    seen[name] = node_config_id(name)
        node_config_ids = seen  # {node_name: config_id}

        # Generate one Queue def per unique queue config ID
        queue_defs = {}
        for config_id in sorted(set(node_config_ids.values())):
            q_def = Queue(name=f"queue_{config_id}", suffix="", configs=QueueConfig(configs[f'queue_{config_id}']))
            q_def.generate(em.new(), path_rtl=out_path)
            queue_defs[config_id] = q_def

        # Generate one DependencyChecker def per unique (dc_id, pq_config_id, sq_config_id) triple
        dc_def_map = {}
        for src, dst, dc_id in edge_triples:
            pq_config_id = node_config_ids[src]
            sq_config_id = node_config_ids[dst]
            key = (dc_id, pq_config_id, sq_config_id)
            if key not in dc_def_map:
                dc_config = DependencyCheckerConfig.from_parts(
                    configs[f'dp_{dc_id}'],
                    queue_defs[pq_config_id].configs,
                    queue_defs[sq_config_id].configs,
                )
                dc_def = DependencyChecker(
                    name=f"dependency_checker_{dc_id}_pq{pq_config_id}_sq{sq_config_id}",
                    suffix="",
                    configs=dc_config,
                )
                dc_def.generate(em.new(), path_rtl=out_path)
                dc_def_map[key] = dc_def

        # Validate port maps against any one representative def
        any_queue_def = next(iter(queue_defs.values()))
        any_dc_def = next(iter(dc_def_map.values()))

        q_ports  = set(any_queue_def.ports.keys())
        dc_ports = set(any_dc_def.ports.keys())

        pq_keys, pq_vals = set(DC_TO_PQ_MAP.keys()), set(DC_TO_PQ_MAP.values())
        sq_keys, sq_vals = set(DC_TO_SQ_MAP.keys()), set(DC_TO_SQ_MAP.values())

        assert pq_keys <= dc_ports, f"DC_TO_PQ_MAP keys not in DC ports:     {pq_keys - dc_ports}"
        assert pq_vals <= q_ports,  f"DC_TO_PQ_MAP values not in queue ports: {pq_vals - q_ports}"
        assert sq_keys <= dc_ports, f"DC_TO_SQ_MAP keys not in DC ports:     {sq_keys - dc_ports}"
        assert sq_vals <= q_ports,  f"DC_TO_SQ_MAP values not in queue ports: {sq_vals - q_ports}"
        assert (pq_keys | sq_keys) == dc_ports, f"DC ports not fully mapped: {dc_ports - (pq_keys | sq_keys)}"

        # Build one set of universal port arrays per config group, each sized to that group's count
        group_sizes = defaultdict(int)
        for config_id in node_config_ids.values():
            group_sizes[config_id] += 1

        universal_vars = {}  # {config_id: {port: array}}
        for config_id, q_def in queue_defs.items():
            group_size = group_sizes[config_id]
            q_ports = q_def.get_ports()
            global_q_ports = get_global_queue_ports(q_def)
            universal_vars[config_id] = {}
            for port in global_q_ports:
                signal = q_ports[port]
                if type(signal) == LogicVec:
                    universal_vars[config_id][port] = LogicVecArray(em, f"{port}_q{config_id}_array", signal.type, group_size, signal.size)
                elif type(signal) == Logic:
                    universal_vars[config_id][port] = LogicArray(em, f"{port}_q{config_id}_array", signal.type, group_size)
                else:
                    raise Exception(f"Unsupported signal type {type(signal)} for port {port}")

        # Create queue instances, numbered within their config group
        queues = {}
        group_counters = defaultdict(int)
        for queue_name, config_id in node_config_ids.items():
            queue_type = queue_name[:2]
            within_group_idx = group_counters[config_id]
            group_counters[config_id] += 1
            queue_instance = QueueInstance(queue_defs[config_id], within_group_idx, queue_type)
            queue_instance.init_port_vars(universal_vars[config_id])
            queues[queue_name] = queue_instance

        # Create dependency checker instances
        dp_checkers = []
        for src, dst, dc_id in edge_triples:
            key = (dc_id, node_config_ids[src], node_config_ids[dst])
            dp_checker = DependencyCheckerInstance(dc_def_map[key], queues[src], queues[dst])
            dp_checker.init_port_vars(em)
            dp_checkers.append(dp_checker)

        # a queue without predecessors has no allow_alloc signal; without successors no allow_access — default both to 1
        defaults = {}
        defaults["allow_alloc_i"] = Logic(em, "allow_alloc_default", "w")
        defaults["allow_access_i"] = Logic(em, "allow_access_default", "w")
        em.add_assignment(defaults["allow_alloc_i"], Val(1))
        em.add_assignment(defaults["allow_access_i"], Val(1))

        for queue in queues.values():
            queue.instantiate(em, defaults)
        for dp_checker in dp_checkers:
            dp_checker.instantate(em)

        self._write_to_file(em, out_path)