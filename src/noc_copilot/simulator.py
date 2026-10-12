"""Fault-injection simulator. Each scenario is a 2-hour window of alarms, KPIs and change-log entries
with one injected root cause (the ground truth) plus realistic noise.

Realism knobs that make the problem non-trivial:
- Noise alarms (door open, high temperature, a cleared link flap) on unrelated sites.
- An unrelated configuration change in the change log (a red herring).
- `alarm_loss`: the primary alarm for the fault never arrived (telemetry gap), so the
  root cause has to be inferred from secondary symptoms and KPIs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from noc_copilot.topology import CORE_ID, Topology, build_topology

FAULT_TYPES = ["FIBER_CUT", "POWER_OUTAGE", "CORE_CONGESTION", "CONFIG_CHANGE", "RADIO_INTERFERENCE"]

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
BUCKET_MIN = 5
N_BUCKETS = 24  # 2 hours of 5-minute KPI samples
FAULT_BUCKET = 12  # fault starts at 11:00 UTC

CELL_KPIS = {
    # name: (baseline mean, relative noise)
    "availability_pct": (99.9, 0.0005),
    "drop_call_rate_pct": (0.5, 0.08),
    "dl_throughput_mbps": (150.0, 0.05),
    "ul_interference_dbm": (-110.0, 0.01),
    "prb_util_pct": (45.0, 0.06),
}
CORE_KPIS = {
    "latency_ms": (12.0, 0.05),
    "packet_loss_pct": (0.05, 0.1),
    "cpu_pct": (40.0, 0.05),
}


@dataclass
class Scenario:
    seed: int
    fault_type: str
    root_cause: str
    alarm_loss: bool
    alarms: list[dict]
    kpis: dict[str, dict[str, list[float]]]  # entity -> kpi -> series (cells and core)
    change_log: list[dict]
    topology: Topology = field(repr=False)

    @property
    def ground_truth(self) -> dict:
        return {"fault_type": self.fault_type, "root_cause": self.root_cause, "alarm_loss": self.alarm_loss}


def _ts(bucket: int, minute_offset: int = 0) -> str:
    return (T0 + timedelta(minutes=bucket * BUCKET_MIN + minute_offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _series(rng: random.Random, mean: float, noise: float) -> list[float]:
    return [round(mean + rng.gauss(0, abs(mean) * noise), 3) for _ in range(N_BUCKETS)]


def _apply(series: list[float], start: int, fn) -> None:
    for i in range(start, N_BUCKETS):
        series[i] = round(fn(series[i]), 3)


class _Builder:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.topo = build_topology()
        self.alarms: list[dict] = []
        self.kpis: dict[str, dict[str, list[float]]] = {}
        self.change_log: list[dict] = []
        for cell in self.topo.of_kind("cell"):
            self.kpis[cell.id] = {k: _series(self.rng, m, n) for k, (m, n) in CELL_KPIS.items()}
            self.kpis[cell.id]["availability_pct"] = [min(100.0, v) for v in self.kpis[cell.id]["availability_pct"]]
        self.kpis[CORE_ID] = {k: _series(self.rng, m, n) for k, (m, n) in CORE_KPIS.items()}

    def alarm(self, entity: str, type_: str, severity: str, text: str, bucket: int, minute: int = 0,
              status: str = "ACTIVE") -> None:
        self.alarms.append({
            "id": f"ALM-{len(self.alarms) + 1:04d}",
            "time": _ts(bucket, minute),
            "entity": entity,
            "type": type_,
            "severity": severity,
            "status": status,
            "text": text,
        })

    def cells_down(self, cells: list[str], bucket: int) -> None:
        for c in cells:
            _apply(self.kpis[c]["availability_pct"], bucket, lambda v: 0.0)
            _apply(self.kpis[c]["dl_throughput_mbps"], bucket, lambda v: 0.0)
            _apply(self.kpis[c]["prb_util_pct"], bucket, lambda v: 0.0)


def generate_scenario(seed: int, fault_type: str | None = None, alarm_loss: bool | None = None) -> Scenario:
    b = _Builder(seed)
    rng, topo = b.rng, b.topo
    fault_type = fault_type or rng.choice(FAULT_TYPES)
    alarm_loss = (rng.random() < 0.35) if alarm_loss is None else alarm_loss
    sites = [n.id for n in topo.of_kind("site")]
    F = FAULT_BUCKET

    if fault_type == "FIBER_CUT":
        agg = rng.choice([n.id for n in topo.of_kind("agg")])
        root = agg
        if not alarm_loss:
            b.alarm(agg, "LINK_DOWN", "CRITICAL", "Uplink ge-0/0/1 to core down (loss of signal)", F)
        for site in topo.descendants(agg, "site"):
            b.alarm(site, "NG_LINK_FAILURE", "MAJOR", "NG interface to core unreachable", F, 1)
            for cell in topo.descendants(site, "cell"):
                b.alarm(cell, "CELL_OUT_OF_SERVICE", "CRITICAL", "Cell out of service", F, 2)
        b.cells_down(topo.descendants(agg, "cell"), F)

    elif fault_type == "POWER_OUTAGE":
        site = rng.choice(sites)
        root = site
        if not alarm_loss:
            b.alarm(site, "MAINS_POWER_FAILURE", "MAJOR", "AC mains failure, running on battery", F - 7)
        b.alarm(site, "BATTERY_LOW", "MAJOR", "Battery voltage below 46V", F - 2)
        b.alarm(site, "NG_LINK_FAILURE", "MAJOR", "NG interface to core unreachable", F, 1)
        for cell in topo.descendants(site, "cell"):
            b.alarm(cell, "CELL_OUT_OF_SERVICE", "CRITICAL", "Cell out of service", F, 1)
        b.cells_down(topo.descendants(site, "cell"), F)

    elif fault_type == "CORE_CONGESTION":
        root = CORE_ID
        if not alarm_loss:
            b.alarm(CORE_ID, "UPF_CPU_HIGH", "MAJOR", "User-plane function CPU above 95%", F, 2)
        b.alarm(CORE_ID, "GTPU_PATH_DEGRADED", "MINOR", "GTP-U echo latency above threshold", F, 4)
        core = b.kpis[CORE_ID]
        _apply(core["latency_ms"], F, lambda v: v * 7)
        _apply(core["packet_loss_pct"], F, lambda v: v * 45)
        _apply(core["cpu_pct"], F, lambda v: min(99.0, v * 2.4))
        for cell in topo.of_kind("cell"):
            _apply(b.kpis[cell.id]["dl_throughput_mbps"], F, lambda v: v * 0.6)

    elif fault_type == "CONFIG_CHANGE":
        site = rng.choice(sites)
        root = site
        b.change_log.append({
            "time": _ts(F, -3), "entity": site, "author": "son-automation",
            "change": "Mobility parameter push: A3 handover offset 3 dB -> 12 dB on all cells",
        })
        for cell in topo.descendants(site, "cell"):
            _apply(b.kpis[cell]["drop_call_rate_pct"], F, lambda v: v * 9)
            if not alarm_loss:
                b.alarm(cell, "HANDOVER_FAILURE_RATE_HIGH", "MINOR", "Outgoing handover failure rate above 5%", F + 3)

    elif fault_type == "RADIO_INTERFERENCE":
        site = rng.choice(sites)
        cell = rng.choice(topo.descendants(site, "cell"))
        root = cell
        if not alarm_loss:
            b.alarm(cell, "UL_INTERFERENCE_HIGH", "MAJOR", "Uplink interference above -95 dBm", F + 2)
        k = b.kpis[cell]
        _apply(k["ul_interference_dbm"], F, lambda v: v + 22)
        _apply(k["dl_throughput_mbps"], F, lambda v: v * 0.55)
        _apply(k["drop_call_rate_pct"], F, lambda v: v * 3)

    else:
        raise ValueError(f"unknown fault type {fault_type!r}")

    _add_noise(b, root)
    b.alarms.sort(key=lambda a: a["time"])
    for i, a in enumerate(b.alarms, 1):
        a["id"] = f"ALM-{i:04d}"
    return Scenario(seed=seed, fault_type=fault_type, root_cause=root, alarm_loss=alarm_loss,
                    alarms=b.alarms, kpis=b.kpis, change_log=b.change_log, topology=topo)


def _add_noise(b: _Builder, root: str) -> None:
    rng, topo = b.rng, b.topo
    root_site = topo.site_of(root) if root in topo and topo.get(root).kind in ("site", "cell") else None
    quiet_sites = [n.id for n in topo.of_kind("site") if n.id != root_site and topo.agg_of(n.id) != root]
    noise_sites = rng.sample(quiet_sites, 3)
    b.alarm(noise_sites[0], "DOOR_OPEN", "WARNING", "Cabinet door open", rng.randint(2, 20))
    b.alarm(noise_sites[1], "HIGH_TEMPERATURE", "MINOR", "Cabinet temperature 41C", rng.randint(2, 20))
    b.alarm(noise_sites[2], "LINK_FLAP", "MINOR", "Microwave link flapped (auto-recovered)",
            rng.randint(2, 20), status="CLEARED")
    # Red-herring change on another site with no KPI impact.
    other = rng.choice([s for s in quiet_sites if s not in noise_sites])
    b.change_log.append({
        "time": _ts(rng.randint(4, 14)), "entity": other, "author": "j.weber",
        "change": "Neighbour list update: added 2 inter-frequency neighbours",
    })
    b.change_log.sort(key=lambda c: c["time"])
