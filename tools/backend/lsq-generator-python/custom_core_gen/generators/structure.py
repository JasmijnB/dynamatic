#! /usr/bin/env python3

from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, Type, reduce_bin
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

        queue_def = Queue(name="store_queue", suffix="", configs=configs['QConfig'])
        dc_def = DependencyChecker(name="dependency_checker", suffix="", configs=configs['DCConfig'])

        queue_def.generate(em.new(), lsq_submodules=None, path_rtl=out_path)
        dc_def.generate(em.new(), path_rtl=out_path)

        q_ports  = set(queue_def.ports.keys())
        dc_ports = set(dc_def.ports.keys())

        pq_keys, pq_vals = set(DC_TO_PQ_MAP.keys()), set(DC_TO_PQ_MAP.values())
        sq_keys, sq_vals = set(DC_TO_SQ_MAP.keys()), set(DC_TO_SQ_MAP.values())

        assert pq_keys <= dc_ports, f"DC_TO_PQ_MAP keys not in DC ports:     {pq_keys - dc_ports}"
        assert pq_vals <= q_ports,  f"DC_TO_PQ_MAP values not in queue ports: {pq_vals - q_ports}"
        assert sq_keys <= dc_ports, f"DC_TO_SQ_MAP keys not in DC ports:     {sq_keys - dc_ports}"
        assert sq_vals <= q_ports,  f"DC_TO_SQ_MAP values not in queue ports: {sq_vals - q_ports}"

        dc_mapped = pq_keys | sq_keys
        assert dc_mapped == dc_ports, f"DC ports not fully mapped: {dc_ports - dc_mapped}"

        successors = defaultdict(list)
        # only contains the queues with an edge in between
        queue_nodes = set()

        for edge in graph.get_edges():
            queue_nodes.add(edge.get_source())
            queue_nodes.add(edge.get_destination())
            
        # give the queues a fixed order
        queue_nodes = list(queue_nodes)
            
        num_queues = len(queue_nodes)
        queue_ports = queue_def.get_ports()
        global_queue_ports = get_global_queue_ports(queue_def)
        # for each universal queue port, create an output/input variable
        universal_vars = {}
        for port in global_queue_ports:
            signal = queue_ports[port]
            if type(signal) == LogicVec:
                universal_vars[port] = LogicVecArray(em, port + "_array", signal.type, num_queues, signal.size)
            elif type(signal) == Logic:
                universal_vars[port] = LogicArray(em, port + "_array", signal.type, num_queues)
            else:
                raise Exception(f"Unsupported signal type {type(signal)} for port {port}")
            
        # generate the queue signal maps
        queues = {}
        for i, queue in enumerate(queue_nodes):
            queue_type = queue[:2]
            queue_instance = QueueInstance(queue_def, i, queue_type)
            queue_instance.init_port_vars(universal_vars)
            queues[queue] = queue_instance
            
        # generate the dependeny checker signal maps and connecting signals between queues and dependency checkers
        dp_checkers = []
        for edge in graph.get_edges():
            src = edge.get_source()
            dst = edge.get_destination()
            if src not in queues or dst not in queues:
                raise Exception(f"{src} or {dst} not in queues")

            dp_checker = DependencyCheckerInstance(dc_def, queues[src], queues[dst])
            dp_checker.init_port_vars(em)
            dp_checkers.append(dp_checker)
            
        # instantiate the default maps
        # a queue without predecessors does not have allow_alloc and a queue without successors does not have allow_access
        # so in this case we can simply set these to true
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