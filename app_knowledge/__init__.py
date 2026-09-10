"""
Per-app curated domain knowledge, loaded by `target.app_name`, not hardcoded by capability_id.

`app_knowledge/<app_name>.yaml` holds the expected-outcome branches, extract contracts, and
per-capability replay config (risk level + success checkpoint) for one target application.
Onboarding a new app is: point discovery at it, let the agent propose rules
(`provenance: proposed` on the artifact), review, and promote the good ones into a new YAML -
no code change. See REPORT.md §2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from artifact.schema import Checkpoint, ExpectedOutcome, ExtractContract

_KNOWLEDGE_DIR = Path(__file__).parent


@dataclass
class CapabilityConfig:
    risk_level: str | None = None
    checkpoint: Checkpoint | None = None
    verify_scenarios: list[dict] = field(default_factory=list)


@dataclass
class AppKnowledge:
    app_name: str
    _outcomes: dict[str, list[dict]] = field(default_factory=dict)
    _contracts: dict[str, dict[str, ExtractContract]] = field(default_factory=dict)
    _capabilities: dict[str, CapabilityConfig] = field(default_factory=dict)

    def outcome_rules(self, capability_id: str) -> list[dict]:
        """[{match: {...}, outcome: ExpectedOutcome(provenance='curated')}] for this capability."""
        rules = []
        for raw in self._outcomes.get(capability_id, []):
            rules.append({
                "match": dict(raw.get("match", {})),
                "outcome": ExpectedOutcome(
                    condition=raw["condition"],
                    classification=raw["classification"],
                    code=raw.get("code"),
                    handling=raw.get("handling"),
                    provenance="curated",
                ),
            })
        return rules

    def extract_contracts(self, capability_id: str) -> dict[str, ExtractContract]:
        out = {}
        for key, raw in self._contracts.get(capability_id, {}).items():
            out[key] = ExtractContract(
                pattern=raw.get("pattern"),
                nonempty=raw.get("nonempty", True),
                placeholders=list(raw.get("placeholders", [])),
                reason=raw.get("reason", ""),
                provenance="curated",
            )
        return out

    def capability_config(self, capability_id: str) -> CapabilityConfig:
        return self._capabilities.get(capability_id, CapabilityConfig())


def _parse_checkpoint(raw: dict | None) -> Checkpoint | None:
    if not raw:
        return None
    return Checkpoint(
        type=raw["type"], locator=raw.get("locator"), expected=raw["expected"],
        provenance="curated",
    )


def load(app_name: str) -> AppKnowledge:
    """Load `app_knowledge/<app_name>.yaml`. A missing file is not an error - it just means no
    curated knowledge yet (a brand-new target), so everything comes from the agent's proposals
    until a reviewer writes the YAML."""
    # app_name is used as a filename component; keep it to a safe charset.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", app_name)
    path = _KNOWLEDGE_DIR / f"{safe}.yaml"
    if not path.exists():
        return AppKnowledge(app_name=app_name)

    data = yaml.safe_load(path.read_text()) or {}
    caps = {
        cid: CapabilityConfig(
            risk_level=cfg.get("risk_level"),
            checkpoint=_parse_checkpoint(cfg.get("checkpoint")),
            verify_scenarios=list(cfg.get("verify_scenarios") or []),
        )
        for cid, cfg in (data.get("capabilities") or {}).items()
    }
    return AppKnowledge(
        app_name=data.get("app_name", app_name),
        _outcomes=data.get("expected_outcomes") or {},
        _contracts=data.get("extract_contracts") or {},
        _capabilities=caps,
    )
