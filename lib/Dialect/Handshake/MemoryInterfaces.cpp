//===- MemoryInterfaces.cpp - Memory interface helpers ----------*- C++ -*-===//
//
// Dynamatic is under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Implements support to work with Handshake memory interfaces.
//
//===----------------------------------------------------------------------===//

#include "dynamatic/Dialect/Handshake/MemoryInterfaces.h"
#include "dynamatic/Dialect/Handshake/HandshakeAttributes.h"
#include "dynamatic/Dialect/Handshake/HandshakeInterfaces.h"
#include "dynamatic/Dialect/Handshake/HandshakeOps.h"
#include "dynamatic/Support/Attribute.h"
#include "dynamatic/Support/Backedge.h"
#include "dynamatic/Support/CFG.h"
#include "mlir/Dialect/Affine/IR/AffineOps.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/IR/MLIRContext.h"
#include "mlir/IR/Value.h"
#include "mlir/Transforms/DialectConversion.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/Twine.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/MathExtras.h"

using namespace llvm;
using namespace mlir;
using namespace dynamatic;
using namespace dynamatic::handshake;

//===----------------------------------------------------------------------===//
// MemoryInterfaceBuilder
//===----------------------------------------------------------------------===//

void MemoryInterfaceBuilder::addMCPort(handshake::MemPortOpInterface portOp) {
  std::optional<unsigned> bb = getLogicBB(portOp);
  assert(bb && "MC port must belong to basic block");
  if (isa<handshake::LoadOp>(portOp)) {
    ++mcNumLoads;
  } else {
    assert(isa<handshake::StoreOp>(portOp) && "invalid MC port");
  }
  mcPorts[*bb].push_back(portOp);
}

void MemoryInterfaceBuilder::addLSQPort(unsigned group,
                                        handshake::MemPortOpInterface portOp) {
  if (isa<handshake::LoadOp>(portOp)) {
    ++lsqNumLoads;
  } else {
    assert(isa<handshake::StoreOp>(portOp) && "invalid LSQ port");
    ++lsqNumStores;
  }
  lsqPorts[group].push_back(portOp);
}

LogicalResult MemoryInterfaceBuilder::instantiateInterfaces(
    OpBuilder &builder, handshake::MemoryControllerOp &mcOp,
    handshake::MemOrderingUnitOp &lsqOp) {
  BackedgeBuilder edgeBuilder(builder, memref.getLoc());

  FConnectLoad connect = [&](LoadOp loadOp, Value dataIn) {
    loadOp->setOperand(1, dataIn);
  };
  return instantiateInterfaces(builder, edgeBuilder, connect, mcOp, lsqOp);
}

LogicalResult MemoryInterfaceBuilder::instantiateInterfaces(
    PatternRewriter &rewriter, handshake::MemoryControllerOp &mcOp,
    handshake::MemOrderingUnitOp &lsqOp) {
  BackedgeBuilder edgeBuilder(rewriter, memref.getLoc());
  FConnectLoad connect = [&](LoadOp loadOp, Value dataIn) {
    rewriter.updateRootInPlace(loadOp, [&] { loadOp->setOperand(1, dataIn); });
  };
  return instantiateInterfaces(rewriter, edgeBuilder, connect, mcOp, lsqOp);
}

