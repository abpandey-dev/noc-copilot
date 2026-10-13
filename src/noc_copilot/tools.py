"""Read-only investigation tools the agent can call. None of them change the network:
remediation is only ever *proposed* in the final RCA and needs a human to approve it.
"""

from __future__ import annotations

import json
import re
from importlib import resources

from noc_copilot import analytics
from noc_copilot.simulator import BUCKET_MIN, CELL_KPIS, CORE_KPIS, Scenario, _ts

SEVERITY_RANK = {"WARNING": 0, "MINOR": 1, "MAJOR": 2, "CRITICAL": 3}


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOL_DEFINITIONS = [
    {
        "name": "list_alarms",
        "description": "List alarms in the incident window, oldest first, at or above a minimum severity. "
                       "Includes cleared alarms (status CLEARED).",
        "strict": True,
        "input_schema": _schema(
            {"min_severity": {"type": "string", "enum": list(SEVERITY_RANK)}}, ["min_severity"]),
    },
    {
        "name": "get_topology",
        "description": "Look up a network element (core, agg router, site or cell): its type, city, "
                       "upstream path to the core and direct children.",
        "strict": True,
        "input_schema": _schema({"entity_id": {"type": "string"}}, ["entity_id"]),
    },
    {
        "name": "get_kpis",
        "description": "KPI summary for an entity: baseline (first 40 min) vs recent (last 30 min) mean, "
                       "change % and an anomaly flag. Sites and agg routers are averaged over their cells. "
                       "Cell KPIs: availability, drop-call rate, DL throughput, UL interference, PRB utilisation. "
                       "Core KPIs: latency, packet loss, CPU.",
        "strict": True,
        "input_schema": _schema({"entity_id": {"type": "string"}}, ["entity_id"]),
    },
    {
        "name": "find_kpi_anomalies",
        "description": "Scan every cell and the core for KPI anomalies, grouped by site (or the core), largest "
                       "change first. Each group lists the affected cells and the % change range per KPI. "
                       "Use this to spot problems that raised no alarm.",
        "strict": True,
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_kpi_timeseries",
        "description": "Raw 5-minute samples of one KPI for an entity over the whole incident window (sites and "
                       "agg routers are averaged over their cells). A null value means no data was received. "
                       "Use it to see when a change started or to check a KPI the summary makes unclear.",
        "strict": True,
        "input_schema": _schema({"entity_id": {"type": "string"},
                                 "kpi": {"type": "string", "enum": [*CELL_KPIS, *CORE_KPIS]}},
                                ["entity_id", "kpi"]),
    },
    {
        "name": "get_change_log",
        "description": "Configuration changes made in the incident window (who, what, when, where).",
        "strict": True,
        "input_schema": _schema({}, []),
    },
    {
        "name": "search_runbooks",
        "description": "Search the NOC runbook library and return the best-matching runbook text.",
        "strict": True,
        "input_schema": _schema({"query": {"type": "string"}}, ["query"]),
    },
]


def _load_runbooks() -> dict[str, str]:
    folder = resources.files("noc_copilot") / "runbooks"
    return {p.name: p.read_text(encoding="utf-8") for p in folder.iterdir() if p.name.endswith(".md")}


class ToolError(Exception):
    pass


class NocTools:
    def __init__(self, scenario: Scenario):
        self.s = scenario
        self.runbooks = _load_runbooks()

    def call(self, name: str, args: dict) -> str:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            raise ToolError(f"Unknown tool {name!r}")
        return json.dumps(handler(**args), indent=1)

    def _entity(self, entity_id: str):
        if entity_id not in self.s.topology:
            raise ToolError(f"Unknown entity {entity_id!r}. IDs look like AGG-BER-01, SITE-BER-001, SITE-BER-001-C1.")
        return self.s.topology.get(entity_id)

    def _list_alarms(self, min_severity: str) -> list[dict]:
        floor = SEVERITY_RANK[min_severity]
        return [a for a in self.s.alarms if SEVERITY_RANK[a["severity"]] >= floor]

    def _get_topology(self, entity_id: str) -> dict:
        n = self._entity(entity_id)
        return {"id": n.id, "kind": n.kind, "city": n.city,
                "path_to_core": self.s.topology.path_to_core(n.id), "children": n.children}

    def _get_kpis(self, entity_id: str) -> dict:
        self._entity(entity_id)
        return {"entity": entity_id, "kpis": analytics.entity_kpis(self.s, entity_id)}

    def _find_kpi_anomalies(self) -> list[dict]:
        """Grouped by site (or the core) so one big outage can't crowd a second problem off the list."""
        groups: dict[str, dict] = {}
        for a in analytics.find_anomalies(self.s):
            key = self.s.topology.site_of(a["entity"]) or a["entity"]
            g = groups.setdefault(key, {"entity": key, "cells_affected": set(), "kpis": {}})
            if a["entity"] != key:
                g["cells_affected"].add(a["entity"])
            if a.get("no_recent_data"):
                g["kpis"][a["kpi"]] = {"no_recent_data": True}
                continue
            k = g["kpis"].setdefault(a["kpi"], {"min_change_pct": a["change_pct"], "max_change_pct": a["change_pct"]})
            if "no_recent_data" in k:
                continue
            k["min_change_pct"] = min(k["min_change_pct"], a["change_pct"])
            k["max_change_pct"] = max(k["max_change_pct"], a["change_pct"])
        out = [{**g, "cells_affected": sorted(g["cells_affected"])} for g in groups.values()]
        size = lambda v: 100.0 if "no_recent_data" in v else max(abs(v["min_change_pct"]), abs(v["max_change_pct"]))
        out.sort(key=lambda g: -max(size(v) for v in g["kpis"].values()))
        return out

    def _get_kpi_timeseries(self, entity_id: str, kpi: str) -> dict:
        node = self._entity(entity_id)
        source = self.s.kpis.get(node.id)
        if node.kind in ("site", "agg"):
            cells = self.s.topology.descendants(node.id, "cell")
            source = {k: analytics.aggregate([self.s.kpis[c][k] for c in cells]) for k in self.s.kpis[cells[0]]}
        if kpi not in source:
            raise ToolError(f"{entity_id} has no KPI {kpi!r}; available: {sorted(source)}")
        return {"entity": entity_id, "kpi": kpi, "interval_min": BUCKET_MIN,
                "samples": [{"time": _ts(i), "value": v} for i, v in enumerate(source[kpi])]}

    def _get_change_log(self) -> list[dict]:
        return self.s.change_log

    def _search_runbooks(self, query: str) -> dict:
        words = set(re.findall(r"[a-z0-9]+", query.lower()))
        scored = sorted(
            ((len(words & set(re.findall(r"[a-z0-9]+", text.lower()))), name, text)
             for name, text in self.runbooks.items()),
            reverse=True,
        )
        score, name, text = scored[0]
        if score == 0:
            return {"match": None, "available": sorted(self.runbooks)}
        return {"match": name, "text": text}
