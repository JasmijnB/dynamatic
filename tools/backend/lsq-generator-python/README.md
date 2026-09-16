### LSQ generator
This Python-based LSQ generator generates the LSQ design outlined in Hailin Wang's master thesis.

### Configuration parameters

- `ldOrder`: Defines the order matrix for each group, the same as the previous `loadOffsets` parameter with a new format.
- `ldPortIdx`: Specifies the access port index for each load operation within a group.
- `stPortIdx`: Specifies the access port index for each store operation within a group.
- `indexWidth`: Number of bits for the ID in the memory interfaces.
- `numLdChannels`: Indicates the number of load channels at the memory interface (fixed to 1 in this design).
- `numStChannels`: Indicates the number of store channels at the memory interface (fixed to 1 in this design).
- `stResp`: Enables or disables the store response channel in the store access port.
- `groupMulti`: Whether multiple groups are allowed to request an allocation at the same cycle.
- `pipe0En`: Enables or disables the insertion of pipeline register 0 in the LSQ.
- `pipe1En`: Enables or disables the insertion of pipeline register 1 in the LSQ.
- `pipeCompEn`: Enables or disables the insertion of the `pipeComp` pipeline register in the LSQ.
- `headLagEn`: Determines whether the head pointer of the load queue updates one cycle later than the valid bits of its entries.
- `bypassEn`: Enables or disables the bypass network (for forwarding store data to subsequent loads).


### Sampele usage

```
usage: lsq-generator.py [-h] [--output-dir OUTPUT_PATH] --config-file CONFIG_FILES [--hdl [vhdl|verilog]]
```

### Sample json configuration file (Example: Histogram)


```
{
  "addrWidth":10,
  "bufferDepth":0,
  "bypassEn": 1,
  "dataWidth":32,
  "fifoDepth":16,
  "fifoDepth_L":16,
  "fifoDepth_S":16,
  "groupMulti":0,
  "headLagEn":0,
  "indexWidth":4,
  "ldOrder":[[0]],
  "ldPortIdx":[[0]],
  "loadOffsets":[[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]],
  "loadPorts":[[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]],
  "master":true,
  "name":"handshake_lsq_lsq1",
  "numBBs":1,
  "numLdChannels":1,
  "numLoadPorts":1,
  "numLoads":[1],
  "numStChannels":1,
  "numStorePorts":1,
  "numStores":[1],
  "pipe0En":0,
  "pipe1En":0,
  "pipeCompEn":0,
  "stPortIdx":[[0]],
  "stResp":0,
  "storeOffsets":[[1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]],
  "storePorts":[[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]]
  }
```

### Generated Files

- `<lsq_name>.vhd` (or `.v` with `--hdl verilog`) : A wrapper module that instantiates the core LSQ logic and integrates the required components for memory port interfaces. The new design assumes AXI interfaces.
- `<lsq_name>_core.vhd` (or `.v` with `--hdl verilog`): Contains the core LSQ logic, which is derived from Hailin Wang's master thesis. Minor modifications have been made to the code for integration purposes, but the core logic remains unchanged.

---
### Revert to chisel LSQ generator
Configuration parameters needed for both chisel and Python based LSQ-generator coexist in the JSON file. If you want to use the chisel based LSQ generator, please change the corresponding location in `$DYNAMATIC/data/rtl-config-vhdl.json` to:

```
{
    "name": "handshake.lsq",
    "generator": "java -jar -Xmx7G \"$DYNAMATIC/bin/generators/lsq-generator.jar\" --target-dir \"$OUTPUT_DIR\" --spec-file \"$OUTPUT_DIR/$MODULE_NAME.json\" > /dev/null",
    "use-json-config": "$OUTPUT_DIR/$MODULE_NAME.json",
    "hdl": "verilog",
    "io-kind": "flat",
    "io-map": [{ "clk": "clock" }, { "rst": "reset" }, { "*": "io_*" }],
    "io-signals": {
      "data": "_bits"
    }
  },
```



## Directory & File Descriptions

- **lsq-generator.py**  
  Runs the tool.

