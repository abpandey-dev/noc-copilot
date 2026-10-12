"""Deterministic AIOps helpers: KPI anomaly detection and a rule-based alarm-correlation baseline.

The baseline mirrors a classic NOC rulebook - it reads alarms only. It is fast and cheap but brittle
when the primary alarm is missing, which is exactly where an investigating agent should do better.
"""

from __future__ import annotations

from statistics import mean, pstdev

from noc_copilot.models import RCA
from noc_copilot.simulator import Scenario
from noc_copilot.topology import CORE_ID

BASELINE_WINDOW = slice(0, 8)  # first 40 minutes = "normal"
RECENT_WINDOW = slice(-6, None)  # last 30 minutes


def kpi_summary(series: list[float]) -> dict:
    base, recent = series[BASELINE_WINDOW], series[RECENT_WINDOW]
    b_mean, r_mean = mean(base), mean(recent)
    spread = max(pstdev(base), abs(b_mean) * 0.01, 1e-6)
    z = (r_mean - b_mean) / spread
    change_pct = (r_mean - b_mean) / abs(b_mean) * 100 if b_mean else 0.0
    return {
        "baseline_mean": round(b_mean, 2),
        "recent_mean": round(r_mean, 2),
        "change_pct": round(change_pct, 1),
        "anomaly": abs(z) > 6 and abs(change_pct) > 15,
    }


def aggregate(series_list: list[list[float]]) -> list[float]:
    return [round(mean(vals), 3) for vals in zip(*series_list)]


def entity_kpis(s: Scenario, entity: str) -> dict[str, dict]:
    """KPI summaries for a cell, site (mean of its cells), agg (mean of its cells) or the core."""
    topo = s.topology
    if entity == CORE_ID or topo.get(entity).kind == "cell":
        raw = s.kpis[entity]
    else:
        cells = topo.descendants(entity, "cell")
        raw = {k: aggregate([s.kpis[c][k] for c in cells]) for k in s.kpis[cells[0]]}
    return {k: kpi_summary(v) for k, v in raw.items()}


def find_anomalies(s: Scenario) -> list[dict]:
    out = []
    for entity, kpis in s.kpis.items():
        for kpi, series in kpis.items():
            summ = kpi_summary(series)
            if summ["anomaly"]:
                out.append({"entity": entity, "kpi": kpi, **summ})
    out.sort(key=lambda r: -abs(r["change_pct"]))
    return out


def baseline_rca(s: Scenario) -> RCA:
    """Alarm-only correlation rules, in the order a NOC rulebook would apply them."""
    topo, alarms = s.topology, [a for a in s.alarms if a["status"] == "ACTIVE"]

    def first(type_: str) -> dict | None:
        return next((a for a in alarms if a["type"] == type_), None)

    def rca(fault: str, entity: str, why: str) -> RCA:
        return RCA(fault_type=fault, root_cause_entity=entity, confidence="medium", summary=why,
                   evidence=[why], affected_entities=[], recommended_actions=[], customer_impact="unknown")

    if a := first("LINK_DOWN"):
        return rca("FIBER_CUT", a["entity"], "Aggregation uplink LINK_DOWN alarm")
    if a := first("MAINS_POWER_FAILURE"):
        return rca("POWER_OUTAGE", a["entity"], "Mains power failure alarm at site")
    if first("UPF_CPU_HIGH"):
        return rca("CORE_CONGESTION", CORE_ID, "UPF CPU high alarm on core")
    if a := first("UL_INTERFERENCE_HIGH"):
        return rca("RADIO_INTERFERENCE", a["entity"], "Uplink interference alarm on cell")
    if a := first("HANDOVER_FAILURE_RATE_HIGH"):
        site = topo.site_of(a["entity"])
        if any(c["entity"] == site for c in s.change_log):
            return rca("CONFIG_CHANGE", site, "Handover failures on a site with a recent config change")
    # Fallback: many sites down under one aggregation router -> transport fault.
    down_sites = {a["entity"] for a in alarms if a["type"] == "NG_LINK_FAILURE"}
    by_agg: dict[str, int] = {}
    for site in down_sites:
        by_agg[topo.agg_of(site)] = by_agg.get(topo.agg_of(site), 0) + 1
    if by_agg and max(by_agg.values()) >= 3:
        agg = max(by_agg, key=by_agg.get)
        return rca("FIBER_CUT", agg, f"{by_agg[agg]} sites lost NG link under the same aggregation router")
    return rca("UNKNOWN", "", "No correlation rule matched")
