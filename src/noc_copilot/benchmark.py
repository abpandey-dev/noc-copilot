"""Benchmark: run a triage method over many simulated incidents and score it against ground truth.

Scoring: the set of root causes the method names (main + additional) must equal the true set exactly.
Missing a fault or naming an extra one both count as wrong; a NOC acting on a phantom root cause
wastes as much time as missing a real one. `recall` gives partial credit for multi-fault incidents.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from pydantic import BaseModel

from noc_copilot.models import RCA
from noc_copilot.simulator import (FAULT_TYPES, HARD_CATEGORIES, Scenario, generate_hard_scenario,
                                   generate_scenario)

SUITES = {"standard": FAULT_TYPES, "hard": HARD_CATEGORIES}


class CaseScore(BaseModel):
    seed: int
    category: str
    truth: list[tuple[str, str]]
    predicted: list[tuple[str, str]]
    alarm_loss: bool
    correct: bool
    recall: float
    error: str | None = None

    # Back-compat with v0.1 report fields.
    @property
    def fault_type(self) -> str:
        return self.truth[0][0]

    @property
    def root_cause(self) -> str:
        return self.truth[0][1]


class BenchmarkResult(BaseModel):
    method: str
    suite: str = "standard"
    cases: list[CaseScore]

    def accuracy(self, cases: list[CaseScore] | None = None) -> float:
        cases = self.cases if cases is None else cases
        return sum(c.correct for c in cases) / len(cases) if cases else 0.0

    def by_category(self) -> dict[str, float]:
        groups = defaultdict(list)
        for c in self.cases:
            groups[c.category].append(c)
        return {k: self.accuracy(v) for k, v in groups.items()}

    def by_alarm_loss(self) -> dict[str, float]:
        return {
            "all alarms present": self.accuracy([c for c in self.cases if not c.alarm_loss]),
            "primary alarm missing": self.accuracy([c for c in self.cases if c.alarm_loss]),
        }


def scenarios(n: int, start_seed: int = 1, suite: str = "standard") -> list[Scenario]:
    """Balanced set: categories rotate, and every other pass drops the primary alarm."""
    cats = SUITES[suite]
    gen = generate_scenario if suite == "standard" else generate_hard_scenario
    out = []
    for i in range(n):
        alarm_loss = (i // len(cats)) % 2 == 1
        out.append(gen(start_seed + i, cats[i % len(cats)], alarm_loss))
    return out


def score(s: Scenario, rca: RCA | None, error: str | None = None) -> CaseScore:
    truth = set(s.root_causes)
    predicted = rca.all_root_causes() if rca else set()
    return CaseScore(seed=s.seed, category=s.category, truth=sorted(truth), predicted=sorted(predicted),
                     alarm_loss=s.alarm_loss, correct=predicted == truth,
                     recall=len(truth & predicted) / len(truth), error=error)


def run_benchmark(method: str, triage: Callable[[Scenario], tuple[RCA | None, str | None]],
                  n: int = 20, start_seed: int = 1, suite: str = "standard") -> BenchmarkResult:
    return BenchmarkResult(method=method, suite=suite,
                           cases=[score(s, *triage(s)) for s in scenarios(n, start_seed, suite)])


def to_markdown(results: list[BenchmarkResult]) -> str:
    cats = SUITES[results[0].suite]
    lines = ["| Method | Overall | " + " | ".join(cats) + " | Alarms present | Primary alarm missing |",
             "|---" * (len(cats) + 4) + "|"]
    for r in results:
        bc, bl = r.by_category(), r.by_alarm_loss()
        cells = [f"{bc[c]:.0%}" if c in bc else "-" for c in cats]
        lines.append(f"| {r.method} | **{r.accuracy():.0%}** | " + " | ".join(cells)
                     + f" | {bl['all alarms present']:.0%} | {bl['primary alarm missing']:.0%} |")
    return "\n".join(lines) + "\n"
