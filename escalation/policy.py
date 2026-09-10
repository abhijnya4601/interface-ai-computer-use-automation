"""
Declarative escalation policy - the rules that decide, before a step runs, whether to proceed,
pause for a human, or block.

Two things ask "should this stop for a person?":

  * In **discovery** the model can voluntarily call `escalate()` (its system prompt tells it not
    to take irreversible actions itself). That is the agent's own judgment.
  * This engine is the **floor under that judgment**, and in **replay** - where there is no
    model - it is the *only* gate. It runs against `(capability, params, step)` before every
    state-changing step and returns `allow` / `escalate` / `block`.

Rules live in `escalation/rules.yaml` and are editable at runtime (the web console writes the
file). First matching rule wins; nothing matching means `allow`. A rule is:

    - id: pause-account-opens
      description: opening an account always needs a human
      when:
        capability_id: open_subaccount          # exact, or a list
        risk_level: risky                        # safe | risky
        target_app: mock-core-banking
        step_action: [click, type]              # exact, or a list
        step_name_contains: [Confirm, Submit]   # step's accessible name contains ANY (ci)
        param: {name: opening_deposit, op: gt, value: 100}
      then: escalate                            # escalate | block | allow

All `when` keys are ANDed; omit the ones you don't care about.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

RULES_PATH = Path(__file__).parent / "rules.yaml"

_ACTIONS = ("allow", "escalate", "block")


@dataclass
class Decision:
    action: str          # "allow" | "escalate" | "block"
    reason: str
    rule_id: str | None   # which rule matched, or None for the default


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _match_param(clause: dict, params: dict) -> bool:
    name = clause.get("name")
    if name is None or name not in params:
        return False
    op = (clause.get("op") or "eq").lower()
    want = clause.get("value")
    got = params[name]
    try:
        if op in ("gt", "lt", "gte", "lte"):
            g, w = float(got), float(want)
            return {"gt": g > w, "lt": g < w, "gte": g >= w, "lte": g <= w}[op]
    except (TypeError, ValueError):
        return False
    if op == "contains":
        return str(want).lower() in str(got).lower()
    if op == "ne":
        return str(got) != str(want)
    return str(got) == str(want)


def _rule_matches(when: dict, ctx: dict) -> bool:
    step = ctx.get("step") or {}
    step_name = (step.get("name") or "")

    if "capability_id" in when and ctx.get("capability_id") not in _as_list(when["capability_id"]):
        return False
    if "risk_level" in when and ctx.get("risk_level") != when["risk_level"]:
        return False
    if "target_app" in when and ctx.get("target_app") not in _as_list(when["target_app"]):
        return False
    if "step_action" in when and step.get("action_type") not in _as_list(when["step_action"]):
        return False
    if "step_name_contains" in when:
        needles = [str(n).lower() for n in _as_list(when["step_name_contains"])]
        if not any(n in step_name.lower() for n in needles):
            return False
    return not ("param" in when and not _match_param(when["param"], ctx.get("params") or {}))


def load_rules(path: Path | None = None) -> list[dict]:
    p = path or RULES_PATH
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text()) or {}
    return [r for r in (data.get("rules") or []) if isinstance(r, dict) and r.get("then") in _ACTIONS]


def save_rules(rules: list[dict], path: Path | None = None) -> None:
    p = path or RULES_PATH
    p.parent.mkdir(exist_ok=True)
    p.write_text(yaml.safe_dump({"rules": rules}, sort_keys=False, width=100))


def evaluate(ctx: dict, rules: list[dict] | None = None) -> Decision:
    """`ctx` = {capability_id, risk_level, target_app, step:{action_type,name}, params}.
    First matching rule wins; no match -> allow."""
    for rule in (rules if rules is not None else load_rules()):
        if _rule_matches(rule.get("when") or {}, ctx):
            return Decision(rule["then"], rule.get("description") or f"rule {rule.get('id')}",
                            rule.get("id"))
    return Decision("allow", "no rule matched", None)
