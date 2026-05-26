import json
import pathlib

from core_gen.emitters import VerilogEmitter, VHDLEmitter
from custom_core_gen.configs import QueueConfig
from custom_core_gen.generators.queue import Queue

directory = pathlib.Path(__file__).parent.resolve()
(directory / "out").mkdir(exist_ok=True)

with open(directory / "queue-config.json") as f:
    config = QueueConfig(json.load(f))
queue = Queue(name="store_queue", suffix="", configs=config)
queue.generate(em=VerilogEmitter(), path_rtl=directory / "out")
queue.generate(em=VHDLEmitter(), path_rtl=directory / "out")
