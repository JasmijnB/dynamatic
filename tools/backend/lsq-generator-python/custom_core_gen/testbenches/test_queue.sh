#! /usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LSQ_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

for TEST_DIR in "${SCRIPT_DIR}"/*/; do
    echo "=== Processing ${TEST_DIR} ==="
    NAME="$(basename "${TEST_DIR}")"
    OUT_DIR="${TEST_DIR}/out"
    mkdir -p "${OUT_DIR}"
    rm -rf "${OUT_DIR}"/*

    echo "=== Generating ${NAME} ==="
    PYTHONPATH="${LSQ_ROOT}" python "${TEST_DIR}/generate.py"

    echo "=== Compiling and simulating ${NAME} ==="
    cd "${OUT_DIR}"
    [ -d work ] && vdel -all -lib work
    vlib work
    vlog -sv "${TEST_DIR}/${NAME}_tb.sv" && \
    vlog "${NAME}.v" && \
    vsim -c -wlf output.wlf -voptargs=+acc "${NAME}_tb" -do "
      log -r /*;
      run -all;
      quit
    "
done
