from core_gen.emitters.emitter import Emitter, Meta
from core_gen.ir import Statement, Bin, Un, BinOp, UnOp, Bit, Type, Val
from core_gen.signals import Logic, LogicVec, LogicArray, LogicVecArray


# ===----------------------------------------------------------------------===#
# Global Parameter Initialization
# ===----------------------------------------------------------------------===#
class VerilogEmitter(Emitter):
    """
    A context object to replace global variables for VHDL code generation.
    Holds indentation level, temporary name counter, and initialization strings.
    """

    def __init__(self, clock_name="clk", reset_name="rst"):
        # Initialize common emitter fields
        super().__init__(clock_name=clock_name, reset_name=reset_name)

    def get_reg_init_str(self) -> str:
        return f"always @(posedge {self.clock_name}) begin\n"

    def get_reg_end_str(self) -> str:
        return "end\n"

    def get_port_init_str(self) -> str:
        return f"(\n\t\tinput {self.reset_name},\n\t\tinput {self.clock_name}"

    def get_port_end_str(self) -> str:
        return "\n\t);"

    def add_reg_str(self, code: str):
        self.regInitString += self.get_current_indent() + code + "\n"

    def add_comment(self, comment: str):
        for line in comment.split("\n"):
            self.statementString += self.get_current_indent() + f"// {line}\n"

    def add_assignment(self, out, statement: Statement, in_process=False, to_reg_str=False):
        out_str, size = self.assigned_var_to_str(out, use_read_name=to_reg_str)
        out_type = out.get_type() if isinstance(out, Logic) else Type.LOGIC

        meta = Meta(size, out_type, -1)
        statement_str = statement.to_str(self, meta)

        if out_type == Type.SIGNED and statement.get_type() not in (Type.SIGNED, Type.ANY):
            statement_str = f"$signed({statement_str})"

        if in_process or to_reg_str:
            line = f"{out_str} <= {statement_str};"
        else:
            line = f"assign {out_str} = {statement_str};"

        if to_reg_str:
            self.add_reg_str(line)
        else:
            self.statementString += self.get_current_indent() + line + "\n"

    def get_definition_str(self, module_name: str, write_regs=True) -> str:
        return (
            f"module {module_name} "
            + self.get_current_indent()
            + self.get_port_init_str()
            + self.portInitString
            + self.get_port_end_str()
            + "\n"
            + "// SIGNAL INIT\n"
            + self.signalInitString
            + "\n"
            + "// STATEMENTS\n"
            + self.statementString
            + "\n"
            + (
                self.get_current_indent()
                + self.get_reg_init_str()
                + self.regInitString
                + self.get_reg_end_str()
                if write_regs and self.regInitString != ""
                else ""
            )
            + "endmodule\n"
        )

    def start_instantiation(self, module_name: str, instance_name: str = None) -> str:
        if self.inst_started:  # Sanity check to prevent overlapping instantiations
            raise ValueError(
                "start_instantiation called while another instantiation is in progress"
            )

        if instance_name is None:
            instance_name = module_name

        self.inst_started = True
        self.first_map = True
        self.inst_str = f"{self.get_current_indent()}{module_name} {instance_name} (\n"
        self.increase_indent()

    def add_map(self, port_name: str, signal_name: str = "") -> str:
        if not self.inst_started:
            raise ValueError("add_map can only be called after start_instantiation")

        assert isinstance(port_name, str) and isinstance(
            signal_name, str
        ), "port name and signal name must be strings"

        if not self.first_map:
            self.inst_str += ",\n"
        else:
            self.first_map = False

        self.inst_str += f"{self.get_current_indent()}.{port_name}({signal_name})"

    def complete_instantiation(self) -> str:
        self.inst_started = False
        self.decrease_indent()
        self.inst_str += self.get_current_indent() + ");\n"
        self.statementString += self.inst_str
        self.inst_str = ""

    BINOP_STRINGS = {
        BinOp.ADD: "+",
        BinOp.SUB: "-",
        BinOp.AND: "&",
        BinOp.OR: "|",
        BinOp.XOR: "^",
        BinOp.MUL: "*",
        BinOp.GE: ">=",
        BinOp.LE: "<=",
        BinOp.GT: ">",
        BinOp.LT: "<",
        BinOp.EQ: "==",
        BinOp.NEQ: "!=",
    }

    def get_binop_str(self, op: Bin) -> str:
        if op in self.BINOP_STRINGS:
            return self.BINOP_STRINGS[op]
        else:
            raise ValueError("Invalid binary operator: " + str(op))

    def get_unop_str(self, unop: UnOp) -> str:
        if unop == UnOp.NOT:
            return "~"
        else:
            raise ValueError("Invalid unary operator")

    def get_bit_str(self, bit: Bit) -> str:
        if bit.value == 0:
            return "1'b0"
        elif bit.value == 1:
            return "1'b1"
        else:
            raise ValueError("Invalid bit value")

    def bin_to_str(self, bin: Bin, meta: Meta) -> str:
        param_type = bin.get_param_type()
        left_type = bin.left.get_type()
        right_type = bin.right.get_type()

        # Propagate a concrete type to the ANY operand so integers are rendered correctly
        effective_left = right_type if left_type == Type.ANY and right_type != Type.ANY else param_type
        effective_right = left_type if right_type == Type.ANY and left_type != Type.ANY else param_type

        left_str = bin.left.to_str(self, Meta(meta.size, effective_left, bin.get_precedence()))
        right_str = bin.right.to_str(self, Meta(meta.size, effective_right, bin.get_precedence()))

        if bin.op == BinOp.CONCAT:
            return f"{{{left_str}, {right_str}}}"

        if param_type == Type.SIGNED:
            left_str = f"$signed({left_str})"
            right_str = f"$signed({right_str})"

        return f"{left_str} {self.get_binop_str(bin.op)} {right_str}"

    def un_to_str(self, un: Un, meta: Meta) -> str:
        meta = Meta(meta.size, un.get_type(), un.get_precedence())
        val_str = un.val.to_str(self, meta)
        return f"{self.get_unop_str(un.op)} {val_str}"

    def when_else_to_str(self, when_else, meta: Meta) -> str:
        meta = Meta(meta.size, when_else.get_type(), when_else.get_precedence())
        true_str = when_else.true_statement.to_str(self, meta)
        false_str = when_else.false_statement.to_str(self, meta)
        cond_str = when_else.condition.to_str(self, meta)

        return f"{cond_str} ? {true_str} : {false_str}"

    def logic_signal_init(self, signal: Logic, sufix: str):
        """
        Appends the appropriate declaration or port line for this signal to a global buffer.
        """
        if signal.type == "w":
            prefix = "reg" if signal.force_reg else "wire"
            self.add_signal_str(f"\t{prefix} {signal.get_base_name(sufix)};\n")
        elif signal.type == "r":
            self.add_signal_str(f"\twire {signal.get_base_name(sufix)}_d;\n")
            self.add_signal_str(f"\treg {signal.get_base_name(sufix)}_q;\n")
        elif signal.type == "i":
            self.add_port_str(",\n")
            self.add_port_str(
                f'\t\tinput {signal.get_base_name(sufix)}{'_i' if not signal.dyn_comp else ""}'
            )
        elif signal.type == "o":
            self.add_port_str(",\n")
            self.add_port_str(
                f'\t\toutput {signal.get_base_name(sufix)}{'_o' if not signal.dyn_comp else ""}'
            )

    def logicvec_signal_init(self, vec: LogicVec, sufix: str):
        signed_str = " signed" if vec.is_signed else ""
        if vec.type == "w":
            prefix = "reg" if vec.force_reg else "wire"
            self.add_signal_str(
                f"\t{prefix}{signed_str} [{vec.size-1}:0] {vec.get_base_name(sufix)};\n"
            )
        elif vec.type == "r":
            self.add_signal_str(
                f"\twire{signed_str} [{vec.size-1}:0] {vec.get_base_name(sufix)}_d;\n"
            )
            self.add_signal_str(
                f"\treg{signed_str} [{vec.size-1}:0] {vec.get_base_name(sufix)}_q;\n"
            )
        elif vec.type == "i":
            self.add_port_str(",\n")
            self.add_port_str(
                f'\t\tinput{signed_str} [{vec.size-1}:0] {vec.get_base_name(sufix)}{'_i' if not vec.dyn_comp else ""}'
            )
        elif vec.type == "o":
            self.add_port_str(",\n")
            self.add_port_str(
                f'\t\toutput{signed_str} [{vec.size-1}:0] {vec.get_base_name(sufix)}{'_o' if not vec.dyn_comp else ""}'
            )

    def logic_reg_init(self, logic: Logic, enable=None, init=None) -> None:
        """
        Generates a clocked process snippet that sets up the register's behavior.
        For example,

        if (rst = '1') then
            <name>_q <= '0';
        elsif (rising_edge(clk)) then
            <name>_q <= <name>_d;
        end if;
        """
        assert logic.type == "r"
        if init is None:
            init = 0
        self.increase_indent()  # 1→2
        in_else = False
        if init is not None:
            self.add_reg_str(f"if ({self.reset_name})")
            self.increase_indent()  # 2→3
            self.add_assignment(logic, Bit(init), to_reg_str=True)
            self.decrease_indent()  # 3→2
            self.add_reg_str("else begin")
            in_else = True
            self.increase_indent()  # 2→3

        if enable is not None:
            self.add_reg_str(f"if ({enable.getNameRead()})")
            self.increase_indent()  # →+1
            self.add_assignment(logic, Val(logic.getNameWrite()), to_reg_str=True)
            self.decrease_indent()  # ←-1
        else:
            self.add_assignment(logic, Val(logic.getNameWrite()), to_reg_str=True)

        if in_else:
            self.decrease_indent()  # 3→2
            self.add_reg_str("end")
        self.decrease_indent()  # 2→1

    def logicvec_reg_init(self, vec: LogicVec, enable=None, init=None) -> None:
        assert vec.type == "r"
        if init is None:
            init = 0
        self.increase_indent()  # 1→2
        in_else = False
        if init is not None:
            self.add_reg_str(f"if ({self.reset_name})")
            self.increase_indent()  # 2→3
            self.add_assignment(vec, Val(init), to_reg_str=True)
            self.decrease_indent()  # 3→2
            self.add_reg_str("else begin")
            in_else = True
            self.increase_indent()  # 2→3

        if enable is not None:
            self.add_reg_str(f"if ({enable.getNameRead()})")
            self.increase_indent()  # →+1
            self.add_assignment(vec, Val(vec.getNameWrite()), to_reg_str=True)
            self.decrease_indent()  # ←-1
        else:
            self.add_assignment(vec, Val(vec.getNameWrite()), to_reg_str=True)

        if in_else:
            self.decrease_indent()  # 3→2
            self.add_reg_str("end")
        self.decrease_indent()  # 2→1

    def logicarray_reg_init(self, array: LogicArray, enable=None, init=None) -> None:
        assert array.type == "r"
        self.increase_indent()  # 1→2
        if init is None:
            init = [0] * array.length
        in_else = False
        if init is not None:
            self.add_reg_str(f"if ({self.reset_name}) begin")
            self.increase_indent()  # 2→3
            for i in range(array.length):
                self.add_assignment((array, i), Bit(init[i]), to_reg_str=True)
            self.decrease_indent()  # 3→2
            self.add_reg_str("end")
            self.add_reg_str("else begin")
            in_else = True
            self.increase_indent()  # 2→3

        if enable is not None:
            for i in range(array.length):
                self.add_reg_str(f"if ({enable.getNameRead(i)})")
                self.increase_indent()  # →+1
                self.add_assignment((array, i), Val(array.getNameWrite(i)), to_reg_str=True)
                self.decrease_indent()  # ←-1
        else:
            for i in range(array.length):
                self.add_assignment((array, i), Val(array.getNameWrite(i)), to_reg_str=True)

        if in_else:
            self.decrease_indent()  # 3→2
            self.add_reg_str("end")
        self.decrease_indent()  # 2→1

    def logicvecarray_reg_init(
        self, array: LogicVecArray, enable=None, init=None
    ) -> None:
        assert array.type == "r"
        self.increase_indent()  # 1→2
        if init is None:
            init = [0] * array.length
        in_else = False
        if init is not None:
            self.add_reg_str(f"if ({self.reset_name}) begin")
            self.increase_indent()  # 2→3
            for i in range(array.length):
                self.add_assignment((array, i), Val(init[i]), to_reg_str=True)
            self.decrease_indent()  # 3→2
            self.add_reg_str("end")
            self.add_reg_str("else begin")
            in_else = True
            self.increase_indent()  # 2→3

        if enable is not None:
            for i in range(array.length):
                self.add_reg_str(f"if ({enable.getNameRead(i)})")
                self.increase_indent()  # →+1
                self.add_assignment((array, i), Val(array.getNameWrite(i)), to_reg_str=True)
                self.decrease_indent()  # ←-1
        else:
            for i in range(array.length):
                self.add_assignment((array, i), Val(array.getNameWrite(i)), to_reg_str=True)

        if in_else:
            self.decrease_indent()  # 3→2
            self.add_reg_str("end")
        self.decrease_indent()  # 2→1

    def get_file_suffix(self) -> str:
        return "v"

    def index_var(self, var_name, index):
        return f"{var_name}[{index}]"

    def slice_var(self, var_name, high, low):
        return f"{var_name}[{high}:{low}]"

    @staticmethod
    def int_to_str(din, size=None, meta=None) -> str:
        if meta is not None and meta.type in (Type.ARITH, Type.SIGNED):
            return str(din)

        if size == None:
            size = 1

        return f"{size}'b{Emitter._int_to_bin(din, size)}"

    @staticmethod
    def mask_less(din, size) -> str:
        """
        Example:
            MaskLess(3, 5)  # Output: "00111"
            MaskLess(2, 6)  # Output: "000011"
            MaskLess(5, 5)  # Output: "11111"
            MaskLess(0, 4)  # Output: "0000"
        """
        if din > size:
            raise ValueError("Unknown value!")
        return f"{size}'b" + "0" * (size - din) + "1" * din

    @staticmethod
    def new() -> Emitter:
        return VerilogEmitter()

    def mux_index(self, din, sel) -> str:
        """
        Generate a Verilog array-index expression for selecting an element
        """
        return f"{din.getNameRead()}[{sel.getNameRead()}]"
    
    def get_custom_str(self, custom_statement) -> str:
        return custom_statement.verilog_str

    def add_custom_statement(self, custom_statement):
        for line in self.get_custom_str(custom_statement).splitlines():
            self.statementString += self.get_current_indent() + line + "\n"
