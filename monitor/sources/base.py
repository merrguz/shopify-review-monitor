"""Source adapter contract.

Adding a review source means adding one module here that returns
`(reviews, health)` and registering it in `sources/__init__.py`. Nothing
downstream (state, Clay, Slack) needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol

from ..models import Review


@dataclass
class SourceHealth:
    """Per-target health, consumed by the dead-man's switch.

    The distinction that matters: `total_reviews_seen` is *all* reviews visible
    on the page, not just negative ones. Zero new negative reviews is the normal
    case. Zero total reviews means the parser broke.
    """

    source: str
    target: str                      # app handle / company slug
    ok: bool = True
    total_reviews_seen: int = 0
    negative_expected: int = 0       # from the page's own rating buckets, if available
    negative_parsed: int = 0
    notes: List[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        self.notes.append(msg)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.notes.append(msg)


class Source(Protocol):
    name: str

    def collect(self, target: dict, defaults: dict) -> tuple[List[Review], SourceHealth]:
        ...
