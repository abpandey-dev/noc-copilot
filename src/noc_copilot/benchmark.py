"""Benchmark: run a triage method over many simulated incidents and score it against ground truth."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from pydantic import BaseModel

from noc_copilot.models import RCA
from noc_copilot.simulator import FAULT_TYPES, Scenario, generate_scenario


class CaseScore(BaseModel):
    seed: int
    fault_type: str
    root_cause: str
    alarm_loss: bool
    predicted_type: str
    predicted_entity: str
    type_correct: bool
    entity_correct: bool
    error: str | None = None


class BenchmarkResult(BaseModel):
    method: str
    cases: list[CaseScore]

    def accuracy(self, cases: list[CaseScore] | None = None) -> float:
        cases = self.cases if cases is None else cases
        return sum(c.type_correct and c.entity_correct for c in cases) / len(cases) if cases else 0.0

    def by_fault(self) -> dict[str, float]:
        groups = defaultdict(list)
        for c in self.cases:
            groups[c.fault_type].append(c)
        return {k: self.accuracy(v) for k, v in sorted(groups.items())}

    def by_alarm_loss(self) -> dict[str, float]:
        return {
            "all alarms present": self.accuracy([c for c in self.cases if not c.alarm_loss]),
            "primary alarm missing": self.accuracy([c for c in self.cases if c.alarm_loss]),
        }


def scenarios(n: int, start_seed: int = 1) -> list[Scenario]:
    """Balanced set: fault types rotate, and every other pass drops the primary alarm."""
    out = []
    for i in range(n):
        fault = FAULT_TYPES[i % len(FAULT_TYPES)]
        alarm_loss = (i // len(FAULT_TYPES)) % 2 == 1
        out.append(generate_scenario(start_seed + i, fault_type=fault, alarm_loss=alarm_loss))
    return out


def score(s: Scenario, rca: RCA | None, error: str | None = None) -> CaseScore:
    p_type = rca.fault_type if rca else "ERROR"
    p_entity = rca.root_cause_entity if rca else ""
    return CaseScore(seed=s.seed, fault_type=s.fault_type, root_cause=s.root_cause, alarm_loss=s.alarm_loss,
                     predicted_type=p_type, predicted_entity=p_entity,
                     type_correct=p_type == s.fault_type, entity_correct=p_entity == s.root_cause, error=error)


def run_benchmark(method: str, triage: Callable[[Scenario], tuple[RCA | None, str | None]],
                  n: int = 20, start_seed: int = 1) -> BenchmarkResult:
    return BenchmarkResult(method=method, cases=[score(s, *triage(s)) for s in scenarios(n, start_seed)])


def to_markdown(results: list[BenchmarkResult]) -> str:
    lines = ["| Method | Overall | " + " | ".join(FAULT_TYPES) + " | Alarms present | Primary alarm missing |",
             "|---" * (len(FAULT_TYPES) + 4) + "|"]
    for r in results:
        bf, bl = r.by_fault(), r.by_alarm_loss()
        cells = [f"{bf.get(f, 0):.0%}" for f in FAULT_TYPES]
        lines.append(f"| {r.method} | **{r.accuracy():.0%}** | " + " | ".join(cells)
                     + f" | {bl['all alarms present']:.0%} | {bl['primary alarm missing']:.0%} |")
    return "\n".join(lines) + "\n"
