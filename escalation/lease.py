"""
The lease is the entire "who's in control" model: exactly one of {automation, human} at any
time, plus whatever context explains why it's currently that way. Deliberately this small —
see escalation/controller.py's docstring for why a file-backed lease is the right amount of
infrastructure here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Lease:
    state: Literal["automation", "human"] = "automation"
    context: dict = field(default_factory=dict)
