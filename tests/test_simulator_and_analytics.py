import pytest

from noc_copilot import analytics
from noc_copilot.benchmark import run_benchmark, scenarios
from noc_copilot.simulator import FAULT_TYPES, generate_scenario
from noc_copilot.topology import CORE_ID, build_topology


def test_topology_shape():
    t = build_topology()
    assert len(t.of_kind("agg")) == 4 and len(t.of_kind("site")) == 16 and len(t.of_kind("cell")) == 48
    assert t.path_to_core("SITE-BER-001-C1") == ["SITE-BER-001", "AGG-BER-01", CORE_ID]
    assert t.agg_of("SITE-HAM-003-C2") == "AGG-HAM-01"


def test_scenarios_are_deterministic():
    a, b = generate_scenario(42), generate_scenario(42)
    assert a.ground_truth == b.ground_truth and a.alarms == b.alarms


@pytest.mark.parametrize("fault", FAULT_TYPES)
@pytest.mark.parametrize("seed", range(1, 9))
def test_every_fault_leaves_a_kpi_trace_on_the_root_cause(fault, seed):
    """Even with the primary alarm missing, the fault must be discoverable from KPIs."""
    s = generate_scenario(seed, fault_type=fault, alarm_loss=True)
    anomalous = {a["entity"] for a in analytics.find_anomalies(s)}
    root = s.root_cause
    related = {root} | set(s.topology.descendants(root)) if root in s.topology else {root}
    assert anomalous & related, f"no KPI anomaly near {root}"


@pytest.mark.parametrize("seed", range(1, 21))
def test_healthy_cells_do_not_raise_false_anomalies(seed):
    s = generate_scenario(seed, fault_type="CONFIG_CHANGE", alarm_loss=False)
    for a in analytics.find_anomalies(s):
        assert s.topology.site_of(a["entity"]) == s.root_cause, a


def test_alarm_loss_removes_primary_alarm():
    with_alarm = generate_scenario(5, "POWER_OUTAGE", alarm_loss=False)
    without = generate_scenario(5, "POWER_OUTAGE", alarm_loss=True)
    assert any(a["type"] == "MAINS_POWER_FAILURE" for a in with_alarm.alarms)
    assert not any(a["type"] == "MAINS_POWER_FAILURE" for a in without.alarms)


def test_benchmark_set_is_balanced():
    ss = scenarios(20)
    assert {s.fault_type for s in ss} == set(FAULT_TYPES)
    assert sum(s.alarm_loss for s in ss) == 10


def test_baseline_is_perfect_with_alarms_and_weak_without():
    result = run_benchmark("baseline", lambda s: (analytics.baseline_rca(s), None), n=20)
    by_loss = result.by_alarm_loss()
    assert by_loss["all alarms present"] == 1.0
    assert by_loss["primary alarm missing"] < 0.5
