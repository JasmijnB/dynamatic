import pathlib

from core_gen.emitters import VerilogEmitter
from custom_core_gen.configs import OrderingNetworkConfig
from custom_core_gen.generators.structure import Structure

directory = pathlib.Path(__file__).parent.resolve()
(directory / "out").mkdir(exist_ok=True)

for file in (directory / "out").glob("*"):
    file.unlink()

em = VerilogEmitter()
config = OrderingNetworkConfig.from_json(directory / "structure-config.json")
structure = Structure(name="structure_simple", suffix="", configs=config)
structure.generate_from_json(em, config, directory / "out")
