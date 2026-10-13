"""Minimal OpenTelemetry-style spans, stored with each run (and exportable as JSON)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Span:
    name: str
    start_ms: float
    end_ms: float = 0.0
    attrs: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    @property
    def duration_ms(self) -> float:
        return round(self.end_ms - self.start_ms, 2)


@dataclass
class Tracer:
    spans: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        s = Span(name, time.perf_counter() * 1000, attrs=dict(attrs))
        try:
            yield s
        except Exception as e:
            s.status = f"error: {type(e).__name__}"
            raise
        finally:
            s.end_ms = time.perf_counter() * 1000
            self.spans.append(s)

    def export(self) -> list[dict[str, Any]]:
        return [{**asdict(s), "duration_ms": s.duration_ms} for s in self.spans]
