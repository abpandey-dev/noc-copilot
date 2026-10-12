"""Tool behaviour and the agent loop, using a scripted fake client (no API calls, no cost)."""

import json
from types import SimpleNamespace as NS

from noc_copilot.agent import FALLBACK_BETA, NocAgent
from noc_copilot.models import RCA_SCHEMA
from noc_copilot.simulator import generate_scenario
from noc_copilot.tools import TOOL_DEFINITIONS, NocTools, ToolError


def test_tool_schemas_are_strict():
    for t in TOOL_DEFINITIONS:
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False


def test_tools_return_json_and_reject_unknown_entities():
    tools = NocTools(generate_scenario(3, "FIBER_CUT", alarm_loss=False))
    alarms = json.loads(tools.call("list_alarms", {"min_severity": "CRITICAL"}))
    assert alarms and all(a["severity"] == "CRITICAL" for a in alarms)
    topo = json.loads(tools.call("get_topology", {"entity_id": "SITE-BER-001"}))
    assert topo["path_to_core"][-1] == "CORE-FRA-UPF1"
    try:
        tools.call("get_kpis", {"entity_id": "SITE-XXX-999"})
        raise AssertionError("expected ToolError")
    except ToolError as e:
        assert "Unknown entity" in str(e)


def test_runbook_search_finds_relevant_runbook():
    tools = NocTools(generate_scenario(1))
    hit = json.loads(tools.call("search_runbooks", {"query": "battery mains power site down"}))
    assert hit["match"] == "power-outage.md"


# --- scripted fake Claude -------------------------------------------------------------------------

def tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def resp(content, stop_reason):
    return NS(content=content, stop_reason=stop_reason, stop_details=None,
              usage=NS(input_tokens=100, output_tokens=20))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kwargs):
        # Snapshot the message list: the agent keeps appending to the same list object.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


FINAL = {
    "fault_type": "POWER_OUTAGE", "root_cause_entity": "SITE-HAM-002", "confidence": "high",
    "summary": "Site lost mains power and drained its battery.",
    "evidence": ["BATTERY_LOW then full site outage", "Neighbouring sites healthy"],
    "affected_entities": ["SITE-HAM-002"],
    "recommended_actions": [{"action": "Dispatch generator", "risk": "low"}],
    "customer_impact": "No coverage from one site",
}


def test_agent_loop_runs_tools_and_parses_final_rca():
    s = generate_scenario(2, "POWER_OUTAGE", alarm_loss=True)
    client = FakeClient([
        resp([NS(type="thinking", thinking=""),
              tool_use("t1", "list_alarms", {"min_severity": "MAJOR"}),
              tool_use("t2", "get_change_log", {})], "tool_use"),
        resp([tool_use("t3", "get_kpis", {"entity_id": "NOPE"})], "tool_use"),
        resp([NS(type="text", text=json.dumps(FINAL))], "end_turn"),
    ])
    r = NocAgent(client=client).investigate(s)

    assert r.error is None and r.rca.fault_type == "POWER_OUTAGE"
    assert [t["tool"] for t in r.trace] == ["list_alarms", "get_change_log", "get_kpis"]
    assert r.trace[-1]["is_error"] is True  # bad entity is reported back to the model, not raised
    assert r.rca.recommended_actions[0].requires_human_approval is True

    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"]["format"]["schema"] == RCA_SCHEMA
    assert first["betas"] == [FALLBACK_BETA] and first["fallbacks"] == "default"
    assert "thinking" not in first  # Opus 5.5: leave thinking at its default; effort controls depth
    # Parallel tool results go back in ONE user message.
    second_msgs = client.calls[1]["messages"]
    assert second_msgs[-1]["role"] == "user" and len(second_msgs[-1]["content"]) == 2


def test_agent_reports_refusal_and_turn_limit():
    s = generate_scenario(2)
    refused = NocAgent(client=FakeClient([resp([], "refusal")])).investigate(s)
    assert refused.rca is None and "declined" in refused.error

    loop = [resp([tool_use(f"t{i}", "get_change_log", {})], "tool_use") for i in range(3)]
    stuck = NocAgent(client=FakeClient(loop), max_turns=3).investigate(s)
    assert stuck.rca is None and "No final answer" in stuck.error
