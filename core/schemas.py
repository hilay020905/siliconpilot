"""
Typed message/payload schemas used across all SiliconPilot agents.
No agent passes raw strings between each other - everything is one of these models.
"""
from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field
import uuid
import datetime


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Bus envelope
# ---------------------------------------------------------------------------

class Message(BaseModel):
    msg_id: str = Field(default_factory=new_id)
    trace_id: str
    parent_msg_id: Optional[str] = None
    from_agent: str
    to_agent: str
    type: Literal["task", "result", "error", "escalation"]
    task_type: str
    payload: dict[str, Any]
    created_at: str = Field(default_factory=now)


# ---------------------------------------------------------------------------
# Project understanding
# ---------------------------------------------------------------------------

class ProjectManifest(BaseModel):
    root: str
    rtl_files: list[str]
    tb_files: list[str]
    top_module: Optional[str] = None
    toolchain: str = "icarus"


# ---------------------------------------------------------------------------
# RTL parsing
# ---------------------------------------------------------------------------

class Port(BaseModel):
    name: str
    direction: Literal["input", "output", "inout"]
    width: int = 1


class ModuleAST(BaseModel):
    name: str
    file: str
    ports: list[Port] = []
    instances: list[str] = []  # names of module types instantiated inside
    registers: list[str] = []


# ---------------------------------------------------------------------------
# Dependency graph (serialized summary; full graph lives in NetworkX/Neo4j)
# ---------------------------------------------------------------------------

class DependencyEdge(BaseModel):
    src: str
    dst: str
    kind: Literal["instantiates", "drives"]


class DependencyGraphSummary(BaseModel):
    modules: list[str]
    edges: list[DependencyEdge]


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

class Diagnostic(BaseModel):
    severity: Literal["error", "warning"]
    file: str
    line: Optional[int] = None
    message: str
    code: Optional[str] = None


class CompileReport(BaseModel):
    success: bool
    tool: str
    duration_ms: int
    diagnostics: list[Diagnostic] = []
    binary_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class SimResult(BaseModel):
    success: bool
    tool: str
    duration_ms: int
    passed_checks: int = 0
    failed_checks: int = 0
    stdout_tail: str = ""
    vcd_path: Optional[str] = None
    failure_summary: Optional[str] = None


# ---------------------------------------------------------------------------
# Waveform analysis
# ---------------------------------------------------------------------------

class WaveAnomaly(BaseModel):
    type: Literal["x_propagation", "unexpected_glitch", "stuck_signal"]
    signal: str
    time_ns: Optional[int] = None
    detail: str = ""


class WaveformFindings(BaseModel):
    vcd_path: str
    anomalies: list[WaveAnomaly] = []


# ---------------------------------------------------------------------------
# Root cause / bugs
# ---------------------------------------------------------------------------

class RootCauseHypothesis(BaseModel):
    hypothesis_id: str = Field(default_factory=new_id)
    confidence: float
    summary: str
    implicated_files: list[str] = []
    implicated_lines: list[int] = []
    evidence_refs: list[str] = []


class Bug(BaseModel):
    bug_id: str = Field(default_factory=new_id)
    severity: Literal["low", "medium", "high", "critical"]
    summary: str
    hypothesis: RootCauseHypothesis
    status: Literal["open", "patched", "verified", "rejected"] = "open"


# ---------------------------------------------------------------------------
# Patch generation / QA
# ---------------------------------------------------------------------------

class PatchProposal(BaseModel):
    patch_id: str = Field(default_factory=new_id)
    bug_id: str
    target_file: str
    diff: str
    rationale: str
    risk_level: Literal["low", "medium", "high"] = "low"


class QAVerdict(BaseModel):
    verdict: Literal["approve", "reject"]
    reasons: list[str] = []
    blocking: bool = False


# ---------------------------------------------------------------------------
# Lint / CDC / Coverage (lightweight versions for the OSS Phase-1 stack)
# ---------------------------------------------------------------------------

class LintFinding(BaseModel):
    severity: Literal["error", "warning", "style"]
    file: str
    line: Optional[int] = None
    message: str


class LintReport(BaseModel):
    findings: list[LintFinding] = []


class CoverageReport(BaseModel):
    line_pct: float = 0.0
    toggle_pct: float = 0.0
    notes: str = ""


# ---------------------------------------------------------------------------
# Static RTL analysis (new capability §5)
# ---------------------------------------------------------------------------

