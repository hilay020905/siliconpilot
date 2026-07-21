"""
Explainability (Priority Feature #6) + Architecture Visualization (Priority
Feature #7).

Every agent contributes a structured ``AgentTrace`` (observation / reasoning /
evidence / confidence / action taken) to ``RunState.agent_traces``. This
module renders that into a human-readable Markdown report, and generates the
four requested Mermaid diagrams:

    1. Module hierarchy       (from the knowledge graph's `contains`/
                                `instantiates` edges)
    2. Dependency graph        (module `instantiates`/`depends_on` edges)
    3. Agent workflow           (static: which agents exist and hand off to
                                whom)
    4. Planner state machine    (from `RunState.planner_decisions`, i.e. the
                                actual transitions taken this run)
"""
from __future__ import annotations

import re
from typing import Optional

from core.knowledge_graph import ProjectKnowledgeGraph
from core.schemas import RunState


def _safe_id(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", text)


def module_hierarchy_diagram(kg: ProjectKnowledgeGraph) -> str:
    """Mermaid diagram of module `contains` structure (module -> its ports/
    registers/wires/always-blocks/FSMs)."""
    lines = ["graph TD"]
    for u, v, d in kg.graph.edges(data=True):
        if d.get("kind") != "contains":
            continue
        u_data, v_data = kg.graph.nodes[u], kg.graph.nodes[v]
        u_label = f'{_safe_id(u)}["{u_data.get("kind")}: {u_data.get("name", u.split("::")[-1])}"]'
        v_label = f'{_safe_id(v)}["{v_data.get("kind")}: {v_data.get("name", v.split("::")[-1])}"]'
        lines.append(f"    {u_label}")
        lines.append(f"    {v_label}")
        lines.append(f"    {_safe_id(u)} --> {_safe_id(v)}")
    if len(lines) == 1:
        lines.append('    empty["No containment edges found"]')
    return "\n".join(lines)


def dependency_diagram(kg: ProjectKnowledgeGraph) -> str:
    """Mermaid diagram of module instantiation / dependency edges only."""
    lines = ["graph LR"]
    for u, v, d in kg.graph.edges(data=True):
        if d.get("kind") not in ("instantiates", "depends_on"):
            continue
        u_name = kg.graph.nodes[u].get("name", u.split("::")[-1])
        v_name = kg.graph.nodes[v].get("name", v.split("::")[-1])
        lines.append(f'    {_safe_id(u)}["{u_name}"] -->|{d.get("kind")}| {_safe_id(v)}["{v_name}"]')
    if len(lines) == 1:
        lines.append('    empty["No dependency edges found"]')
    return "\n".join(lines)


def agent_workflow_diagram() -> str:
    """Static Mermaid diagram of the agent roster and their hand-offs, as
    orchestrated by the Dynamic Planner (contrast with the fixed pipeline this
    replaces)."""
    return "\n".join([
        "graph TD",
        '    PU["Project Understanding"] --> RP["RTL Parser"]',
        '    RP --> KG["Knowledge Graph Builder"]',
        '    KG --> PL["Dynamic Planner"]',
        '    PL -->|"decides next"| CS["Compiler Agent"]',
        '    PL -->|"decides next"| SIM["Simulation Agent"]',
        '    PL -->|"decides next"| WV["Waveform Analysis"]',
        '    PL -->|"decides next"| RC["Root Cause Engine"]',
        '    PL -->|"decides next"| PG["Patch Generator"]',
        '    PL -->|"decides next"| QA["QA / Critic"]',
        '    PL -->|"decides next"| PV["Patch Validation"]',
        '    CS --> PL',
        '    SIM --> PL',
        '    WV --> PL',
        '    RC -->|"consults"| MEM["Engineering Memory"]',
        '    RC --> PL',
        '    PG -->|"consults"| MEM',
        '    QA --> PL',
        '    PV -->|"rollback on regression"| PL',
        '    PL -->|"records"| MEM',
        '    PL -->|"terminate"| DONE["Final Report"]',
    ])


def planner_state_machine_diagram(state: RunState) -> str:
    """Mermaid state diagram of the actual transitions the Dynamic Planner
    took during this run (built from `RunState.planner_decisions`)."""
    lines = ["stateDiagram-v2", "    [*] --> scan_project"]
    for decision in state.planner_decisions:
        src = decision.from_node or "[*]"
        lines.append(f"    {src} --> {decision.to_node}: {decision.reason[:40]}")
    if not state.planner_decisions:
        lines.append("    scan_project --> terminate: no decisions recorded")
    return "\n".join(lines)


def render_report(
    state: RunState,
    kg: Optional[ProjectKnowledgeGraph] = None,
) -> str:
    """Renders the full explainability report as Markdown, embedding all four
    Mermaid diagrams plus every agent's structured reasoning trace."""
    parts: list[str] = []
    parts.append("# SiliconPilot Autonomous Engineering Report\n")
    parts.append(f"**Status:** {state.status}  \n**Stop reason:** {state.stop_reason}  \n"
                 f"**Iterations:** {state.iteration}\n")

    parts.append("## Agent Reasoning Trace\n")
    if not state.agent_traces:
        parts.append("_No structured agent traces were recorded for this run._\n")
    for t in state.agent_traces:
        parts.append(f"### {t.agent}")
        parts.append(f"- **Observation:** {t.observation}")
        parts.append(f"- **Reasoning:** {t.reasoning}")
        if t.evidence:
            parts.append("- **Evidence:**")
            for e in t.evidence:
                parts.append(f"  - {e}")
        parts.append(f"- **Confidence:** {t.confidence:.2f}")
        parts.append(f"- **Action taken:** {t.action_taken}\n")

    if state.root_cause_reports:
        parts.append("## Root Cause Reports\n")
        for r in state.root_cause_reports:
            parts.append(f"- **Root cause:** {r.root_cause} (confidence={r.confidence:.2f})")
            parts.append(f"  - Recommended fix: {r.recommended_fix}")
            for ev in r.evidence:
                parts.append(f"  - Evidence [{ev.source}]: {ev.detail}")
            for alt in r.alternative_hypotheses:
                parts.append(f"  - Alternative (confidence={alt.confidence:.2f}): {alt.summary}")
        parts.append("")

    if state.validation_results:
        parts.append("## Patch Validation Results\n")
        for v in state.validation_results:
            outcome = "ROLLED BACK" if v.rolled_back else ("IMPROVED" if v.improved else "NO CHANGE")
            parts.append(
                f"- Patch `{v.patch_id[:8]}`: compiled={v.compiled}, simulated={v.simulated}, "
                f"{v.passed_checks} passed / {v.failed_checks} failed "
                f"(baseline {v.baseline_passed_checks}/{v.baseline_failed_checks}) -> **{outcome}**"
            )
            for n in v.notes:
                parts.append(f"  - {n}")
        parts.append("")

    parts.append("## Architecture Diagrams\n")
    parts.append("### 1. Module Hierarchy\n```mermaid\n" +
                  (module_hierarchy_diagram(kg) if kg else "graph TD\n    n[No KG available]") +
                  "\n```\n")
    parts.append("### 2. Dependency Graph\n```mermaid\n" +
                  (dependency_diagram(kg) if kg else "graph TD\n    n[No KG available]") +
                  "\n```\n")
    parts.append("### 3. Agent Workflow\n```mermaid\n" + agent_workflow_diagram() + "\n```\n")
    parts.append("### 4. Planner State Machine (this run)\n```mermaid\n" +
                  planner_state_machine_diagram(state) + "\n```\n")

    return "\n".join(parts)
