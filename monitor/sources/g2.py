"""G2 source -- SCAFFOLD, and the hardest of the three.

G2 sits behind aggressive Cloudflare bot management. A plain `requests.get` from
a GitHub Actions runner gets a challenge page, not reviews. Datacenter IPs are
blocked essentially always. Do not sink an afternoon into fighting this with
headers and sleeps -- it doesn't work.

Realistic options:

1. PAID UNBLOCKER (~$50-100/mo). ScrapingBee, Zyte, Bright Data, or Apify's
   proxy+browser stack. You pass the URL, they return rendered HTML. This is the
   only reliable scraping route. Wire it in as a swap for `monitor.http.get`:
       html = unblocker.get(url)   # instead of http.get(url)

2. G2 PARTNER/API ACCESS. G2 sells a Content/Syndication API and, if TestParty
   claims its own G2 profile, a Buyer Intent product. If you're going to spend
   money, spending it here gets you licensed data plus the intent feed, which is
   a materially better signal than scraped reviews. Worth a sales conversation
   before paying an unblocker.

3. SKIP G2. Trustpilot plus the Shopify App Store may cover enough. G2 reviews of
   accessibility vendors skew enterprise, and those buyers aren't Shopify
   merchants -- so G2 is arguably the least relevant of the three for TestParty's
   ICP even though it has the most reviews.

Same modelling caveat as Trustpilot, more so: G2 reviewers are frequently
anonymized to "Verified User in Retail", which is useless as a lead. Treat as
intel, not pipeline.

URL shape if you do implement:
    https://www.g2.com/products/{slug}/reviews?filters[rating][]=1&order=most_recent
"""

from __future__ import annotations

from typing import List, Tuple

from ..models import Review
from .base import SourceHealth


class G2Source:
    name = "g2"

    def collect(self, target: dict, defaults: dict) -> Tuple[List[Review], SourceHealth]:
        health = SourceHealth(source=self.name, target=target.get("slug", "?"))
        health.warn(
            "g2 adapter is a scaffold; needs a paid unblocker or G2 API access "
            "before it can return anything"
        )
        return [], health
