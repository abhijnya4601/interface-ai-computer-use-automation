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


Provenance = Literal["curated", "proposed"]
"""Where a rule came from. `curated` = ratified domain knowledge loaded from
`app_knowledge/<app>.yaml` (a human has signed off). `proposed` = the discovery agent suggested
it from what it explored this run; replay uses it (it's a real signal), but the capability's
`lifecycle` stays `draft` until a reviewer promotes it — see scripts/review_capability.py."""


class ExpectedOutcome(BaseModel):
    condition: str = Field(..., description="e.g. \"page contains 'No member record found'\"")
    classification: Literal[
        "business_outcome", "recoverable", "hard_failure", "data_unavailable"
    ]
    code: str | None = Field(default=None, description='e.g. "MEMBER_NOT_FOUND"')
    handling: str | None = Field(default=None, description='e.g. "dismiss and retry"')
    provenance: Provenance = "curated"


class ExtractContract(BaseModel):
    """
    The shape an `extract` step's value must have to count as real data. Checked
    deterministically by replay right after the extraction: a resolved locator that pulls an
    empty string, a placeholder ("--", "N/A"), or a value that doesn't match `pattern` means the
    page rendered but the data behind it did not — replay returns `data_unavailable`, not
    `success` with a junk value and not `hard_failure` (nothing is broken; the datum just isn't
    there). Authored at compile time from domain knowledge of the target app, same as
    `ExpectedOutcome` — curated in app_knowledge/<app>.yaml or proposed by discovery (see
    `provenance`).
    """
    pattern: str | None = Field(
        default=None,
        description=r'regex the value must fullmatch, e.g. r"\$[\d,]+\.\d{2}" for a currency amount',
    )
    nonempty: bool = Field(
        default=True, description="reject an empty / whitespace-only extracted value"
    )
    placeholders: list[str] = Field(
        default_factory=list,
        description='literal strings that mean "no data here", e.g. ["--", "N/A", "Not available"]',
    )
    reason: str = Field(
        default="",
        description="why this contract — for the human reviewer, same spirit as LocatorTarget.reasoning",
    )
    provenance: Provenance = "curated"


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
    ready_when: str | None = Field(
        default=None,
        description=(
            'optional readiness gate, same literal form as ExpectedOutcome.condition — e.g. '
            '"page contains \'Savings Balance\'". The step waits for this marker to appear '
            "before acting; if it never does within wait_policy.timeout_ms, replay returns "
            "`data_unavailable` (the shell loaded, the data region never populated) rather than "
            "acting on a half-rendered page and calling a miss a hard failure."
        ),
    )
    extract_contract: ExtractContract | None = Field(
        default=None,
        description="for extract steps: the shape the value must have to count as real data",
    )


class Checkpoint(BaseModel):
    type: Literal["element_present", "text_match", "url_match"]
    locator: dict | None = None
    expected: str
    provenance: Provenance = "curated"


class VerificationRecord(BaseModel):
    """Result of running every declared outcome branch against the live app (see
    replay.verify.verify_capability). A capability with `lifecycle="verified"` must carry one
    of these whose `all_passed` is True."""
    ran_at: float
    all_passed: bool
    scenarios: list[dict] = Field(
        default_factory=list,
        description="[{params, expected_status, expected_code, observed_status, observed_code, ok}]",
    )
    evidence_refs: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    schema_version: str = Field(
        default="1.1",
        description="artifact schema version. Bump when a field's meaning changes; a loader can "
        "migrate older artifacts forward (see artifact/migrate.py).",
    )
    lifecycle: Literal["draft", "verified", "published", "deprecated"] = Field(
        default="draft",
        description="draft = freshly compiled, may carry `proposed` rules a human hasn't ratified; "
        "verified = every declared branch replayed clean (carries a VerificationRecord); "
        "published = cleared for production callers; deprecated = superseded.",
    )
    verification: VerificationRecord | None = None
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

    def unratified_rules(self) -> list[str]:
        """Human-readable list of every rule on this capability that the discovery agent
        proposed and a reviewer has NOT yet promoted to `curated`. Non-empty ⇒ this capability
        should stay `lifecycle="draft"` until scripts/review_capability.py clears it."""
        out: list[str] = []
        if self.checkpoint.provenance == "proposed":
            out.append("checkpoint")
        for step in self.steps:
            for o in step.expected_outcomes:
                if o.provenance == "proposed":
                    out.append(f"{step.step_id}: expected_outcome {o.code or o.condition!r}")
            if step.extract_contract and step.extract_contract.provenance == "proposed":
                out.append(f"{step.step_id}: extract_contract for {step.extract_as!r}")
        return out


class Result(BaseModel):
    status: Literal[
        "success",
        "business_outcome",
        "recoverable_handled",
        "data_unavailable",
        "hard_failure",
        "escalated",
    ]
    outputs: dict = Field(default_factory=dict)
    business_outcome_code: str | None = None
    failure_detail: dict | None = Field(
        default=None, description="{step_id, expected, observed, screenshot_ref}"
    )
    evidence_ref: str | None = None
    idempotent_replay: bool = Field(
        default=False,
        description="True when this Result was replayed from the idempotency ledger rather than "
        "re-executed — a risky capability called twice with the same idempotency_key returns "
        "the first run's outcome instead of, e.g., opening a second sub-account.",
    )
    trace: list[dict] = Field(
        default_factory=list,
        description=(
            "per-step execution record: {step_id, action_type, tier, ms, outcome}. The tier "
            "column is the same drift signal REPORT §3 describes; the ms column makes latency "
            "observable per step. Also written to evidence/replay_<run_id>_trace.jsonl."
        ),
    )