LogicalResult MemoryInterfaceBuilder::instantiateInterfaces(
    OpBuilder &builder, BackedgeBuilder &edgeBuilder,
    const FConnectLoad &connect, handshake::MemoryControllerOp &mcOp,
    handshake::MemOrderingUnitOp &lsqOp) {

  // Determine interfaces' inputs
  InterfaceInputs inputs;
  if (failed(determineInterfaceInputs(inputs, builder)))
    return failure();
  if (inputs.mcInputs.empty() && inputs.lsqInputs.empty())
    return success();

  mcOp = nullptr;
  lsqOp = nullptr;

  builder.setInsertionPointToStart(&funcOp.front());
  Location loc = memref.getLoc();

  if (!inputs.mcInputs.empty() && inputs.lsqInputs.empty()) {
    // We only need a memory controller
    mcOp = builder.create<handshake::MemoryControllerOp>(
        loc, memref, memStart, inputs.mcInputs, ctrlEnd, inputs.mcBlocks,
        mcNumLoads);
  } else if (inputs.mcInputs.empty() && !inputs.lsqInputs.empty()) {
    // We only need an LSQ
    lsqOp = builder.create<handshake::MemOrderingUnitOp>(
        loc, memref, memStart, inputs.lsqInputs, ctrlEnd, inputs.lsqGroupSizes,
        lsqNumLoads, orderingKind);
  } else {
    // We need a MC and an LSQ. They need to be connected with 4 new channels
    // so that the LSQ can forward its loads and stores to the MC. We need
    // load address, store address, and store data channels from the LSQ to
    // the MC and a load data channel from the MC to the LSQ
    unsigned nLoads, nStores;

    if (orderingKind == handshake::MemOrderingKind::LSQ) {
      nLoads = nStores = 1;
    } else {
      nLoads = lsqNumLoads;
      nStores = lsqNumStores;
    }

    MemRefType memrefType = memref.getType().cast<MemRefType>();

    // Create nLoads+nStores+nStores backedges for the MC inputs coming from the
    // OU: one load address per load, one store address and store data per
    // store.
    MLIRContext *ctx = builder.getContext();
    Type addrType = handshake::ChannelType::getAddrChannel(ctx);
    Type dataType = handshake::ChannelType::get(memrefType.getElementType());
    std::vector<Backedge> ldAddrs, stAddrs, stDatas;
    for (unsigned i = 0; i < nLoads; ++i)
      ldAddrs.push_back(edgeBuilder.get(addrType));
    for (unsigned i = 0; i < nStores; ++i) {
      stAddrs.push_back(edgeBuilder.get(addrType));
      stDatas.push_back(edgeBuilder.get(dataType));
    }
    for (Backedge &e : ldAddrs)
      inputs.mcInputs.push_back(e);
    for (Backedge &e : stAddrs)
      inputs.mcInputs.push_back(e);
    for (Backedge &e : stDatas)
      inputs.mcInputs.push_back(e);

    // Create the memory controller, adding nLoads to its load count so that it
    // generates a load data result for each OU load
    mcOp = builder.create<handshake::MemoryControllerOp>(
        loc, memref, memStart, inputs.mcInputs, ctrlEnd, inputs.mcBlocks,
        mcNumLoads + nLoads);

    // Add the MC's load data results to the OU's inputs and create the OU. The
    // OU produces one load-data result per circuit load port (lsqNumLoads), but
    // its MC-facing interface only has nLoads load channels and nStores store
    // channels (one each for an LSQ, one per port for an ordering network).
    ValueRange mcOutputs = mcOp.getOutputs();
    for (unsigned i = 0; i < nLoads; ++i)
      inputs.lsqInputs.push_back(mcOutputs[mcNumLoads + i]);
    lsqOp = builder.create<handshake::MemOrderingUnitOp>(
        loc, mcOp, inputs.lsqInputs, inputs.lsqGroupSizes,
        /*numCircuitLoads=*/lsqNumLoads, /*numMCLoads=*/nLoads,
        /*numMCStores=*/nStores, orderingKind);

    // Resolve the backedges to fully connect the MC and OU
    ValueRange lsqMemResults =
        lsqOp.getOutputs().take_back(nLoads + 2 * nStores);
    for (unsigned i = 0; i < nLoads; ++i)
      ldAddrs[i].setValue(lsqMemResults[i]);
    for (unsigned i = 0; i < nStores; ++i)
      stAddrs[i].setValue(lsqMemResults[nLoads + i]);
    for (unsigned i = 0; i < nStores; ++i)
      stDatas[i].setValue(lsqMemResults[nLoads + nStores + i]);
  }

  // At this point, all load ports are missing their second operand which is the
  // data value coming from a memory interface back to the port
  if (mcOp)
    reconnectLoads(mcPorts, mcOp, connect);
  if (lsqOp)
    reconnectLoads(lsqPorts, lsqOp, connect);

  return success();
}

