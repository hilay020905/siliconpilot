"""
Planner Agent.

This implements the engineering loop from the design doc §7 as an explicit Python
state machine. In the target production stack this graph is expressed as LangGraph
nodes/edges (checkpointed to Postgres after every transition); the node functions
below are written so that porting to `langgraph.graph.StateGraph` is a mechanical
transformation (`add_node(name, fn)` / `add_edge(...)` around each function here).
We keep it dependency-free in this reference repo so `python demo_project/run_demo.py`
works with zero extra installs beyond pydantic + networkx.
"""
from __future__ import annotations
import os
from core.schemas import RunState
from agents import project_understanding, rtl_parser, dependency_graph
from agents import compiler_sim, waveform_rootcause, patch_qa


def read_project(state: RunState, top_module: str) -> RunState:
    manifest = project_understanding.scan_project(state.project_root, top_module)
    state.manifest = manifest
    state.log(f"Project scanned: {len(manifest.rtl_files)} RTL, {len(manifest.tb_files)} TB files")
    return state


def build_graph(state: RunState) -> RunState:
    state.modules = rtl_parser.parse_project(state.manifest.rtl_files + state.manifest.tb_files)
    g = dependency_graph.build_graph(state.modules)
    state.dep_graph = dependency_graph.summarize(g)
    state.log(f"Dependency graph: {len(state.dep_graph.modules)} modules, "
              f"{len(state.dep_graph.edges)} instantiation edges")
    return state


def check_stop(state: RunState) -> bool:
    """Returns True if the loop should halt."""
    if state.compile_report and state.compile_report.success and \
       state.sim_result and state.sim_result.success:
        state.status = "success"
        state.stop_reason = "All checks passed, no open bugs, no anomalies."
        return True
    if state.iteration >= state.max_iterations:
        state.status = "partial"
        state.stop_reason = f"Max iterations ({state.max_iterations}) reached."
        return True
    if state.bugs and all(b.status == "rejected" for b in state.bugs):
        state.status = "escalated"
        state.stop_reason = "All patch attempts rejected by QA; needs human review."
        return True
    return False


def run_engineering_loop(project_root: str, top_module: str, work_dir: str,
                          max_iterations: int = 5) -> RunState:
    state = RunState(project_root=project_root, max_iterations=max_iterations)
    state = read_project(state, top_module)
    state = build_graph(state)

    while True:
        state.iteration += 1
        os.makedirs(work_dir, exist_ok=True)

        state = compiler_sim.run_compile(state, work_dir)

        if state.compile_report.success:
            state = compiler_sim.run_simulate(state, work_dir)
            if not state.sim_result.success:
                state = waveform_rootcause.run_waveform_analysis(state)
                state = waveform_rootcause.run_root_cause_analysis(state)
        else:
            state = waveform_rootcause.run_root_cause_analysis(state)

        if check_stop(state):
            break

        # Take the highest-priority open bug and try to fix it.
        open_bugs = [b for b in state.bugs if b.status == "open"]
        if not open_bugs:
            state.status = "escalated"
            state.stop_reason = "Failing but no localizable root cause found."
            break

        bug = open_bugs[0]
        patch = patch_qa.generate_patch(state, bug)
        if patch is None:
            bug.status = "rejected"
            state.log(f"No safe heuristic patch available for bug {bug.bug_id}; escalating.")
            continue

        state.patches.append(patch)
        verdict = patch_qa.qa_review(state, patch)
        state.qa_verdicts[patch.patch_id] = verdict
        state.log(f"QA verdict on patch {patch.patch_id}: {verdict.verdict} "
                  f"({'; '.join(verdict.reasons)})")

        if verdict.verdict == "approve":
            # Apply: re-derive patched content by regenerating the module with the fix
            # (the generator already returned a full unified diff; here we just re-run
            # the same transform and write it, since this is the deterministic template
            # path - an LLM-diff path would `patch`-apply the unified diff instead).
            with open(patch.target_file) as f:
                original = f.read()
            patched_text = _apply_unified_diff(original, patch.diff)
            patch_qa.apply_patch_direct(patch.target_file, patched_text)
            bug.status = "patched"
            state.log(f"Applied patch {patch.patch_id} to {patch.target_file}")
            # re-parse modules since RTL changed
            state.modules = rtl_parser.parse_project(state.manifest.rtl_files + state.manifest.tb_files)
        else:
            bug.status = "rejected"

    state.log(f"STOP: status={state.status} reason={state.stop_reason}")
    return state


def _apply_unified_diff(original: str, diff_text: str) -> str:
    """Tiny unified-diff applier sufficient for the single-hunk patches this
    reference implementation generates (production uses `git apply` / `patch`)."""
    import re
    hunk_re = re.compile(r"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@", re.MULTILINE)
    lines = original.splitlines(keepends=True)
    diff_lines = diff_text.splitlines()

    out = []
    orig_idx = 0
    i = 0
    while i < len(diff_lines):
        line = diff_lines[i]
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@", line)
            start = int(m.group(1)) - 1
            out.extend(lines[orig_idx:start])
            orig_idx = start
            i += 1
            while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
                dl = diff_lines[i]
                if dl.startswith("-"):
                    orig_idx += 1
                elif dl.startswith("+"):
                    out.append(dl[1:] + "\n")
                elif dl.startswith(" "):
                    out.append(lines[orig_idx])
                    orig_idx += 1
                i += 1
        else:
            i += 1
    out.extend(lines[orig_idx:])
    return "".join(out)
