"""The NOC Copilot agent: Claude investigates an incident with read-only tools and returns a structured RCA.

Design choices:
- Manual agentic loop so every tool call is recorded in an audit trace (who looked at what, and why).
- Tools are read-only; recommended actions come back as proposals flagged for human approval.
- The final answer is constrained to the RCA JSON schema (structured outputs), so it can feed a
  ticketing system or the benchmark directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import anthropic

from noc_copilot.models import RCA, RCA_SCHEMA
from noc_copilot.simulator import Scenario
from noc_copilot.tools import TOOL_DEFINITIONS, NocTools, ToolError

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Server-side refusal fallbacks ("default" mode) are only available on these models.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

# USD per million tokens (input, output) - used to report cost per incident.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICES:
        return None
    p_in, p_out = PRICES[model]
    return (input_tokens * p_in + output_tokens * p_out) / 1_000_000

SYSTEM_PROMPT = """You are a senior NOC (network operations centre) engineer for a 5G mobile network in Germany.
An incident is in progress. Investigate it with the tools and find the single most likely root cause.

The network: one packet core (CORE-FRA-UPF1), one aggregation router per city (AGG-<city>-01), four cell
sites per router (SITE-<city>-00N) and three cells per site (SITE-<city>-00N-C1..C3).

Fault categories: FIBER_CUT (aggregation uplink), POWER_OUTAGE (one site), CORE_CONGESTION (core UPF),
CONFIG_CHANGE (a parameter change that degraded a site), RADIO_INTERFERENCE (one cell), or UNKNOWN.

Alarms can be noisy or missing, so check KPIs and the change log as well as alarms, and look at the
runbooks when useful. Name the root-cause entity at the right level: the aggregation router for a fibre
cut, the site for power or config problems, the core for congestion, the single cell for interference.
Your tools are read-only. Recommended actions are proposals that a human engineer will approve.
When you are confident, give your final answer in the required JSON format."""


@dataclass
class AgentResult:
    rca: RCA | None
    trace: list[dict] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float | None = None
    error: str | None = None


class NocAgent:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "medium", max_turns: int = 12, client=None):
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_turns = max_turns

    def investigate(self, scenario: Scenario,
                    brief: str = "Multiple alarms and KPI degradation reported in the last hour. Find the root cause.") -> AgentResult:
        tools = NocTools(scenario)
        messages = [{"role": "user", "content": brief}]
        result = AgentResult(rca=None)
        start = time.perf_counter()

        extra = {}
        if self.model in FALLBACK_MODELS:
            # A declined request is retried on Anthropic's recommended fallback model.
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}

        for turn in range(1, self.max_turns + 1):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": RCA_SCHEMA}},
                **extra,
            )
            result.turns = turn
            result.input_tokens += response.usage.input_tokens
            result.output_tokens += response.usage.output_tokens

            if response.stop_reason == "refusal":
                result.error = "Model declined the request"
                break
            if response.stop_reason == "max_tokens":
                result.error = "Response hit max_tokens before finishing"
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if response.stop_reason == "tool_use" and tool_uses:
                results = []
                for block in tool_uses:
                    try:
                        output, is_error = tools.call(block.name, dict(block.input)), False
                    except (ToolError, TypeError) as e:
                        output, is_error = f"Error: {e}", True
                    result.trace.append({"turn": turn, "tool": block.name, "input": dict(block.input),
                                         "is_error": is_error, "output_chars": len(output)})
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": output, "is_error": is_error})
                # All results for parallel calls go back in a single user message.
                messages.append({"role": "user", "content": results})
                continue

            if response.stop_reason == "pause_turn":
                continue

            text = "".join(b.text for b in response.content if b.type == "text")
            try:
                result.rca = RCA.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValueError) as e:
                result.error = f"Could not parse final RCA: {e}"
            break
        else:
            result.error = f"No final answer after {self.max_turns} turns"

        result.latency_s = round(time.perf_counter() - start, 2)
        result.cost_usd = estimate_cost(self.model, result.input_tokens, result.output_tokens)
        return result
