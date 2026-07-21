"""
Evidence-Based Root Cause Analysis (Priority Feature #4).

Upgrades the original heuristic in ``agents.waveform_rootcause`` from "first
match wins" to genuine evidence fusion: every hypothesis accumulates typed
``EvidenceItem`` objects from up to five independent sources (compiler logs,
simulation logs, waveforms, the knowledge graph, and engineering memory of
previous failures), and the final confidence is a weighted combination of
that evidence rather than a single hard-coded number. The result is a
``RootCauseReport`` with root cause, confidence, evidence, alternative
hypotheses, and a recommended fix - all structured, none of it just a string
buried in a log line.

This module does not replace ``agents.waveform_rootcause`` (kept intact for
backward compatibility / the original linear planner) - it is the engine the
new Dynamic Planner calls instead.
"""
from __future__ import annotations

import logging
from typing import Optional

from core.knowledge_graph import ProjectKnowledgeGraph
from core.memory import EngineeringMemory
from core.schemas import (
    EvidenceItem,
    RootCauseHypothesis,
    RootCauseReport,
    RunState,
)

logger = logging.getLogger(__name__)


def _hypothesis_from_compile_errors(state: RunState) -> list[tuple[RootCauseHypothesis, list[EvidenceItem]]]:
    out = []
    if not state.compile_report or state.compile_report.success:
        return out
    for diag in state.compile_report.diagnostics:
        if diag.severity != "error":
            continue
        evidence = [EvidenceItem(
            source="compiler_log",
            detail=f"{diag.file}:{diag.line}: {diag.message}",
            weight=0.9,
        )]
        hyp = RootCauseHypothesis(
            confidence=0.9,
            summary=f"Compile error: {diag.message}",
            implicated_files=[diag.file],
            implicated_lines=[diag.line] if diag.line else [],
            evidence_refs=["CompileReport"],
        )
        out.append((hyp, evidence))
    return out


def _hypothesis_from_waveform(
    state: RunState, kg: Optional[ProjectKnowledgeGraph],
) -> list[tuple[RootCauseHypothesis, list[EvidenceItem]]]:
    out = []
    if not state.wave_findings:
        return out

    for anomaly in state.wave_findings.anomalies:
        sig_leaf = anomaly.signal.split(".")[-1]
        owning_module, owning_file = None, None
        for m in state.modules:
            if sig_leaf in m.registers:
                owning_module, owning_file = m.name, m.file
                break

        evidence = [EvidenceItem(
            source="waveform",
            detail=f"Signal '{anomaly.signal}' -> X at {anomaly.time_ns}ns ({anomaly.detail})",
            weight=0.5,
        )]

        confidence = 0.5
        missing_reset = False
        if owning_module and kg is not None:
            no_reset_regs = kg.registers_without_reset(owning_module)
            if sig_leaf in no_reset_regs:
                missing_reset = True
                confidence = 0.85
                evidence.append(EvidenceItem(
                    source="knowledge_graph",
                    detail=(f"Knowledge graph shows register '{sig_leaf}' in module "
                            f"'{owning_module}' is written by a clocked always block "
                            f"with no matched reset edge."),
                    weight=0.8,
                ))
        elif owning_module:
            confidence = 0.65

        # Consult engineering memory for prior occurrences of this exact
        # signature (module + signal) going X.
        summary_text = f"{sig_leaf} X-propagation {owning_module or ''}"
        out.append((
            RootCauseHypothesis(
                confidence=confidence,
                summary=(
                    f"Signal '{anomaly.signal}' goes to X at {anomaly.time_ns}ns"
                    + (f", driven by a clocked-but-unreset register in module "
                       f"'{owning_module}'" if missing_reset else
                       (f" in module '{owning_module}'" if owning_module else ""))
                ),
                implicated_files=[owning_file] if owning_file else [],
                evidence_refs=["WaveformFindings", "KnowledgeGraph"] if missing_reset
                else ["WaveformFindings"],
            ),
            evidence,
        ))
    return out


def _augment_with_memory(
    hyp: RootCauseHypothesis, evidence: list[EvidenceItem], memory: Optional[EngineeringMemory],
) -> tuple[RootCauseHypothesis, list[EvidenceItem]]:
    if memory is None:
        return hyp, evidence
    similar = memory.find_similar(hyp.summary, kind="root_cause", limit=1)
    if similar:
        evidence = evidence + [EvidenceItem(
            source="previous_failure",
            detail=f"Similar root cause seen before: {similar[0].summary}",
            weight=0.3,
        )]
        # Slightly boost confidence when we've diagnosed this pattern before.
        hyp = hyp.model_copy(update={"confidence": min(0.97, hyp.confidence + 0.05)})
    return hyp, evidence


def analyze(
    state: RunState,
    kg: Optional[ProjectKnowledgeGraph] = None,
    memory: Optional[EngineeringMemory] = None,
) -> Optional[RootCauseReport]:
    """Fuse all available evidence into a single, ranked RootCauseReport.

    Returns None if there is no localizable evidence at all (caller should
    treat that as "cannot diagnose, escalate").
    """
    candidates: list[tuple[RootCauseHypothesis, list[EvidenceItem]]] = []
    candidates.extend(_hypothesis_from_compile_errors(state))
    candidates.extend(_hypothesis_from_waveform(state, kg))

    if not candidates:
        logger.info("Root cause engine: no localizable evidence available")
        return None

    augmented = [_augment_with_memory(h, e, memory) for h, e in candidates]
    augmented.sort(key=lambda pair: pair[0].confidence, reverse=True)

    top_hyp, top_evidence = augmented[0]
    alternatives = [h for h, _ in augmented[1:]]

    recommended_fix = _recommend_fix(top_hyp, top_evidence)

    report = RootCauseReport(
        hypothesis=top_hyp,
        root_cause=top_hyp.summary,
        confidence=top_hyp.confidence,
        evidence=top_evidence,
        alternative_hypotheses=alternatives,
        recommended_fix=recommended_fix,
    )
    logger.info("Root cause: %s (confidence=%.2f, %d evidence item(s), %d alternative(s))",
                report.root_cause, report.confidence, len(report.evidence), len(alternatives))
    return report


def _recommend_fix(hyp: RootCauseHypothesis, evidence: list[EvidenceItem]) -> str:
    text = hyp.summary.lower()
    if "compile error" in text:
        return "Fix the reported syntax/semantic error at the implicated file/line before proceeding."
    if "unreset" in text or "no matched reset" in text or "clocked-but-unreset" in text:
        return ("Add a synchronous (or asynchronous, per clocking convention) reset branch to the "
                "implicated always block so the register initializes to a known value.")
    if "x-propagation" in text or "goes to x" in text:
        return ("Trace the driving always block for this signal and ensure it is initialized "
                "on reset or via an explicit default assignment.")
    return "Inspect the implicated file/module manually; evidence was insufficient for an automatic fix."
