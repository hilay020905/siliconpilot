"""
Design Quality Report (new capability #12).

Wraps ``core.explainability.render_report`` (agent traces + Mermaid
diagrams, unchanged) and appends the new analysis sections: static analysis
findings, FSM analysis (with per-FSM Mermaid state diagrams), counter
analysis, generated assertions, and coverage estimates. Emits both a
Markdown file and a minimal self-contained HTML file (Markdown embedded in a
``<pre>``-free styled shell with Mermaid rendered client-side via CDN, so no
new Python dependency is required for either format).
"""
from __future__ import annotations

import html
import os
from typing import Optional

from analysis.fsm_analyzer import to_mermaid as fsm_to_mermaid
from core.explainability import render_report
from core.knowledge_graph import ProjectKnowledgeGraph
from core.schemas import RunState

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SiliconPilot Design Quality Report</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
         max-width: 980px; margin: 2rem auto; padding: 0 1rem; color: #1c1c1e; }}
  h1, h2, h3 {{ color: #111; }}
  code, pre {{ background: #f4f4f6; border-radius: 6px; }}
  pre {{ padding: 0.75rem; overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; font-size: 0.9rem; }}
  th {{ background: #f0f0f3; }}
  .sev-error {{ color: #b00020; font-weight: 600; }}
  .sev-warning {{ color: #a06400; font-weight: 600; }}
  .sev-info {{ color: #555; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.8rem;
            background: #eee; }}
</style>
</head>
<body>
{body}
<script>mermaid.initialize({{ startOnLoad: true }});</script>
</body>
</html>
"""


def _md_to_html_fragment(markdown_text: str) -> str:
    """Very small Markdown->HTML transform covering the subset this report
    actually emits (#/##/### headers, bullet lists, mermaid fenced blocks,
    other fenced code, bold, paragraphs). Avoids a new dependency."""
    lines = markdown_text.splitlines()
    out: list[str] = []
    in_code = False
    code_lang = ""
    in_list = False

    for line in lines:
        if line.strip().startswith("```"):
            if not in_code:
                in_code = True
                code_lang = line.strip()[3:].strip()
                tag = "div class=\"mermaid\"" if code_lang == "mermaid" else "pre"
                out.append(f"<{tag}>")
            else:
                in_code = False
                tag = "div" if code_lang == "mermaid" else "pre"
                out.append(f"</{tag}>")
            continue
        if in_code:
            out.append(line if code_lang == "mermaid" else html.escape(line))
            continue

        stripped = line.strip()
        if stripped.startswith("### "):
            out.append(f"<h3>{html.escape(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{html.escape(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h1>{html.escape(stripped[2:])}</h1>")
        elif stripped.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html.escape(stripped[2:])}</li>")
            continue
        elif stripped == "":
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("")
        else:
            out.append(f"<p>{html.escape(stripped)}</p>")

        if in_list and not stripped.startswith("- "):
            out.append("</ul>")
            in_list = False

    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _static_analysis_section(state: RunState) -> str:
    if not state.static_report or not state.static_report.findings:
        return "## Static RTL Analysis\n\n_No static analysis findings (or analysis not run)._\n"
    parts = ["## Static RTL Analysis\n"]
    parts.append(f"Analyzed {state.static_report.modules_analyzed} module(s) in "
                 f"{state.static_report.duration_ms}ms; "
                 f"{len(state.static_report.findings)} finding(s).\n")
    for sev in ("error", "warning", "info"):
        findings = state.static_report.by_severity(sev)
        if not findings:
            continue
        parts.append(f"### {sev.title()} ({len(findings)})\n")
        for f in findings:
            loc = f"{os.path.basename(f.file)}" + (f":{f.line}" if f.line else "")
            parts.append(f"- **[{f.kind}]** `{loc}` {f.message} (confidence={f.confidence:.2f})")
        parts.append("")
    return "\n".join(parts)


def _fsm_section(state: RunState) -> str:
    if not state.fsm_report or not state.fsm_report.fsms:
        return "## FSM Analysis\n\n_No finite state machines detected._\n"
    parts = ["## FSM Analysis\n"]
    for fsm in state.fsm_report.fsms:
        parts.append(f"### {fsm.module}.{fsm.state_signal}\n")
        parts.append(f"- States: {', '.join(fsm.states) or '(none recovered)'}")
        parts.append(f"- Transitions: {len(fsm.transitions)}")
        parts.append(f"- Encoding (heuristic): {fsm.encoding}")
        if fsm.unreachable_states:
            parts.append(f"- ⚠️ Unreachable states: {', '.join(fsm.unreachable_states)}")
        if fsm.unused_states:
            parts.append(f"- ⚠️ Declared but unused states: {', '.join(fsm.unused_states)}")
        if fsm.deadlock_states:
            parts.append(f"- 🛑 Possible deadlock states (no escape found): {', '.join(fsm.deadlock_states)}")
        parts.append("\n```mermaid\n" + fsm_to_mermaid(fsm) + "\n```\n")
    return "\n".join(parts)


def _counter_section(state: RunState) -> str:
    if not state.counter_report or not state.counter_report.counters:
        return "## Counter Analysis\n\n_No counters detected._\n"
    parts = ["## Counter Analysis\n"]
    parts.append("| Module | Signal | Width | Reset | Enable | Issues |")
    parts.append("|---|---|---|---|---|---|")
    for c in state.counter_report.counters:
        issues = "; ".join(c.issues) if c.issues else "none"
        parts.append(f"| {c.module} | {c.signal} | {c.width or '?'} | "
                     f"{'yes' if c.has_reset else 'no'} | {'yes' if c.has_enable else 'no'} | {issues} |")
    parts.append("")
    return "\n".join(parts)


def _assertions_section(state: RunState) -> str:
    if not state.assertion_suites:
        return "## Generated Assertions\n\n_No assertions generated._\n"
    parts = ["## Generated Assertions\n"]
    for suite in state.assertion_suites:
        parts.append(f"### {suite.module} ({len(suite.assertions)} assertion(s))\n")
        if suite.file_path:
            parts.append(f"Written to `{suite.file_path}`.\n")
        for a in suite.assertions:
            parts.append(f"- **{a.name}** ({a.kind}): {a.rationale}")
        parts.append("")
    return "\n".join(parts)


def _coverage_section(state: RunState) -> str:
    if not state.coverage_estimates:
        return "## Coverage Estimation\n\n_No coverage estimates available._\n"
    parts = ["## Coverage Estimation\n"]
    parts.append("| Module | FSM State Coverage | Branch Estimate | Toggle Opportunities |")
    parts.append("|---|---|---|---|")
    for c in state.coverage_estimates:
        fsm_pct = f"{c.fsm_state_coverage_pct:.1f}%" if c.fsm_state_coverage_pct is not None else "n/a"
        br_pct = f"{c.branch_estimate_pct:.1f}%" if c.branch_estimate_pct is not None else "n/a"
        parts.append(f"| {c.module} | {fsm_pct} | {br_pct} | {c.toggle_opportunities} |")
    parts.append("")
    for c in state.coverage_estimates:
        for note in c.uncovered_notes:
            parts.append(f"- **{c.module}**: {note}")
    parts.append("")
    return "\n".join(parts)


def render_markdown(state: RunState, kg: Optional[ProjectKnowledgeGraph] = None) -> str:
    """Full Design Quality Report: explainability report (unchanged) plus the
    new static/FSM/counter/assertion/coverage sections."""
    base = render_report(state, kg)
    sections = [
        base,
        "\n---\n",
        "# Design Quality Report\n",
        _static_analysis_section(state),
        _fsm_section(state),
        _counter_section(state),
        _assertions_section(state),
        _coverage_section(state),
    ]
    return "\n".join(sections)


def render_html(markdown_text: str) -> str:
    return _HTML_TEMPLATE.format(body=_md_to_html_fragment(markdown_text))


def write_reports(state: RunState, kg: Optional[ProjectKnowledgeGraph], out_dir: str) -> dict[str, str]:
    """Writes ``report.md`` and ``report.html`` under ``out_dir`` and returns
    their paths (also stashed on ``state.report_paths``)."""
    os.makedirs(out_dir, exist_ok=True)
    markdown_text = render_markdown(state, kg)
    md_path = os.path.join(out_dir, "report.md")
    html_path = os.path.join(out_dir, "report.html")
    with open(md_path, "w") as f:
        f.write(markdown_text)
    with open(html_path, "w") as f:
        f.write(render_html(markdown_text))
    paths = {"markdown": md_path, "html": html_path}
    state.report_paths.update(paths)
    return paths
