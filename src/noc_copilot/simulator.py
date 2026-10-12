"""Fault-injection simulator. Each scenario is a 2-hour window of alarms, KPIs and change-log entries
with injected root cause(s) (the ground truth) plus realistic noise. `generate_scenario` builds the
standard v0.1 incidents; `generate_hard_scenario` builds the v0.2 ones (multi-fault, cascades, decoys).

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
    # Every independent root cause as (fault_type, entity). One entry, except MULTI_FAULT incidents.
    root_causes: list[tuple[str, str]] = field(default_factory=list)
    category: str = ""

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


def _inject(b: _Builder, fault_type: str, alarm_loss: bool, avoid_aggs: frozenset[str] = frozenset()) -> str:
    """Inject one fault and return its root-cause entity. `avoid_aggs` keeps a second fault in another region."""
    rng, topo = b.rng, b.topo
    sites = [n.id for n in topo.of_kind("site") if topo.agg_of(n.id) not in avoid_aggs]
    aggs = [n.id for n in topo.of_kind("agg") if n.id not in avoid_aggs]
    F = FAULT_BUCKET

    if fault_type == "FIBER_CUT":
        agg = rng.choice(aggs)
        if not alarm_loss:
            b.alarm(agg, "LINK_DOWN", "CRITICAL", "Uplink ge-0/0/1 to core down (loss of signal)", F)
        for site in topo.descendants(agg, "site"):
            b.alarm(site, "NG_LINK_FAILURE", "MAJOR", "NG interface to core unreachable", F, 1)
            for cell in topo.descendants(site, "cell"):
                b.alarm(cell, "CELL_OUT_OF_SERVICE", "CRITICAL", "Cell out of service", F, 2)
        b.cells_down(topo.descendants(agg, "cell"), F)
        return agg

    if fault_type == "POWER_OUTAGE":
        site = rng.choice(sites)
        if not alarm_loss:
            b.alarm(site, "MAINS_POWER_FAILURE", "MAJOR", "AC mains failure, running on battery", F - 7)
        b.alarm(site, "BATTERY_LOW", "MAJOR", "Battery voltage below 46V", F - 2)
        b.alarm(site, "NG_LINK_FAILURE", "MAJOR", "NG interface to core unreachable", F, 1)
        for cell in topo.descendants(site, "cell"):
            b.alarm(cell, "CELL_OUT_OF_SERVICE", "CRITICAL", "Cell out of service", F, 1)
        b.cells_down(topo.descendants(site, "cell"), F)
        return site

    if fault_type == "CORE_CONGESTION":
        if not alarm_loss:
            b.alarm(CORE_ID, "UPF_CPU_HIGH", "MAJOR", "User-plane function CPU above 95%", F, 2)
        b.alarm(CORE_ID, "GTPU_PATH_DEGRADED", "MINOR", "GTP-U echo latency above threshold", F, 4)
        core = b.kpis[CORE_ID]
        _apply(core["latency_ms"], F, lambda v: v * 7)
        _apply(core["packet_loss_pct"], F, lambda v: v * 45)
        _apply(core["cpu_pct"], F, lambda v: min(99.0, v * 2.4))
        for cell in topo.of_kind("cell"):
            _apply(b.kpis[cell.id]["dl_throughput_mbps"], F, lambda v: v * 0.6)
        return CORE_ID

    if fault_type == "CONFIG_CHANGE":
        site = rng.choice(sites)
        b.change_log.append({
            "time": _ts(F, -3), "entity": site, "author": "son-automation",
            "change": "Mobility parameter push: A3 handover offset 3 dB -> 12 dB on all cells",
        })
        for cell in topo.descendants(site, "cell"):
            _apply(b.kpis[cell]["drop_call_rate_pct"], F, lambda v: v * 9)
            if not alarm_loss:
                b.alarm(cell, "HANDOVER_FAILURE_RATE_HIGH", "MINOR", "Outgoing handover failure rate above 5%", F + 3)
        return site

    if fault_type == "RADIO_INTERFERENCE":
        site = rng.choice(sites)
        cell = rng.choice(topo.descendants(site, "cell"))
        if not alarm_loss:
            b.alarm(cell, "UL_INTERFERENCE_HIGH", "MAJOR", "Uplink interference above -95 dBm", F + 2)
        k = b.kpis[cell]
        _apply(k["ul_interference_dbm"], F, lambda v: v + 22)
        _apply(k["dl_throughput_mbps"], F, lambda v: v * 0.55)
        _apply(k["drop_call_rate_pct"], F, lambda v: v * 3)
        return cell

    raise ValueError(f"unknown fault type {fault_type!r}")


def generate_scenario(seed: int, fault_type: str | None = None, alarm_loss: bool | None = None) -> Scenario:
    """Standard (v0.1) incident: exactly one root cause."""
    b = _Builder(seed)
    rng = b.rng
    fault_type = fault_type or rng.choice(FAULT_TYPES)
    alarm_loss = (rng.random() < 0.35) if alarm_loss is None else alarm_loss
    root = _inject(b, fault_type, alarm_loss)
    _add_noise(b, [root])
    return _finish(b, seed, [(fault_type, root)], alarm_loss, category=fault_type)


# --- v0.2: harder incidents ---------------------------------------------------------------------

HARD_CATEGORIES = ["MULTI_FAULT", "CASCADE_AGG_POWER", "CASCADE_CONFIG_LOAD", "DECOY_MAINTENANCE"]
_LOCAL_FAULTS = ["FIBER_CUT", "POWER_OUTAGE", "CONFIG_CHANGE", "RADIO_INTERFERENCE"]


def generate_hard_scenario(seed: int, category: str | None = None, alarm_loss: bool | None = None) -> Scenario:
    """Harder incidents:

    MULTI_FAULT          two independent faults in different regions at the same time (both must be found)
    CASCADE_AGG_POWER    an aggregation router loses power; every site behind it drops, which looks like a fibre cut
    CASCADE_CONFIG_LOAD  a coverage-reducing tilt push on one site overloads its neighbours, which raise the alarms
    DECOY_MAINTENANCE    a planned-maintenance site fires CRITICAL outage alarms while the real fault is elsewhere
    """
    b = _Builder(seed + 10_000)  # separate random stream from the standard suite
    rng, topo = b.rng, b.topo
    category = category or rng.choice(HARD_CATEGORIES)
    alarm_loss = (rng.random() < 0.5) if alarm_loss is None else alarm_loss
    F = FAULT_BUCKET
    extra_quiet: list[str] = []

    if category == "MULTI_FAULT":
        first, second = rng.sample(_LOCAL_FAULTS, 2)
        r1 = _inject(b, first, alarm_loss)
        r2 = _inject(b, second, alarm_loss, avoid_aggs=frozenset({topo.agg_of(r1)}))
        truth = [(first, r1), (second, r2)]

    elif category == "CASCADE_AGG_POWER":
        agg = rng.choice([n.id for n in topo.of_kind("agg")])
        if not alarm_loss:
            b.alarm(agg, "MAINS_POWER_FAILURE", "MAJOR", "AC mains failure, running on battery", F - 9)
        b.alarm(agg, "BATTERY_LOW", "MAJOR", "Battery voltage below 46V", F - 3)
        b.alarm(CORE_ID, "PEER_UNREACHABLE", "MAJOR", f"BFD session to {agg} down", F)
        for site in topo.descendants(agg, "site"):
            b.alarm(site, "NG_LINK_FAILURE", "MAJOR", "NG interface to core unreachable", F, 1)
            for cell in topo.descendants(site, "cell"):
                b.alarm(cell, "CELL_OUT_OF_SERVICE", "CRITICAL", "Cell out of service", F, 2)
        b.cells_down(topo.descendants(agg, "cell"), F)
        truth = [("POWER_OUTAGE", agg)]

    elif category == "CASCADE_CONFIG_LOAD":
        site = rng.choice([n.id for n in topo.of_kind("site")])
        b.change_log.append({
            "time": _ts(F, -3), "entity": site, "author": "son-automation",
            "change": "Antenna electrical tilt 2 deg -> 9 deg on all cells (coverage optimisation)",
        })
        for cell in topo.descendants(site, "cell"):
            _apply(b.kpis[cell]["prb_util_pct"], F, lambda v: v * 0.3)
            _apply(b.kpis[cell]["dl_throughput_mbps"], F, lambda v: v * 0.5)
        agg = topo.agg_of(site)
        for neighbour in topo.descendants(agg, "site"):
            if neighbour == site:
                continue
            for cell in topo.descendants(neighbour, "cell"):
                _apply(b.kpis[cell]["prb_util_pct"], F, lambda v: min(98.0, v * 1.9))
                _apply(b.kpis[cell]["dl_throughput_mbps"], F, lambda v: v * 0.7)
                b.alarm(cell, "PRB_UTILISATION_HIGH", "MINOR", "Downlink PRB utilisation above 80%", F + 4)
        truth = [("CONFIG_CHANGE", site)]

    elif category == "DECOY_MAINTENANCE":
        real = rng.choice(["POWER_OUTAGE", "CONFIG_CHANGE", "RADIO_INTERFERENCE", "CORE_CONGESTION"])
        root = _inject(b, real, alarm_loss)
        busy_aggs = {topo.agg_of(root)} if root != CORE_ID else set()
        decoy = rng.choice([n.id for n in topo.of_kind("site") if topo.agg_of(n.id) not in busy_aggs])
        D = 9  # maintenance starts 10:45
        b.change_log.append({
            "time": _ts(D, -5), "entity": decoy, "author": "noc-planning",
            "change": "Planned maintenance CRQ-4471: all cells locked for antenna swap, 10:45-12:30",
        })
        for cell in topo.descendants(decoy, "cell"):
            b.alarm(cell, "CELL_OUT_OF_SERVICE", "CRITICAL", "Cell out of service", D, 1)
        b.cells_down(topo.descendants(decoy, "cell"), D)
        truth = [(real, root)]
        extra_quiet.append(decoy)

    else:
        raise ValueError(f"unknown hard category {category!r}")

    _add_noise(b, [e for _, e in truth] + extra_quiet)
    return _finish(b, seed, truth, alarm_loss, category=category)


def _finish(b: _Builder, seed: int, truth: list[tuple[str, str]], alarm_loss: bool, category: str) -> Scenario:
    b.alarms.sort(key=lambda a: a["time"])
    for i, a in enumerate(b.alarms, 1):
        a["id"] = f"ALM-{i:04d}"
    return Scenario(seed=seed, fault_type=truth[0][0], root_cause=truth[0][1], alarm_loss=alarm_loss,
                    alarms=b.alarms, kpis=b.kpis, change_log=b.change_log, topology=b.topo,
                    root_causes=truth, category=category)


def _add_noise(b: _Builder, busy: list[str]) -> None:
    """Noise on sites unrelated to any root cause (or decoy) in `busy`."""
    rng, topo = b.rng, b.topo
    busy_sites = {topo.site_of(e) for e in busy if e in topo and topo.get(e).kind in ("site", "cell")}
    busy_aggs = {e for e in busy if e in topo and topo.get(e).kind == "agg"}
    quiet_sites = [n.id for n in topo.of_kind("site")
                   if n.id not in busy_sites and topo.agg_of(n.id) not in busy_aggs]
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
