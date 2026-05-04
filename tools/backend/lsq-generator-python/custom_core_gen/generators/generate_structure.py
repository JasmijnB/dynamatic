#! /usr/bin/env python3

import argparse
import pathlib
import pydot
from collections import defaultdict


def instantiate_queue(node):
    pass

def instantiate_dependency_checker(successor, predecessor, pressure_signal_name):
    pass

def generate_from_dot(dot_path):
    graphs = pydot.graph_from_dot_file(dot_path)
    graph = graphs[0]
    
    nodes = defaultdict(list)
    queues = {}
    
    for edge in graph.get_edges():
        nodes[edge.get_source()].append(edge.get_destination())
        
    for (predecessor, successors) in nodes.items():
        for memory_access in [predecessor] + successors:
            if memory_access not in queues:
                queues[memory_access] = instantiate_queue(memory_access)

        if (len(successors) == 1):
            # Process single successor
            pass
        else:
            # Process multiple successors
            pass