"""
Compiler Agent + Simulation Agent.
Thin orchestration layer over tool_adapters.icarus_adapter - keeps agents free of
subprocess/CLI details per the design doc's "agents never parse raw stdout" rule.
"""
from __future__ import annotations
from core.schemas import RunState
from tool_adapters import icarus_adapter


def run_compile(state: RunState, work_dir: str) -> RunState:
    rtl_files = state.manifest.rtl_files + state.manifest.tb_files
    top = state.manifest.top_module
    report = icarus_adapter.compile_project(rtl_files, top, work_dir)
    state.compile_report = report
    if report.success:
        state.log(f"Compile PASS ({report.duration_ms}ms)")
    else:
        state.log(f"Compile FAIL: {len(report.diagnostics)} diagnostic(s)")
        for d in report.diagnostics:
            state.log(f"  {d.severity.upper()} {d.file}:{d.line} {d.message}")
    return state


def run_simulate(state: RunState, work_dir: str) -> RunState:
    result = icarus_adapter.simulate(state.compile_report.binary_path, work_dir)
    state.sim_result = result
    if result.success:
        state.log(f"Simulate PASS ({result.passed_checks} checks)")
    else:
        state.log(f"Simulate FAIL: {result.failed_checks} failed - {result.failure_summary}")
    return state