StaticFindingKind = Literal[
    "missing_reset", "latch_inference", "unused_signal", "unreachable_state",
    "multiple_drivers", "combinational_loop", "blocking_in_sequential",
    "nonblocking_in_combinational", "width_mismatch", "signed_unsigned_mix",
    "floating_wire", "constant_output", "dead_logic", "clock_gating",
]


class StaticFinding(BaseModel):
    kind: StaticFindingKind
    severity: Literal["error", "warning", "info"] = "warning"
    module: str
    file: str
    line: Optional[int] = None
    signal: Optional[str] = None
    message: str
    confidence: float = 0.7


class StaticAnalysisReport(BaseModel):
    findings: list[StaticFinding] = Field(default_factory=list)
    modules_analyzed: int = 0
    duration_ms: int = 0

    def by_severity(self, severity: str) -> list[StaticFinding]:
        return [f for f in self.findings if f.severity == severity]


# ---------------------------------------------------------------------------
# FSM analysis (new capability §6)
# ---------------------------------------------------------------------------

class FSMTransition(BaseModel):
    src_state: str
    dst_state: str
    condition: str = ""


class FSMInfo(BaseModel):
    module: str
    state_signal: str
    states: list[str] = Field(default_factory=list)
    transitions: list[FSMTransition] = Field(default_factory=list)
    unreachable_states: list[str] = Field(default_factory=list)
    unused_states: list[str] = Field(default_factory=list)
    deadlock_states: list[str] = Field(default_factory=list)
    encoding: Literal["binary", "one_hot", "gray", "unknown"] = "unknown"


class FSMAnalysisReport(BaseModel):
    fsms: list[FSMInfo] = Field(default_factory=list)

    def total_transitions(self) -> int:
        return sum(len(f.transitions) for f in self.fsms)


# ---------------------------------------------------------------------------
# Counter analysis (new capability §7)
# ---------------------------------------------------------------------------

class CounterInfo(BaseModel):
    module: str
    signal: str
    width: int = 0
    has_reset: bool = False
    has_enable: bool = False
    increments_by: Optional[str] = None
    issues: list[str] = Field(default_factory=list)


class CounterAnalysisReport(BaseModel):
    counters: list[CounterInfo] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Assertion generation (new capability §14)
# ---------------------------------------------------------------------------

class GeneratedAssertion(BaseModel):
    name: str
    module: str
    kind: Literal["reset", "fsm_legality", "counter_monotonic", "handshake"]
    sva_text: str
    rationale: str = ""


class AssertionSuite(BaseModel):
    module: str
    assertions: list[GeneratedAssertion] = Field(default_factory=list)
    file_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Coverage estimation (new capability §16, complements CoverageReport)
# ---------------------------------------------------------------------------

class CoverageEstimate(BaseModel):
    module: str
    fsm_state_coverage_pct: Optional[float] = None
    branch_estimate_pct: Optional[float] = None
    toggle_opportunities: int = 0
    uncovered_notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Run-level state (what the Planner threads through the LangGraph-style loop)
# ---------------------------------------------------------------------------

class RunState(BaseModel):
    trace_id: str = Field(default_factory=new_id)
    project_root: str
    manifest: Optional[ProjectManifest] = None
    modules: list[ModuleAST] = []
    dep_graph: Optional[DependencyGraphSummary] = None
    compile_report: Optional[CompileReport] = None
    sim_result: Optional[SimResult] = None
    wave_findings: Optional[WaveformFindings] = None
    bugs: list[Bug] = []
    patches: list[PatchProposal] = []
    qa_verdicts: dict[str, QAVerdict] = {}
    lint_report: Optional[LintReport] = None
    coverage_report: Optional[CoverageReport] = None
    iteration: int = 0
    max_iterations: int = 10
    status: Literal["running", "success", "partial", "escalated"] = "running"
    stop_reason: Optional[str] = None
    trace_log: list[str] = []

    # --- new: dynamic planner / knowledge graph / explainability threading ---
    kg_summary: Optional["KnowledgeGraphSummary"] = None
    agent_traces: list["AgentTrace"] = []
    planner_decisions: list["PlannerDecision"] = []
    validation_results: list["ValidationResult"] = []
    root_cause_reports: list["RootCauseReport"] = []
    baseline_sim_result: Optional[SimResult] = None

    # --- new: static analysis / FSM / counter / assertions / coverage ---
    static_report: Optional["StaticAnalysisReport"] = None
    fsm_report: Optional["FSMAnalysisReport"] = None
    counter_report: Optional["CounterAnalysisReport"] = None
    assertion_suites: list["AssertionSuite"] = []
    coverage_estimates: list["CoverageEstimate"] = []
    report_paths: dict[str, str] = {}

    def add_trace(self, trace: "AgentTrace") -> None:
        """Record a structured explainability trace from an agent."""
        self.agent_traces.append(trace)

    def log(self, msg: str):
        self.trace_log.append(f"[iter {self.iteration}] {msg}")


