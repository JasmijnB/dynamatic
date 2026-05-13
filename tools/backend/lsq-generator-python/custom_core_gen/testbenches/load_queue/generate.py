import pathlib

from core_gen.emitters import VerilogEmitter
from custom_core_gen.configs import QueueConfig
from custom_core_gen.generators.queue import Queue

directory = pathlib.Path(__file__).parent.resolve()
# make out directory if it doesn't exist
(directory / "out").mkdir(exist_ok=True)

em = VerilogEmitter()
config = QueueConfig.from_json(directory / "queue-config.json")
queue = Queue(name="load_queue", suffix="", configs=config)
queue.generate(em=em, path_rtl=directory / "out")
