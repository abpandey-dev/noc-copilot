"""Read-only investigation tools the agent can call. None of them change the network:
remediation is only ever *proposed* in the final RCA and needs a human to approve it.
"""

from __future__ import annotations

import json
import re
from importlib import resources

from noc_copilot import analytics
from noc_copilot.simulator import Scenario

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
        "description": "Scan every cell and the core for KPI anomalies and return them, largest change first. "
                       "Use this to spot problems that raised no alarm.",
        "strict": True,
        "input_schema": _schema({}, []),
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
        return analytics.find_anomalies(self.s)[:25]

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
