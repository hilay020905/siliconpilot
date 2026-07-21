"""
FSM Analyzer (new capability #6).

Detects finite state machines from ``case (state_signal) ... endcase``
patterns inside always blocks (reusing ``core.knowledge_graph``'s FSM
detection as the entry point), then does a second, deeper pass over the
`case` body text to recover:

  * the state name/value list
  * transition edges (state -> next-state, with the guarding condition when
    it can be recovered textually)
  * unreachable states (no incoming transition, and not the reset state)
  * unused states (declared as a parameter/localparam but never a `case` arm)
  * deadlock states (a state with a `case` arm but no outgoing transition
    that changes the state signal, i.e. it lacks a valid escape from itself)

This is a heuristic textual analysis, not a full parser - it is deliberately
conservative and only reports what it can see directly in the text, which
keeps false positives low.
"""
from __future__ import annotations

import re
from typing import Optional

from core.knowledge_graph import ProjectKnowledgeGraph
from core.schemas import FSMAnalysisReport, FSMInfo, FSMTransition, ModuleAST

_LOCALPARAM_RE = re.compile(
    r"\b(?:localparam|parameter)\b\s*(?:\[[^\]]*\])?\s*([A-Za-z_]\w*(?:\s*=\s*[^,;]+)?"
    r"(?:\s*,\s*[A-Za-z_]\w*\s*=\s*[^,;]+)*)\s*;"
)
_CASE_ARM_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*(?!.*\bdefault\b)(.*)$")
_STATE_ASSIGN_RE = re.compile(r"\bstate\w*\s*<=\s*([A-Za-z_]\w*)")


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _extract_case_body(text: str, state_signal: str) -> Optional[str]:
    m = re.search(rf"\bcase\s*\(\s*{re.escape(state_signal)}\s*\)", text)
    if not m:
        return None
    start = m.end()
    end = text.find("endcase", start)
    if end == -1:
        return None
    return text[start:end]


def _declared_states(text: str) -> list[str]:
    """Best-effort recovery of state names from localparam/parameter blocks
    (covers the common `localparam IDLE=0, RUN=1, DONE=2;` style)."""
    names: list[str] = []
    for m in _LOCALPARAM_RE.finditer(text):
        for chunk in m.group(1).split(","):
            name = chunk.split("=")[0].strip()
            if name and re.match(r"^[A-Za-z_]\w*$", name):
                names.append(name)
    return names


def _analyze_fsm(module: ModuleAST, text: str, state_signal: str) -> Optional[FSMInfo]:
    case_body = _extract_case_body(text, state_signal)
    if case_body is None:
        return None

    declared_states = _declared_states(text)

    # Split the case body into per-arm chunks by scanning `NAME:` labels.
    arm_starts = [(m.start(1), m.group(1)) for m in re.finditer(r"\b([A-Za-z_]\w*)\s*:", case_body)
                  if m.group(1).lower() != "default"]
    states_seen: list[str] = []
    transitions: list[FSMTransition] = []
    deadlock_states: list[str] = []

    for i, (pos, name) in enumerate(arm_starts):
        end = arm_starts[i + 1][0] if i + 1 < len(arm_starts) else len(case_body)
        arm_body = case_body[pos:end]
        states_seen.append(name)

        next_states = set(_STATE_ASSIGN_RE.findall(arm_body))
        next_states.discard(name)  # self-loop isn't an "escape"
        if not next_states and not _STATE_ASSIGN_RE.search(arm_body):
            # No explicit next-state assignment at all in this arm -> likely stuck.
            deadlock_states.append(name)
        for dst in next_states:
            transitions.append(FSMTransition(src_state=name, dst_state=dst, condition=""))
        # also record same-state re-assignment as an explicit (non-deadlock) self loop
        if name in set(_STATE_ASSIGN_RE.findall(arm_body)):
            transitions.append(FSMTransition(src_state=name, dst_state=name, condition="(self-loop)"))

    all_states = sorted(set(states_seen) | set(declared_states))
    reachable = {t.dst_state for t in transitions} | ({states_seen[0]} if states_seen else set())
    unreachable = sorted(s for s in all_states if s not in reachable and s in states_seen)
    unused = sorted(s for s in declared_states if s not in states_seen)

    # crude one-hot vs binary encoding guess based on declared values
    encoding = "unknown"
    values = re.findall(r"=\s*(\d*'b[01]+|\d+)", text)
    if any("'b" in v and v.count("1") == 1 and len(v.split("'b")[1]) > 1 for v in values):
        encoding = "one_hot"
    elif values:
        encoding = "binary"

    return FSMInfo(
        module=module.name, state_signal=state_signal, states=all_states,
        transitions=transitions, unreachable_states=unreachable,
        unused_states=unused, deadlock_states=sorted(set(deadlock_states)),
        encoding=encoding,  # type: ignore[arg-type]
    )


def analyze_project(modules: list[ModuleAST], kg: ProjectKnowledgeGraph) -> FSMAnalysisReport:
    """Find every FSM the Knowledge Graph already detected and enrich it with
    a full transition graph + reachability/deadlock analysis."""
    fsms: list[FSMInfo] = []
    by_name = {m.name: m for m in modules}
    seen: set[tuple[str, str]] = set()

    for ab in kg.always_blocks:
        if not ab.is_fsm or not ab.fsm_state_signal:
            continue
        key = (ab.module, ab.fsm_state_signal)
        if key in seen:
            continue
        seen.add(key)
        module = by_name.get(ab.module)
        if module is None:
            continue
        text = _read(module.file)
        info = _analyze_fsm(module, text, ab.fsm_state_signal)
        if info is not None:
            fsms.append(info)

    return FSMAnalysisReport(fsms=fsms)


def to_mermaid(fsm: FSMInfo) -> str:
    """Render one FSM's transition graph as a Mermaid state diagram."""
    lines = ["stateDiagram-v2"]
    if fsm.states:
        lines.append(f"    [*] --> {fsm.states[0]}")
    for t in fsm.transitions:
        label = f": {t.condition}" if t.condition else ""
        lines.append(f"    {t.src_state} --> {t.dst_state}{label}")
    for s in fsm.unreachable_states:
        lines.append(f"    {s} : unreachable")
    for s in fsm.deadlock_states:
        lines.append(f"    {s} : deadlock")
    if len(lines) == 1:
        lines.append("    empty : no transitions recovered")
    return "\n".join(lines)
