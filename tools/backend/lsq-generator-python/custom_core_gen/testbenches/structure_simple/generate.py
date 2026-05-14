import pathlib

from core_gen.emitters import VerilogEmitter
from custom_core_gen.generators.structure import Structure

directory = pathlib.Path(__file__).parent.resolve()
(directory / "out").mkdir(exist_ok=True)

for file in (directory / "out").glob("*"):
    file.unlink()

# Load queue is the predecessor (pq, config_id=0 in the dot file).
# It must retire before the store queue may issue to the same address.
load_config_dict = {
    "QueueType": "load",
    "NumEntries": 4,
    "DataWidth": 32,
    "AddrWidth": 32,
    "IDWidth": 2,
    "IDVal": 0,
    "LDPAddrWidth": 0,
    "StResp": False,
    "pipe0En": False,
    "pipe1En": False,
    "pipeCompEn": False,
    "master": False,
}

# Store queue is the successor (sq, config_id=1 in the dot file).
# It is gated by the dependency checker until its paired load retires.
store_config_dict = {
    "QueueType": "store",
    "NumEntries": 4,
    "DataWidth": 32,
    "AddrWidth": 32,
    "IDWidth": 2,
    "IDVal": 0,
    "LDPAddrWidth": 0,
    "StResp": False,
    "pipe0En": False,
    "pipe1En": False,
    "pipeCompEn": False,
    "master": False,
}

configs = {
    "queue_0": load_config_dict,
    "queue_1": store_config_dict,
    "dp_0": {
        "TailOffsetWidth": 4,
        "AccessDisparityWidth": 4,
    },
}

em = VerilogEmitter()
structure = Structure(name="structure_simple", suffix="", configs=configs)
structure.generate_from_dot(em, directory / "deps.dot", directory / "out", configs)
