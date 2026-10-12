"""A small synthetic 5G network: one packet core, regional aggregation routers, cell sites and cells.

    CORE-FRA-UPF1 (Frankfurt user-plane core)
      └─ AGG-<city>-01   (aggregation router, one per city)
           └─ SITE-<city>-00N   (gNodeB cell site)
                └─ SITE-<city>-00N-C1..C3   (sector cells)
"""

from __future__ import annotations

from dataclasses import dataclass, field

CITIES = {"BER": "Berlin", "HAM": "Hamburg", "MUC": "Munich", "CGN": "Cologne"}
SITES_PER_CITY = 4
CELLS_PER_SITE = 3
CORE_ID = "CORE-FRA-UPF1"


@dataclass
class Node:
    id: str
    kind: str  # core | agg | site | cell
    city: str
    parent: str | None
    children: list[str] = field(default_factory=list)


class Topology:
    def __init__(self, nodes: dict[str, Node]):
        self.nodes = nodes

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.nodes

    def get(self, node_id: str) -> Node:
        return self.nodes[node_id]

    def of_kind(self, kind: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == kind]

    def path_to_core(self, node_id: str) -> list[str]:
        path, cur = [], self.nodes[node_id].parent
        while cur:
            path.append(cur)
            cur = self.nodes[cur].parent
        return path

    def descendants(self, node_id: str, kind: str | None = None) -> list[str]:
        out, stack = [], list(self.nodes[node_id].children)
        while stack:
            nid = stack.pop()
            out.append(nid)
            stack.extend(self.nodes[nid].children)
        out.sort()
        return [n for n in out if kind is None or self.nodes[n].kind == kind]

    def site_of(self, node_id: str) -> str | None:
        node = self.nodes[node_id]
        if node.kind == "site":
            return node.id
        if node.kind == "cell":
            return node.parent
        return None

    def agg_of(self, node_id: str) -> str | None:
        node = self.nodes[node_id]
        if node.kind == "agg":
            return node.id
        return next((p for p in self.path_to_core(node_id) if self.nodes[p].kind == "agg"), None)


def build_topology() -> Topology:
    nodes: dict[str, Node] = {CORE_ID: Node(CORE_ID, "core", "Frankfurt", None)}
    for code, city in CITIES.items():
        agg = f"AGG-{code}-01"
        nodes[agg] = Node(agg, "agg", city, CORE_ID)
        nodes[CORE_ID].children.append(agg)
        for s in range(1, SITES_PER_CITY + 1):
            site = f"SITE-{code}-{s:03d}"
            nodes[site] = Node(site, "site", city, agg)
            nodes[agg].children.append(site)
            for c in range(1, CELLS_PER_SITE + 1):
                cell = f"{site}-C{c}"
                nodes[cell] = Node(cell, "cell", city, site)
                nodes[site].children.append(cell)
    return Topology(nodes)
