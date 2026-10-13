"""PII detection and redaction, applied before text reaches the model, the logs or the user.

Detectors are deliberately conservative (checksums where they exist) to avoid mangling
order numbers and dates. Extend DETECTORS for your domain.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def _luhn(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def _ssn_ok(s: str) -> bool:
    a, b, c = s.split("-")
    return a not in {"000", "666"} and not a.startswith("9") and b != "00" and c != "0000"


@dataclass(frozen=True)
class Detector:
    kind: str
    pattern: re.Pattern[str]
    check: Callable[[str], bool] | None = None


DETECTORS = [
    Detector("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    Detector("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b"), _luhn),
    Detector("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), _ssn_ok),
    Detector("PHONE", re.compile(r"(?<!\w)\+?\d{1,2}[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b")),
    Detector("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
]


@dataclass
class Finding:
    kind: str
    start: int
    end: int


def find(text: str, kinds: set[str] | None = None) -> list[Finding]:
    found: list[Finding] = []
    taken: list[tuple[int, int]] = []
    for det in DETECTORS:
        if kinds and det.kind not in kinds:
            continue
        for m in det.pattern.finditer(text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            if det.check and not det.check(m.group()):
                continue
            found.append(Finding(det.kind, s, e))
            taken.append((s, e))
    return sorted(found, key=lambda f: f.start)


def redact(text: str, kinds: set[str] | None = None) -> tuple[str, list[Finding]]:
    findings = find(text, kinds)
    out, last = [], 0
    for f in findings:
        out.append(text[last : f.start])
        out.append(f"[{f.kind}]")
        last = f.end
    out.append(text[last:])
    return "".join(out), findings


def redact_obj(obj: Any, kinds: set[str] | None = None) -> tuple[Any, int]:
    """Redact every string inside a JSON-like structure. Returns (copy, n_findings)."""
    if isinstance(obj, str):
        red, f = redact(obj, kinds)
        return red, len(f)
    if isinstance(obj, dict):
        total, out = 0, {}
        for k, v in obj.items():
            out[k], n = redact_obj(v, kinds)
            total += n
        return out, total
    if isinstance(obj, list):
        items = [redact_obj(v, kinds) for v in obj]
        return [i[0] for i in items], sum(i[1] for i in items)
    return obj, 0
