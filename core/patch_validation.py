"""
Patch Validation (Priority Feature #5).

A generated patch is never trusted on QA approval alone. Before it becomes
the new baseline, this module:

1. Snapshots the pre-patch file content (for rollback).
2. Applies the patch.
3. Recompiles.
4. Re-simulates (regression).
5. Compares pass/fail counts against the pre-patch baseline.
6. If the patched design is not strictly better (or is broken), the file is
   rolled back to the snapshot automatically.

This closes the loop the original ``planner.planner`` left open: there,
QA-approved patches were written straight to disk with no post-hoc check that
they actually fixed anything.
"""
from __future__ import annotations

import logging
import os

from core.schemas import PatchProposal, RunState, ValidationResult
from tool_adapters import icarus_adapter

logger = logging.getLogger(__name__)


def validate_and_apply(
    state: RunState,
    patch: PatchProposal,
    patched_text: str,
    work_dir: str,
) -> ValidationResult:
    """Applies `patch` to disk, re-verifies it, and rolls back automatically
    if it is not an improvement over the current baseline.

    Baseline pass/fail counts are taken from `state.baseline_sim_result` if
    present (set by the planner before the first patch attempt of a run),
    otherwise from `state.sim_result`.
    """
    target_file = patch.target_file
    with open(target_file, "r") as f:
        original_text = f.read()

    baseline = state.baseline_sim_result or state.sim_result
    baseline_passed = baseline.passed_checks if baseline else 0
    baseline_failed = baseline.failed_checks if baseline else 1

    notes: list[str] = []
    result = ValidationResult(
        patch_id=patch.patch_id,
        compiled=False,
        simulated=False,
        regression_passed=False,
        baseline_passed_checks=baseline_passed,
        baseline_failed_checks=baseline_failed,
    )

    try:
        with open(target_file, "w") as f:
            f.write(patched_text)

        compile_report = icarus_adapter.compile_project(
            state.manifest.rtl_files + state.manifest.tb_files,
            state.manifest.top_module,
            work_dir,
        )
        result.compiled = compile_report.success
        if not compile_report.success:
            notes.append("Patched design failed to compile; rolling back.")
            _rollback(target_file, original_text)
            result.rolled_back = True
            result.notes = notes
            return result

        sim_result = icarus_adapter.simulate(compile_report.binary_path, work_dir)
        result.simulated = True
        result.passed_checks = sim_result.passed_checks
        result.failed_checks = sim_result.failed_checks

        strictly_better = (
            sim_result.failed_checks < baseline_failed
            or (sim_result.failed_checks == baseline_failed
                and sim_result.passed_checks > baseline_passed)
        )
        no_worse = sim_result.failed_checks <= baseline_failed
        result.improved = strictly_better
        result.regression_passed = sim_result.success or strictly_better

        if sim_result.success:
            notes.append("Patched design compiles and all testbench checks pass.")
        elif strictly_better:
            notes.append(
                f"Patched design improves regression: {sim_result.failed_checks} failed "
                f"(was {baseline_failed}), {sim_result.passed_checks} passed (was {baseline_passed})."
            )
        elif no_worse:
            notes.append("Patch made no measurable improvement; treating as non-improving, rolling back.")
            _rollback(target_file, original_text)
            result.rolled_back = True
        else:
            notes.append(
                f"Patched design regressed: {sim_result.failed_checks} failed "
                f"(was {baseline_failed}); rolling back."
            )
            _rollback(target_file, original_text)
            result.rolled_back = True

    except (OSError, TimeoutError) as exc:  # pragma: no cover - defensive
        logger.exception("Patch validation raised an exception; rolling back")
        notes.append(f"Validation error ({exc}); rolling back for safety.")
        _rollback(target_file, original_text)
        result.rolled_back = True

    result.notes = notes
    return result


def _rollback(target_file: str, original_text: str) -> None:
    """Restores `target_file` to its pre-patch content."""
    with open(target_file, "w") as f:
        f.write(original_text)
    logger.info("Rolled back %s to pre-patch content", os.path.basename(target_file))
