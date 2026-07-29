"""Canonical review record shared by every source adapter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")


def _parse_date(raw: str) -> Optional[str]:
    """Normalize a human date string to ISO YYYY-MM-DD. Returns None if unparseable."""
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@dataclass
class Review:
    """One review from any source.

    `review_id` is the dedupe key. Prefer a real platform ID; fall back to a
    content hash so the same review never re-fires even without one.
    """

    source: str                      # "shopify" | "trustpilot" | "g2"
    competitor_handle: str
    competitor_name: str
    bucket: str
    rating: int
    review_date: Optional[str]       # ISO date
    review_text: str
    reviewer_store_name: str
    country: str = ""
    used_app_for: str = ""
    developer_reply: str = ""
    developer_reply_date: Optional[str] = None
    review_url: str = ""
    platform_review_id: Optional[str] = None
    first_seen_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def review_id(self) -> str:
        if self.platform_review_id:
            return f"{self.source}:{self.competitor_handle}:{self.platform_review_id}"
        # Content hash fallback. Deliberately excludes `used_app_for`, which
        # ticks upward over time ("3 days" -> "About 1 month") and would
        # otherwise make the same review look new on every run.
        basis = "|".join(
            [
                self.source,
                self.competitor_handle,
                self.review_date or "",
                str(self.rating),
                _norm(self.reviewer_store_name),
                _norm(self.review_text)[:200],
            ]
        )
        digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
        return f"{self.source}:{self.competitor_handle}:h{digest}"

    def to_payload(self) -> dict:
        d = asdict(self)
        d["review_id"] = self.review_id
        return d


def _norm(s: str) -> str:
    """Collapse whitespace and lowercase, so trivial reflows don't change the hash."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


__all__ = ["Review", "_parse_date"]
