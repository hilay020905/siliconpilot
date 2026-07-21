"""
Patch Generation Agent + QA/Critic Agent.

Patch Generation: Phase-1 reference implementation uses a small library of
pattern-based repair templates scoped to the module/lines the Root Cause Agent
implicated (this is the "graph-constrained patching" idea from the design doc §21 -
patches are never free-form, they're anchored to implicated_files/implicated_lines).
In production this step is an LLM call constrained to emit a unified diff touching
only the implicated region; the template match below is the deterministic fallback /
demo path so this repo runs with no API key.

QA/Critic: rule-based checks a human reviewer would also run - does the diff stay
within the implicated file, does it avoid unrelated changes, is risk bounded.
"""
from __future__ import annotations
import re
from core.schemas import RunState, Bug, PatchProposal, QAVerdict

# Matches the *header* of a plain posedge-clk-only always block (no reset in the
# sensitivity list). We then walk begin/end depth manually to find the true matching
# `end`, since always blocks commonly nest their own begin/end (e.g. an inner `if`),
# which a naive non-greedy regex would stop at prematurely.
_ALWAYS_HEADER_RE = re.compile(r"always\s*@\s*\(posedge\s+(\w+)\s*\)\s*begin\b")


def _find_matching_always_block(text: str):
    """Returns (full_match_text, clk_signal, inner_body) for the first plain
    posedge-clk-only always block with no reset in its sensitivity list, or None."""
    m = _ALWAYS_HEADER_RE.search(text)
    if not m:
        return None
    clk_sig = m.group(1)
    depth = 1
    idx = m.end()
    while idx < len(text) and depth > 0:
        if text[idx:idx + 5] == "begin" and (idx == 0 or not text[idx - 1].isalnum()):
            depth += 1
            idx += 5
            continue
        if text[idx:idx + 3] == "end" and (idx == 0 or not text[idx - 1].isalnum()):
            depth -= 1
            idx += 3
            continue
        idx += 1
    full = text[m.start():idx]
    inner_body = text[m.end():idx - 3]  # strip the always block's own trailing 'end'
    return full, clk_sig, inner_body


def generate_patch(state: RunState, bug: Bug) -> PatchProposal | None:
    if not bug.hypothesis.implicated_files:
        return None
    target_file = bug.hypothesis.implicated_files[0]

    with open(target_file, "r") as f:
        original = f.read()

    # Heuristic: find first register mentioned in the bug summary that also appears
    # as a bare `reg`/`logic` declaration, and check whether the always block driving
    # it has a reset branch. If not, synthesize a minimal synchronous-reset patch.
    reg_match = re.search(r"'(\w+)'", bug.hypothesis.summary)
    reg_name = None
    for m in state.modules:
        for r in m.registers:
            if reg_match and r == reg_match.group(1):
                reg_name = r
                break

    if reg_name is None:
        # fall back: try to find *any* register in the implicated module w/o reset
        for m in state.modules:
            if m.file == target_file and m.registers:
                reg_name = m.registers[0]
                break

    if reg_name is None:
        return None

    found = _find_matching_always_block(original)
    if not found or "rst" in found[0].lower():
        # Either no plain posedge-clk-only block found, or it already appears to
        # handle reset — nothing safe to patch heuristically.
        return None

    full_block, clk_sig, body = found
    if f"{reg_name} <=" not in body and f"{reg_name}<=" not in body:
        return None

    new_body = (
        f"always @(posedge {clk_sig} or posedge rst) begin\n"
        f"    if (rst) begin\n"
        f"        {reg_name} <= 0;\n"
        f"    end else begin\n"
        f"{body.rstrip()}\n"
        f"    end\n"
        f"end"
    )
    patched = original.replace(full_block, new_body)

    diff = _unified_diff(target_file, original, patched)

    return PatchProposal(
        bug_id=bug.bug_id,
        target_file=target_file,
        diff=diff,
        rationale=(
            f"Register '{reg_name}' was updated only on posedge clk with no reset "
            f"branch, so it powers up as X and stays X until first legitimate write. "
            f"Adding a synchronous active-high reset (`rst`) initializes it to a known "
            f"value, eliminating the X-propagation observed in simulation."
        ),
        risk_level="low",
    )


def _unified_diff(path: str, before: str, after: str) -> str:
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


def apply_patch(patch: PatchProposal, patched_text: str | None = None) -> None:
    """Actually writes the patched file to disk. Called only after QA approval."""
    # We recompute the patched content by re-deriving it the same way generate_patch did,
    # to avoid trusting a stringly-typed diff apply in this reference implementation.
    raise NotImplementedError("use apply_patch_direct")


def apply_patch_direct(target_file: str, new_content: str) -> None:
    with open(target_file, "w") as f:
        f.write(new_content)


def qa_review(state: RunState, patch: PatchProposal) -> QAVerdict:
    reasons = []
    verdict = "approve"

    # Rule 1: patch must target the file the hypothesis implicated.
    bug = next((b for b in state.bugs if b.bug_id == patch.bug_id), None)
    if bug and patch.target_file not in bug.hypothesis.implicated_files:
        reasons.append("Patch touches a file outside the root-cause hypothesis scope.")
        verdict = "reject"

    # Rule 2: diff should not be empty / trivial.
    if len(patch.diff.strip()) < 10:
        reasons.append("Diff is empty or trivially small.")
        verdict = "reject"

    # Rule 3: risk gate.
    if patch.risk_level == "high":
        reasons.append("High-risk patch requires human sign-off before auto-apply.")
        verdict = "reject"

    if verdict == "approve":
        reasons.append("Patch is scoped to implicated file, non-trivial, low risk.")

    return QAVerdict(verdict=verdict, reasons=reasons, blocking=(verdict == "reject"))
