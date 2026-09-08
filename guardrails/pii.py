"""
Gray-area PII handling — the layer above `policy.redact`'s two hard passes (secret-shaped keys,
SSN/card-number value shapes).

The gray area is everything that is *sometimes* fine and *sometimes* a leak: a customer name, a
street address, an email, a phone number. A name legitimately belongs in a capability's declared
outputs (redacting it there would break the system's whole purpose — see policy.py's docstring),
but the same name must never be in a page observation shipped to a third-party LLM during
discovery. So the decision can't be "redact names: yes/no" — it has to be keyed on where the
data is going. That's what `sink` is:

  - "artifact"    : the compiled Capability on disk. Hard passes only; gray-area PASSES THROUGH
                    (declared outputs are the point). Identical to the original redact().
  - "evidence"    : transcripts / failure detail on disk. Gray-area is TOKENIZED
                    (`<PERSON_1>`) so a reviewer can still follow the flow, and a sidecar map
                    can reverse it under audit.
  - "llm_prompt"  : anything about to be sent to the model. Gray-area is HARD-MASKED, plus
                    ZIP-shaped and 6+ digit runs, because this is the one-way door.

Detection backend: Microsoft Presidio if installed (`pip install -r requirements-pii.txt`),
giving real NER for PERSON/LOCATION and confidence scores; otherwise a regex fallback that
covers EMAIL_ADDRESS / PHONE_NUMBER / STREET_ADDRESS / US_ZIP but not PERSON (honest about the
gap rather than pretending a regex finds names). Either way every run produces a
`RedactionReport`: counts per entity type, the backend used, and anything found at low
confidence (0.4-0.7) — flagged for human review instead of silently kept or silently dropped,
which is the whole point of calling it a "gray area."
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from common.logging import get_logger

_log = get_logger("pii")

Sink = Literal["artifact", "evidence", "llm_prompt"]

# score bands
_REVIEW_LOW = 0.40
_REVIEW_HIGH = 0.70

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"
)
_ZIP_RE = re.compile(r"(?<!\d)\d{5}(?:-\d{4})?(?!\d)")
_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d{6,}(?!\d)")
_STREET_RE = re.compile(
    r"\d{1,6}\s+(?:[A-Z][A-Za-z]+\.?\s+){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Court|Ct|Way|Boulevard|Blvd|Terrace|Ter|Place|Pl)"
    # optional ", City ST 12345" tail so a full mailing address is one span
    r"(?:,?\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?,?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?)?",
)


@dataclass
class RedactionReport:
    """What a single sink-aware redaction pass did. Cheap to log alongside every run."""
    sink: str
    backend: str
    by_type: dict[str, int] = field(default_factory=dict)
    low_confidence: list[dict] = field(default_factory=list)
    # numerics deliberately left intact (see scrub_text) so a reviewer can still confirm them
    flagged_not_masked: list[dict] = field(default_factory=list)

    def _count(self, entity_type: str) -> None:
        self.by_type[entity_type] = self.by_type.get(entity_type, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.by_type.values())

    def as_dict(self) -> dict:
        return {
            "sink": self.sink,
            "backend": self.backend,
            "total_redacted": self.total,
            "by_type": dict(sorted(self.by_type.items())),
            "low_confidence_for_review": self.low_confidence,
            "flagged_not_masked": self.flagged_not_masked,
        }

    def summary_line(self) -> str:
        if not self.total:
            return f"[redaction:{self.sink}] nothing to redact (backend={self.backend})"
        parts = ", ".join(f"{n} {t}" for t, n in sorted(self.by_type.items()))
        tail = ""
        if self.low_confidence:
            tail = f"; {len(self.low_confidence)} low-confidence flagged for review"
        return f"[redaction:{self.sink}] {self.total} redacted ({parts}){tail}, backend={self.backend}"


def _load_presidio():
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore
    except Exception:
        return None
    try:
        return AnalyzerEngine()
    except Exception:
        # presidio importable but its spaCy model isn't downloaded — treat as unavailable
        return None


_ANALYZER = _load_presidio()
BACKEND = "presidio" if _ANALYZER is not None else "regex-fallback"

# Entities we act on. Presidio names on the left; the regex fallback fills the subset it can.
_GRAY_AREA = ("PERSON", "LOCATION", "STREET_ADDRESS", "EMAIL_ADDRESS", "PHONE_NUMBER", "US_ZIP")


class _Tokenizer:
    """Stable `<TYPE_n>` placeholders within one redaction pass, so a tokenized transcript is
    still internally consistent (the same email is the same token everywhere) and reversible via
    the returned map."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self.map: dict[str, str] = {}

    def token_for(self, entity_type: str, value: str) -> str:
        if value in self.map:
            return self.map[value]
        self._counters[entity_type] = self._counters.get(entity_type, 0) + 1
        token = f"<{entity_type}_{self._counters[entity_type]}>"
        self.map[value] = token
        return token


def _spans_presidio(text: str) -> list[tuple[int, int, str, float]]:
    results = _ANALYZER.analyze(text=text, language="en", entities=list(_GRAY_AREA))
    return [(r.start, r.end, r.entity_type, float(r.score)) for r in results]


