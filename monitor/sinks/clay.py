"""Clay webhook sink.

Clay's webhook source accepts one JSON object per POST and creates one row from
it. There is no batch endpoint, so we POST per review -- fine at this volume
(single digits per month).

Note the 50,000-submission cap per Clay webhook. At a few reviews a month you
will never hit it, but if you later point a high-volume source at the same
webhook, be aware you'd need to recreate it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import List, Set

import requests

from ..models import Review

log = logging.getLogger(__name__)


class ClaySink:
    def __init__(self, webhook_url: str, auth_token: str = "", dry_run: bool = False):
        self.webhook_url = webhook_url
        self.auth_token = auth_token
        self.dry_run = dry_run or not webhook_url

    def send(self, reviews: List[Review]) -> Set[str]:
        """POST each review. Returns the set of review_ids that landed.

        Only delivered ids are returned, and only those get marked as seen by the
        caller -- so a review that failed to reach Clay is retried next run
        instead of being silently lost.
        """
        if not reviews:
            return set()
        if self.dry_run:
            for r in reviews:
                log.info("[dry-run] would POST to Clay: %s", json.dumps(r.to_payload())[:400])
            # Dry runs still count as delivered so local testing doesn't
            # re-notify forever; use a throwaway state file when testing.
            return {r.review_id for r in reviews}

        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["x-clay-webhook-auth"] = self.auth_token

        delivered: Set[str] = set()
        for r in reviews:
            payload = r.to_payload()
            for attempt in range(1, 4):
                try:
                    resp = requests.post(
                        self.webhook_url, json=payload, headers=headers, timeout=20
                    )
                    if resp.status_code < 300:
                        delivered.add(r.review_id)
                        break
                    log.warning(
                        "Clay rejected %s: %s %s",
                        r.review_id, resp.status_code, resp.text[:200],
                    )
                except requests.RequestException as exc:
                    log.warning("Clay POST failed for %s: %s", r.review_id, exc)
                time.sleep(2 ** attempt)
            else:
                log.error("giving up on Clay POST for %s; will retry next run", r.review_id)
            time.sleep(0.3)
        return delivered
