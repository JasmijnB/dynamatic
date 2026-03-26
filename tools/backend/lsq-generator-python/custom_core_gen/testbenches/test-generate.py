import argparse
import json
import os

from core_gen.emitters import VHDLEmitter, VerilogEmitter

from custom_core_gen.configs import QueueConfig
from custom_core_gen.generators.load_queue import Queue


def generate(config_path: str, output_dir: str, hdl: str, name: str = "load_queue"):
    """
    Generate a load queue component from a JSON config file.

    Parameters:
        config_path : Path to the JSON configuration file.
        output_dir  : Directory where the output HDL file will be written.
        hdl         : Target language, either 'vhdl' or 'verilog'.
        name        : Name of the generated module (default: 'load_queue').
    """
    if hdl == "vhdl":
        emitter = VHDLEmitter()
    elif hdl == "verilog":
        emitter = VerilogEmitter()
    else:
        raise ValueError(f"Unsupported HDL '{hdl}'. Use 'vhdl' or 'verilog'.")

    with open(config_path) as f:
        config = QueueConfig(json.load(f))

    # 'master' is not part of QueueConfig; default to False (no memStart/ctrlEnd ports)
    config.master = False

    os.makedirs(output_dir, exist_ok=True)

    output_file = os.path.join(output_dir, f"{name}.{emitter.get_file_suffix()}")
    open(output_file, "w").close()

    lq = Queue(name=name, suffix="", configs=config)
    lq.generate_load_queue(em=emitter, lsq_submodules=None, path_rtl=output_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a load queue HDL component from a JSON config."
    )
    parser.add_argument(
        "--config-file", "-c", required=True, dest="config_file",
        help="Path to the JSON configuration file.",
    )
    parser.add_argument(
        "--output-dir", "-o", dest="output_dir", default=".",
        help="Directory to write the generated HDL file (default: current directory).",
    )
    parser.add_argument(
        "--hdl", "-l", dest="hdl", default="vhdl", choices=["vhdl", "verilog"],
        help="Output language: 'vhdl' or 'verilog' (default: vhdl).",
    )
    parser.add_argument(
        "--name", "-n", dest="name", default="load_queue",
        help="Module name for the generated component (default: load_queue).",
    )

    args = parser.parse_args()
    generate(
        config_path=args.config_file,
        output_dir=args.output_dir,
        hdl=args.hdl,
        name=args.name,
    )
