"""Trustpilot source -- SCAFFOLD, not yet implemented.

This is where the volume is. accessiBe, UserWay, AudioEye and EqualWeb all have
far more reviews on Trustpilot than on the Shopify App Store, and AudioEye and
EqualWeb have no Shopify listing at all, so this is the only way to see them.

Two routes, in order of preference:

1. OFFICIAL API (recommended). Trustpilot has a public Business Unit API:
       GET https://api.trustpilot.com/v1/business-units/search?query=accessibe.com
       GET https://api.trustpilot.com/v1/business-units/{id}/reviews?stars=1&stars=2&stars=3
   Needs a free API key. It supports multi-value `stars` filtering properly,
   returns JSON with stable review IDs, and won't break when the site restyles.
   Terms restrict some commercial redistribution -- worth a read before shipping.

2. HTML scraping. Trustpilot review pages embed the full review list as JSON in
   a `<script id="__NEXT_DATA__" type="application/json">` blob. Parsing that is
   far more durable than parsing the DOM:
       props.pageProps.reviews[] -> {id, stars, title, text, dates.publishedDate,
                                     consumer.displayName, ...}
   URL shape: https://www.trustpilot.com/review/{domain}?stars=1&stars=2&stars=3

IMPORTANT MODELLING DIFFERENCE: Trustpilot reviewers are individuals, often with
just a first name and a country. There is no store name and no domain. That makes
them much weaker as leads than Shopify reviews -- you get the complaint but not
the company. Treat Trustpilot as competitive intelligence and messaging research,
and Shopify as the actual lead source. Set `lead_grade: "intel"` on these rows in
Clay so nobody tries to prospect them.

To implement: fill in `collect()`, add targets under a `trustpilot:` key in
competitors.yaml with `domain:` instead of `handle:`, and register below in
sources/__init__.py.
"""

from __future__ import annotations

from typing import List, Tuple

from ..models import Review
from .base import SourceHealth


class TrustpilotSource:
    name = "trustpilot"

    def collect(self, target: dict, defaults: dict) -> Tuple[List[Review], SourceHealth]:
        health = SourceHealth(source=self.name, target=target.get("domain", "?"))
        health.warn("trustpilot adapter is a scaffold; nothing collected")
        return [], health
