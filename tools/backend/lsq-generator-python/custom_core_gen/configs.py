import json
import math
import os


class QueueConfig:
    def __init__(self, config: dict):
        self.q_type = config["QueueType"]
        self.num_entries = config["NumEntries"]
        self.data_width = config["DataWidth"]
        self.addr_width = config["AddrWidth"]
        self.st_resp = config["StResp"]
        self.bypass = config.get("Bypass", False)
        # 0 for the degenerate single-entry queue: no index, only the
        # generation bit (see generators/ptr_utils.py for the pointers).
        self.q_addr_width = math.ceil(math.log2(self.num_entries))
        # Set by the structure from the graph.
        self.is_succ = False
        self.is_pred = False
        # This port takes part in a cross-BB dependency checker: the queue
        # then owns the dep-array state every such checker of this port
        # shares (see Queue._shared_dep_state).
        self.shares_dep_state = False
        # The successor BBs for which this (predecessor) port keeps a shared
        # boundary-mark unit (one per BB, used by every mark-scheme checker
        # from this port to a successor in that BB).
        self.mark_bbs = ()
        # This (predecessor) port keeps the shared live-slot bits that its
        # matrix-scheme checkers copy into their rows.
        self.needs_live = False

        assert self.q_type in ("load", "store"), "QueueType must be either 'load' or 'store'"
        assert self.num_entries > 0, "NumEntries must be greater than 0"
        assert self.data_width > 0, "DataWidth must be greater than 0"
        assert self.addr_width > 0, "AddrWidth must be greater than 0"


def _forced_sequential(config: dict) -> bool:
    """A checker's `forcedSequential` key. A checker without one takes the
    ON_FORCED_SEQUENTIAL environment variable (1 = on), so a whole flow, e.g.
    the integration tests, can be run with every checker sequential."""
    default = os.environ.get("ON_FORCED_SEQUENTIAL", "0") not in ("", "0")
    return bool(config.get("forcedSequential", default))


def _dep_scheme(config: dict) -> str:
    """A cross-BB checker's scheme, key `depScheme`: "runs" (boundary bits
    and a run-disparity counter), "exact" (runs plus a claimed-window
    register: no lookahead limit), "matrix" (dependence matrix) or "auto"
    (the cheaper of runs and matrix for the queue depths, see
    DependencyChecker.cross_bb_scheme). Without the key, the ON_DEP_SCHEME
    environment variable, default "auto"."""
    scheme = config.get("depScheme", os.environ.get("ON_DEP_SCHEME") or "auto")
    assert scheme in ("auto", "runs", "exact", "matrix"), f"unknown depScheme {scheme}"
    return scheme


def _run_lookahead(config: dict) -> int:
    """`runs` scheme: how many marks ahead the head's boundary is resolved
    (key `runLookahead`, env ON_RUN_LOOKAHEAD, default 1)."""
    k = int(config.get("runLookahead", os.environ.get("ON_RUN_LOOKAHEAD", "1")))
    assert k >= 1, "runLookahead must be >= 1"
    return k


class DependencyCheckerConfig:
    def __init__(self, config: dict, pq: "QueueConfig" = None, sq: "QueueConfig" = None,
                 pq_bb: int = None, sq_bb: int = None):
        self.pq = pq
        self.sq = sq
        self.pq_bb = pq_bb
        self.sq_bb = sq_bb
        # The successor precedes the predecessor in its BB, so its first
        # access has nothing to check.
        self.succ_can_execute_once = config.get("succCanExecuteOnce")
        self.access_disparity_width = config.get("AccessDisparityWidth")
        self.forced_sequential = _forced_sequential(config)
        self.dep_scheme = _dep_scheme(config)
        self.run_lookahead = _run_lookahead(config)


class OrderingNetworkConfig:
    def __init__(self, config: dict):
        self.name = config["name"]
        self.edge_src = config["edgeSrc"]
        self.edge_dst = config["edgeDst"]
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