SmallVector<Value, 2>
MemoryInterfaceBuilder::getMemResultsToInterface(Operation *memOp) {
  // For loads, address output goes to memory
  if (auto loadOp = dyn_cast<handshake::LoadOp>(memOp))
    return SmallVector<Value, 2>{loadOp.getAddressResult()};

  // For stores, all outputs (address and data) go to memory
  auto storeOp = dyn_cast<handshake::StoreOp>(memOp);
  assert(storeOp && "input operation must either be load or store");
  return SmallVector<Value, 2>{storeOp->getResults()};
}

Value MemoryInterfaceBuilder::getMCControl(Value ctrl, unsigned numStores,
                                           OpBuilder &builder) {
  assert(isa<handshake::ControlType>(ctrl.getType()) &&
         "control signal must have !handshake.control type");
  if (Operation *defOp = ctrl.getDefiningOp())
    builder.setInsertionPointAfter(defOp);
  else
    builder.setInsertionPointToStart(ctrl.getParentBlock());
  handshake::ConstantOp cstOp = builder.create<handshake::ConstantOp>(
      ctrl.getLoc(), builder.getI32IntegerAttr(numStores), ctrl);
  inheritBBFromValue(ctrl, cstOp);
  return cstOp.getResult();
}

LogicalResult
MemoryInterfaceBuilder::determineInterfaceInputs(InterfaceInputs &inputs,
                                                 OpBuilder &builder) {

  // Determine LSQ inputs
  for (auto [group, lsqGroupOps] : lsqPorts) {
    // First, determine the group's control signal, which is dictated by the BB
    // of the first memory port in the group
    Operation *firstOpInGroup = lsqGroupOps.front();
    std::optional<unsigned> block = getLogicBB(firstOpInGroup);
    if (!block)
      return firstOpInGroup->emitError() << "LSQ port must belong to a BB.";
    Value groupCtrl = getCtrl(*block);
    if (!groupCtrl)
      return failure();
    inputs.lsqInputs.push_back(groupCtrl);

    // Then, add all memory port results that go the interface to the list of
    // LSQ inputs
    for (Operation *lsqOp : lsqGroupOps) {
      llvm::copy(getMemResultsToInterface(lsqOp),
                 std::back_inserter(inputs.lsqInputs));
    }
    // Add the size of the group to our list
    inputs.lsqGroupSizes.push_back(lsqGroupOps.size());
  }

  // Ordering networks always connect through a memory controller, so even when
  // there are no direct MC circuit ports we still need to compute the block
  // control signals (from LSQ stores) so that instantiateInterfaces creates the
  // MC+ordering-network pair instead of a standalone ordering network.
  if (mcPorts.empty() &&
      orderingKind != handshake::MemOrderingKind::OrderingNetwork)
    return success();

  // The MC needs control signals from all blocks containing store ports
  // connected to an LSQ, since these requests end up being forwarded to the MC,
  // so we need to know the number of LSQ stores per basic block
  DenseMap<unsigned, unsigned> lsqStoresPerBlock;
  for (auto &[_, lsqGroupOps] : lsqPorts) {
    for (Operation *lsqOp : lsqGroupOps) {
      if (isa<handshake::StoreOp>(lsqOp)) {
        std::optional<unsigned> block = getLogicBB(lsqOp);
        if (!block)
          return lsqOp->emitError() << "LSQ port must belong to a BB.";
        ++lsqStoresPerBlock[*block];
      }
    }
  }

  // Inputs from blocks that have at least one direct load/store access port to
  // the MC are added to the future MC's operands first
  for (auto &[block, mcBlockOps] : mcPorts) {
    // Count the total number of stores in the block, either directly connected
    // to the MC or going through an LSQ
    unsigned numStoresInBlock = lsqStoresPerBlock.lookup(block);
    for (Operation *memOp : mcBlockOps) {
      if (isa<handshake::StoreOp>(memOp))
        ++numStoresInBlock;
    }

    // Blocks with at least one store need to provide a control signal fed
    // through a constant indicating the number of stores in the block
    if (numStoresInBlock > 0) {
      Value blockCtrl = getCtrl(block);
      if (!blockCtrl)
        return failure();
      inputs.mcInputs.push_back(
          getMCControl(blockCtrl, numStoresInBlock, builder));
    }

    // Traverse the list of memory operations in the block once more and
    // accumulate memory inputs coming from the block
    for (Operation *mcOp : mcBlockOps)
      llvm::copy(getMemResultsToInterface(mcOp),
                 std::back_inserter(inputs.mcInputs));

    inputs.mcBlocks.push_back(block);
  }

  // Control ports from blocks which do not have memory ports directly
  // connected to the MC but from which the LSQ will forward store requests from
  // are then added to the future MC's operands
  for (auto &[lsqBlock, numStores] : lsqStoresPerBlock) {
    // We only need to do something if the block has stores that have not yet
    // been accounted for
    if (mcPorts.contains(lsqBlock) || numStores == 0)
      continue;

    // Identically to before, blocks with stores need a cntrol signal
    Value blockCtrl = getCtrl(lsqBlock);
    if (!blockCtrl)
      return failure();
    inputs.mcInputs.push_back(getMCControl(blockCtrl, numStores, builder));

    inputs.mcBlocks.push_back(lsqBlock);
  }

  return success();
}

