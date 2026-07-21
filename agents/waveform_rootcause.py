"""
Waveform Analysis Agent + Root Cause Analysis Agent.

Waveform Analysis: parses VCD, flags anomalies (X-propagation etc).
Root Cause Analysis: fuses CompileReport diagnostics + WaveformFindings + the module
AST/dependency graph into a typed RootCauseHypothesis. In the full design this step
also calls an LLM constrained to reason over structured evidence only (see design doc
§4/§21) - here we implement the deterministic evidence-fusion part, with a clearly
marked extension point for that LLM call.
"""
from __future__ import annotations
from core.schemas import RunState, WaveformFindings, WaveAnomaly, RootCauseHypothesis, Bug
from tool_adapters import vcd_adapter


def run_waveform_analysis(state: RunState) -> RunState:
    if not state.sim_result or not state.sim_result.vcd_path:
        state.log("No VCD produced, skipping waveform analysis")
        return state

    vcd = vcd_adapter.parse_vcd(state.sim_result.vcd_path)
    anomalies_raw = vcd_adapter.find_x_propagation(vcd)
    anomalies = [
        WaveAnomaly(type="x_propagation", signal=a["signal"], time_ns=a["time_ns"],
                    detail=f"value={a['value']}")
        for a in anomalies_raw
    ]
    state.wave_findings = WaveformFindings(vcd_path=state.sim_result.vcd_path, anomalies=anomalies)
    state.log(f"Waveform analysis: {len(anomalies)} anomaly(ies)")
    for a in anomalies:
        state.log(f"  X-propagation on {a.signal} @ {a.time_ns}ns")
    return state


# --- Extension point -------------------------------------------------------
# def llm_localize(evidence: dict) -> RootCauseHypothesis:
#     """Real deployment: call Claude with the structured evidence dict below
#     (never raw stdout/VCD text) and a response schema forcing RootCauseHypothesis
#     JSON out. Left as a stub here so the reference implementation runs with zero
#     API key / network dependency."""
#     ...
# ----------------------------------------------------------------------------

def run_root_cause_analysis(state: RunState) -> RunState:
    """Deterministic evidence fusion (Phase-1 heuristic engine, swappable for an LLM call)."""
    hypotheses: list[RootCauseHypothesis] = []

    # 1) Compile errors are the strongest, highest-confidence signal.
    if state.compile_report and not state.compile_report.success:
        for d in state.compile_report.diagnostics:
            if d.severity == "error":
                hypotheses.append(RootCauseHypothesis(
                    confidence=0.9,
                    summary=f"Compile error: {d.message}",
                    implicated_files=[d.file],
                    implicated_lines=[d.line] if d.line else [],
                    evidence_refs=["CompileReport"],
                ))

    # 2) Waveform X-propagation, correlated against register declarations in modules
    #    that lack a reset-sensitive always block (heuristic proxy for "missing reset").
    if state.wave_findings:
        for a in state.wave_findings.anomalies:
            sig_leaf = a.signal.split(".")[-1]
            owning_module = None
            owning_file = None
            for m in state.modules:
                if sig_leaf in m.registers:
                    owning_module = m.name
                    owning_file = m.file
                    break
            hypotheses.append(RootCauseHypothesis(
                confidence=0.75 if owning_module else 0.5,
                summary=(f"Signal '{a.signal}' goes to X at {a.time_ns}ns, "
                         f"consistent with an uninitialized/unreset register"
                         + (f" in module '{owning_module}'" if owning_module else "")),
                implicated_files=[owning_file] if owning_file else [],
                evidence_refs=["WaveformFindings"],
            ))

    if not hypotheses:
        state.log("Root cause analysis: no localizable evidence")
        return state

    hypotheses.sort(key=lambda h: h.confidence, reverse=True)
    top = hypotheses[0]
    bug = Bug(
        severity="high" if top.confidence >= 0.85 else "medium",
        summary=top.summary,
        hypothesis=top,
    )
    state.bugs.append(bug)
    state.log(f"Root cause hypothesis (confidence={top.confidence:.2f}): {top.summary}")
    return state
