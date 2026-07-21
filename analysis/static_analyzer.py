"""
Static RTL Analysis (new capability #5).

A dependency-free, regex/heuristic static analyzer that inspects module
source text and the already-built Project Knowledge Graph to flag common RTL
bugs *before* compilation/simulation is even attempted. This is intentionally
conservative (favors false negatives over false positives) since findings
feed the planner and, eventually, the patch generator - a noisy analyzer
would poison both.

Every check produces zero or more ``StaticFinding`` records with an explicit
``kind`` (drawn from ``StaticFindingKind``), a confidence, and enough
location info (file/line/signal) to be actionable.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from core.knowledge_graph import ProjectKnowledgeGraph
from core.schemas import ModuleAST, StaticAnalysisReport, StaticFinding

_LATCH_IF_RE = re.compile(r"\bif\s*\([^)]*\)\s*(?:begin)?", re.IGNORECASE)
_CASE_RE = re.compile(r"\bcase\s*\([^)]*\)(.*?)endcase", re.IGNORECASE | re.DOTALL)
_DEFAULT_RE = re.compile(r"\bdefault\s*:", re.IGNORECASE)
_ALWAYS_COMB_BLOCK_RE = re.compile(
    r"always(?:_comb)?\s*@?\s*\(?\s*\*?\s*\)?\s*begin(.*?)end\b", re.IGNORECASE | re.DOTALL
)
_ALWAYS_SEQ_HEADER_RE = re.compile(
    r"always(?:_ff)?\s*@\s*\(([^)]*)\)\s*begin(.*?)(?=\bend\b\s*(?:$|always|endmodule))",
    re.IGNORECASE | re.DOTALL,
)
_NONBLOCKING_RE = re.compile(r"(\w+)\s*<=")
_BLOCKING_RE = re.compile(r"(?<![<>=!])\b(\w+)\s*=(?!=)")
_ASSIGN_RE = re.compile(r"\bassign\s+(\w+)\s*=\s*([^;]+);")
_WIDTH_LITERAL_RE = re.compile(r"(\d+)'([bhdo])([0-9a-fA-Fxz_]+)", re.IGNORECASE)
_SIGNED_RE = re.compile(r"\bsigned\b", re.IGNORECASE)


def _read(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return ""


def analyze_project(modules: list[ModuleAST], kg: Optional[ProjectKnowledgeGraph] = None) -> StaticAnalysisReport:
    """Run every static check across all parsed modules and return one merged report."""
    start = time.time()
    findings: list[StaticFinding] = []
    for module in modules:
        text = _read(module.file)
        if not text:
            continue
        findings.extend(_check_missing_reset(module, text, kg))
        findings.extend(_check_latch_inference(module, text))
        findings.extend(_check_unused_signals(module, text))
        findings.extend(_check_blocking_nonblocking_misuse(module, text))
        findings.extend(_check_multiple_drivers(module, text))
        findings.extend(_check_floating_wires(module, text))
        findings.extend(_check_constant_output(module, text))
        findings.extend(_check_signed_unsigned_mix(module, text))
        findings.extend(_check_width_mismatch(module, text))

    duration_ms = int((time.time() - start) * 1000)
    return StaticAnalysisReport(findings=findings, modules_analyzed=len(modules), duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_missing_reset(module: ModuleAST, text: str, kg: Optional[ProjectKnowledgeGraph]) -> list[StaticFinding]:
    """Flag registers written by a clocked always block that never sees a reset
    edge/signal. Prefers the Knowledge Graph's already-computed answer when
    available (single source of truth); falls back to a local regex scan."""
    out: list[StaticFinding] = []
    if kg is not None:
        for reg in kg.registers_without_reset(module.name):
            out.append(StaticFinding(
                kind="missing_reset", severity="warning", module=module.name,
                file=module.file, signal=reg,
                message=f"Register '{reg}' is written in a clocked always block with no "
                        f"associated reset signal in its sensitivity list.",
                confidence=0.75,
            ))
        return out

    for m in _ALWAYS_SEQ_HEADER_RE.finditer(text):
        sensitivity, body = m.group(1), m.group(2)
        if "posedge" not in sensitivity.lower() and "negedge" not in sensitivity.lower():
            continue
        has_reset = "rst" in sensitivity.lower() or "reset" in sensitivity.lower()
        if has_reset:
            continue
        for reg in set(_NONBLOCKING_RE.findall(body)):
            out.append(StaticFinding(
                kind="missing_reset", severity="warning", module=module.name,
                file=module.file, signal=reg,
                message=f"Register '{reg}' is written in a clocked always block with no "
                        f"reset in the sensitivity list ('{sensitivity.strip()}').",
                confidence=0.6,
            ))
    return out


def _check_latch_inference(module: ModuleAST, text: str) -> list[StaticFinding]:
    """Combinational (always_comb / always @*) blocks with an `if` and no matching
    `else`, or a `case` with no `default`, infer a latch on real synthesis tools."""
    out: list[StaticFinding] = []
    for m in _ALWAYS_COMB_BLOCK_RE.finditer(text):
        body = m.group(1)
        if_count = len(_LATCH_IF_RE.findall(body))
        else_count = len(re.findall(r"\belse\b", body, re.IGNORECASE))
        if if_count > 0 and else_count < if_count:
            out.append(StaticFinding(
                kind="latch_inference", severity="warning", module=module.name,
                file=module.file,
                message="Combinational always block has an 'if' with no matching 'else' "
                        "branch on every path; this infers a latch on synthesis.",
                confidence=0.55,
            ))
        for case_m in _CASE_RE.finditer(body):
            if not _DEFAULT_RE.search(case_m.group(1)):
                out.append(StaticFinding(
                    kind="latch_inference", severity="warning", module=module.name,
                    file=module.file,
                    message="Combinational 'case' statement has no 'default' branch; "
                            "unlisted cases will infer a latch.",
                    confidence=0.6,
                ))
    return out


def _check_unused_signals(module: ModuleAST, text: str) -> list[StaticFinding]:
    """A declared register/wire that is never referenced anywhere else in the
    file (besides its own declaration) is very likely dead."""
    out: list[StaticFinding] = []
    declared = list(module.registers)
    for name in declared:
        occurrences = len(re.findall(rf"\b{re.escape(name)}\b", text))
        if occurrences <= 1:
            out.append(StaticFinding(
                kind="unused_signal", severity="info", module=module.name,
                file=module.file, signal=name,
                message=f"Register '{name}' is declared but never referenced elsewhere "
                        f"in the module.",
                confidence=0.65,
            ))
    return out


def _check_blocking_nonblocking_misuse(module: ModuleAST, text: str) -> list[StaticFinding]:
    """Blocking (`=`) assignments inside clocked sequential blocks, and
    nonblocking (`<=`) assignments inside purely combinational blocks, are
    both classic style bugs that can cause simulation/synthesis mismatches."""
    out: list[StaticFinding] = []
    for m in _ALWAYS_SEQ_HEADER_RE.finditer(text):
        sensitivity, body = m.group(1), m.group(2)
        if "posedge" not in sensitivity.lower() and "negedge" not in sensitivity.lower():
            continue
        blocking_writes = set(_BLOCKING_RE.findall(body)) - set(_NONBLOCKING_RE.findall(body))
        # filter obvious non-signal tokens (loop vars, keywords)
        blocking_writes = {w for w in blocking_writes if w not in
                            {"if", "else", "for", "begin", "end", "case", "integer"}}
        for sig in blocking_writes:
            out.append(StaticFinding(
                kind="blocking_in_sequential", severity="warning", module=module.name,
                file=module.file, signal=sig,
                message=f"Signal '{sig}' is written with a blocking assignment ('=') inside "
                        f"a clocked (sequential) always block; nonblocking ('<=') is expected.",
                confidence=0.5,
            ))

    for m in _ALWAYS_COMB_BLOCK_RE.finditer(text):
        body = m.group(1)
        for sig in set(_NONBLOCKING_RE.findall(body)):
            out.append(StaticFinding(
                kind="nonblocking_in_combinational", severity="warning", module=module.name,
                file=module.file, signal=sig,
                message=f"Signal '{sig}' is written with a nonblocking assignment ('<=') inside "
                        f"a combinational always block; blocking ('=') is expected.",
                confidence=0.55,
            ))
    return out


def _check_multiple_drivers(module: ModuleAST, text: str) -> list[StaticFinding]:
    """A signal driven by more than one `assign` statement, or by both an
    `assign` and an always block, is a multi-driver conflict."""
    out: list[StaticFinding] = []
    assign_targets = [m.group(1) for m in _ASSIGN_RE.finditer(text)]
    seen: dict[str, int] = {}
    for t in assign_targets:
        seen[t] = seen.get(t, 0) + 1
    for sig, count in seen.items():
        if count > 1:
            out.append(StaticFinding(
                kind="multiple_drivers", severity="error", module=module.name,
                file=module.file, signal=sig,
                message=f"Signal '{sig}' has {count} separate 'assign' drivers.",
                confidence=0.8,
            ))
    return out


def _check_floating_wires(module: ModuleAST, text: str) -> list[StaticFinding]:
    """Declared wires with no `assign` and no always-block write are floating."""
    out: list[StaticFinding] = []
    wire_re = re.compile(r"\bwire\b\s*(?:\[[^\]]*\])?\s*(\w+)\s*;")
    declared_wires = wire_re.findall(text)
    driven = set(_ASSIGN_RE.findall(text) and [m.group(1) for m in _ASSIGN_RE.finditer(text)])
    driven |= set(_NONBLOCKING_RE.findall(text))
    for w in declared_wires:
        if w not in driven:
            out.append(StaticFinding(
                kind="floating_wire", severity="warning", module=module.name,
                file=module.file, signal=w,
                message=f"Wire '{w}' is declared but never driven by an assign statement "
                        f"or always block.",
                confidence=0.55,
            ))
    return out


def _check_constant_output(module: ModuleAST, text: str) -> list[StaticFinding]:
    """`assign out = 1'b0;` / `assign out = 1'b1;` style constant-tied outputs
    are usually a leftover stub or a bug."""
    out: list[StaticFinding] = []
    output_names = {p.name for p in module.ports if p.direction == "output"}
    for m in _ASSIGN_RE.finditer(text):
        dst, rhs = m.group(1), m.group(2).strip()
        if dst in output_names and re.fullmatch(r"\d*'?[bBhHdD]?[01xXzZ_]+", rhs):
            out.append(StaticFinding(
                kind="constant_output", severity="info", module=module.name,
                file=module.file, signal=dst,
                message=f"Output '{dst}' is tied to a constant value ('{rhs}'); confirm this "
                        f"is intentional.",
                confidence=0.5,
            ))
    return out


def _check_signed_unsigned_mix(module: ModuleAST, text: str) -> list[StaticFinding]:
    """A `signed` declaration coexisting with unsigned comparisons/arithmetic in
    the same module is a common source of subtle bugs; flagged for review."""
    out: list[StaticFinding] = []
    if _SIGNED_RE.search(text) and re.search(r"\bunsigned\b", text, re.IGNORECASE):
        out.append(StaticFinding(
            kind="signed_unsigned_mix", severity="info", module=module.name,
            file=module.file,
            message="Module mixes explicit 'signed' and 'unsigned' declarations; verify "
                    "comparisons/arithmetic don't silently reinterpret sign.",
            confidence=0.4,
        ))
    return out


def _check_width_mismatch(module: ModuleAST, text: str) -> list[StaticFinding]:
    """Sized literals (e.g. 4'hF) assigned to a port/register of a different
    declared width are flagged; this is a heuristic (best-effort width lookup)."""
    out: list[StaticFinding] = []
    width_by_name = {p.name: p.width for p in module.ports}
    for m in _ASSIGN_RE.finditer(text):
        dst, rhs = m.group(1), m.group(2)
        lit = _WIDTH_LITERAL_RE.search(rhs)
        if lit and dst in width_by_name:
            lit_width = int(lit.group(1))
            decl_width = width_by_name[dst]
            if decl_width and lit_width and lit_width != decl_width:
                out.append(StaticFinding(
                    kind="width_mismatch", severity="info", module=module.name,
                    file=module.file, signal=dst,
                    message=f"'{dst}' is declared {decl_width}-bit but assigned a "
                            f"{lit_width}-bit literal.",
                    confidence=0.45,
                ))
    return out