Value MemoryInterfaceBuilder::getCtrl(unsigned block) {
  auto groupCtrl = ctrlVals.find(block);
  if (groupCtrl == ctrlVals.end()) {
    llvm::errs() << "Cannot determine control signal for BB " << block << "\n";
    return nullptr;
  }
  return groupCtrl->second;
}

void MemoryInterfaceBuilder::reconnectLoads(InterfacePorts &ports,
                                            Operation *memIfaceOp,
                                            const FConnectLoad &connect) {
  unsigned resIdx = 0;
  for (auto &[_, memGroupOps] : ports) {
    for (Operation *memOp : memGroupOps)
      if (auto loadOp = dyn_cast<handshake::LoadOp>(memOp))
        connect(loadOp, memIfaceOp->getResult(resIdx++));
  }
}

//===----------------------------------------------------------------------===//
// LSQGenerationInfo
//===----------------------------------------------------------------------===//

LSQGenerationInfo::LSQGenerationInfo(handshake::MemOrderingUnitOp lsqOp,
                                     StringRef name)
    : lsqOp(lsqOp), name(name) {
  FuncMemoryPorts lsqPorts = getMemoryPorts(lsqOp);
  fromPorts(lsqPorts);
}

LSQGenerationInfo::LSQGenerationInfo(FuncMemoryPorts &ports, StringRef name)
    : lsqOp(cast<handshake::MemOrderingUnitOp>(ports.memOp)), name(name) {
  fromPorts(ports);
}

