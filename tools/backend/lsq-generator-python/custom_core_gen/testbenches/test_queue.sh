#! /usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LSQ_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

for TEST_DIR in "${SCRIPT_DIR}"/*/; do
    echo "=== Processing ${TEST_DIR} ==="
    NAME="$(basename "${TEST_DIR}")"
    OUT_DIR="${TEST_DIR}/out"
    mkdir -p "${OUT_DIR}"

    echo "=== Generating ${NAME} ==="
    PYTHONPATH="${LSQ_ROOT}" python "${SCRIPT_DIR}/test-generate.py" \
        --config-file "${TEST_DIR}/queue-config.json" \
        --output-dir  "${OUT_DIR}" \
        --hdl         verilog \
        --name        "${NAME}"

    echo "=== Compiling and simulating ${NAME} ==="
    cd "${OUT_DIR}"
    vlib work
    vlog -sv "${TEST_DIR}/${NAME}_tb.sv" && \
    vlog "${NAME}.v" && \
    vsim -c "${NAME}_tb" -do "
      log -r /*;
      run -all;
      write format wlf output.wlf;
      quit
    "
done
