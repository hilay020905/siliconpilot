"""
Dependency Graph Agent.
Builds the module instantiation graph with NetworkX (production system persists this
into Neo4j - see design doc §6). Also exposes blast-radius queries used by
Bug Prioritization.
"""
from __future__ import annotations
import networkx as nx
from core.schemas import ModuleAST, DependencyGraphSummary, DependencyEdge


def build_graph(modules: list[ModuleAST]) -> nx.DiGraph:
    g = nx.DiGraph()
    known = {m.name for m in modules}
    for m in modules:
        g.add_node(m.name, file=m.file, n_ports=len(m.ports), n_regs=len(m.registers))
        for inst in m.instances:
            if inst in known:
                g.add_edge(m.name, inst, kind="instantiates")
    return g


def summarize(g: nx.DiGraph) -> DependencyGraphSummary:
    edges = [DependencyEdge(src=u, dst=v, kind=d.get("kind", "instantiates"))
             for u, v, d in g.edges(data=True)]
    return DependencyGraphSummary(modules=list(g.nodes()), edges=edges)


def blast_radius(g: nx.DiGraph, module_name: str) -> int:
    """How many modules (upstream instantiators) would be affected by changing this module."""
    if module_name not in g:
        return 0
    return len(nx.ancestors(g, module_name))
