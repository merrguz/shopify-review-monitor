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

# Slack rejects a section block whose text exceeds 3000 chars with
# `400 invalid_blocks`, and rejects messages with more than 50 blocks. Staying
# well under both is cheaper than discovering the limit in production -- which is
# exactly how the health alert failed on the first real run.
MAX_SECTION_CHARS = 2800
MAX_BLOCKS_PER_MESSAGE = 45

STARS = {1: ":red_circle: 1", 2: ":red_circle: 2", 3: ":large_yellow_circle: 3"}
def _clip(s: str, limit: int) -> str:
    s = s or ""
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


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
                # Include the block sizes: `invalid_blocks` is almost always a
                # length problem, and knowing which chunk was oversized is the
                # whole diagnosis.
                sizes = [
                    len(b.get("text", {}).get("text", ""))
                    for b in payload.get("blocks", [])
                ]
                log.warning(
                    "Slack rejected: %s %s (blocks=%d, section sizes=%s)",
                    resp.status_code, resp.text[:200], len(sizes), sizes,
                )
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Slack post failed: %s", exc)
            return False

    def _post_long(self, fallback: str, lines: List[str]) -> bool:
        """Post `lines`, chunked so no section exceeds Slack's 3000-char limit.

        Splits across multiple messages if there are more chunks than Slack
        allows blocks in one message.
        """
        chunks: List[str] = []
        current = ""
        for line in lines:
            line = _clip(line, MAX_SECTION_CHARS)
            if len(current) + len(line) + 1 > MAX_SECTION_CHARS:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

        ok = True
        for i in range(0, len(chunks) or 1, MAX_BLOCKS_PER_MESSAGE):
            batch = chunks[i:i + MAX_BLOCKS_PER_MESSAGE]
            ok = self._post({
                "text": _clip(fallback, 500),
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": c}}
                    for c in batch
                ],
            }) and ok
        return ok

    def notify_reviews(self, reviews: List[Review]) -> None:
        for r in reviews:
            stars = STARS.get(r.rating, f":star: {r.rating}")
            bucket = BUCKET_LABEL.get(r.bucket, r.bucket)
            body = _clip(r.review_text.strip(), 1200)

            lines = [
                f"*{stars}-star review of {r.competitor_name}*  _({bucket})_",
                f"*{r.reviewer_store_name or 'unknown store'}*"
                + (f" — {r.country}" if r.country else "")
                + (f" — {r.used_app_for}" if r.used_app_for else ""),
                "",
                "> " + body.replace("\n", "\n> "),
            ]
            if r.developer_reply:
                lines.append(f"_Vendor replied {r.developer_reply_date or ''}_")
            lines.append(f"<{r.review_url}|View on the Shopify App Store>")

            self._post_long(
                f"{r.rating}-star review of {r.competitor_name} by "
                f"{r.reviewer_store_name or 'unknown store'}",
                lines,
            )

    def notify_health(self, unhealthy: List[SourceHealth]) -> None:
        """Dead-man's switch. Fires when the scraper looks broken, not when
        there's simply nothing new -- that distinction is the whole point.

        Hard failures are listed before warnings, since a failure means an app
        produced no data at all while a warning usually means it produced
        slightly-off data.
        """
        if not unhealthy:
            return

        failures = [h for h in unhealthy if not h.ok]
        warnings = [h for h in unhealthy if h.ok]

        header = (
            f":warning: *Review monitor needs attention* — "
            f"{len(failures)} failed, {len(warnings)} warning(s)"
        )
        lines = [header, ""]
        for label, group in (("FAILED", failures), ("warning", warnings)):
            for h in group[:15]:
                notes = "; ".join(h.notes) or "(no detail)"
                lines.append(f"• `{h.target}` *{label}*: {_clip(notes, 300)}")
            if len(group) > 15:
                lines.append(f"_…and {len(group) - 15} more {label} entries_")

        lines += [
            "",
            "Full detail is in the workflow log and in `state/seen.json` under `meta`. "
            "If many apps failed at once, Shopify likely changed its markup — "
            "run the workflow with command `doctor`.",
        ]
        self._post_long("Review monitor needs attention", lines)
