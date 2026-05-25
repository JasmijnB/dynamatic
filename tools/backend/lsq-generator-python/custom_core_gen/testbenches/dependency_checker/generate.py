import json
import pathlib

from core_gen.emitters import VerilogEmitter, VHDLEmitter
from custom_core_gen.configs import DependencyCheckerConfig, QueueConfig
from custom_core_gen.generators.dependency_checker import DependencyChecker

directory = pathlib.Path(__file__).parent.resolve()
(directory / "out").mkdir(exist_ok=True)

for file in (directory / "out").glob("dependency_checker*"):
    file.unlink()

with open(directory / "dependency-config.json") as f:
    raw = json.load(f)

pq = QueueConfig(raw["PQConfig"])
sq = QueueConfig(raw["SQConfig"])
config = DependencyCheckerConfig.from_parts(raw, pq, sq)

verilog_em = VerilogEmitter()
vhdl_em = VHDLEmitter()
dependency_checker = DependencyChecker(name="dependency_checker", suffix="", configs=config)
dependency_checker.generate(em=verilog_em, path_rtl=directory / "out")
dependency_checker.generate(em=vhdl_em, path_rtl=directory / "out")
