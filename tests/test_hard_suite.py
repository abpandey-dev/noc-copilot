"""v0.2 hard incidents: multi-fault, cascades and decoys."""

import json

import pytest

from noc_copilot import analytics
from noc_copilot.benchmark import run_benchmark, scenarios, score
from noc_copilot.models import RCA, RootCause
from noc_copilot.simulator import HARD_CATEGORIES, generate_hard_scenario
from noc_copilot.tools import NocTools


def rca(fault, entity, extra=()):
    return RCA(fault_type=fault, root_cause_entity=entity, confidence="high", summary="",
               additional_root_causes=[RootCause(fault_type=f, root_cause_entity=e) for f, e in extra])


@pytest.mark.parametrize("seed", range(1, 13))
def test_multi_fault_has_two_causes_in_different_regions(seed):
    s = generate_hard_scenario(seed, "MULTI_FAULT")
    (_, e1), (_, e2) = s.root_causes
    assert s.topology.agg_of(e1) != s.topology.agg_of(e2)


@pytest.mark.parametrize("category", HARD_CATEGORIES)
@pytest.mark.parametrize("seed", range(1, 9))
def test_every_root_cause_is_visible_in_grouped_anomalies(category, seed):
    """Each true root cause must show up in the agent's anomaly tool, even next to a large outage."""
    s = generate_hard_scenario(seed, category, alarm_loss=True)
    groups = json.loads(NocTools(s).call("find_kpi_anomalies", {}))
    flagged = {g["entity"] for g in groups}
    for _, entity in s.root_causes:
        node = s.topology.get(entity)
        near = {entity, s.topology.site_of(entity)} | set(s.topology.descendants(entity, "site"))
        assert flagged & near, f"{category}: no anomaly group near {entity} ({node.kind})"


def test_cascade_agg_power_looks_like_fibre_cut_to_rules():
    s = generate_hard_scenario(6, "CASCADE_AGG_POWER", alarm_loss=True)
    assert s.root_causes == [("POWER_OUTAGE", s.root_cause)] and s.topology.get(s.root_cause).kind == "agg"
    assert analytics.baseline_rca(s).fault_type == "FIBER_CUT"  # the trap


def test_decoy_site_is_in_maintenance_not_a_root_cause():
    s = generate_hard_scenario(4, "DECOY_MAINTENANCE")
    decoy = next(c["entity"] for c in s.change_log if c["author"] == "noc-planning")
    assert decoy not in {e for _, e in s.root_causes}
    assert any(a["entity"].startswith(decoy) and a["severity"] == "CRITICAL" for a in s.alarms)


def test_config_load_alarms_point_at_neighbours_not_the_root():
    s = generate_hard_scenario(3, "CASCADE_CONFIG_LOAD")
    root = s.root_cause
    prb_alarms = {s.topology.site_of(a["entity"]) for a in s.alarms if a["type"] == "PRB_UTILISATION_HIGH"}
    assert prb_alarms and root not in prb_alarms


def test_scoring_requires_exact_set_and_gives_partial_recall():
    s = generate_hard_scenario(1, "MULTI_FAULT")
    (f1, e1), (f2, e2) = s.root_causes
    assert score(s, rca(f1, e1, [(f2, e2)])).correct
    half = score(s, rca(f1, e1))
    assert not half.correct and half.recall == 0.5
    extra = score(s, rca(f1, e1, [(f2, e2), ("POWER_OUTAGE", "SITE-BER-001")]))
    assert not extra.correct  # a phantom root cause is wrong too


def test_hard_suite_is_balanced_and_baseline_struggles():
    ss = scenarios(20, suite="hard")
    assert [s.category for s in ss[:4]] == HARD_CATEGORIES
    result = run_benchmark("baseline", lambda s: (analytics.baseline_rca(s), None), n=20, suite="hard")
    assert result.accuracy() < 0.5
    assert result.by_category()["MULTI_FAULT"] == 0.0
