from core_gen.emitters import Emitter
from core_gen.signals import Logic, LogicVec, LogicVecArray


class Generator:
    def __init__(self, name: str, suffix: str, configs):
        self.name = name
        self.module_name = name + suffix
        self.configs = configs
        self.ports: dict[str, Logic | LogicVec | LogicVecArray] = {}

    def _add_port(
        self, signal: Logic | LogicVec | LogicVecArray
    ) -> Logic | LogicVec | LogicVecArray:
        """Register a port signal so instantiate() can use it later.

        For LogicVecArray the key is "<name>_<dir>" because getNameRead/Write
        on an array requires an index. For scalar signals the key is derived
        from getNameRead / getNameWrite as usual.
        """
        if isinstance(signal, LogicVecArray):
            key = f"{signal.name}_{signal.type}"
        elif signal.type == "i":
            key = signal.getNameRead()
        else:
            key = signal.getNameWrite()
        self.ports[key] = signal
        return signal

    def _write_to_file(self, em: Emitter, path_rtl: str, out_file: str = None) -> None:
        output_str = em.get_definition_str(self.module_name)
        path = (
            out_file
            if out_file is not None
            else f"{path_rtl}/{self.name}.{em.get_file_suffix()}"
        )
        with open(path, "a") as file:
            file.write(output_str)

    def get_ports(self) -> dict:
        if not self.ports:
            raise ValueError(
                "Ports have not been generated yet. Call generate() before get_ports()."
            )
        return self.ports

    def _generate_master_interface(self, em: Emitter, empty: Logic):
        memStart_ready = self._add_port(Logic(em, "memStart_ready", "o"))
        memStart_valid = self._add_port(Logic(em, "memStart_valid", "i"))
        ctrlEnd_ready = self._add_port(Logic(em, "ctrlEnd_ready", "o"))
        ctrlEnd_valid = self._add_port(Logic(em, "ctrlEnd_valid", "i"))
        memEnd_ready = self._add_port(Logic(em, "memEnd_ready", "i"))
        memEnd_valid = self._add_port(Logic(em, "memEnd_valid", "o"))

        memStartReady = Logic(em, "memStartReady", "w", force_reg=True)
        memEndValid = Logic(em, "memEndValid", "w", force_reg=True)
        ctrlEndReady = Logic(em, "ctrlEndReady", "w", force_reg=True)
        temp_gen_mem = Logic(em, "TEMP_GEN_MEM", "w")

        em.add_comment(
            "This signal indicates that all mem. ops are completed and func. can return."
        )
        em.add_comment("Queue can return iff all the following conditions are true:")
        em.add_comment("1. No more upcoming BBs containing memory accesses.")
        em.add_comment("2. The queue is empty.")
        em.add_assignment(temp_gen_mem, ctrlEnd_valid & empty)

        em.add_comment("Define logic for the new interfaces needed by dynamatic")
        vhdl_str = "\tprocess (clk) is\n\tbegin\n"
        vhdl_str += "\t\tif rising_edge(clk) then\n"
        vhdl_str += "\t\t\tif rst = '1' then\n"
        vhdl_str += "\t\t\t\tmemStartReady <= '1';\n"
        vhdl_str += "\t\t\t\tmemEndValid <= '0';\n"
        vhdl_str += "\t\t\t\tctrlEndReady <= '0';\n"
        vhdl_str += "\t\t\telse\n"
        vhdl_str += "\t\t\t\tmemStartReady <= (memEndValid and memEnd_ready_i) or ((not (memStart_valid_i and memStartReady)) and memStartReady);\n"
        vhdl_str += "\t\t\t\tmemEndValid <= TEMP_GEN_MEM or memEndValid;\n"
        vhdl_str += "\t\t\t\tctrlEndReady <= (not (ctrlEnd_valid_i and ctrlEndReady)) and (TEMP_GEN_MEM or ctrlEndReady);\n"
        vhdl_str += "\t\t\tend if;\n"
        vhdl_str += "\t\tend if;\n"
        vhdl_str += "\tend process;\n\n"

        verilog_str = """
always @(posedge clk) begin
    if (rst) begin
        memStartReady <= 1'b1;
        memEndValid   <= 1'b0;
        ctrlEndReady  <= 1'b0;
    end
    else begin
        memStartReady <= (memEndValid && memEnd_ready_i) ||
                         ((!(memStart_valid_i && memStartReady)) && memStartReady);
        memEndValid   <= TEMP_GEN_MEM || memEndValid;
        ctrlEndReady  <= (!(ctrlEnd_valid_i && ctrlEndReady)) &&
                         (TEMP_GEN_MEM || ctrlEndReady);
    end
end
        """
        em.add_custom_statement(CustomStatement(vhdl_str, verilog_str))

        em.add_comment("Update new memory interfaces")
        em.add_assignment(memStart_ready, memStartReady)
        em.add_assignment(ctrlEnd_ready, ctrlEndReady)
        em.add_assignment(memEnd_valid, memEndValid)

    def instantiate(
        self, em: Emitter, signal_map: dict, instance_name: str = None
    ) -> Emitter:
        """Wire this module into a parent by mapping each port to an external signal.

        Must be called after generate(). Keys in signal_map must match self.ports.keys().
        Array ports use key "<name>_<dir>" (e.g. "pq_addr_i"); scalar ports use the
        full port name (e.g. "pq_done_i", "allow_pq_alloc_o").
        """
        assert self.ports, "instantiate() must be called after generate()"
        if instance_name is None:
            instance_name = self.module_name

        em.start_instantiation(self.module_name, instance_name)
        em.add_map("rst", "rst")
        em.add_map("clk", "clk")

        for port_name, port_signal in self.ports.items():
            if port_name not in signal_map:
                if port_signal.type == "i":
                    raise ValueError(
                        f"Input port {port_name} not found in signal_map. All input ports must be mapped."
                    )
                else:
                    if isinstance(port_signal, LogicVecArray):
                        for i in range(port_signal.length):
                            em.add_map(port_signal.getNameWrite(i))
                    else:
                        em.add_map(port_name)
            else:
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