# ---------------------------------------------------------------------------
# Knowledge Graph (§2)
# ---------------------------------------------------------------------------

KGNodeKind = Literal[
    "module", "signal", "register", "wire", "fsm", "clock_domain",
    "always_block", "pipeline_stage", "input", "output", "submodule",
]

KGEdgeKind = Literal[
    "contains", "drives", "reads", "writes", "instantiates",
    "depends_on", "clocked_by", "reset_by",
]


class KGNode(BaseModel):
    """A single node in the Project Knowledge Graph."""
    node_id: str
    kind: KGNodeKind
    attrs: dict[str, Any] = Field(default_factory=dict)


class KGEdge(BaseModel):
    """A single directed, typed edge in the Project Knowledge Graph."""
    src: str
    dst: str
    kind: KGEdgeKind
    attrs: dict[str, Any] = Field(default_factory=dict)


class KnowledgeGraphSummary(BaseModel):
    """Serializable snapshot of the NetworkX knowledge graph, for reports/UI."""
    nodes: list[KGNode] = Field(default_factory=list)
    edges: list[KGEdge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Engineering Memory (§3)
# ---------------------------------------------------------------------------

MemoryKind = Literal[
    "bug", "fix", "compiler_error", "waveform_observation",
    "root_cause", "successful_patch", "regression_result", "planner_decision",
]


class MemoryRecord(BaseModel):
    """A single episodic entry in Engineering Memory. Persisted to disk as JSONL
    so the planner can consult prior runs, not just the current one."""
    record_id: str = Field(default_factory=new_id)
    trace_id: str
    kind: MemoryKind
    project_root: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    outcome: Optional[Literal["success", "failure", "unknown"]] = None
    created_at: str = Field(default_factory=now)


# ---------------------------------------------------------------------------
# Evidence-based Root Cause Analysis (§4)
# ---------------------------------------------------------------------------

class EvidenceItem(BaseModel):
    """One unit of evidence feeding a root-cause hypothesis, with explicit
    provenance so the final report can show its work."""
    source: Literal[
        "compiler_log", "simulation_log", "waveform", "knowledge_graph",
        "previous_failure",
    ]
    detail: str
    weight: float = 0.5


class RootCauseReport(BaseModel):
    """Structured, evidence-fused root cause output (replaces ad-hoc printing).
    `hypothesis` is required so downstream code can treat this the same way it
    treats a RootCauseHypothesis (e.g. attach it to a Bug)."""
    hypothesis: RootCauseHypothesis
    root_cause: str
    confidence: float
    evidence: list[EvidenceItem] = Field(default_factory=list)
    alternative_hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)
    recommended_fix: str = ""


# ---------------------------------------------------------------------------
# Patch validation (§5)
# ---------------------------------------------------------------------------

class ValidationResult(BaseModel):
    """Outcome of compiling + simulating + regression-testing a candidate patch
    in isolation, before it is allowed to become the new baseline."""
    patch_id: str
    compiled: bool
    simulated: bool
    regression_passed: bool
    passed_checks: int = 0
    failed_checks: int = 0
    baseline_passed_checks: int = 0
    baseline_failed_checks: int = 0
    improved: bool = False
    rolled_back: bool = False
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Explainability (§6)
# ---------------------------------------------------------------------------

class AgentTrace(BaseModel):
    """Structured reasoning trace every agent contributes to the final report."""
    agent: str
    observation: str
    reasoning: str
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    action_taken: str = ""
    created_at: str = Field(default_factory=now)


# ---------------------------------------------------------------------------
# Dynamic planner (§1)
# ---------------------------------------------------------------------------

PlannerNodeName = Literal[
    "scan_project", "build_knowledge_graph", "static_analysis", "compile", "simulate",
    "analyze_waveform", "root_cause_analysis", "generate_patch",
    "validate_patch", "apply_patch", "rollback_patch", "generate_report", "terminate",
]


class PlannerDecision(BaseModel):
    """One transition taken by the dynamic planner's decision graph."""
    from_node: Optional[PlannerNodeName] = None
    to_node: PlannerNodeName
    reason: str
    iteration: int
    created_at: str = Field(default_factory=now)
