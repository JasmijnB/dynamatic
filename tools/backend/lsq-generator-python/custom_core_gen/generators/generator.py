from core_gen.emitters import Emitter
from core_gen.signals import Logic, LogicVec, LogicVecArray


class Generator:
    def __init__(self, name: str, suffix: str, configs):
        self.name = name
        self.module_name = name + suffix
        self.configs = configs
        self.ports: dict[str, Logic | LogicVec | LogicVecArray] = {}

    def _add_port(self, signal: Logic | LogicVec | LogicVecArray) -> Logic | LogicVec | LogicVecArray:
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

    def _write_to_file(self, em: Emitter, path_rtl: str) -> None:
        output_str = em.get_definition_str(self.module_name)
        with open(f"{path_rtl}/{self.name}.{em.get_file_suffix()}", "a") as file:
            file.write(output_str)

    def get_ports(self) -> dict:
        if not self.ports:
            raise ValueError("Ports have not been generated yet. Call generate() before get_ports().")
        return self.ports

    def instantiate(self, em: Emitter, signal_map: dict, instance_name: str = None) -> Emitter:
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
                    raise ValueError(f"Input port {port_name} not found in signal_map. All input ports must be mapped.")
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
