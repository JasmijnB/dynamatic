import json
import math
import os


class QueueConfig:
    q_type: str = "load"
    num_entries: int = 3
    data_width: int = 16
    addr_width: int = 13
    id_width: int = 2
    id_val: int = 0
    q_addr_width: int = 2
    st_resp: bool = False
    bypass: bool = False
    is_succ = False
    is_pred = False
    # Set by the structure when this port takes part in a cross-BB dependency
    # checker (physical frame): the queue then owns the dep-array tail that
    # every such checker of this port shares (see Queue._shared_dep_state).
    shares_dep_state: bool = False
    # Set by the structure: the successor BBs for which this (predecessor)
    # port keeps a shared boundary-mark unit (one per BB, used by every
    # run-scheme checker from this port to a successor in that BB).
    mark_bbs: tuple = ()
    # Set by the structure: this (predecessor) port keeps the shared
    # live-slot bits that its matrix-scheme checkers copy into their rows.
    needs_live: bool = False

    def __init__(self, config: dict):
        self.q_type = config["QueueType"]
        self.num_entries = config["NumEntries"]
        self.data_width = config["DataWidth"]
        self.addr_width = config["AddrWidth"]
        self.id_width = config["IDWidth"]
        self.id_val = config["IDVal"]
        self.ldp_addr_width = config["LDPAddrWidth"]
        self.st_resp = config["StResp"]
        self.bypass = config.get("Bypass", False)
        self.q_addr_width = math.ceil(math.log2(self.num_entries))
        self.is_succ = False
        self.is_pred = False
        self.shares_dep_state = False
        self.mark_bbs = ()
        self.needs_live = False

        assert self.q_type in [
            "load",
            "store",
        ], "QueueType must be either 'load' or 'store'"
        assert self.num_entries > 0, "NumEntries must be greater than 0"
        # Any NumEntries >= 1 (see generators/ptr_utils.py for the pointers).
        assert self.data_width > 0, "DataWidth must be greater than 0"
        assert self.addr_width > 0, "AddrWidth must be greater than 0"
        assert self.id_width > 0, "IDWidth must be greater than 0"
        # q_addr_width == 0 is the degenerate single-entry queue (NumEntries == 1):
        # there is no index to represent, only the ptr_width == 1 generation bit.
        # Generators special-case this rather than requiring q_addr_width > 0.
        assert (
            0 <= self.id_val < (1 << self.id_width)
        ), f"IDVal must be between 0 and {(1 << self.id_width) - 1}"


def _forced_sequential(config: dict) -> bool:
    """A checker's `forcedSequential` key. A checker without one takes the
    ON_FORCED_SEQUENTIAL environment variable (1 = on), so a whole flow, e.g.
    the integration tests, can be run with every checker sequential."""
    default = os.environ.get("ON_FORCED_SEQUENTIAL", "0") not in ("", "0")
    return bool(config.get("forcedSequential", default))


def _dep_scheme(config: dict) -> str:
    """A cross-BB checker's scheme, key `depScheme`: "runs" (boundary bits
    and a run-disparity counter), "matrix" (dependence matrix) or "auto"
    (the cheaper one for the queue depths, see
    DependencyChecker.cross_bb_scheme). Without the key, the ON_DEP_SCHEME
    environment variable, default "auto"."""
    scheme = config.get("depScheme", os.environ.get("ON_DEP_SCHEME") or "auto")
    assert scheme in ("auto", "runs", "matrix"), f"unknown depScheme {scheme}"
    return scheme


def _run_lookahead(config: dict) -> int:
    """`runs` scheme: how many marks ahead the head's boundary is resolved
    (key `runLookahead`, env ON_RUN_LOOKAHEAD, default 1)."""
    k = int(config.get("runLookahead", os.environ.get("ON_RUN_LOOKAHEAD", "1")))
    assert k >= 1, "runLookahead must be >= 1"
    return k


class DependencyCheckerConfig:
    pq: "QueueConfig"
    sq: "QueueConfig"
    pq_bb: int
    sq_bb: int
    dep_entry_ratio = 1  # The ratio between the size of the dependency array and the number of entries in the queues
    
    succ_can_execute_once: bool = (
        False  # Whether this dependency checker checks for dependencies in reverse order
    )
    access_disparity_width: int = 4
    forced_sequential: bool = (
        False  # Never compare addresses: grant a successor access only once every
        # predecessor access preceding it in program order has completed
    )

    def __init__(
        self, config: dict, pq: "QueueConfig" = None, sq: "QueueConfig" = None, pq_bb: int = None, sq_bb: int = None
    ):
        self.pq = pq
        self.sq = sq
        self.pq_bb = pq_bb
        self.sq_bb = sq_bb
        self.succ_can_execute_once = config.get("succCanExecuteOnce")
        self.access_disparity_width = config.get("AccessDisparityWidth")
        self.dep_entry_ratio = config.get("depEntryRatio", 1)
        self.forced_sequential = _forced_sequential(config)
        self.dep_scheme = _dep_scheme(config)
        self.run_lookahead = _run_lookahead(config)

    @staticmethod
    def from_parts(
        dc_config: dict, pq: "QueueConfig", sq: "QueueConfig"
    ) -> "DependencyCheckerConfig":
        obj = DependencyCheckerConfig.__new__(DependencyCheckerConfig)
        obj.pq = pq
        obj.sq = sq
        obj.access_disparity_width = dc_config.get("AccessDisparityWidth")
        obj.succ_can_execute_once = dc_config.get("succCanExecuteOnce")
        obj.dep_entry_ratio = dc_config.get("depEntryRatio", 1)
        obj.forced_sequential = _forced_sequential(dc_config)
        obj.dep_scheme = _dep_scheme(dc_config)
        obj.run_lookahead = _run_lookahead(dc_config)
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
