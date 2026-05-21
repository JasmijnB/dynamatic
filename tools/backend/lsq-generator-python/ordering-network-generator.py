#!/usr/bin/env python3
import argparse
import json
import os

parser = argparse.ArgumentParser()
parser.add_argument("-o", required=True, help="Output directory")
parser.add_argument("-c", required=True, help="Path to JSON config file")
parser.add_argument("--hdl", required=True, help="HDL target (vhdl/verilog/smv)")
args = parser.parse_args()

with open(args.c) as f:
    config = json.load(f)

module_name = os.path.splitext(os.path.basename(args.c))[0]
out_path = os.path.join(args.o, module_name + ".txt")

with open(out_path, "w") as f:
    json.dump(config, f, indent=2)

print(f"[ordering-network-generator] wrote {out_path}")
