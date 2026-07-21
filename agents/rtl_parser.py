"""
RTL Parser Agent.

Production SiliconPilot uses Tree-sitter's Verilog/SV grammar (see design doc §8/§12).
This is a dependency-free structural parser covering the common subset needed for the
demo/eval harness: module headers, port directions, always-block registers, and
sub-module instantiations. It's intentionally regex/state-machine based rather than a
full parser so the reference implementation has zero external grammar dependency.
"""
from __future__ import annotations
import re
from core.schemas import ModuleAST, Port

_MODULE_RE = re.compile(r"\bmodule\s+(\w+)\s*(?:#\s*\([^;]*?\))?\s*\(", re.DOTALL)
_PORT_LINE_RE = re.compile(
    r"\b(input|output|inout)\b\s*(?:reg|wire|logic)?\s*(?:\[\s*([\w:\-\+]+)\s*:\s*([\w:\-\+]+)\s*\])?\s*(\w+)"
)
_REG_RE = re.compile(r"\b(?:reg|logic)\b\s*(?:\[[^\]]*\])?\s*(\w+)\s*;")
_INSTANCE_RE = re.compile(r"^\s*(\w+)\s+(?:#\s*\([^;]*?\))?\s*(\w+)\s*\(", re.MULTILINE)

# module/interface type keywords to exclude from instance detection false-positives
_KEYWORDS = {"if", "else", "for", "while", "case", "begin", "end", "assign", "always",
             "always_ff", "always_comb", "initial", "function", "task", "module",
             "endmodule", "input", "output", "inout", "wire", "reg", "logic", "parameter"}


def parse_file(path: str) -> list[ModuleAST]:
    with open(path, "r") as f:
        text = f.read()

    modules: list[ModuleAST] = []
    for m in _MODULE_RE.finditer(text):
        name = m.group(1)
        # grab the port list block up to the closing ) of the header, then the body
        header_start = m.end()
        depth = 1
        idx = header_start
        while idx < len(text) and depth > 0:
            if text[idx] == "(":
                depth += 1
            elif text[idx] == ")":
                depth -= 1
            idx += 1
        port_block = text[header_start:idx]

        # find body: from idx to matching endmodule
        end_idx = text.find("endmodule", idx)
        body = text[idx:end_idx] if end_idx != -1 else text[idx:]

        ports = []
        for pm in _PORT_LINE_RE.finditer(port_block + "\n" + body[:400]):
            direction, hi, lo, pname = pm.groups()
            width = 1
            if hi is not None and lo is not None:
                try:
                    width = abs(int(hi) - int(lo)) + 1
                except ValueError:
                    width = 1
            ports.append(Port(name=pname, direction=direction, width=width))

        registers = [rm.group(1) for rm in _REG_RE.finditer(body)]

        instances = []
        for im in _INSTANCE_RE.finditer(body):
            type_name, inst_name = im.groups()
            if type_name not in _KEYWORDS:
                instances.append(type_name)

        modules.append(ModuleAST(
            name=name, file=path, ports=ports,
            instances=instances, registers=registers,
        ))
    return modules


def parse_project(rtl_files: list[str]) -> list[ModuleAST]:
    out: list[ModuleAST] = []
    for f in rtl_files:
        out.extend(parse_file(f))
    return out
