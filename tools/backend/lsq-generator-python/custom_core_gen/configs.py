import json
import math


class QueueConfig:
    q_type: str = "load"
    num_entries: int = 3
    data_width: int = 16
    addr_width: int = 13
    id_width: int = 2
    id_val: int = 0
    q_addr_width: int = 2
    st_resp: bool = False
    is_succ = False
    is_pred = False

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
        self.is_succ = False
        self.is_pred = False

        assert self.q_type in [
            "load",
            "store",
        ], "QueueType must be either 'load' or 'store'"
        assert self.num_entries > 0, "NumEntries must be greater than 0"
        assert (
            self.num_entries & (self.num_entries - 1)
        ) == 0, f"NumEntries must be a power of 2, got {self.num_entries}"
        assert self.data_width > 0, "DataWidth must be greater than 0"
        assert self.addr_width > 0, "AddrWidth must be greater than 0"
        assert self.id_width > 0, "IDWidth must be greater than 0"
        assert self.q_addr_width > 0, "QueueAddrWidth must be greater than 0"
        assert (
            0 <= self.id_val < (1 << self.id_width)
        ), f"IDVal must be between 0 and {(1 << self.id_width) - 1}"


class DependencyCheckerConfig:
    pq: "QueueConfig"
    sq: "QueueConfig"
    pq_bb: int
    sq_bb: int
    dep_entry_ratio = 2  # The ratio between the size of the dependency array and the number of entries in the queues
    
    succ_can_execute_once: bool = (
        False  # Whether this dependency checker checks for dependencies in reverse order
    )
    access_disparity_width: int = 4

    def __init__(
        self, config: dict, pq: "QueueConfig" = None, sq: "QueueConfig" = None, pq_bb: int = None, sq_bb: int = None
    ):
        self.pq = pq
        self.sq = sq
        self.pq_bb = pq_bb
        self.sq_bb = sq_bb
        self.succ_can_execute_once = config.get("succCanExecuteOnce")
        self.access_disparity_width = config.get("AccessDisparityWidth")
        self.dep_entry_ratio = config.get("depEntryRatio", 2)

    @staticmethod
    def from_parts(
        dc_config: dict, pq: "QueueConfig", sq: "QueueConfig"
    ) -> "DependencyCheckerConfig":
        obj = DependencyCheckerConfig.__new__(DependencyCheckerConfig)
        obj.pq = pq
        obj.sq = sq
        obj.access_disparity_width = dc_config.get("AccessDisparityWidth")
        obj.succ_can_execute_once = dc_config.get("succCanExecuteOnce")
        obj.dep_entry_ratio = dc_config.get("depEntryRatio", 2)
        return obj


class OrderingNetworkConfig:
    name: str
    edge_src: list
    edge_dst: list
    edge_dp: list
    port_bb_ids: list
    ports_to_queue: list
    queues: list  # list[QueueConfig]
    dependency_checkers: list  # list[DependencyCheckerConfig]

    def __init__(self, config: dict):
        self.name = config["name"]
        self.edge_src = config["edgeSrc"]
        self.edge_dst = config["edgeDst"]
        self.edge_dp = config["edgeDp"]
        self.port_bb_ids = config["portBBIds"]
        self.ports_to_queue = config["portsToQueue"]
        self.queues = [QueueConfig(q) for q in config["queues"]]
        self.dependency_checkers = [
            DependencyCheckerConfig(dc) for dc in config["dependencyCheckers"]
        ]

    @staticmethod
    def from_json(json_path: str) -> "OrderingNetworkConfig":
        with open(json_path, "r") as f:
            return OrderingNetworkConfig(json.load(f))
