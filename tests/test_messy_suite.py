"""v0.3 messy telemetry: alarm storms, reporting gaps, staggered faults and a network-wide busy hour."""

import json

import pytest

from noc_copilot import analytics
from noc_copilot.benchmark import run_benchmark, scenarios
from noc_copilot.simulator import MESSY_CATEGORIES, generate_messy_scenario
from noc_copilot.tools import NocTools

SEEDS = range(1, 11)


def evidence_near(s, entity) -> bool:
    """Something an investigator can find without guessing: an alarm, a change-log entry or an anomaly group."""
    topo = s.topology
    near = {entity, topo.site_of(entity)} | set(topo.descendants(entity))
    alarms = {a["entity"] for a in s.alarms if a["status"] == "ACTIVE"}
    changes = {c["entity"] for c in s.change_log}
    groups = {g["entity"] for g in json.loads(NocTools(s).call("find_kpi_anomalies", {}))}
    return bool(near & (alarms | changes | groups))


@pytest.mark.parametrize("category", MESSY_CATEGORIES)
@pytest.mark.parametrize("seed", SEEDS)
def test_every_root_cause_leaves_evidence_even_without_primary_alarm(category, seed):
    s = generate_messy_scenario(seed, category, alarm_loss=True)
    for _, entity in s.root_causes:
        assert evidence_near(s, entity), f"{category} seed {seed}: {entity} is undiscoverable"


@pytest.mark.parametrize("seed", SEEDS)
def test_alarm_storm_buries_the_fault(seed):
    s = generate_messy_scenario(seed, "ALARM_STORM", alarm_loss=False)
    assert len(s.alarms) >= 50
    flaps = [a for a in s.alarms if a["type"] == "LINK_FLAP"]
    assert len({a["entity"] for a in flaps}) <= 2 and all(a["status"] == "CLEARED" for a in flaps)
    assert any("re-raised" in a["text"] for a in s.alarms)


@pytest.mark.parametrize("seed", SEEDS)
def test_telemetry_gap_is_missing_data_not_zero(seed):
    s = generate_messy_scenario(seed, "TELEMETRY_GAP")
    gap_site = next(a["entity"] for a in s.alarms if a["type"] == "PM_FILE_MISSING")
    assert gap_site not in {e for _, e in s.root_causes}
    cell = s.topology.descendants(gap_site, "cell")[0]
    assert s.kpis[cell]["availability_pct"][-1] is None  # not 0.0
    summary = analytics.entity_kpis(s, gap_site)["availability_pct"]
    assert summary["no_recent_data"] and summary["change_pct"] is None and not summary["anomaly"]
    # Both the real outage and the collection gap look the same in the KPI scan: only alarms tell them apart.
    groups = {g["entity"] for g in json.loads(NocTools(s).call("find_kpi_anomalies", {}))}
    assert gap_site in groups


def test_staggered_first_fault_hides_from_the_anomaly_scan():
    for seed in range(1, 21):
        s = generate_messy_scenario(seed, "STAGGERED_MULTI", alarm_loss=True)
        first = s.root_causes[0][1]
        flagged = {a["entity"] for a in analytics.find_anomalies(s)}
        # The baseline window already contains the fault, so the before/after comparison can't see it.
        assert not flagged & ({first} | set(s.topology.descendants(first))), seed


def test_timeseries_shows_when_a_fault_started():
    s = generate_messy_scenario(3, "STAGGERED_MULTI", alarm_loss=True)
    ts = json.loads(NocTools(s).call("get_kpi_timeseries", {"entity_id": "CORE-FRA-UPF1", "kpi": "latency_ms"}))
    assert len(ts["samples"]) == 24 and ts["samples"][0]["time"] == "2026-10-12T10:00:00Z"


@pytest.mark.parametrize("seed", SEEDS)
def test_busy_hour_flags_most_of_the_network(seed):
    s = generate_messy_scenario(seed, "BUSY_HOUR_SUBTLE")
    groups = json.loads(NocTools(s).call("find_kpi_anomalies", {}))
    assert len(groups) >= 12  # 16 sites; the real fault is one of them


def test_messy_suite_is_balanced_and_baseline_misses_staggered_faults():
    ss = scenarios(20, suite="messy")
    assert [s.category for s in ss[:4]] == MESSY_CATEGORIES
    result = run_benchmark("baseline", lambda s: (analytics.baseline_rca(s), None), n=20, suite="messy")
    assert result.by_category()["STAGGERED_MULTI"] == 0.0  # rules stop at the first match
    assert result.by_alarm_loss()["primary alarm missing"] < 0.5
    # Alarm rules never read KPIs, so the telemetry-gap trap only targets KPI-reading methods.
    assert result.by_category()["TELEMETRY_GAP"] == 1.0
