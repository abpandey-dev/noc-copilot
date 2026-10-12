"""`noc` command line.

  noc simulate --seed 3              show an incident (alarms, change log, anomalies) without the answer
  noc triage --seed 3 --method baseline|claude
  noc benchmark --method baseline|claude --n 20
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from noc_copilot import analytics
from noc_copilot.benchmark import run_benchmark, to_markdown
from noc_copilot.models import RCA
from noc_copilot.simulator import FAULT_TYPES, Scenario, generate_scenario


def _baseline(s: Scenario) -> tuple[RCA | None, str | None]:
    return analytics.baseline_rca(s), None


def _claude_triage(model: str, effort: str):
    from noc_copilot.agent import NocAgent

    agent = NocAgent(model=model, effort=effort)

    def triage(s: Scenario) -> tuple[RCA | None, str | None]:
        r = agent.investigate(s)
        print(f"  seed {s.seed}: {r.turns} turns, {len(r.trace)} tool calls, "
              f"{r.input_tokens + r.output_tokens} tokens, {r.latency_s}s", file=sys.stderr)
        return r.rca, r.error

    return triage


def _print_rca(rca: RCA | None) -> None:
    if rca is None:
        print("No RCA produced.")
        return
    print(f"\nRoot cause : {rca.fault_type} at {rca.root_cause_entity}  (confidence: {rca.confidence})")
    print(f"Summary    : {rca.summary}")
    for e in rca.evidence:
        print(f"  evidence : {e}")
    for a in rca.recommended_actions:
        print(f"  PROPOSED [{a.risk} risk, needs approval]: {a.action}")
    if rca.customer_impact:
        print(f"Impact     : {rca.customer_impact}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="noc", description="NOC Copilot: agentic RCA for 5G network operations")
    sub = p.add_subparsers(dest="cmd", required=True)

    sim = sub.add_parser("simulate", help="Show a simulated incident")
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--fault", choices=FAULT_TYPES)
    sim.add_argument("--reveal", action="store_true", help="Also print the ground truth")

    tri = sub.add_parser("triage", help="Run root-cause analysis on one incident")
    tri.add_argument("--seed", type=int, default=1)
    tri.add_argument("--fault", choices=FAULT_TYPES)
    tri.add_argument("--method", choices=["baseline", "claude"], default="baseline")
    tri.add_argument("--model", default="claude-opus-5-5")
    tri.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    tri.add_argument("--trace", action="store_true", help="Print the agent's tool-call audit trail")

    bench = sub.add_parser("benchmark", help="Score a method over many incidents")
    bench.add_argument("--method", choices=["baseline", "claude"], default="baseline")
    bench.add_argument("--n", type=int, default=20)
    bench.add_argument("--model", default="claude-opus-5-5")
    bench.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    bench.add_argument("--out", default="reports")

    args = p.parse_args(argv)

    if args.cmd == "simulate":
        s = generate_scenario(args.seed, fault_type=args.fault)
        print(f"Incident seed {s.seed}: {len(s.alarms)} alarms, {len(s.change_log)} changes\n")
        for a in s.alarms:
            print(f"{a['time']}  {a['severity']:<8} {a['status']:<7} {a['entity']:<18} {a['type']}")
        print("\nChange log:")
        for c in s.change_log:
            print(f"{c['time']}  {c['entity']:<18} {c['author']:<15} {c['change']}")
        print("\nTop KPI anomalies:")
        for a in analytics.find_anomalies(s)[:8]:
            print(f"  {a['entity']:<18} {a['kpi']:<22} {a['baseline_mean']:>8} -> {a['recent_mean']:<8} ({a['change_pct']:+}%)")
        if args.reveal:
            print(f"\nGround truth: {json.dumps(s.ground_truth)}")
        return 0

    if args.cmd == "triage":
        s = generate_scenario(args.seed, fault_type=args.fault)
        if args.method == "baseline":
            rca = analytics.baseline_rca(s)
        else:
            from noc_copilot.agent import NocAgent

            r = NocAgent(model=args.model, effort=args.effort).investigate(s)
            if args.trace:
                print("Audit trail:")
                for t in r.trace:
                    print(f"  turn {t['turn']}: {t['tool']}({json.dumps(t['input'])}){'  [error]' if t['is_error'] else ''}")
            print(f"\n{r.turns} turns, {len(r.trace)} tool calls, {r.input_tokens}+{r.output_tokens} tokens, {r.latency_s}s")
            if r.error:
                print(f"Error: {r.error}")
            rca = r.rca
        _print_rca(rca)
        ok = rca is not None and rca.fault_type == s.fault_type and rca.root_cause_entity == s.root_cause
        print(f"\nGround truth: {s.fault_type} at {s.root_cause} -> {'CORRECT' if ok else 'WRONG'}")
        return 0

    triage = _baseline if args.method == "baseline" else _claude_triage(args.model, args.effort)
    label = "Alarm-rule baseline" if args.method == "baseline" else f"Claude agent ({args.model}, {args.effort})"
    result = run_benchmark(label, triage, n=args.n)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"benchmark_{args.method}.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    table = to_markdown([result])
    (out / f"benchmark_{args.method}.md").write_text(table, encoding="utf-8")
    print(table)
    misses = [c for c in result.cases if not (c.type_correct and c.entity_correct)]
    for c in misses:
        print(f"  miss seed {c.seed}: truth {c.fault_type}@{c.root_cause} (alarm_loss={c.alarm_loss}) "
              f"-> predicted {c.predicted_type}@{c.predicted_entity or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
