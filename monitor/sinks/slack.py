"""Slack sink: one message per new negative review, plus health alerts.

Uses an incoming webhook URL (SLACK_WEBHOOK_URL). Simplest thing that works and
needs no OAuth app.
"""

from __future__ import annotations

import logging
from typing import List

import requests

from ..models import Review
from ..sources.base import SourceHealth

log = logging.getLogger(__name__)

STARS = {1: ":red_circle: 1", 2: ":red_circle: 2", 3: ":large_yellow_circle: 3"}
BUCKET_LABEL = {
    "overlay": "overlay vendor",
    "code-fix": "code-fix competitor",
    "scanner": "scanner",
    "alt-text": "alt-text tool",
    "gdpr-bundle": "GDPR bundle",
}


class SlackSink:
    def __init__(self, webhook_url: str, dry_run: bool = False):
        self.webhook_url = webhook_url
        self.dry_run = dry_run or not webhook_url

    def _post(self, payload: dict) -> bool:
        if self.dry_run:
            log.info("[dry-run] would post to Slack: %s", payload.get("text", "")[:300])
            return True
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=15)
            if resp.status_code >= 300:
                log.warning("Slack rejected: %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Slack post failed: %s", exc)
            return False

    def notify_reviews(self, reviews: List[Review]) -> None:
        for r in reviews:
            stars = STARS.get(r.rating, f":star: {r.rating}")
            bucket = BUCKET_LABEL.get(r.bucket, r.bucket)
            body = r.review_text.strip()
            if len(body) > 1200:
                body = body[:1200].rstrip() + "..."

            lines = [
                f"*{stars}-star review of {r.competitor_name}*  _({bucket})_",
                f"*{r.reviewer_store_name or 'unknown store'}*"
                + (f" - {r.country}" if r.country else "")
                + (f" - {r.used_app_for}" if r.used_app_for else ""),
                "",
                f"> {body.replace(chr(10), chr(10) + '> ')}",
            ]
            if r.developer_reply:
                lines.append(f"_Vendor replied {r.developer_reply_date or ''}_")
            lines.append(f"<{r.review_url}|View on the Shopify App Store>")

            self._post({
                "text": f"{r.rating}-star review of {r.competitor_name} by "
                        f"{r.reviewer_store_name or 'unknown store'}",
                "blocks": [
                    {"type": "section",
                     "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
                ],
            })

    def notify_health(self, unhealthy: List[SourceHealth]) -> None:
        """Dead-man's switch. Fires when the scraper looks broken, not when
        there's simply nothing new -- that distinction is the whole point."""
        if not unhealthy:
            return
        lines = [":warning: *Review monitor needs attention*", ""]
        for h in unhealthy[:20]:
            status = "FAILED" if not h.ok else "warning"
            lines.append(f"- `{h.source}/{h.target}` {status}: {'; '.join(h.notes)}")
        if len(unhealthy) > 20:
            lines.append(f"_...and {len(unhealthy) - 20} more_")
        lines.append("")
        lines.append(
            "If several apps failed at once, Shopify probably changed its markup. "
            "Run `python -m monitor doctor` to see which parse strategy is matching."
        )
        self._post({
            "text": "Review monitor needs attention",
            "blocks": [{"type": "section",
                        "text": {"type": "mrkdwn", "text": "\n".join(lines)}}],
        })
