"""
Project Knowledge Graph Agent (Priority Feature #2).

Builds a single NetworkX ``MultiDiGraph`` that represents the RTL project at a
semantic level - not just "which module instantiates which" (that's still
available via ``agents.dependency_graph`` and is folded in here too), but the
full node/edge vocabulary requested by the architecture upgrade:

Nodes: Module, Signal, Register, Wire, FSM, ClockDomain, AlwaysBlock,
       PipelineStage, Input, Output, Submodule
Edges: contains, drives, reads, writes, instantiates, depends_on,
       clocked_by, reset_by

Every other agent (root cause engine, patch generator, planner) should query
this graph via ``ProjectKnowledgeGraph`` instead of re-parsing RTL text, which
keeps parsing logic in exactly one place (``agents.rtl_parser``) while making
graph queries (blast radius, "what clocks this register", "what always block
drives this signal") a single, reusable API.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import networkx as nx

from core.schemas import (
    KGEdge,
    KGNode,
    KnowledgeGraphSummary,
    ModuleAST,
)

logger = logging.getLogger(__name__)

# Matches an always block header and captures its full sensitivity list, e.g.
# "always @(posedge clk or posedge rst)" or "always @(posedge clk)".
_ALWAYS_RE = re.compile(
    r"always(?:_ff|_comb)?\s*@?\s*\(?([^)]*)\)?\s*begin", re.IGNORECASE
)
_EDGE_SIG_RE = re.compile(r"(posedge|negedge)\s+(\w+)", re.IGNORECASE)
_NONBLOCKING_ASSIGN_RE = re.compile(r"(\w+)\s*<=")
_BLOCKING_ASSIGN_RE = re.compile(r"(\w+)\s*=[^=]")
_WIRE_RE = re.compile(r"\bwire\b\s*(?:\[[^\]]*\])?\s*(\w+)\s*;")
_ASSIGN_RE = re.compile(r"\bassign\s+(\w+)\s*=\s*([^;]+);")
_CASE_STATE_RE = re.compile(r"\bcase\s*\(\s*(\w+)\s*\)", re.IGNORECASE)


@dataclass
class AlwaysBlockInfo:
    """Lightweight structural summary of one always block, used both to build
    graph nodes/edges and as evidence for root-cause analysis."""
    module: str
    block_id: str
    sensitivity: str
    clock_signal: Optional[str]
    reset_signal: Optional[str]
    written_regs: list[str] = field(default_factory=list)
    read_signals: list[str] = field(default_factory=list)
    is_fsm: bool = False
    fsm_state_signal: Optional[str] = None


class ProjectKnowledgeGraph:
    """Wraps a ``networkx.MultiDiGraph`` and the query API agents use."""

    def __init__(self) -> None:
        self.graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self.always_blocks: list[AlwaysBlockInfo] = []

    # -- construction ---------------------------------------------------

    def build(self, modules: list[ModuleAST]) -> "ProjectKnowledgeGraph":
        """Populate the graph from parsed module ASTs plus a light re-scan of
        each module's source text for signals the structural AST doesn't
        capture in detail (wires, always-block bodies, FSM case statements)."""
        known_modules = {m.name for m in modules}
        self.graph.clear()
        self.always_blocks.clear()

        for module in modules:
            self._add_module(module, known_modules)

        logger.info(
            "Knowledge graph built: %d nodes, %d edges",
            self.graph.number_of_nodes(),
            self.graph.number_of_edges(),
        )
        return self

    def _add_module(self, module: ModuleAST, known_modules: set[str]) -> None:
        mod_id = f"module::{module.name}"
        self.graph.add_node(mod_id, kind="module", name=module.name, file=module.file)

        try:
            with open(module.file, "r") as f:
                text = f.read()
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not re-read %s for KG enrichment: %s", module.file, exc)
            text = ""

        # Ports -> Input/Output nodes, contained by the module.
        for port in module.ports:
            port_id = f"signal::{module.name}.{port.name}"
            kind = "input" if port.direction == "input" else (
                "output" if port.direction == "output" else "signal"
            )
            self.graph.add_node(port_id, kind=kind, name=port.name, width=port.width,
                                 module=module.name)
            self.graph.add_edge(mod_id, port_id, kind="contains")

        # Registers.
        for reg in module.registers:
            reg_id = f"register::{module.name}.{reg}"
            self.graph.add_node(reg_id, kind="register", name=reg, module=module.name)
            self.graph.add_edge(mod_id, reg_id, kind="contains")

        # Wires (regex re-scan; not captured in ModuleAST today).
        for wm in _WIRE_RE.finditer(text):
            wire_name = wm.group(1)
            wire_id = f"wire::{module.name}.{wire_name}"
            if not self.graph.has_node(wire_id):
                self.graph.add_node(wire_id, kind="wire", name=wire_name, module=module.name)
                self.graph.add_edge(mod_id, wire_id, kind="contains")

        # assign statements -> drives/reads edges on wires/outputs.
        for am in _ASSIGN_RE.finditer(text):
            dst, rhs = am.group(1), am.group(2)
            dst_id = self._resolve_signal_id(module.name, dst)
            if dst_id:
                self.graph.add_edge(mod_id, dst_id, kind="drives")
                for rd in re.findall(r"\b(\w+)\b", rhs):
                    src_id = self._resolve_signal_id(module.name, rd)
                    if src_id and src_id != dst_id:
                        self.graph.add_edge(dst_id, src_id, kind="reads")

        # Always blocks -> clock domains, FSMs, drives/reads edges.
        for idx, am in enumerate(_ALWAYS_RE.finditer(text)):
            sensitivity = am.group(1) or ""
            block_id = f"always::{module.name}.{idx}"
            self.graph.add_node(block_id, kind="always_block", module=module.name,
                                 sensitivity=sensitivity.strip())
            self.graph.add_edge(mod_id, block_id, kind="contains")

            edges = _EDGE_SIG_RE.findall(sensitivity)
            clock_signal = next((sig for edge, sig in edges if "clk" in sig.lower()), None)
            if clock_signal is None and edges:
                clock_signal = edges[0][1]
            reset_signal = next(
                (sig for edge, sig in edges if "rst" in sig.lower() or "reset" in sig.lower()),
                None,
            )

            if clock_signal:
                clk_dom_id = f"clock_domain::{clock_signal}"
                if not self.graph.has_node(clk_dom_id):
                    self.graph.add_node(clk_dom_id, kind="clock_domain", name=clock_signal)
                self.graph.add_edge(block_id, clk_dom_id, kind="clocked_by")

            if reset_signal:
                reset_id = self._resolve_signal_id(module.name, reset_signal)
                if reset_id:
                    self.graph.add_edge(block_id, reset_id, kind="reset_by")

            # Body slice: from this match to the next always/endmodule, best-effort.
            body_start = am.end()
            next_match = text.find("always", body_start)
            end_idx = text.find("endmodule", body_start)
            body_end = min([x for x in (next_match, end_idx) if x != -1] or [len(text)])
            body = text[body_start:body_end]

            written = sorted(set(_NONBLOCKING_ASSIGN_RE.findall(body)
                                  + _BLOCKING_ASSIGN_RE.findall(body)))
            for w in written:
                w_id = self._resolve_signal_id(module.name, w)
                if w_id:
                    self.graph.add_edge(block_id, w_id, kind="writes")

            state_match = _CASE_STATE_RE.search(body)
            is_fsm = state_match is not None and len(written) >= 1
            fsm_state_signal = state_match.group(1) if state_match else None
            if is_fsm:
                fsm_id = f"fsm::{module.name}.{idx}"
                self.graph.add_node(fsm_id, kind="fsm", module=module.name,
                                     state_signal=fsm_state_signal)
                self.graph.add_edge(mod_id, fsm_id, kind="contains")
                self.graph.add_edge(block_id, fsm_id, kind="depends_on")

            self.always_blocks.append(AlwaysBlockInfo(
                module=module.name, block_id=block_id, sensitivity=sensitivity.strip(),
                clock_signal=clock_signal, reset_signal=reset_signal,
                written_regs=written, read_signals=sorted(set(re.findall(r"\b(\w+)\b", body))),
                is_fsm=is_fsm, fsm_state_signal=fsm_state_signal,
            ))

        # Submodule instantiation edges.
        for inst in module.instances:
            if inst in known_modules:
                inst_id = f"module::{inst}"
                self.graph.add_edge(mod_id, inst_id, kind="instantiates")
                self.graph.add_edge(mod_id, inst_id, kind="depends_on")

    def _resolve_signal_id(self, module: str, name: str) -> Optional[str]:
        for prefix in ("register", "wire", "input", "output", "signal"):
            candidate = f"{prefix}::{module}.{name}"
            if self.graph.has_node(candidate):
                return candidate
        return None

    # -- queries ----------------------------------------------------------

    def find_register(self, module: str, name: str) -> Optional[str]:
        node_id = f"register::{module}.{name}"
        return node_id if self.graph.has_node(node_id) else None

    def always_blocks_for(self, module: str) -> list[AlwaysBlockInfo]:
        return [a for a in self.always_blocks if a.module == module]

    def always_block_writing(self, module: str, reg_name: str) -> Optional[AlwaysBlockInfo]:
        for a in self.always_blocks_for(module):
            if reg_name in a.written_regs:
                return a
        return None

    def clock_domains(self) -> list[str]:
        return [n for n, d in self.graph.nodes(data=True) if d.get("kind") == "clock_domain"]

    def fsms_in(self, module: str) -> list[str]:
        return [n for n, d in self.graph.nodes(data=True)
                if d.get("kind") == "fsm" and d.get("module") == module]

    def blast_radius(self, module_name: str) -> int:
        """Number of modules that transitively instantiate/depend on this one."""
        node_id = f"module::{module_name}"
        if not self.graph.has_node(node_id):
            return 0
        depends_on_graph = nx.DiGraph()
        for u, v, d in self.graph.edges(data=True):
            if d.get("kind") in ("instantiates", "depends_on"):
                depends_on_graph.add_edge(u, v)
        if node_id not in depends_on_graph:
            return 0
        return len(nx.ancestors(depends_on_graph, node_id))

    def registers_without_reset(self, module: str) -> list[str]:
        """Registers in `module` written by an always block that has a clock
        edge but no matched reset edge - the graph-native version of the old
        regex-only heuristic in patch_qa."""
        out = []
        for a in self.always_blocks_for(module):
            if a.clock_signal and not a.reset_signal:
                out.extend(a.written_regs)
        return sorted(set(out))

    def summarize(self) -> KnowledgeGraphSummary:
        nodes = [
            KGNode(node_id=n, kind=d.get("kind", "signal"),
                   attrs={k: v for k, v in d.items() if k != "kind"})
            for n, d in self.graph.nodes(data=True)
        ]
        edges = [
            KGEdge(src=u, dst=v, kind=d.get("kind", "depends_on"),
                   attrs={k: v for k, v in d.items() if k != "kind"})
            for u, v, d in self.graph.edges(data=True)
        ]
        return KnowledgeGraphSummary(nodes=nodes, edges=edges)

    # -- visualization ------------------------------------------------------

    def to_mermaid(self, max_edges: int = 200) -> str:
        """Renders a Mermaid ``graph TD`` diagram of the knowledge graph. Capped
        at `max_edges` so large designs stay renderable."""
        lines = ["graph TD"]
        seen_nodes: set[str] = set()

        def safe_id(node_id: str) -> str:
            return re.sub(r"[^a-zA-Z0-9_]", "_", node_id)

        def label(node_id: str, data: dict[str, Any]) -> str:
            kind = data.get("kind", "node")
            name = data.get("name") or node_id.split("::")[-1]
            return f'{safe_id(node_id)}["{kind}: {name}"]'

        count = 0
        for u, v, d in self.graph.edges(data=True):
            if count >= max_edges:
                lines.append("    %% ... truncated ...")
                break
            if u not in seen_nodes:
                lines.append(f"    {label(u, self.graph.nodes[u])}")
                seen_nodes.add(u)
            if v not in seen_nodes:
                lines.append(f"    {label(v, self.graph.nodes[v])}")
                seen_nodes.add(v)
            lines.append(f"    {safe_id(u)} -->|{d.get('kind', '')}| {safe_id(v)}")
            count += 1
        return "\n".join(lines)
