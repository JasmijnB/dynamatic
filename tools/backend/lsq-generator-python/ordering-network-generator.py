#!/usr/bin/env python3
import argparse
import os
import sys

# Ensure the package root is on the path when invoked from an arbitrary directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_gen.emitters import VerilogEmitter, VHDLEmitter
from custom_core_gen.configs import OrderingNetworkConfig
from custom_core_gen.generators.structure import Structure

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
structure = Structure(name=config.name, suffix="", configs=config)
structure.generate_from_json(em, config, args.o)