void LSQGenerationInfo::fromPorts(FuncMemoryPorts &ports) {
  dataWidth = ports.dataWidth;
  addrWidth = ports.addrWidth;

  handshake::LSQDepthAttr lsqDepthAttr =
      getDialectAttr<handshake::LSQDepthAttr>(lsqOp);
  if (lsqDepthAttr) {
    depthLoad = lsqDepthAttr.getLoadQueueDepth();
    depthStore = lsqDepthAttr.getStoreQueueDepth();
    // "depth" Parameter is theoretically unused, but still needed by the
    // current LSQGenerator
    depth = std::max(depthLoad, depthStore);
  } else {
    depthLoad = 16;
    depthStore = 16;
  }

  numGroups = ports.getNumGroups();
  numLoads = ports.getNumPorts<LoadPort>();
  numStores = ports.getNumPorts<StorePort>();

  unsigned loadIdx = 0, storeIdx = 0;
  for (GroupMemoryPorts &groupPorts : ports.groups) {
    // Number of load and store ports per block
    unsigned numLoadsInGroup = groupPorts.getNumPorts<LoadPort>();
    unsigned numStoresInGroup = groupPorts.getNumPorts<StorePort>();
    loadsPerGroup.push_back(numLoadsInGroup);
    storesPerGroup.push_back(numStoresInGroup);

    // Track the numebr of stores and ld idx within a group
    unsigned numStoresCount = 0, ldIdx = 0;

    // Compute the offset of first load/store in the group and indices of
    // each load/store port
    std::optional<unsigned> firstLoadOffset, firstStoreOffset;
    SmallVector<unsigned> groupLoadPorts, groupStorePorts;

    // ldOrderOfOneGroup: the ldOrder of all the loads in one group
    // Example: ldOrder = [
    //    [1, 2], <--- for the first group: ldOrderOfOneGroup prepares this
    //    vector [1]
    // ]
    SmallVector<unsigned> ldOrderOfOneGroup(numLoadsInGroup, 0);

    // This for loop has two purposes:
    // 1. It iterates through all the LDs/STs in a group, for each LD/ST:
    //   If it is an LD, then it saves how many STs have to
    //   complete before it
    // 2. It records the IDs of the LDs/STs in a group.
    for (auto [portIdx, accessPort] : llvm::enumerate(groupPorts.accessPorts)) {
      if (isa<LoadPort>(accessPort)) {
        if (!firstLoadOffset)
          firstLoadOffset = portIdx;

        // Sets "the number of stores before load[ldIdx]" = numStoresCount
        ldOrderOfOneGroup[ldIdx++] = numStoresCount;

        groupLoadPorts.push_back(loadIdx++);
      } else {
        assert(isa<StorePort>(accessPort) && "port must be load or store");
        if (!firstStoreOffset)
          firstStoreOffset = portIdx;

        numStoresCount++;
        groupStorePorts.push_back(storeIdx++);
      }
    }

    // If there are no loads or no stores in the block, set the corresponding
    // offset to 0
    loadOffsets.push_back(SmallVector<unsigned>{firstLoadOffset.value_or(0)});
    storeOffsets.push_back(SmallVector<unsigned>{firstStoreOffset.value_or(0)});

    loadPorts.push_back(groupLoadPorts);
    storePorts.push_back(groupStorePorts);
    ldPortIdx.push_back(groupLoadPorts);
    stPortIdx.push_back(groupStorePorts);

    // Push back the new ldOrder Info
    ldOrder.push_back(ldOrderOfOneGroup);
  }

  /// Adds as many 0s as necessary to the array so that its size equals the
  /// depth. Asserts if the array size is larger than the depth.
  auto capArray = [&](SmallVector<unsigned> &array, unsigned depth) -> void {
    assert(array.size() <= depth && "array larger than LSQ depth");
    for (size_t i = 0, e = array.size(); i < depth - e; ++i)
      array.push_back(0);
  };

  /// Adds as many 0s as necessary to each nested array so that their size
  /// equals the depth.
  auto capBiArray = [&](SmallVector<SmallVector<unsigned>> &biArray,
                        unsigned depth) -> void {
    for (SmallVector<unsigned> &array : biArray)
      capArray(array, depth);
  };

  // Port offsets and index arrays must have length equal to the depth
  capBiArray(loadOffsets, depthLoad);
  capBiArray(storeOffsets, depthStore);
  capBiArray(loadPorts, depthLoad);
  capBiArray(storePorts, depthStore);

  // Update the index width
  indexWidth = llvm::Log2_64_Ceil(depthLoad);
}

//===----------------------------------------------------------------------===//
// QueueConfig / DependencyCheckerConfig
//===----------------------------------------------------------------------===//

mlir::DictionaryAttr QueueConfig::toAttrDict(mlir::MLIRContext *ctx) const {
  Builder b(ctx);
  SmallVector<NamedAttribute> entries = {
      {b.getStringAttr("QueueType"), b.getStringAttr(qType)},
      {b.getStringAttr("NumEntries"), b.getUI32IntegerAttr(numEntries)},
      {b.getStringAttr("DataWidth"), b.getUI32IntegerAttr(dataWidth)},
      {b.getStringAttr("AddrWidth"), b.getUI32IntegerAttr(addrWidth)},
      {b.getStringAttr("IDWidth"), b.getUI32IntegerAttr(idWidth)},
      {b.getStringAttr("IDVal"), b.getUI32IntegerAttr(idVal)},
      {b.getStringAttr("LDPAddrWidth"), b.getUI32IntegerAttr(ldpAddrWidth)},
      {b.getStringAttr("StResp"), b.getBoolAttr(stResp)},
  };
  return DictionaryAttr::get(ctx, entries);
}

