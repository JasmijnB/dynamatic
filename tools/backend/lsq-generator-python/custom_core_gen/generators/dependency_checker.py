from core_gen.emitters import Emitter
from core_gen.signals import *
from core_gen.operators import CyclicRightShift, MuxLookUp, Reduce
from core_gen.ir import BinOp, Bin, Val, Bit, CustomStatement, Type
from custom_core_gen.configs import DependencyCheckerConfig


class DependencyChecker:
    def __init__(self, name: str, suffix: str, configs: DependencyCheckerConfig):
        """
        LoadQueue

        Models the top-level Load Queue (LQ) module with a single load port and
        single AXI read channel.

        This class integrates the core load queue logic without a store queue,
        dependency checking, dispatcher submodules, group allocator, or pipeline
        stages (pipe0/pipe1/pipeComp are not supported).

        Entries are allocated directly when a load address arrives from the kernel:
        on the port_addr handshake, the entry at q_tail is allocated and the
        address is written into it in a single cycle.

        Parameters:
            name    : Base name of the LQ. "<name saved in configs>_core"
            suffix  : Suffix appended to the name to form the VHDL entity name.
            configs : configuration generated from JSON

        Instance Variable:
            self.module_name = name + suffix : Entity and architecture identifier

        Example:
            lq_core = LoadQueue("config_0_core", '', configs)
            lq_corVal(0)e.generate(...)

        """

        self.name = name
        self.module_name = name + suffix
        self.configs = configs
        self.ports: dict[str, Logic | LogicVec | LogicVecArray] = {}

    def _add_port(self, signal: Logic | LogicVec | LogicVecArray) -> Logic | LogicVec | LogicVecArray:
        """Register a port signal so instantiate() can use it later.

        For LogicVecArray the key is "<name>_<dir>" (e.g. "pq_addr_i") because
        getNameRead/Write on an array requires an index. For scalar signals the
        key is derived from getNameRead / getNameWrite as usual.
        """
        if isinstance(signal, LogicVecArray):
            key = f"{signal.name}_{signal.type}"
        elif signal.type == "i":
            key = signal.getNameRead()
        else:
            key = signal.getNameWrite()
        self.ports[key] = signal
        return signal

    def generate(self, em: Emitter, lsq_submodules, path_rtl) -> None:
        """
        Generates the VHDL 'entity' and 'architecture' sections for a Load Queue.

        Appends the following to '<path_rtl>/<self.name>.vhd':
            1. 'entity <self.module_name>' declaration
            2. 'architecture arch of <self.module_name>' implementation

        Parameters:
            em              : an instance of the Emitter class used for code generation
            lsq_submodules  : unused (kept for API compatibility)
            path_rtl        : Output directory for VHDL files.

        """
        self.ports.clear()

        ######  Queue Inputs ######
        # ===[ predecessor ]===
        pq_addr_i = self._add_port(LogicVecArray(
            em, "pq_addr", "i", self.configs.pq.num_entries, self.configs.pq.addr_width
        ))
        pq_done_i     = self._add_port(LogicVec(em, "pq_done",    "i", self.configs.pq.q_addr_width))
        pq_done_en_i  = self._add_port(Logic   (em, "pq_send_en", "i"))
        pq_alloc_en_i = self._add_port(Logic   (em, "pq_alloc_en","i"))

        # ====[ successor ]===
        sq_addr_i = self._add_port(LogicVecArray(
            em, "sq_addr", "i", self.configs.sq.num_entries, self.configs.sq.addr_width
        ))
        self._add_port(         LogicVec(em, "sq_tail",      "i", self.configs.sq.q_addr_width))
        sq_head_i      = self._add_port(LogicVec(em, "sq_head",      "i", self.configs.sq.q_addr_width))
        sq_access_en_i = self._add_port(Logic   (em, "sq_access_en", "i"))

        ######  Outputs ######
        allow_pq_alloc_o  = self._add_port(Logic(em, "allow_pq_alloc",  "o"))
        allow_sq_access_o = self._add_port(Logic(em, "allow_sq_access", "o"))

        tail_offset      = LogicVec(em, "tail_offset",      "w", self.configs.tail_offset_width)
        access_disparity = LogicVec(em, "access_disparity", "w", self.configs.access_disparity_width, is_signed=True)

        conflict = Logic(em, "conflict", "w")

        em.add_assignment(access_disparity, access_disparity - pq_done_en_i + sq_access_en_i)

        check_mask = LogicVec(em, "check_mask", "w", self.configs.pq.num_entries)
        ones = LogicVec(em, "ones", "w", self.configs.pq.num_entries)
        # generate access_disparity 1's for the check mask
        for i in range(self.configs.pq.num_entries):
            em.add_assignment((ones, i), Bit(1).when(Val(str(i)) <= access_disparity).else_(Bit(0)))
        CyclicRightShift(em, check_mask, ones, pq_done_i)

        head_address = LogicVec(em, "head_address", "w", self.configs.sq.addr_width)
        MuxLookUp(em, head_address, sq_addr_i, sq_head_i)

        conflicts = LogicVec(em, "conflicts", "w", self.configs.pq.num_entries)
        for i in range(self.configs.pq.num_entries):
            em.add_assignment((conflicts, i), Val(check_mask, i) & (Val(pq_addr_i, i) == head_address))

        Reduce(em, conflict, conflicts, BinOp.OR)

        em.add_assignment(tail_offset, tail_offset + pq_alloc_en_i - sq_access_en_i)

        em.add_comment("Allow access if:"
                    "\t- The tail position is not 0 (i.e. the predecessor has allocated the corresponding entry for this access"
                    "\t- AND, there is either: " \
                    "       - no conflict  "
                    "       - if the access disparity is negative, so the tail has moved past any preceding accesses from the precessor"
                    "\t- AND, the access disparity offset is not maxed out"
                    )

        tail_offset_width = self.configs.tail_offset_width
        max_ad_val = (1 << (self.configs.access_disparity_width - 1)) - 1
        em.add_assignment(allow_sq_access_o, (tail_offset != Val(0, tail_offset_width)) & (~conflict | (access_disparity < Val(0))) & (access_disparity <= Val(max_ad_val)))
        em.add_assignment(allow_pq_alloc_o, (tail_offset != Val((1 << tail_offset_width) - 1, tail_offset_width)))

        # don't allow the successor to send if the access disparity is max negative already
        # TODO: Implement this
        # issue with this: Done is a different number than send, so would need to keep track of a separate disparity between sends of P and accesses of S, if sends max out then already stop, however increases complexity :(

        # Write to the file
        output_str = em.get_definition_str(self.module_name)
        with open(f"{path_rtl}/{self.name}.{em.get_file_suffix()}", "a") as file:
            file.write(output_str)

    def instantiate(self, em: Emitter, signal_map: dict) -> Emitter:
        """
        Dependency Checker Instantiation

        Creates the port mapping for the DependencyChecker entity using the ports
        recorded during generate(). Must be called after generate().

        Parameters:
            em          : Emitter for the calling (top-level) module.
            signal_map  : Dict mapping each port key to the corresponding external
                          signal in the calling architecture. Keys match self.ports
                          (scalar ports use the entity port name, e.g. "pq_done_i";
                          array ports use "<name>_<dir>", e.g. "pq_addr_i"). Values
                          are Logic, LogicVec, or LogicVecArray objects from the
                          calling module.

        Returns:
            The emitter after the instantiation has been appended.

        Example:
            dep_checker.instantiate(em, {
                "pq_addr_i":         lq_q_addr,       # LogicVecArray
                "pq_done_i":         lq_q_done,
                "pq_send_en_i":      lq_done_en,
                "pq_alloc_en_i":     lq_alloc_en,
                "sq_addr_i":         sq_q_addr,       # LogicVecArray
                "sq_tail_i":         sq_q_tail,
                "sq_head_i":         sq_q_head,
                "sq_access_en_i":    sq_load_en,
                "allow_pq_alloc_o":  lq_allow_alloc,
                "allow_sq_access_o": sq_allow_access,
            })

        The set of required keys equals self.ports.keys(), which is populated
        by generate().
        """
        assert self.ports, "instantiate() must be called after generate()"

        em.start_instantiation(self.module_name)
        em.add_map("rst", "rst")
        em.add_map("clk", "clk")

        for port_name, port_signal in self.ports.items():
            ext = signal_map[port_name]
            if isinstance(port_signal, LogicVecArray):
                for i in range(port_signal.length):
                    if port_signal.type == "i":
                        em.add_map(port_signal.getNameRead(i), ext.getNameRead(i))
                    else:
                        em.add_map(port_signal.getNameWrite(i), ext.getNameWrite(i))
            elif port_signal.type == "i":
                em.add_map(port_name, ext.getNameRead())
            else:
                em.add_map(port_name, ext.getNameWrite())

        em.complete_instantiation()
        return em
