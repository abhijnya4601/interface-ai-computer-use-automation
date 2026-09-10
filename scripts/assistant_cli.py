"""
CLI for the assistant -- the same "plan a request into tasks, reuse a capability where one
exists, discover one where it doesn't" flow the console's Chatbot tab runs, without a browser
UI. It is a thin wrapper over agent_interface.assistant.run().

    # mock bank on :5050 (make app), then:
    export ANTHROPIC_API_KEY=sk-ant-...        # or OPENAI_API_KEY / GEMINI_API_KEY
    python scripts/assistant_cli.py "look up member 12345 and read the name and the balance"

    python scripts/assistant_cli.py "read the member status for 12345" \\
        --target http://localhost:5051/find --app-name hostile-dom

A task with a matching capability replays deterministically (no model call); a task with none
triggers a discovery run and the new capability is saved for next time. Prints the merged
structured outputs + a one-line answer as JSON; --stream also prints progress to stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_interface.assistant import run


def main() -> int:
    ap = argparse.ArgumentParser(description="run one plain-English request via the assistant")
    ap.add_argument("request", help="what you want done, in plain English")
    ap.add_argument("--target", default="http://localhost:5050/search",
                    help="entry URL of the target app (default: the mock bank)")
    ap.add_argument("--app-name", default="mock-core-banking",
                    help="app_knowledge key for the target (default: mock-core-banking)")
    ap.add_argument("--provider", default="auto",
                    choices=["auto", "anthropic", "openai", "gemini"],
                    help="force a model provider (default: auto-detect from the key)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--confirm", action="store_true",
                    help="allow a risky (state-changing) capability to execute (no operator here)")
    ap.add_argument("--stream", action="store_true", help="print progress to stderr as it runs")
    args = ap.parse_args()

    def _on_event(e: dict) -> None:
        if args.stream:
            body = {k: v for k, v in e.items() if k != "type"}
            print(f"  · {e.get('type')}: {json.dumps(body, default=str)[:140]}", file=sys.stderr)

    res = run(args.request, target_url=args.target, app_name=args.app_name,
              provider=args.provider, headless=not args.headed, confirm=args.confirm,
              on_event=_on_event)

    print(json.dumps({
        "status": res.status,
        "answer": res.answer,
        "outputs": res.outputs,
        "tasks": [vars(t) for t in res.tasks],
        "learned": res.learned,
        "business_outcome_code": res.business_outcome_code,
    }, indent=2, default=str))
    return 0 if res.status in ("success", "business_outcome") else 1


if __name__ == "__main__":
    raise SystemExit(main())