mlir::DictionaryAttr
DependencyCheckerConfig::toAttrDict(mlir::MLIRContext *ctx) const {
  Builder b(ctx);
  SmallVector<NamedAttribute> entries = {
      {b.getStringAttr("AccessDisparityWidth"),
       b.getUI32IntegerAttr(accessDisparityWidth)},
      {b.getStringAttr("succCanExecuteOnce"),
       b.getBoolAttr(succCanExecuteOnce)},

  };
  return DictionaryAttr::get(ctx, entries);
}

//===----------------------------------------------------------------------===//
// OrderingNetworkGenerationInfo
//===----------------------------------------------------------------------===//

OrderingNetworkGenerationInfo::OrderingNetworkGenerationInfo(
    handshake::MemOrderingUnitOp memoryOrderingUnitOp, StringRef name)
    : memoryOrderingUnitOp(memoryOrderingUnitOp), name(name) {
  FuncMemoryPorts ports = getMemoryPorts(memoryOrderingUnitOp);
  fromPorts(ports);
}

OrderingNetworkGenerationInfo::OrderingNetworkGenerationInfo(
    FuncMemoryPorts &ports, StringRef name)
    : memoryOrderingUnitOp(cast<handshake::MemOrderingUnitOp>(ports.memOp)),
      name(name) {
  fromPorts(ports);
}

void OrderingNetworkGenerationInfo::fromPorts(FuncMemoryPorts &ports) {
  // TODO: Calculate depth better
  const unsigned depthLoad = 16;
  const unsigned depthStore = 16;

  // Assign a global port index to each access port (in program order across
  // all groups) and build the vertex→group mapping.
  unsigned globalIdx = 0;
  DenseMap<StringRef, unsigned> nameToPortIdx;

  for (auto [groupID, groupPorts] : llvm::enumerate(ports.groups)) {
    for (MemoryPort &accessPort : groupPorts.accessPorts) {
      portBBIds.push_back(groupID);
      nameToPortIdx[getUniqueName(accessPort.portOp)] = globalIdx;

      // For now, just index into a single load/store queue, later more advanced
      // size analysis and configuration, e.g. have a config per port instead of
      // a shared config for every store/load which can then be compressed if
      // there are two ports with the same configs
      if (isa<LoadPort>(accessPort)) {
        portsToQueue.push_back(0); // index into queues: load config
      } else {
        portsToQueue.push_back(1); // index into queues: store config
      }

      ++globalIdx;
    }
  }

  // Build dependency edges from active MemDependenceAttrs on each access
  // port.
  globalIdx = 0;
  for (GroupMemoryPorts &groupPorts : ports.groups) {
    for (MemoryPort &accessPort : groupPorts.accessPorts) {
      if (auto deps =
              getDialectAttr<MemDependenceArrayAttr>(accessPort.portOp)) {
        for (MemDependenceAttr dep : deps.getDependencies()) {
          if (dep.getIsActive()) {
            auto dstIt = nameToPortIdx.find(dep.getDstAccess());
            assert(dstIt != nameToPortIdx.end() &&
                   "dependency destination not found among ports");
            sources.push_back(globalIdx);
            destinations.push_back(dstIt->second);
          }
        }
      }
      ++globalIdx;
    }
  }

  // stResp is always set to false in LSQ generation, however the option exists
  bool stResp = false;

  // Two shared queue configs: one for all load ports, one for all store
  // ports. queues[0] = load queue, queues[1] = store queue.
  // TODO: See if idWidth/val is still necessary (was for AXI interface)
  queues.emplace_back("load", depthLoad, ports.dataWidth, ports.addrWidth,
                      ports.addrWidth, /*idVal=*/0,
                      llvm::Log2_64_Ceil(depthLoad), false);
  queues.emplace_back("store", depthStore, ports.dataWidth, ports.addrWidth,
                      ports.addrWidth, /*idVal=*/0,
                      llvm::Log2_64_Ceil(depthStore), stResp);

  for (unsigned i = 0; i < sources.size(); i++) {
    // if the source is ahead of the destination in program order,
    // the source is allowed to execute once before waiting on the destination
    // TODO: 8 is a random access disparity width
    bool succCanExecuteOnce = sources[i] > destinations[i];
    edgesToDp.push_back(i);
    dependencyCheckers.emplace_back(8, succCanExecuteOnce);
  }
}
