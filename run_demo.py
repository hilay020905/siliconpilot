#!/usr/bin/env python3
"""
End-to-end demo: point SiliconPilot's Planner at demo_project/, which contains a
seeded bug (accumulator register with no reset), and watch the engineering loop
detect it, localize it, patch it, and re-verify - fully autonomously.

Run:
    python3 run_demo.py
"""
import argparse
import logging
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(__file__))

from planner.planner import run_engineering_loop as run_legacy_loop  # noqa: E402
from planner.dynamic_planner import DynamicPlanner  # noqa: E402

SOURCE_PROJECT = os.path.join(os.path.dirname(__file__), "demo_project")
RUN_DIR = os.path.join(os.path.dirname(__file__), "runs", "demo_run")
SANDBOX_PROJECT = os.path.join(RUN_DIR, "project")   # SiliconPilot never edits SOURCE_PROJECT directly
WORK_DIR = os.path.join(RUN_DIR, "work")
REPORT_PATH = os.path.join(RUN_DIR, "report.md")


def main():
    parser = argparse.ArgumentParser(description="Run the SiliconPilot autonomous engineering loop.")
    parser.add_argument("--legacy", action="store_true",
                         help="Use the original fixed compile->simulate->analyze->patch pipeline "
                              "instead of the Dynamic Planner.")
    parser.add_argument("--verbose", action="store_true", help="Enable INFO-level logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Fresh sandbox copy each run (mirrors the design doc's "never edit main, work
    # in a sandbox branch" principle - see System Design §14 Security).
    if os.path.exists(RUN_DIR):
        shutil.rmtree(RUN_DIR)
    shutil.copytree(SOURCE_PROJECT, SANDBOX_PROJECT)
    os.makedirs(WORK_DIR, exist_ok=True)

    print("=" * 78)
    print(f"SiliconPilot - autonomous engineering loop starting "
          f"({'legacy pipeline' if args.legacy else 'Dynamic Planner'})")
    print(f"Source project (untouched): {SOURCE_PROJECT}")
    print(f"Sandbox copy (agent edits this): {SANDBOX_PROJECT}")
    print("=" * 78)

    if args.legacy:
        state = run_legacy_loop(
            project_root=SANDBOX_PROJECT,
            top_module="tb_accumulator",
            work_dir=WORK_DIR,
            max_iterations=5,
        )
    else:
        dyn_planner = DynamicPlanner(
            project_root=SANDBOX_PROJECT,
            top_module="tb_accumulator",
            work_dir=WORK_DIR,
            max_iterations=5,
        )
        state = dyn_planner.run()
        with open(REPORT_PATH, "w") as f:
            f.write(dyn_planner.last_report)
        print(f"Explainability report + architecture diagrams written to: {REPORT_PATH}")

    print()
    print("-" * 78)
    print("FULL TRACE")
    print("-" * 78)
    for line in state.trace_log:
        print(line)

    print()
    print("-" * 78)
    print("RUN SUMMARY")
    print("-" * 78)
    print(f"Status:       {state.status}")
    print(f"Stop reason:  {state.stop_reason}")
    print(f"Iterations:   {state.iteration}")
    print(f"Bugs found:   {len(state.bugs)}")
    for b in state.bugs:
        print(f"  - [{b.status}] {b.summary}")
    print(f"Patches:      {len(state.patches)}")
    for p in state.patches:
        verdict = state.qa_verdicts.get(p.patch_id)
        print(f"  - {p.patch_id[:8]} on {p.target_file} -> QA: {verdict.verdict if verdict else '?'}")

    if state.patches:
        print()
        print("-" * 78)
        print("PATCH DIFF(S) APPLIED")
        print("-" * 78)
        for p in state.patches:
            v = state.qa_verdicts.get(p.patch_id)
            if v and v.verdict == "approve":
                print(p.diff)
                print(f"Rationale: {p.rationale}")

    print()
    if state.status == "success":
        print("RESULT: design now compiles and passes all testbench checks. 🟢")
    else:
        print(f"RESULT: run ended in state '{state.status}'. See trace above.")


if __name__ == "__main__":
    main()
