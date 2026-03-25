import json
import math

class QueueConfig:
    q_type:     str = "load"    
    num_entries:    int = 3 # Queue size        (Number of entries in the queue)
    data_width:     int = 16  # Data width        (Number of bits for load/store data)
    addr_width:     int = 13  # Address width     (Number of bits for memory address)
    id_width:       int = 2  # ID width          (Number of bits for ID in the memory interface)
    id_val:         int = 0  # ID value          (ID value used for all requests in the queue)
    
    q_addr_width: int = 2  # queue address width

    st_resp: bool = False  # Whether store response channel in store access port is enabled)

    pipe0: bool = False  # Enable pipeline register 0
    pipe1: bool = False  # Enable pipeline register 1
    pipe_comb: bool = False  # Enable pipeline register pipeComp
    
    master: bool = False # Whether the queue is a master

    def __init__(self, config: dict):
        self.q_type = config["QueueType"]
        self.num_entries = config["NumEntries"]
        self.data_width = config["DataWidth"]
        self.addr_width = config["AddrWidth"]
        self.id_width = config["IDWidth"]
        self.id_val = config["IDVal"]
        self.ldp_addr_width = config["LDPAddrWidth"]
        self.st_resp = config["StResp"]

        self.q_addr_width = math.ceil(math.log2(self.num_entries))
        
        self.pipe0 = bool(config["pipe0En"])
        self.pipe1 = bool(config["pipe1En"])
        self.pipeComp = bool(config["pipeCompEn"])
        self.master = bool(config["master"])
        
        assert(self.q_type in ["load", "store"]), "QueueType must be either 'load' or 'store'"
        assert(self.num_entries > 0), "NumEntries must be greater than 0"
        assert(self.data_width > 0), "DataWidth must be greater than 0"
        assert(self.addr_width > 0), "AddrWidth must be greater than 0"
        assert(self.id_width > 0), "IDWidth must be greater than 0"
        assert(self.q_addr_width > 0), "QueueAddrWidth must be greater than 0"
        assert(self.id_val >= 0 and self.id_val < (1 << self.id_width)), f"IDVal must be between 0 and { (1 << self.id_width) - 1 }"
        
    @staticmethod
    def from_json(json_path: str):
        with open(json_path, "r") as file:
            config_dict = json.load(file)
            return QueueConfig.from_dict(config_dict)