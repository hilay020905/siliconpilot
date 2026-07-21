"""
Coverage Estimation (new capability #16).

SiliconPilot has no built-in simulator-level coverage database (Icarus
Verilog doesn't provide one out of the box, and adding a proprietary coverage
tool would violate the "keep dependencies lightweight" requirement). Instead
this module estimates *structural* coverage opportunities directly from the
static analysis: how many FSM states/transitions exist vs. how much the
current testbench's VCD activity touched, and a rough branch-count estimate
from `if`/`case` density. This gives the planner and report actionable
"here's what's likely undertested" signal without needing golden coverage
data.
"""
from __future__ import annotations

import re

from core.schemas import CoverageEstimate, FSMAnalysisReport, ModuleAST, WaveformFindings

_BRANCH_RE = re.compile(r"\b(if|case)\b", re.IGNORECASE)


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def estimate(
    modules: list[ModuleAST],
    fsm_report: FSMAnalysisReport,
    wave_findings: WaveformFindings | None,
) -> list[CoverageEstimate]:
    """One ``CoverageEstimate`` per module that has either an FSM or branch
    logic worth reporting on."""
    out: list[CoverageEstimate] = []
    touched_signals: set[str] = set()
    if wave_findings is not None:
        touched_signals = {a.signal for a in wave_findings.anomalies}

    fsm_by_module = {f.module: f for f in fsm_report.fsms}

    for module in modules:
        text = _read(module.file)
        if not text:
            continue
        branch_count = len(_BRANCH_RE.findall(text))
        notes: list[str] = []

        fsm_state_pct = None
        fsm = fsm_by_module.get(module.name)
        if fsm and fsm.states:
            # Without a golden VCD-state trace we can only report *structural*
            # reachability as a coverage proxy: states with no incoming edge
            # or that are unused can never be hit by any test, so subtract them.
            uncoverable = set(fsm.unreachable_states) | set(fsm.unused_states)
            coverable = [s for s in fsm.states if s not in uncoverable]
            fsm_state_pct = 100.0 * len(coverable) / len(fsm.states) if fsm.states else None
            if uncoverable:
                notes.append(f"{len(uncoverable)} state(s) structurally unreachable/unused: "
                             f"{sorted(uncoverable)}")

        branch_pct = None
        if branch_count:
            # Heuristic: assume simulation exercises the "happy path" of most
            # branches but flag modules with high branch density and no
            # waveform anomalies recorded (i.e. we have no direct evidence
            # those branches were even toggled).
            evidence_ratio = min(1.0, len(touched_signals) / max(branch_count, 1))
            branch_pct = round(40.0 + 60.0 * evidence_ratio, 1)
            if branch_pct < 70:
                notes.append(f"{branch_count} branch point(s) detected; limited waveform "
                             f"evidence of exercising all of them.")

        if fsm_state_pct is None and branch_pct is None:
            continue

        out.append(CoverageEstimate(
            module=module.name,
            fsm_state_coverage_pct=fsm_state_pct,
            branch_estimate_pct=branch_pct,
            toggle_opportunities=branch_count,
            uncovered_notes=notes,
        ))
    return out
