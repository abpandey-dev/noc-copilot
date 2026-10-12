"""The root-cause analysis (RCA) contract shared by the agent, the baseline and the benchmark."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FaultType = Literal["FIBER_CUT", "POWER_OUTAGE", "CORE_CONGESTION", "CONFIG_CHANGE", "RADIO_INTERFERENCE", "UNKNOWN"]


class Action(BaseModel):
    action: str
    risk: Literal["low", "medium", "high"]
    requires_human_approval: bool = True  # the agent proposes; an engineer approves


class RootCause(BaseModel):
    fault_type: FaultType
    root_cause_entity: str


class RCA(BaseModel):
    fault_type: FaultType
    root_cause_entity: str
    confidence: Literal["low", "medium", "high"]
    summary: str
    evidence: list[str] = Field(default_factory=list)
    affected_entities: list[str] = Field(default_factory=list)
    recommended_actions: list[Action] = Field(default_factory=list)
    customer_impact: str = ""
    # Other *independent* faults happening at the same time (not symptoms of the main one).
    additional_root_causes: list[RootCause] = Field(default_factory=list)

    def all_root_causes(self) -> set[tuple[str, str]]:
        causes = {(self.fault_type, self.root_cause_entity)}
        causes |= {(c.fault_type, c.root_cause_entity) for c in self.additional_root_causes}
        return {c for c in causes if c[0] != "UNKNOWN"}


# JSON schema handed to Claude as the structured-output format for the final answer.
RCA_SCHEMA = {
    "type": "object",
    "properties": {
        "fault_type": {"type": "string", "enum": list(FaultType.__args__)},
        "root_cause_entity": {"type": "string", "description": "Exact entity id, e.g. SITE-BER-002 or AGG-HAM-01"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "affected_entities": {"type": "array", "items": {"type": "string"}},
        "recommended_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["action", "risk"],
                "additionalProperties": False,
            },
        },
        "customer_impact": {"type": "string"},
        "additional_root_causes": {
            "type": "array",
            "description": "Other independent faults happening at the same time; empty if there is only one.",
            "items": {
                "type": "object",
                "properties": {
                    "fault_type": {"type": "string", "enum": list(FaultType.__args__)},
                    "root_cause_entity": {"type": "string"},
                },
                "required": ["fault_type", "root_cause_entity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fault_type", "root_cause_entity", "confidence", "summary", "evidence",
                 "affected_entities", "recommended_actions", "customer_impact", "additional_root_causes"],
    "additionalProperties": False,
}
