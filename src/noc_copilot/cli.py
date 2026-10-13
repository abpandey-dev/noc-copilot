"""`noc` command line.

  noc simulate --seed 3              show an incident (alarms, change log, anomalies) without the answer
  noc triage --seed 3 --method baseline|claude
  noc benchmark --method baseline|claude --n 20 [--suite standard|hard]
  noc simulate --seed 3 --category MULTI_FAULT --reveal   (v0.2 hard incidents)
  noc simulate --seed 3 --category TELEMETRY_GAP --reveal (v0.3 messy telemetry)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from noc_copilot import analytics
from noc_copilot.benchmark import run_benchmark, to_markdown
from noc_copilot.models import RCA
from noc_copilot.simulator import (FAULT_TYPES, HARD_CATEGORIES, MESSY_CATEGORIES, Scenario,
                                   generate_hard_scenario, generate_messy_scenario, generate_scenario)


def _baseline(s: Scenario) -> tuple[RCA | None, str | None]:
    return analytics.baseline_rca(s), None


def _claude_triage(model: str, effort: str, totals: dict, minimal_prompt: bool = False):
    from noc_copilot.agent import NocAgent

    agent = NocAgent(model=model, effort=effort, minimal_prompt=minimal_prompt)

    def triage(s: Scenario) -> tuple[RCA | None, str | None]:
        r = agent.investigate(s)
        cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "n/a"
        print(f"  seed {s.seed}: {r.turns} turns, {len(r.trace)} tool calls, "
              f"{r.input_tokens}+{r.output_tokens} tokens, {r.latency_s}s, {cost}", file=sys.stderr)
        totals["cost"] += r.cost_usd or 0.0
        totals["latency"] += r.latency_s
        totals["tool_calls"] += len(r.trace)
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


def _make_scenario(args) -> Scenario:
    if args.category in MESSY_CATEGORIES:
        return generate_messy_scenario(args.seed, category=args.category)
    if args.category:
        return generate_hard_scenario(args.seed, category=args.category)
    return generate_scenario(args.seed, fault_type=args.fault)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="noc", description="NOC Copilot: agentic RCA for 5G network operations")
    sub = p.add_subparsers(dest="cmd", required=True)

    sim = sub.add_parser("simulate", help="Show a simulated incident")
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--fault", choices=FAULT_TYPES, help="standard suite: fault type")
    sim.add_argument("--category", choices=HARD_CATEGORIES + MESSY_CATEGORIES,
                     help="hard/messy suite: incident category")
    sim.add_argument("--reveal", action="store_true", help="Also print the ground truth")

    tri = sub.add_parser("triage", help="Run root-cause analysis on one incident")
    tri.add_argument("--seed", type=int, default=1)
    tri.add_argument("--fault", choices=FAULT_TYPES, help="standard suite: fault type")
    tri.add_argument("--category", choices=HARD_CATEGORIES + MESSY_CATEGORIES,
                     help="hard/messy suite: incident category")
    tri.add_argument("--method", choices=["baseline", "claude"], default="baseline")
    tri.add_argument("--model", default="claude-opus-5-5")
    tri.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    tri.add_argument("--trace", action="store_true", help="Print the agent's tool-call audit trail")

    bench = sub.add_parser("benchmark", help="Score a method over many incidents")
    bench.add_argument("--method", choices=["baseline", "claude"], default="baseline")
    bench.add_argument("--n", type=int, default=20)
    bench.add_argument("--suite", choices=["standard", "hard", "messy"], default="standard")
    bench.add_argument("--minimal-prompt", action="store_true",
                       help="Ablation: system prompt without domain hints")
    bench.add_argument("--model", default="claude-opus-5-5")
    bench.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    bench.add_argument("--out", default="reports")

    args = p.parse_args(argv)

    if args.cmd == "simulate":
        s = _make_scenario(args)
        print(f"Incident seed {s.seed}: {len(s.alarms)} alarms, {len(s.change_log)} changes\n")
        for a in s.alarms:
            print(f"{a['time']}  {a['severity']:<8} {a['status']:<7} {a['entity']:<18} {a['type']}")
        print("\nChange log:")
        for c in s.change_log:
            print(f"{c['time']}  {c['entity']:<18} {c['author']:<15} {c['change']}")
        print("\nTop KPI anomalies:")
        for a in analytics.find_anomalies(s)[:8]:
            if a.get("no_recent_data"):
                print(f"  {a['entity']:<18} {a['kpi']:<22} {str(a['baseline_mean']):>8} -> no data received")
                continue
            print(f"  {a['entity']:<18} {a['kpi']:<22} {a['baseline_mean']:>8} -> {a['recent_mean']:<8} ({a['change_pct']:+}%)")
        if args.reveal:
            print(f"\nGround truth ({s.category}): {s.root_causes}, alarm_loss={s.alarm_loss}")
        return 0

    if args.cmd == "triage":
        s = _make_scenario(args)
        if args.method == "baseline":
            rca = analytics.baseline_rca(s)
        else:
            from noc_copilot.agent import NocAgent

            r = NocAgent(model=args.model, effort=args.effort).investigate(s)
            if args.trace:
                print("Audit trail:")
                for t in r.trace:
                    print(f"  turn {t['turn']}: {t['tool']}({json.dumps(t['input'])}){'  [error]' if t['is_error'] else ''}")
            cost = f", ${r.cost_usd:.4f}" if r.cost_usd is not None else ""
            print(f"\n{r.turns} turns, {len(r.trace)} tool calls, {r.input_tokens}+{r.output_tokens} tokens, "
                  f"{r.latency_s}s{cost}")
            if r.error:
                print(f"Error: {r.error}")
            rca = r.rca
        _print_rca(rca)
        ok = rca is not None and rca.all_root_causes() == set(s.root_causes)
        truth = ", ".join(f"{t} at {e}" for t, e in s.root_causes)
        print(f"\nGround truth ({s.category}): {truth} -> {'CORRECT' if ok else 'WRONG'}")
        return 0

    totals = {"cost": 0.0, "latency": 0.0, "tool_calls": 0}
    triage = _baseline if args.method == "baseline" else _claude_triage(args.model, args.effort, totals, args.minimal_prompt)
    label = "Alarm-rule baseline" if args.method == "baseline" else f"Claude agent ({args.model}, {args.effort}{', minimal prompt' if args.minimal_prompt else ''})"
    result = run_benchmark(label, triage, n=args.n, suite=args.suite)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    name = ("baseline" if args.method == "baseline" else f"{args.model}_{args.effort}" + ("_minimal" if args.minimal_prompt else "")) + f"_{args.suite}"
    (out / f"benchmark_{name}.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    table = to_markdown([result])
    if args.method == "claude":
        n = len(result.cases)
        table += (f"\nTotal cost ${totals['cost']:.2f} (${totals['cost'] / n:.4f}/incident), "
                  f"avg {totals['latency'] / n:.1f}s and {totals['tool_calls'] / n:.1f} tool calls per incident.\n")
    (out / f"benchmark_{name}.md").write_text(table, encoding="utf-8")
    print(table)
    for c in (c for c in result.cases if not c.correct):
        fmt = lambda causes: ", ".join(f"{t}@{e}" for t, e in causes) or "-"
        print(f"  miss seed {c.seed} [{c.category}, alarm_loss={c.alarm_loss}]: truth {fmt(c.truth)} "
              f"-> predicted {fmt(c.predicted)}{'  error: ' + c.error if c.error else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
