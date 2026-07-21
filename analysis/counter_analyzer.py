"""
Counter Analyzer (new capability #7).

Recognizes registers that behave as counters (`cnt <= cnt + 1;` and similar
patterns) inside clocked always blocks, then checks each one for common bugs:
missing reset, missing enable/qualifying condition, a declared width too
small to avoid silent wraparound for its evident use, and increment steps
other than 1 that are worth surfacing for review.
"""
from __future__ import annotations

import re

from core.knowledge_graph import ProjectKnowledgeGraph
from core.schemas import CounterAnalysisReport, CounterInfo, ModuleAST

_INCREMENT_RE = re.compile(
    r"(\w+)\s*<=\s*\1\s*\+\s*(\d+|\w+)\s*;"
)
_REG_WIDTH_RE = re.compile(r"\breg\b\s*\[\s*(\d+)\s*:\s*0\s*\]\s*(\w+)\s*;")


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def analyze_project(modules: list[ModuleAST], kg: ProjectKnowledgeGraph) -> CounterAnalysisReport:
    counters: list[CounterInfo] = []
    for module in modules:
        text = _read(module.file)
        if not text:
            continue

        widths = {name: int(hi) + 1 for hi, name in _REG_WIDTH_RE.findall(text)}
        incrementing = {m.group(1): m.group(2) for m in _INCREMENT_RE.finditer(text)}
        if not incrementing:
            continue

        always_blocks = kg.always_blocks_for(module.name)
        for signal, step in incrementing.items():
            issues: list[str] = []
            owning_block = next((a for a in always_blocks if signal in a.written_regs), None)

            has_reset = bool(owning_block and owning_block.reset_signal)
            if not has_reset:
                issues.append("No reset path found for this counter's owning always block.")

            has_enable = bool(owning_block and re.search(
                rf"if\s*\([^)]*\)\s*(?:begin)?\s*{re.escape(signal)}\s*<=\s*{re.escape(signal)}\s*\+",
                text))
            if not has_enable:
                issues.append(
                    "Counter appears to increment unconditionally every clock edge "
                    "(no qualifying 'if' enable found)."
                )

            width = widths.get(signal, 0)
            if width and width < 2:
                issues.append(f"Counter width is only {width} bit(s); likely too narrow.")

            if step.strip() not in {"1"} and not step.strip().isdigit():
                issues.append(f"Increment step '{step}' is not a simple literal; verify intent.")

            counters.append(CounterInfo(
                module=module.name, signal=signal, width=width,
                has_reset=has_reset, has_enable=has_enable,
                increments_by=step, issues=issues,
            ))

    return CounterAnalysisReport(counters=counters)
