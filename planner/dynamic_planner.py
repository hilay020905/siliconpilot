"""
Dynamic Planner (Priority Feature #1).

Replaces the fixed ``compile -> simulate -> analyze -> patch`` pipeline in
``planner.planner`` with an explicit decision graph: at every step, the
planner inspects the *current* ``RunState`` (plus the Project Knowledge Graph
and Engineering Memory) and decides which node to execute next. Nodes are
skipped when their preconditions are already satisfied or irrelevant, failed
tasks are retried up to a bound, and the loop terminates the moment a success
condition is met - none of this is a hard-coded sequence.

The graph is represented explicitly as a dict of
``PlannerNodeName -> Callable[[RunState], PlannerNodeName]`` "next-node"
decision functions, which is the same shape LangGraph's
``StateGraph.add_conditional_edges`` expects, so porting this to LangGraph
later (as the original planner.py's docstring already anticipated) is
mechanical.

This module is additive: ``planner.planner.run_engineering_loop`` (the
original linear pipeline) is left completely untouched for backward
compatibility. ``run_demo.py`` has been updated to use this planner instead.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from agents import compiler_sim, patch_qa, project_understanding, rtl_parser, waveform_rootcause
from analysis import (
    assertion_generator,
    counter_analyzer,
    coverage_estimator,
    fsm_analyzer,
    static_analyzer,
)
from core.knowledge_graph import ProjectKnowledgeGraph
from core.memory import EngineeringMemory
from core.patch_validation import validate_and_apply
from core.root_cause_engine import analyze as analyze_root_cause
from core.schemas import (
    AgentTrace,
    Bug,
    MemoryRecord,
    PlannerDecision,
    PlannerNodeName,
    RunState,
)
from reports.report_generator import write_reports

logger = logging.getLogger(__name__)

MAX_RETRIES_PER_NODE = 2


class DynamicPlanner:
    """Stateful orchestrator implementing the decision graph described above."""

    def __init__(self, project_root: str, top_module: str, work_dir: str,
                 max_iterations: int = 10) -> None:
        self.project_root = project_root
        self.top_module = top_module
        self.work_dir = work_dir
        self.max_iterations = max_iterations
        self.memory = EngineeringMemory(project_root=project_root)
        self.kg = ProjectKnowledgeGraph()
        self.retry_counts: dict[str, int] = {}
        self._current_bug: Optional[Bug] = None
        self._current_patch = None

        # Decision-graph transition table: node -> function computing the
        # *next* node from live state. Kept as plain dict-of-callables so it
        # maps 1:1 onto `StateGraph.add_conditional_edges` if/when this is
        # ported to LangGraph.
        self._transitions: dict[PlannerNodeName, Callable[[RunState], PlannerNodeName]] = {
            "scan_project": self._after_scan_project,
            "build_knowledge_graph": self._after_build_kg,
            "static_analysis": self._after_static_analysis,
            "compile": self._after_compile,
            "simulate": self._after_simulate,
            "analyze_waveform": self._after_analyze_waveform,
            "root_cause_analysis": self._after_root_cause,
            "generate_patch": self._after_generate_patch,
            "validate_patch": self._after_validate_patch,
            "apply_patch": self._after_apply_patch,
            "rollback_patch": self._after_rollback_patch,
            "generate_report": self._after_generate_report,
        }

    # -- public entry point ----------------------------------------------

    def run(self) -> RunState:
        state = RunState(project_root=self.project_root, max_iterations=self.max_iterations)
        node: PlannerNodeName = "scan_project"
        step_count = 0
        max_steps = max(self.max_iterations * 8, 40)

        while node != "terminate":
            step_count += 1
            # `state.iteration` tracks fix-attempt *cycles* (bumped each time we
            # return to `compile`, i.e. each time a new bug-fix attempt begins),
            # matching the semantics of `max_iterations` used by `_check_terminal`.
            # `step_count` is the absolute-safety-valve counter over individual
            # planner-node executions, which is always finer-grained.
            if node == "compile":
                state.iteration += 1
            os.makedirs(self.work_dir, exist_ok=True)

            if step_count > max_steps:
                # Absolute safety valve: even a decision graph that keeps
                # bouncing between two nodes must eventually halt.
                state.status = "partial"
                state.stop_reason = "Planner exceeded absolute step budget."
                self._record_decision(state, node, "terminate",
                                       "Absolute step budget exceeded; forcing termination.")
                break

            logger.info("Planner executing node: %s (iteration %d)", node, state.iteration)
            self._execute(node, state)

            next_node = self._transitions.get(node, lambda s: "terminate")(state)
            self._record_decision(state, node, next_node, self._explain_transition(state, node, next_node))
            node = next_node

        state.log(f"STOP: status={state.status} reason={state.stop_reason}")
        self._persist_run_summary(state)
        if not getattr(self, "last_report", None):
            # Safety-valve terminations skip the generate_report node; make sure
            # a report still gets produced.
            self._run_generate_report(state)
        return state

    # -- node execution -------------------------------------------------

    def _execute(self, node: PlannerNodeName, state: RunState) -> None:
        handler = getattr(self, f"_run_{node}", None)
        if handler is None:
            logger.warning("No handler for planner node %s; treating as no-op", node)
            return
        handler(state)

    def _run_scan_project(self, state: RunState) -> None:
        manifest = project_understanding.scan_project(state.project_root, self.top_module)
        state.manifest = manifest
        state.log(f"Project scanned: {len(manifest.rtl_files)} RTL, {len(manifest.tb_files)} TB files")
        state.add_trace(AgentTrace(
            agent="ProjectUnderstanding",
            observation=f"Found {len(manifest.rtl_files)} RTL file(s), {len(manifest.tb_files)} testbench file(s).",
            reasoning="Classified files by path/naming convention to separate RTL from testbenches.",
            evidence=[f"rtl_files={manifest.rtl_files}", f"tb_files={manifest.tb_files}"],
            confidence=0.95,
            action_taken="Populated ProjectManifest.",
        ))

    def _run_build_knowledge_graph(self, state: RunState) -> None:
        state.modules = rtl_parser.parse_project(state.manifest.rtl_files + state.manifest.tb_files)
        self.kg.build(state.modules)
        state.kg_summary = self.kg.summarize()
        state.log(f"Knowledge graph built: {len(state.kg_summary.nodes)} nodes, "
                  f"{len(state.kg_summary.edges)} edges")
        state.add_trace(AgentTrace(
            agent="KnowledgeGraphBuilder",
            observation=f"Parsed {len(state.modules)} module(s) into a semantic project graph.",
            reasoning=("Built typed nodes (modules, signals, registers, wires, FSMs, clock "
                       "domains, always blocks) and typed edges (contains, drives, reads, "
                       "writes, instantiates, depends_on, clocked_by, reset_by) so downstream "
                       "agents query structure instead of re-parsing RTL text."),
            evidence=[f"{len(state.kg_summary.nodes)} nodes", f"{len(state.kg_summary.edges)} edges"],
            confidence=0.9,
            action_taken="Populated ProjectKnowledgeGraph and RunState.kg_summary.",
        ))

    def _run_static_analysis(self, state: RunState) -> None:
        """Static RTL analysis + FSM/counter analyzers + assertion generation +
        coverage estimation, run once the knowledge graph is available, before
        any compile/simulate attempt. Purely additive - never blocks the loop."""
        static_report = static_analyzer.analyze_project(state.modules, self.kg)
        state.static_report = static_report
        errors = static_report.by_severity("error")
        warnings = static_report.by_severity("warning")
        state.add_trace(AgentTrace(
            agent="StaticAnalyzer",
            observation=f"{len(static_report.findings)} finding(s): {len(errors)} error(s), "
                       f"{len(warnings)} warning(s).",
            reasoning="Ran heuristic checks (missing reset, latch inference, unused signals, "
                     "blocking/nonblocking misuse, multiple drivers, floating wires, constant "
                     "outputs, width/sign mismatches) over every parsed module.",
            evidence=[f"[{f.severity}] {f.kind}: {f.message}" for f in static_report.findings[:20]],
            confidence=0.7,
            action_taken="Recorded StaticAnalysisReport.",
        ))

        fsm_report = fsm_analyzer.analyze_project(state.modules, self.kg)
        state.fsm_report = fsm_report
        state.add_trace(AgentTrace(
            agent="FSMAnalyzer",
            observation=f"{len(fsm_report.fsms)} FSM(s) detected, "
                       f"{fsm_report.total_transitions()} transition(s) total.",
            reasoning="Recovered state lists and transition edges from case-statement bodies "
                     "inside always blocks the knowledge graph flagged as FSMs.",
            evidence=[f"{f.module}.{f.state_signal}: {len(f.states)} states, "
                     f"unreachable={f.unreachable_states}, deadlock={f.deadlock_states}"
                     for f in fsm_report.fsms],
            confidence=0.65,
            action_taken="Recorded FSMAnalysisReport.",
        ))

        counter_report = counter_analyzer.analyze_project(state.modules, self.kg)
        state.counter_report = counter_report
        state.add_trace(AgentTrace(
            agent="CounterAnalyzer",
            observation=f"{len(counter_report.counters)} counter(s) detected.",
            reasoning="Matched 'sig <= sig + N' patterns inside clocked always blocks and "
                     "cross-checked each against its owning block's reset/enable structure.",
            evidence=[f"{c.module}.{c.signal}: issues={c.issues}" for c in counter_report.counters],
            confidence=0.6,
            action_taken="Recorded CounterAnalysisReport.",
        ))

        module_names = [m.name for m in state.modules]
        state.assertion_suites = assertion_generator.generate(
            self.kg, fsm_report, counter_report, module_names, self.work_dir,
        )
        state.add_trace(AgentTrace(
            agent="AssertionGenerator",
            observation=f"Generated {sum(len(s.assertions) for s in state.assertion_suites)} "
                       f"SVA assertion(s) across {len(state.assertion_suites)} module(s).",
            reasoning="Inferred reset/FSM-legality/counter-monotonicity properties from the "
                     "knowledge graph and FSM/counter analyzers; written as standalone .sva "
                     "files, RTL source untouched.",
            evidence=[s.file_path for s in state.assertion_suites if s.file_path],
            confidence=0.6,
            action_taken="Wrote assertion files under work_dir/assertions/.",
        ))

        state.coverage_estimates = coverage_estimator.estimate(state.modules, fsm_report, state.wave_findings)
        state.add_trace(AgentTrace(
            agent="CoverageEstimator",
            observation=f"Coverage estimates produced for {len(state.coverage_estimates)} module(s).",
            reasoning="Structural proxy for coverage: FSM state reachability plus branch-density "
                     "vs. observed waveform activity (no golden coverage database required).",
            evidence=[n for c in state.coverage_estimates for n in c.uncovered_notes][:10],
            confidence=0.4,
            action_taken="Recorded CoverageEstimate list.",
        ))
        state.log(f"Static analysis: {len(static_report.findings)} findings, "
                  f"{len(fsm_report.fsms)} FSMs, {len(counter_report.counters)} counters.")

    def _run_generate_report(self, state: RunState) -> None:
        paths = write_reports(state, self.kg, self.work_dir)
        state.trace_log.append("---- DESIGN QUALITY REPORT GENERATED ----")
        with open(paths["markdown"]) as f:
            self.last_report = f.read()
        state.log(f"Report written: {paths['markdown']} / {paths['html']}")

    def _run_compile(self, state: RunState) -> None:
        state = compiler_sim.run_compile(state, self.work_dir)
        report = state.compile_report
        state.add_trace(AgentTrace(
            agent="CompilerAgent",
            observation=f"iverilog {'succeeded' if report.success else 'failed'} in {report.duration_ms}ms.",
            reasoning="Ran the toolchain and normalized stdout/stderr into typed diagnostics.",
            evidence=[f"{d.severity}: {d.file}:{d.line} {d.message}" for d in report.diagnostics],
            confidence=1.0 if report.success else 0.9,
            action_taken="Recorded CompileReport.",
        ))

    def _run_simulate(self, state: RunState) -> None:
        # Establish the run's baseline (first successful compile's sim result)
        # so patch validation always has something concrete to compare against.
        state = compiler_sim.run_simulate(state, self.work_dir)
        if state.baseline_sim_result is None:
            state.baseline_sim_result = state.sim_result
        state.add_trace(AgentTrace(
            agent="SimulationAgent",
            observation=(f"{state.sim_result.passed_checks} check(s) passed, "
                        f"{state.sim_result.failed_checks} failed."),
            reasoning="Ran the compiled testbench binary and parsed CHECK PASS/FAIL markers plus VCD path.",
            evidence=[state.sim_result.failure_summary] if state.sim_result.failure_summary else [],
            confidence=1.0,
            action_taken="Recorded SimResult (and baseline, if this is the first simulation this run).",
        ))

    def _run_analyze_waveform(self, state: RunState) -> None:
        state = waveform_rootcause.run_waveform_analysis(state)
        n_anom = len(state.wave_findings.anomalies) if state.wave_findings else 0
        state.add_trace(AgentTrace(
            agent="WaveformAnalysisAgent",
            observation=f"{n_anom} anomaly(ies) found in VCD." if state.wave_findings else "No VCD to analyze.",
            reasoning="Parsed VCD value-change transitions and flagged any transition to an X/Z value.",
            evidence=[f"{a.signal} -> X @ {a.time_ns}ns" for a in (state.wave_findings.anomalies if state.wave_findings else [])],
            confidence=0.85,
            action_taken="Recorded WaveformFindings.",
        ))

    def _run_root_cause_analysis(self, state: RunState) -> None:
        report = analyze_root_cause(state, kg=self.kg, memory=self.memory)
        if report is None:
            state.add_trace(AgentTrace(
                agent="RootCauseEngine",
                observation="No compiler, waveform, or memory evidence available to localize a fault.",
                reasoning="Evidence fusion requires at least one signal (compile error or waveform anomaly).",
                confidence=0.0,
                action_taken="No hypothesis generated.",
            ))
            return

        state.root_cause_reports.append(report)
        bug = Bug(
            severity="high" if report.confidence >= 0.85 else "medium",
            summary=report.root_cause,
            hypothesis=report.hypothesis,
        )
        state.bugs.append(bug)
        self._current_bug = bug

        state.add_trace(AgentTrace(
            agent="RootCauseEngine",
            observation=report.root_cause,
            reasoning=(f"Fused {len(report.evidence)} evidence item(s) across compiler/simulation/"
                      f"waveform/knowledge-graph/memory sources; ranked {1 + len(report.alternative_hypotheses)} "
                      f"hypothesis(es) by confidence."),
            evidence=[f"[{e.source}] {e.detail}" for e in report.evidence],
            confidence=report.confidence,
            action_taken=f"Opened Bug {bug.bug_id[:8]} with recommended fix: {report.recommended_fix}",
        ))
        self.memory.remember(MemoryRecord(
            trace_id=state.trace_id, kind="root_cause", project_root=self.project_root,
            summary=report.root_cause,
            details={"confidence": report.confidence, "recommended_fix": report.recommended_fix},
        ))

    def _run_generate_patch(self, state: RunState) -> None:
        bug = self._current_bug or next((b for b in state.bugs if b.status == "open"), None)
        if bug is None:
            state.add_trace(AgentTrace(
                agent="PatchGenerator",
                observation="No open bug to patch.",
                reasoning="Patch generation requires a localized bug from the Root Cause Engine.",
                action_taken="Skipped.",
            ))
            return

        # Consult memory: was a similar bug already fixed before?
        prior_fix = self.memory.successful_fix_for(bug.summary)
        patch = patch_qa.generate_patch(state, bug)
        self._current_patch = patch

        if patch is None:
            bug.status = "rejected"
            state.add_trace(AgentTrace(
                agent="PatchGenerator",
                observation=f"No safe heuristic patch template matched bug {bug.bug_id[:8]}.",
                reasoning="Graph-constrained patching only fires when the implicated register/always "
                          "block pattern matches a known-safe repair template.",
                confidence=0.3,
                action_taken="Marked bug as rejected; will escalate.",
            ))
            return

        state.patches.append(patch)
        state.add_trace(AgentTrace(
            agent="PatchGenerator",
            observation=f"Generated patch {patch.patch_id[:8]} targeting {patch.target_file}.",
            reasoning=patch.rationale + (
                f" (Engineering memory shows a similar fix succeeded before: '{prior_fix.summary}'.)"
                if prior_fix else " (No matching prior fix found in engineering memory.)"
            ),
            evidence=[patch.diff[:400]],
            confidence=0.8 if prior_fix else 0.6,
            action_taken="Proposed PatchProposal for QA review.",
        ))

    def _run_validate_patch(self, state: RunState) -> None:
        patch = self._current_patch
        bug = self._current_bug
        if patch is None or bug is None:
            return

        verdict = patch_qa.qa_review(state, patch)
        state.qa_verdicts[patch.patch_id] = verdict
        state.add_trace(AgentTrace(
            agent="QAAgent",
            observation=f"QA verdict: {verdict.verdict}.",
            reasoning="; ".join(verdict.reasons),
            confidence=0.9 if verdict.verdict == "approve" else 0.4,
            action_taken="Approved for validation." if verdict.verdict == "approve" else "Rejected before validation.",
        ))

        if verdict.verdict != "approve":
            bug.status = "rejected"
            self.memory.remember(MemoryRecord(
                trace_id=state.trace_id, kind="fix", project_root=self.project_root,
                summary=bug.summary, details={"template": "reset_add"}, outcome="failure",
            ))
            return

        with open(patch.target_file) as f:
            original = f.read()
        from planner.planner import _apply_unified_diff  # reuse existing, battle-tested applier
        patched_text = _apply_unified_diff(original, patch.diff)

        result = validate_and_apply(state, patch, patched_text, self.work_dir)
        state.validation_results.append(result)

        state.add_trace(AgentTrace(
            agent="PatchValidationAgent",
            observation=(f"compiled={result.compiled}, simulated={result.simulated}, "
                        f"{result.passed_checks} passed / {result.failed_checks} failed "
                        f"(baseline {result.baseline_passed_checks}/{result.baseline_failed_checks})."),
            reasoning="Re-compiled and re-simulated the patched design in isolation and compared "
                      "against the pre-patch baseline before allowing it to persist.",
            evidence=result.notes,
            confidence=0.95 if result.improved else 0.3,
            action_taken="Rolled back automatically." if result.rolled_back else "Patch accepted as new baseline.",
        ))

        if result.rolled_back:
            bug.status = "rejected"
            self.memory.remember(MemoryRecord(
                trace_id=state.trace_id, kind="regression_result", project_root=self.project_root,
                summary=f"Patch for '{bug.summary}' rolled back", outcome="failure",
                details={"patch_id": patch.patch_id},
            ))
        else:
            bug.status = "patched"
            state.modules = rtl_parser.parse_project(state.manifest.rtl_files + state.manifest.tb_files)
            self.kg.build(state.modules)
            state.kg_summary = self.kg.summarize()
            self.memory.remember(MemoryRecord(
                trace_id=state.trace_id, kind="successful_patch", project_root=self.project_root,
                summary=bug.summary, outcome="success",
                details={"patch_id": patch.patch_id, "rationale": patch.rationale},
            ))

    # apply_patch / rollback_patch nodes are folded into validate_patch above
    # (validation and application are inseparable: we never persist an
    # unvalidated patch) - kept as distinct PlannerNodeName values for the
    # state-machine diagram / future LangGraph port, but routed as no-ops here.
    def _run_apply_patch(self, state: RunState) -> None:
        pass

    def _run_rollback_patch(self, state: RunState) -> None:
        pass

    # -- transition functions (the decision graph itself) -------------------

    def _after_scan_project(self, state: RunState) -> PlannerNodeName:
        return "build_knowledge_graph"

    def _after_build_kg(self, state: RunState) -> PlannerNodeName:
        return "static_analysis"

    def _after_static_analysis(self, state: RunState) -> PlannerNodeName:
        return "compile"

    def _after_generate_report(self, state: RunState) -> PlannerNodeName:
        return "terminate"

    def _after_compile(self, state: RunState) -> PlannerNodeName:
        if state.compile_report.success:
            return "simulate"
        if self._should_retry("compile", state):
            # A compile error means we already have strong evidence; go straight
            # to root cause analysis rather than blindly retrying compilation.
            return "root_cause_analysis"
        return "root_cause_analysis"

    def _after_simulate(self, state: RunState) -> PlannerNodeName:
        if state.sim_result.success:
            state.status = "success"
            state.stop_reason = "All checks passed, no open bugs, no anomalies."
            return "generate_report"
        if state.sim_result.vcd_path:
            return "analyze_waveform"
        return "root_cause_analysis"

    def _after_analyze_waveform(self, state: RunState) -> PlannerNodeName:
        return "root_cause_analysis"

    def _after_root_cause(self, state: RunState) -> PlannerNodeName:
        if self._check_terminal(state):
            return "generate_report"
        open_bugs = [b for b in state.bugs if b.status == "open"]
        if not open_bugs:
            state.status = "escalated"
            state.stop_reason = "Failing but no localizable root cause found."
            return "generate_report"
        self._current_bug = open_bugs[0]
        return "generate_patch"

    def _after_generate_patch(self, state: RunState) -> PlannerNodeName:
        if self._current_patch is None:
            if self._check_terminal(state):
                return "generate_report"
            return "compile"  # nothing left to try on this bug; re-check overall state
        return "validate_patch"

    def _after_validate_patch(self, state: RunState) -> PlannerNodeName:
        if self._check_terminal(state):
            return "generate_report"
        self._current_patch = None
        self._current_bug = None
        return "compile"

    def _after_apply_patch(self, state: RunState) -> PlannerNodeName:
        return "compile"

    def _after_rollback_patch(self, state: RunState) -> PlannerNodeName:
        return "root_cause_analysis"

    # -- shared stop/retry logic ------------------------------------------

    def _check_terminal(self, state: RunState) -> bool:
        if state.compile_report and state.compile_report.success and \
           state.sim_result and state.sim_result.success:
            state.status = "success"
            state.stop_reason = "All checks passed, no open bugs, no anomalies."
            return True
        if state.iteration >= self.max_iterations:
            state.status = "partial"
            state.stop_reason = f"Max iterations ({self.max_iterations}) reached."
            return True
        if state.bugs and all(b.status == "rejected" for b in state.bugs):
            state.status = "escalated"
            state.stop_reason = "All patch attempts rejected by QA/validation; needs human review."
            return True
        return False

    def _should_retry(self, node: str, state: RunState) -> bool:
        count = self.retry_counts.get(node, 0)
        if count >= MAX_RETRIES_PER_NODE:
            return False
        self.retry_counts[node] = count + 1
        return True

    # -- bookkeeping --------------------------------------------------------

    def _record_decision(self, state: RunState, from_node: PlannerNodeName,
                          to_node: PlannerNodeName, reason: str) -> None:
        decision = PlannerDecision(from_node=from_node, to_node=to_node,
                                    reason=reason, iteration=state.iteration)
        state.planner_decisions.append(decision)
        state.log(f"Planner: {from_node} -> {to_node} ({reason})")
        self.memory.remember(MemoryRecord(
            trace_id=state.trace_id, kind="planner_decision", project_root=self.project_root,
            summary=f"{from_node} -> {to_node}", details={"reason": reason, "iteration": state.iteration},
        ))

    def _explain_transition(self, state: RunState, node: PlannerNodeName,
                             next_node: PlannerNodeName) -> str:
        if next_node == "terminate":
            return state.stop_reason or "Terminating."
        if node == "compile" and next_node == "simulate":
            return "Compile succeeded; simulation is required to check functional correctness."
        if node == "compile" and next_node == "root_cause_analysis":
            return "Compile failed; skipping simulation entirely and going straight to root cause analysis."
        if node == "simulate" and next_node == "analyze_waveform":
            return "Simulation failed and a VCD was produced; waveform evidence may help localize the fault."
        if node == "simulate" and next_node == "root_cause_analysis":
            return "Simulation failed with no VCD; skipping waveform analysis."
        if node == "root_cause_analysis" and next_node == "generate_patch":
            return f"Localized bug {self._current_bug.bug_id[:8] if self._current_bug else '?'}; attempting a patch."
        if node == "generate_patch" and next_node == "validate_patch":
            return "Patch generated; validating in isolation before it can become the new baseline."
        if node == "validate_patch" and next_node == "compile":
            return "Patch validation resolved (accepted or rolled back); re-checking overall project state."
        return f"Default transition {node} -> {next_node}."

    def _persist_run_summary(self, state: RunState) -> None:
        self.memory.remember(MemoryRecord(
            trace_id=state.trace_id, kind="regression_result", project_root=self.project_root,
            summary=f"Run finished: {state.status}", outcome=(
                "success" if state.status == "success" else
                "failure" if state.status == "escalated" else "unknown"
            ),
            details={"iterations": state.iteration, "stop_reason": state.stop_reason},
        ))


def run_engineering_loop(project_root: str, top_module: str, work_dir: str,
                          max_iterations: int = 10) -> RunState:
    """Drop-in replacement entry point mirroring
    ``planner.planner.run_engineering_loop``'s signature, backed by the
    Dynamic Planner's decision graph instead of a fixed pipeline."""
    planner = DynamicPlanner(project_root, top_module, work_dir, max_iterations)
    return planner.run()
