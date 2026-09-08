"""
Multi-tenant capability reuse via **base + per-tenant patch** (REPORT §4), made concrete.

Most tenants running the same vendor product replay the base `Capability` unmodified. A tenant
whose instance differs — a rebrand, a renamed field, one extra confirmation step — gets a small
patch applied over the base at load time, instead of a full separate artifact per tenant.

    base = Capability.model_validate_json(Path("capabilities/lookup_member_balance.v1.json").read_text())
    acme = apply_patch(base, json.loads(Path("capabilities/tenants/acme.patch.json").read_text()))

A patch is a partial `Capability`-shaped dict. Top-level keys deep-merge onto the base;
`steps` is special — keyed by `step_id`, each value merges into that one step:

    {
      "tenant": "acme",
      "target": {"entry_point": "https://acme.bank.example/search"},
      "checkpoint": {"locator": {"name": "Current Savings Balance"}},
      "steps": {
        "s5": {"target": {"primary": {"name": "Current Savings Balance"},
                          "reasoning": "acme renamed this row header"}}
      }
    }

Drift detection reuses the replay tier log (REPORT §3): a tier-2/3 spike for one tenant means
either the base needs updating or that tenant needs a (bigger) patch — without touching the
others.
"""
from __future__ import annotations

import copy

from artifact.schema import Capability

_PATCH_META_KEYS = {"tenant", "note"}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_patch(base: Capability, patch: dict) -> Capability:
    """Return a new Capability = base with `patch` applied. Never mutates `base`. Raises
    `pydantic.ValidationError` if the result isn't a valid Capability (a patch can't produce a
    structurally broken artifact silently)."""
    data = base.model_dump()
    step_overrides = {}

    for key, value in patch.items():
        if key in _PATCH_META_KEYS:
            continue
        if key == "steps":
            if not isinstance(value, dict):
                raise TypeError("patch 'steps' must be a {step_id: partial-step} mapping")
            step_overrides = value
            continue
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = _deep_merge(data[key], value)
        else:
            data[key] = value

    if step_overrides:
        by_id = {s["step_id"]: s for s in data["steps"]}
        unknown = set(step_overrides) - set(by_id)
        if unknown:
            raise KeyError(f"patch targets step id(s) not in the base capability: {sorted(unknown)}")
        data["steps"] = [
            _deep_merge(s, step_overrides[s["step_id"]]) if s["step_id"] in step_overrides else s
            for s in data["steps"]
        ]

    patched = Capability.model_validate(data)
    # a patched capability is a fresh artifact for that tenant — it hasn't been verified against
    # that tenant's instance yet.
    return patched.model_copy(update={
        "lifecycle": "draft",
        "verification": None,
        "description": f"{base.description}".strip() or base.description,
    })


def patch_summary(base: Capability, patch: dict) -> list[str]:
    """Human-readable list of what a patch changes, for a review step."""
    changes = []
    for key, value in patch.items():
        if key in _PATCH_META_KEYS:
            continue
        if key == "steps":
            for sid, override in value.items():
                changes.append(f"step {sid}: {', '.join(sorted(override))}")
        else:
            changes.append(f"{key}: {value!r}")
    return changes