def _spans_regex(text: str) -> list[tuple[int, int, str, float]]:
    # ZIP and long-digit runs are deliberately NOT here — too ambiguous to touch in the
    # evidence sink; scrub_text adds them only for sink="llm_prompt".
    spans: list[tuple[int, int, str, float]] = []
    for m in _EMAIL_RE.finditer(text):
        spans.append((m.start(), m.end(), "EMAIL_ADDRESS", 1.0))
    for m in _PHONE_RE.finditer(text):
        spans.append((m.start(), m.end(), "PHONE_NUMBER", 0.85))
    for m in _STREET_RE.finditer(text):
        spans.append((m.start(), m.end(), "STREET_ADDRESS", 0.6))
    return spans


def _resolve_overlaps(spans: list[tuple[int, int, str, float]]) -> list[tuple[int, int, str, float]]:
    # keep the highest-score span for any overlapping region; process left-to-right
    spans = sorted(spans, key=lambda s: (s[0], -(s[3]), -(s[1] - s[0])))
    out: list[tuple[int, int, str, float]] = []
    last_end = -1
    for start, end, etype, score in spans:
        if start >= last_end:
            out.append((start, end, etype, score))
            last_end = end
    return out


def scrub_text(
    text: str, sink: Sink, report: RedactionReport, tokenizer: _Tokenizer | None
) -> str:
    """Redact gray-area entities in one string according to `sink`. Hard-mask for llm_prompt,
    tokenize for evidence. `artifact` never calls this."""
    if not text or not isinstance(text, str):
        return text

    spans = _spans_presidio(text) if _ANALYZER is not None else _spans_regex(text)

    # Ambiguous bare numerics (a lone 5-digit ZIP, a long digit run) are NOT masked, in any
    # sink: a computer-use agent navigates by exactly these — masking "member 12345" would
    # break discovery. They're recorded on the report as "flagged, left intact" so a reviewer
    # can eyeball them and confirm the target really is a non-prod instance.
    if sink == "llm_prompt":
        covered = {(s[0], s[1]) for s in spans}
        for rx, label in ((_ZIP_RE, "ZIP-shaped"), (_DIGIT_RUN_RE, "long-digit-run")):
            for m in rx.finditer(text):
                if not any(a <= m.start() < b for a, b in covered):
                    report.flagged_not_masked.append(
                        {"kind": label, "preview": m.group()[:2] + "…"}
                    )

    spans = _resolve_overlaps([s for s in spans if s[3] >= _REVIEW_LOW])
    if not spans:
        return text

    pieces = []
    cursor = 0
    for start, end, etype, score in spans:
        pieces.append(text[cursor:start])
        original = text[start:end]
        if sink == "evidence" and tokenizer is not None:
            pieces.append(tokenizer.token_for(etype, original))
        else:
            pieces.append(f"<{etype}:REDACTED>")
        cursor = end
        report._count(etype)
        if _REVIEW_LOW <= score < _REVIEW_HIGH:
            report.low_confidence.append(
                {"type": etype, "preview": (original[:2] + "…") if len(original) > 3 else "…",
                 "score": round(score, 2)}
            )
    pieces.append(text[cursor:])
    return "".join(pieces)


def scrub(obj, sink: Sink, report: RedactionReport, tokenizer: _Tokenizer | None):
    """Recurse a dict/list/str structure, applying `scrub_text` to every string. Returns a new
    object; never mutates the input."""
    if isinstance(obj, dict):
        return {k: scrub(v, sink, report, tokenizer) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, sink, report, tokenizer) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj, sink, report, tokenizer)
    return obj


def scrub_gray_area(obj, sink: Sink) -> tuple[object, RedactionReport, dict[str, str]]:
    """Entry point. Returns (redacted_obj, report, token_map). token_map is empty unless
    sink == 'evidence'."""
    report = RedactionReport(sink=sink, backend=BACKEND)
    tokenizer = _Tokenizer() if sink == "evidence" else None
    scrubbed = scrub(obj, sink, report, tokenizer)
    return scrubbed, report, (tokenizer.map if tokenizer else {})


_REVIEW_QUEUE = Path(__file__).parent.parent / "evidence" / "redaction_review.jsonl"


def append_review(report: RedactionReport, context: dict | None = None) -> None:
    """
    The low-confidence band (and anything flagged-not-masked) is only useful if something acts
    on it. Append those items to evidence/redaction_review.jsonl so a human (or a periodic job)
    can work the queue: confirm a real leak was caught, or tune the detector. Never raises —
    a review-queue write failing must not fail the run that produced it.
    """
    if not report.low_confidence and not report.flagged_not_masked:
        return
    try:
        _REVIEW_QUEUE.parent.mkdir(exist_ok=True)
        row = {
            "ts": time.time(),
            "sink": report.sink,
            "backend": report.backend,
            "low_confidence": report.low_confidence,
            "flagged_not_masked": report.flagged_not_masked,
            **(context or {}),
        }
        with _REVIEW_QUEUE.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as exc:
        _log.warning("redaction_review_write_failed", error=str(exc))
