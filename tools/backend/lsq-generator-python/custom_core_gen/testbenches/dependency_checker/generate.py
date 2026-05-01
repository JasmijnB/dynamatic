import pathlib

from core_gen.emitters import VerilogEmitter, VHDLEmitter
from custom_core_gen.configs import DependencyCheckerConfig
from custom_core_gen.generators.dependency_checker import DependencyChecker

directory = pathlib.Path(__file__).parent.resolve()
# make out directory if it doesn't exist
(directory / "out").mkdir(exist_ok=True)

#clean dependency checker output directory
for file in (directory / "out").glob("dependency_checker*"):
    file.unlink()

verilog_em = VerilogEmitter()
vhdl_em = VHDLEmitter()
config = DependencyCheckerConfig.from_json(directory / "dependency-config.json")
dependency_checker = DependencyChecker(name="dependency_checker", suffix="", configs=config)
dependency_checker.generate(em=verilog_em, lsq_submodules=None, path_rtl=directory / "out")
dependency_checker.generate(em=vhdl_em, lsq_submodules=None, path_rtl=directory / "out")
