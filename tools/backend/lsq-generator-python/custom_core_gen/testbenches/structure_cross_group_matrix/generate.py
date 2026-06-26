import pathlib

from core_gen.emitters import VerilogEmitter, VHDLEmitter
from custom_core_gen.configs import OrderingNetworkConfig
from custom_core_gen.generators.structure import Structure

directory = pathlib.Path(__file__).parent.resolve()
(directory / "out").mkdir(exist_ok=True)

for file in (directory / "out").glob("*"):
    file.unlink()

config = OrderingNetworkConfig.from_json(directory / "structure-config.json")
structure = Structure(name="structure_cross_group_matrix", suffix="", configs=config)
structure.generate_from_json(VerilogEmitter(), config, directory / "out")
structure.generate_from_json(VHDLEmitter(), config, directory / "out")