- **core_gen/**
  - **\_\_init__.py**  
    Re-exports a curated list of public API symbols (e.g. `main`, `generate`, `Logic`, `LSQ`).

  - **codegen.py**  
    Implements the `codeGen(config: Configs)` function.

  - **configs.py**  
    Defines the `Configs` class.

  - **ir.py**  
    Defines the language-agnostic intermediate representation that the generators
    build: `Statement`, `Type`, `Val`, `BinOp`, `Bin`, `UnOp`, `Un`, `Bit`,
    `CustomStatement`, `WhenElse`.

  - **utils.py**  
    - `GetValue`, `isPow2`, `log2Ceil` helper functions.

  - **signals.py**  
    Defines the four signal classes:  `Logic`, `LogicVec`, `LogicArray`, `LogicVecArray`.
    Pass `dyn_comp=True` to drop the `_i`/`_o` port suffixes, which the LSQ
    wrapper needs to match the port names used by Dynamatic.

  - **core_gen/operators/**  
    Low-level functions that build the intermediate representation:  
    - `arithmetic.py`: `WrapAdd`, `WrapAddConst`, `WrapSub`
    - `conversions.py`: `VecToArray`, `BitsToOH`, `BitsToOHSub1`, `OHToBits`
    - `masking.py`: `CyclicPriorityMasking`, `CyclicRangeFill`
    - `mux.py`: `Mux1H`, `Mux1HROM`, `MuxLookUp`
    - `reduction.py`: `ReduceLogicVec`, `ReduceLogicArray`, `ReduceLogicVecArray`, `Reduce`
    - `shifts.py`: `RotateLogicVec`, `RotateLogicArray`, `RotateLogicVecArray`, `CyclicLeftShift`, `CyclicRightShift`

  - **core_gen/generators/**  
    High-level modules that build complete entities/architectures:  
    - `dispatchers.py` : `PortToQueueDispatcher`, `QueueToPortDispatcher`, `PortToQueueDispatcherInit`, `QueueToPortDispatcherInit`
    - `group_allocator.py` : `GroupAllocator`, `GroupAllocatorInit`
    - `lsq.py` : `LSQ`

  - **core_gen/emitters/**  
    Render the intermediate representation into a concrete HDL. The generators
    only talk to the abstract `Emitter`, so adding a language means adding a
    subclass here.  
    - `emitter.py` : abstract base class `Emitter`, plus `Meta`
    - `vhdl_emitter.py` : `VHDLEmitter`
    - `verilog_emitter.py` : `VerilogEmitter`

 

---

## Ordering-network generator

The ordering network is an alternative to the monolithic LSQ: instead of one
queue pair with a central dependency matrix, it builds one queue per memory
port and one dependency checker per program-order edge between them.

- **ordering-network-generator.py**
  Runs the tool.

  ```
  usage: ordering-network-generator.py -o OUTPUT_DIR -c CONFIG_JSON --hdl [vhdl|verilog]
  ```

  Generates `<name>_core.<suffix>` (the structure, its queues and its
  dependency checkers) and `<name>.<suffix>` (a wrapper exposing the Dynamatic
  LSQ interface; ordering networks always connect through a memory controller).

- **custom_core_gen/**
  - **configs.py**
    `QueueConfig`, `DependencyCheckerConfig`, `OrderingNetworkConfig`
    (`OrderingNetworkConfig.from_json` parses the tool's config file).

  - **generators/**
    - `generator.py` : `Generator` base class — port bookkeeping,
      `instantiate()`, and the shared Dynamatic master interface.
    - `queue.py` : `Queue` — a single memory port's address/data queue.
    - `dependency_checker.py` : `DependencyChecker` — gates a successor port's
      accesses on the predecessor port's outstanding ones (same-BB counter
      scheme, cross-BB dep-array scheme, and a forced-sequential variant).
    - `structure.py` : `Structure` — instantiates the queues and dependency
      checkers for one config and wires them together.

  - **testbenches/**
    One directory per testbench, each with a `generate.py`, a config JSON and a
    SystemVerilog `*_tb.sv`. `test.py` is a pytest suite that runs every
    `generate.py` and, when Questa/ModelSim is on `PATH`, simulates the
    generated Verilog and VHDL against the testbench:

    ```
    PYTHONPATH=. python -m pytest custom_core_gen/testbenches/test.py
    ```
