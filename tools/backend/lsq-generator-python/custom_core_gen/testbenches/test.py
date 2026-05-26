import os
import pathlib
import shutil
import subprocess
import sys

import pytest

TESTBENCHES_DIR = pathlib.Path(__file__).parent
LSQ_ROOT = TESTBENCHES_DIR.parent.parent


def _testbench_dirs():
    return sorted(
        d for d in TESTBENCHES_DIR.iterdir()
        if d.is_dir() and (d / "generate.py").exists()
    )


def _has_tool(name: str) -> bool:
    return shutil.which(name) is not None


@pytest.fixture(params=_testbench_dirs(), ids=lambda d: d.name)
def testbench(request):
    return request.param


def test_generate(testbench):
    out_dir = testbench / "out"
    out_dir.mkdir(exist_ok=True)
    for item in out_dir.iterdir():
        shutil.rmtree(item) if item.is_dir() else item.unlink()

    env = {**os.environ, "PYTHONPATH": str(LSQ_ROOT)}
    result = subprocess.run(
        [sys.executable, str(testbench / "generate.py")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"generate.py failed for {testbench.name}:\n{result.stderr}"
    )


@pytest.mark.skipif(not _has_tool("vsim"), reason="ModelSim/Questa not available")
def test_simulate(testbench):
    name = testbench.name
    tb_sv = testbench / f"{name}_tb.sv"

    if not tb_sv.exists():
        pytest.skip(f"no testbench file {tb_sv.name}")
    if not any((testbench / "out").glob("*.v")):
        pytest.skip(f"no generated RTL in out/ — run test_generate first")

    out_dir = testbench / "out"
    env = {**os.environ, "PYTHONPATH": str(LSQ_ROOT)}

    if (out_dir / "work").is_dir():
        subprocess.run(["vdel", "-all", "-lib", "work"], cwd=out_dir, env=env, check=True)

    subprocess.run(["vlib", "work"], cwd=out_dir, env=env, check=True)

    subprocess.run(
        ["vlog", "-sv", str(tb_sv)],
        cwd=out_dir, env=env, check=True,
    )
    rtl_files = sorted(out_dir.glob("*.v"))
    subprocess.run(
        ["vlog"] + [f.name for f in rtl_files],
        cwd=out_dir, env=env, check=True,
    )

    result = subprocess.run(
        [
            "vsim", "-c",
            "-wlf", "output.wlf",
            "-voptargs=+acc",
            f"{name}_tb",
            "-do", "log -r /*; run -all; quit",
        ],
        cwd=out_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    failures = [line for line in output.splitlines() if "FAIL" in line or "TIMEOUT" in line]
    assert result.returncode == 0 and not failures, (
        f"Simulation failed for {name}:\n" + ("\n".join(failures) or output)
    )


@pytest.mark.skipif(not _has_tool("vsim"), reason="ModelSim/Questa not available")
def test_simulate_vhdl(testbench):
    name = testbench.name
    tb_sv = testbench / f"{name}_tb.sv"

    if not tb_sv.exists():
        pytest.skip(f"no testbench file {tb_sv.name}")
    if not any((testbench / "out").glob("*.vhd")):
        pytest.skip(f"no generated VHDL in out/ — run test_generate first")

    out_dir = testbench / "out"
    env = {**os.environ, "PYTHONPATH": str(LSQ_ROOT)}

    if (out_dir / "work").is_dir():
        subprocess.run(["vdel", "-all", "-lib", "work"], cwd=out_dir, env=env, check=True)

    subprocess.run(["vlib", "work"], cwd=out_dir, env=env, check=True)

    vhd_files = sorted(out_dir.glob("*.vhd"))
    subprocess.run(
        ["vcom", "-2008", "-explicit", "-vopt"] + [f.name for f in vhd_files],
        cwd=out_dir, env=env, check=True,
    )
    subprocess.run(
        ["vlog", "-sv", str(tb_sv)],
        cwd=out_dir, env=env, check=True,
    )

    result = subprocess.run(
        [
            "vsim", "-c",
            "-wlf", "output_vhdl.wlf",
            "-voptargs=+acc",
            f"{name}_tb",
            "-do", "log -r /*; run -all; quit",
        ],
        cwd=out_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    failures = [line for line in output.splitlines() if "FAIL" in line or "TIMEOUT" in line]
    assert result.returncode == 0 and not failures, (
        f"VHDL simulation failed for {name}:\n" + ("\n".join(failures) or output)
    )
