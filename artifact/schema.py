"""
Artifact schema — the typed, versioned, agent-invocable "capability" contract.

This is the seam the whole system is built around: the discovery agent (LLM-driven,
non-deterministic) produces a Capability; the replay engine (deterministic, no LLM) consumes
one. Everything downstream of a successful discovery run — human review, replay, an AI agent
invoking this as a tool — only ever sees this schema, never the raw model transcript.

Design notes (see REPORT.md "Artifact schema" for the full write-up):
  - LocatorTarget carries `reasoning` so a human reviewer can judge robustness, not just
    correctness — a capability with a text-match-only locator and no reasoning is a red flag.
  - ExpectedOutcome.classification is authored onto the Step at *recording* time, from what
    discovery actually observed — replay never guesses whether a condition is a business
    outcome vs. a hard failure, it only branches on what was declared.
  - `value: str | dict | None` on Step lets a literal ("christmas_club") and a parameterized
    reference ({"param_ref": "member_id"}) share one field rather than needing two, so replay
    has a single, obvious place to resolve inputs.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TargetSpec(BaseModel):
    app_name: str
    entry_point: str
    surface_type: Literal["web", "legacy_web", "desktop"] = "legacy_web"


class LocatorTarget(BaseModel):
    strategy: Literal["role_name", "structural", "text", "table_position"]
    primary: dict = Field(
        ...,
        description=(
            'e.g. {"role": "button", "name": "Go"}, or for strategy="table_position" '
            '{"table_headers": [...], "row_index": 0, "column_index": 2} — a data-table '
            "cell with no per-row label has nothing stable to anchor on except its own value, "
            "which is exactly what changes between replays, so it's addressed by position "
            "(which table, by its column headers; which row; which column) instead."
        ),
    )
    fallbacks: list[dict] = Field(default_factory=list)
    reasoning: str = Field(..., description="why this locator was chosen, for human review")


class ExpectedOutcome(BaseModel):
    condition: str = Field(..., description="e.g. \"page contains 'No member record found'\"")
    classification: Literal["business_outcome", "recoverable", "hard_failure"]
    code: str | None = Field(default=None, description='e.g. "MEMBER_NOT_FOUND"')
    handling: str | None = Field(default=None, description='e.g. "dismiss and retry"')


class WaitPolicy(BaseModel):
    timeout_ms: int = 5000
    retry_count: int = 2
    retry_on: list[str] = Field(default_factory=lambda: ["transient_load"])


class Step(BaseModel):
    step_id: str
    action_type: Literal[
        "navigate", "click", "type", "select", "wait_for", "extract", "assert_checkpoint"
    ]
    target: LocatorTarget | None = None
    value: str | dict | None = Field(
        default=None, description='literal value OR {"param_ref": "member_id"}'
    )
    extract_as: str | None = None
    wait_policy: WaitPolicy = Field(default_factory=WaitPolicy)
    expected_outcomes: list[ExpectedOutcome] = Field(default_factory=list)


class Checkpoint(BaseModel):
    type: Literal["element_present", "text_match", "url_match"]
    locator: dict | None = None
    expected: str


class Capability(BaseModel):
    capability_id: str
    version: str = Field(..., description="semver, e.g. 1.0.0")
    created_from_run_id: str
    description: str = Field(
        default="",
        description=(
            "human/agent-readable summary of what this capability does — the discovery goal it "
            "was recorded from, verbatim. This plus input_schema is what an AI agent sees when "
            "choosing which capability to call (agent_interface/catalog.py), so it needs to be "
            "a real natural-language description, not a slug."
        ),
    )
    target: TargetSpec
    risk_level: Literal["safe", "risky"]
    input_schema: dict = Field(..., description="JSON-schema-like typed inputs")
    output_schema: dict = Field(..., description="JSON-schema-like typed outputs")
    checkpoint: Checkpoint
    steps: list[Step]


class Result(BaseModel):
    status: Literal[
        "success", "business_outcome", "recoverable_handled", "hard_failure", "escalated"
    ]
    outputs: dict = Field(default_factory=dict)
    business_outcome_code: str | None = None
    failure_detail: dict | None = Field(
        default=None, description="{step_id, expected, observed, screenshot_ref}"
    )
    evidence_ref: str | None = None
