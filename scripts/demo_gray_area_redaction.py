"""
Evidence generator for the sink-aware gray-area redaction (guardrails/pii.py + policy.redact).

Takes one realistic discovery observation — a member-detail page with a name, address, email
and phone in it — and shows the SAME payload redacted three different ways depending on where
it's headed:

  - artifact   : nothing removed (declared outputs are supposed to carry this)
  - evidence   : gray-area entities tokenized (<PERSON_1>...), reversibly
  - llm_prompt : gray-area entities + ZIP / long-digit runs hard-masked (one-way door)

Prints the RedactionReport for each and the reversible token map for the evidence sink. Writes
nothing to /evidence/ or /capabilities/.

Run: python scripts/demo_gray_area_redaction.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from guardrails.pii import BACKEND
from guardrails.policy import redact_with_report

OBSERVATION = {
    "url": "http://localhost:5050/member/12345",
    "last_action_result": "clicked link 'View'",
    "accessibility_tree": {
        "role": "document",
        "children": [
            {"role": "heading", "name": "Member 12345"},
            {"role": "row", "name": "Name Dana Whitfield"},
            {"role": "row", "name": "Email dana.whitfield@example.com"},
            {"role": "row", "name": "Phone (415) 555-0132"},
            {"role": "row", "name": "Mailing address 412 Birchwood Lane, Madison WI 53703"},
            {"role": "row", "name": "Savings Balance $1,842.30"},
        ],
    },
}


def _show(sink: str) -> None:
    redacted, report, token_map = redact_with_report(OBSERVATION, sink=sink)
    print(f"\n=== sink = {sink!r} ===")
    print(report.summary_line())
    for row in redacted["accessibility_tree"]["children"]:
        if row.get("name"):
            print(f"    {row['name']}")
    if report.low_confidence:
        print(f"  low-confidence, flagged for review: {report.low_confidence}")
    if token_map:
        print(f"  reversible token map: {json.dumps(token_map)}")


def main() -> None:
    print(f"gray-area detection backend: {BACKEND}")
    if BACKEND != "presidio":
        print("(install requirements-pii.txt for PERSON/LOCATION NER; regex fallback covers "
              "email / phone / street address / ZIP)")

    for sink in ("artifact", "evidence", "llm_prompt"):
        _show(sink)

    # the property that matters: the artifact keeps the balance (a declared output), the
    # llm_prompt sink still keeps it too (it's not gray-area PII) but drops the identifiers.
    _, _, _ = redact_with_report(OBSERVATION, sink="llm_prompt")
    print("\nKey point: 'Savings Balance $1,842.30' survives every sink — it's a declared "
          "output, not an identifier. Only the who/where fields change by destination.")


if __name__ == "__main__":
    main()
