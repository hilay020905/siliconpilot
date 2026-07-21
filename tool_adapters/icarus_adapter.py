"""
Adapter for Icarus Verilog (iverilog / vvp).
Wraps raw CLI invocation and normalizes output into CompileReport / SimResult.
This is the Phase-1 open-source EDA backend referenced in the design doc (SIliconPilot §8/§12).
"""
from __future__ import annotations
import re
import subprocess
import time
import os

from core.schemas import CompileReport, Diagnostic, SimResult

# iverilog error/warning line format:
#   <file>:<line>: error: <message>
#   <file>:<line>: warning: <message>
_DIAG_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):\s*(?P<sev>error|warning):\s*(?P<msg>.*)$")


def compile_project(rtl_files: list[str], top_module: str, work_dir: str) -> CompileReport:
    os.makedirs(work_dir, exist_ok=True)
    binary_path = os.path.join(work_dir, "sim.out")
    cmd = ["iverilog", "-g2012", "-o", binary_path, "-s", top_module, *rtl_files]

    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return CompileReport(
            success=False, tool="icarus", duration_ms=int((time.time() - start) * 1000),
            diagnostics=[Diagnostic(
                severity="error", file=rtl_files[0] if rtl_files else work_dir,
                message="'iverilog' was not found on PATH. Install Icarus Verilog "
                        "(e.g. `apt install iverilog`) to enable compile/simulate stages; "
                        "all other analysis stages still run.",
            )],
        )
    except subprocess.TimeoutExpired:
        return CompileReport(
            success=False, tool="icarus", duration_ms=int((time.time() - start) * 1000),
            diagnostics=[Diagnostic(severity="error", file=rtl_files[0] if rtl_files else work_dir,
                                     message="iverilog timed out after 60s.")],
        )
    duration_ms = int((time.time() - start) * 1000)

    diagnostics: list[Diagnostic] = []
    for stream in (proc.stdout, proc.stderr):
        for line in stream.splitlines():
            m = _DIAG_RE.match(line.strip())
            if m:
                diagnostics.append(Diagnostic(
                    severity="error" if m.group("sev") == "error" else "warning",
                    file=m.group("file"),
                    line=int(m.group("line")),
                    message=m.group("msg"),
                ))
            elif "error" in line.lower() and line.strip():
                diagnostics.append(Diagnostic(severity="error", file=rtl_files[0], message=line.strip()))

    success = proc.returncode == 0 and os.path.exists(binary_path)
    return CompileReport(
        success=success,
        tool="icarus",
        duration_ms=duration_ms,
        diagnostics=diagnostics,
        binary_path=binary_path if success else None,
    )


# vvp $display convention this adapter expects from testbenches:
#   "CHECK PASS <name>"  /  "CHECK FAIL <name>: <reason>"
_PASS_RE = re.compile(r"CHECK PASS")
_FAIL_RE = re.compile(r"CHECK FAIL(?::)?\s*(.*)")


def simulate(binary_path: str, work_dir: str, vcd_expected: bool = True) -> SimResult:
    start = time.time()
    try:
        proc = subprocess.run(["vvp", binary_path], capture_output=True, text=True,
                               cwd=work_dir, timeout=30)
    except FileNotFoundError:
        return SimResult(
            success=False, tool="icarus-vvp", duration_ms=int((time.time() - start) * 1000),
            failure_summary="'vvp' was not found on PATH; Icarus Verilog is not installed.",
        )
    except subprocess.TimeoutExpired:
        return SimResult(
            success=False, tool="icarus-vvp", duration_ms=int((time.time() - start) * 1000),
            failure_summary="Simulation timed out after 30s.",
        )
    duration_ms = int((time.time() - start) * 1000)

    passed = len(_PASS_RE.findall(proc.stdout))
    failures = _FAIL_RE.findall(proc.stdout)
    failed = len(failures)

    vcd_path = None
    if vcd_expected:
        candidate = os.path.join(work_dir, "dump.vcd")
        if os.path.exists(candidate):
            vcd_path = candidate

    return SimResult(
        success=(proc.returncode == 0 and failed == 0),
        tool="icarus-vvp",
        duration_ms=duration_ms,
        passed_checks=passed,
        failed_checks=failed,
        stdout_tail="\n".join(proc.stdout.splitlines()[-20:]),
        vcd_path=vcd_path,
        failure_summary="; ".join(failures) if failures else None,
    )
