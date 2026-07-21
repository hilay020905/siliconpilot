"""
Minimal VCD (Value Change Dump) parser - no external dependency, hand-rolled.
Extracts signal transitions and flags X-propagation, which the Waveform Analysis
Agent uses as evidence for Root Cause Analysis.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class VcdSignal:
    identifier: str
    name: str
    width: int
    transitions: list[tuple[int, str]] = field(default_factory=list)  # (time_ns, value)


@dataclass
class VcdFile:
    signals: dict[str, VcdSignal]  # keyed by vcd identifier char(s)
    end_time: int


def parse_vcd(path: str) -> VcdFile:
    signals: dict[str, VcdSignal] = {}
    name_stack: list[str] = []
    cur_time = 0
    end_time = 0

    with open(path, "r", errors="ignore") as f:
        lines = f.readlines()

    in_header = True
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue

        if in_header:
            if line.startswith("$scope"):
                parts = line.split()
                if len(parts) >= 3:
                    name_stack.append(parts[2])
                continue
            if line.startswith("$upscope"):
                if name_stack:
                    name_stack.pop()
                continue
            if line.startswith("$var"):
                # $var wire 1 ! sig_name $end
                parts = line.replace("$end", "").split()
                if len(parts) >= 5:
                    width = int(parts[2])
                    ident = parts[3]
                    sig_name = parts[4]
                    full_name = ".".join(name_stack + [sig_name]) if name_stack else sig_name
                    if ident not in signals:
                        signals[ident] = VcdSignal(identifier=ident, name=full_name, width=width)
                continue
            if line.startswith("$enddefinitions"):
                in_header = False
                continue
            continue

        # value change section
        if line.startswith("#"):
            cur_time = int(line[1:])
            end_time = max(end_time, cur_time)
            continue
        if line.startswith("$dumpvars") or line.startswith("$end"):
            continue
        if line[0] in "01xXzZ":
            val, ident = line[0], line[1:]
            if ident in signals:
                signals[ident].transitions.append((cur_time, val))
        elif line[0] == "b":
            # bus value: b0101 ident
            parts = line[1:].split()
            if len(parts) == 2:
                val, ident = parts
                if ident in signals:
                    signals[ident].transitions.append((cur_time, val))

    return VcdFile(signals=signals, end_time=end_time)


def find_x_propagation(vcd: VcdFile) -> list[dict]:
    """Return anomalies where a signal transitions to a value containing X/x."""
    anomalies = []
    for sig in vcd.signals.values():
        for t, val in sig.transitions:
            if "x" in val.lower():
                anomalies.append({
                    "signal": sig.name,
                    "time_ns": t,
                    "value": val,
                })
                break  # first occurrence is enough evidence
    return anomalies
