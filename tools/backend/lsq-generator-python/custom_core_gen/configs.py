import json
import math

class QueueConfig:
    q_type:     str = "load"    
    num_entries:    int = 3 # Queue size        (Number of entries in the queue)
    data_width:     int = 16  # Data width        (Number of bits for load/store data)
    addr_width:     int = 13  # Address width     (Number of bits for memory address)
    id_width:       int = 2  # ID width          (Number of bits for ID in the memory interface)
    id_val:         int = 0  # ID value          (ID value used for all requests in the queue)
    bb_id:          int = 0  # Basic block ID    (Basic block this queue belongs to)
    
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
        self.bb_id = config.get("BasicBlockID", 0)
        self.ldp_addr_width = config["LDPAddrWidth"]
        self.st_resp = config["StResp"]

        self.q_addr_width = math.ceil(math.log2(self.num_entries))

        self.pipe0 = bool(config["pipe0En"])
        self.pipe1 = bool(config["pipe1En"])
        self.pipeComp = bool(config["pipeCompEn"])
        self.master = bool(config["master"])

        assert(self.q_type in ["load", "store"]), "QueueType must be either 'load' or 'store'"
        assert(self.num_entries > 0), "NumEntries must be greater than 0"
        assert(self.num_entries & (self.num_entries - 1)) == 0, f"NumEntries must be a power of 2, got {self.num_entries}"
        assert(self.data_width > 0), "DataWidth must be greater than 0"
        assert(self.addr_width > 0), "AddrWidth must be greater than 0"
        assert(self.id_width > 0), "IDWidth must be greater than 0"
        assert(self.q_addr_width > 0), "QueueAddrWidth must be greater than 0"
        assert(self.id_val >= 0 and self.id_val < (1 << self.id_width)), f"IDVal must be between 0 and { (1 << self.id_width) - 1 }"
        
    @staticmethod
    def from_json(json_path: str):
        with open(json_path, "r") as file:
            config_dict = json.load(file)
            return QueueConfig(config_dict)
        
class DependencyCheckerConfig:
    pq: QueueConfig
    sq: QueueConfig
    tail_offset_width: int = 2 
    access_disparity_width: int = 2

    def __init__(self, config: dict):
        self.pq = QueueConfig(config["PQConfig"])
        self.sq = QueueConfig(config["SQConfig"])
        self.tail_offset_width = config.get("TailOffsetWidth", 3)
        self.access_disparity_width = config.get("AccessDisparityWidth", 2)

    @staticmethod
    def from_parts(dc_config: dict, pq_config: 'QueueConfig', sq_config: 'QueueConfig') -> 'DependencyCheckerConfig':
        obj = DependencyCheckerConfig.__new__(DependencyCheckerConfig)
        obj.pq = pq_config
        obj.sq = sq_config
        obj.tail_offset_width = dc_config.get("TailOffsetWidth", 3)
        obj.access_disparity_width = dc_config.get("AccessDisparityWidth", 2)
        return obj

    @staticmethod
    def from_json(config_path:str, pq_config=None, sq_config=None):
        if isinstance(pq_config, str):
            pq_config_dict = json.load(open(pq_config, "r"))
        elif isinstance(pq_config, dict):
            pq_config_dict = pq_config
        else:
            pq_config_dict = None

        if isinstance(sq_config, str):
            sq_config_dict = json.load(open(sq_config, "r"))
        elif isinstance(sq_config, dict):
            sq_config_dict = sq_config
        else:
            sq_config_dict = None

        with open(config_path, "r") as config_file:
            config_dict = json.load(config_file)
            if pq_config_dict is not None:
                config_dict["PQConfig"] = pq_config_dict
            if sq_config_dict is not None:
                config_dict["SQConfig"] = sq_config_dict
            return DependencyCheckerConfig(config_dict)