"""Shopify App Store review source.

Design notes, because the non-obvious choices here are all deliberate:

1. There is no reviews API, webhook, or RSS. Polling is the only option. The
   review pages are server-rendered HTML, so no browser is needed.

2. `sort_by=newest` is passed explicitly. The site default is "Most relevant",
   which will silently hide new reviews.

3. Ratings are taken from the REQUEST, not the DOM. We fetch one page per rating
   value (`?ratings[]=1`), so a review's rating is known a priori and we never
   depend on parsing star glyphs. Multi-value filters (`ratings[]=1&ratings[]=2`)
   were observed collapsing to a single value against `accesswidget`, so they are
   never used. If the DOM *does* expose a rating and it disagrees with what we
   asked for, that's logged as a warning -- it's the canary for the filter param
   changing behavior.

4. We fetch the unfiltered reviews page first to read the per-rating bucket
   counts. Most apps in this category have zero negative reviews, so this turns
   ~3 requests per app into ~1 for the common case, and gives us an authoritative
   expected count to check our parse against.

5. Field extraction inside a review block is LINE-BASED, not CSS-based. Shopify
   churns class names; the visual order of a review does not change:

       September 3, 2025
       <review body, possibly multiple paragraphs>
       Show more
       Some Store Name
       United States
       Over 1 year using the app
       vendorname replied
       October 1, 2025
       <reply body>

   Anchoring on the "... using the app" line and walking outward is far more
   durable than betting on `.tw-text-fg-secondary`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .. import http
from ..countries import KNOWN_COUNTRIES
from ..models import Review, _parse_date
from .base import SourceHealth

log = logging.getLogger(__name__)

BASE = "https://apps.shopify.com"

DATE_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}$"
)
USED_FOR_RE = re.compile(r"using the app\s*$", re.I)
REPLIED_RE = re.compile(r"\breplied\b", re.I)
TOTAL_RE = re.compile(r"Reviews\s*\(\s*([\d,]+)\s*\)", re.I)
PCT_BUCKET_RE = re.compile(r"(\d{1,3})%\s+of\s+ratings\s+are\s+(\d)\s+stars?", re.I)
STAR_LABEL_RE = re.compile(r"([1-5])(?:\.0)?\s+out of\s+5\s+stars", re.I)
NOISE_LINES = {
    "show more", "show less", "read more", "write a review", "refine",
    "apply", "sort", "most relevant", "install", "helpful", "report",
}


# --------------------------------------------------------------------------- #
# Page-level parsing
# --------------------------------------------------------------------------- #

def parse_summary(html: str) -> dict:
    """Extract total review count and per-rating bucket counts.

    Returns {"total": int|None, "buckets": {1..5: int}, "rating": float|None}.
    Buckets are the authoritative "how many negative reviews should exist" number.

    `total is None` means "could not determine" and is treated by the caller as a
    hard failure. Critically, a genuinely review-less app is NOT that case: 31 of
    the ~55 monitored apps have zero reviews and render a bare "Reviews" heading
    with no "(N)" at all. Reporting those as parse failures would fire the
    dead-man's switch on most of the watchlist every hour, which is how a
    monitor gets muted and then ignored. So zero is distinguished from broken.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    total: Optional[int] = None
    m = TOTAL_RE.search(text)
    if m:
        total = int(m.group(1).replace(",", ""))

    buckets: dict[int, int] = {}

    # Strategy A: the per-level anchors link to the filtered view and their text
    # is the count. Most reliable when present.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        mm = re.search(r"ratings(?:%5B%5D|\[\])=([1-5])", href)
        if not mm:
            continue
        label = a.get_text(" ", strip=True).replace(",", "")
        if label.isdigit():
            buckets[int(mm.group(1))] = int(label)

    # Strategy B: reconstruct from the "N% of ratings are K stars" strings.
    # Only used to fill gaps, since percentages round and can be off by one.
    if total:
        for pct, star in PCT_BUCKET_RE.findall(text):
            star = int(star)
            if star not in buckets:
                buckets[star] = round(int(pct) / 100 * total)

    # Zero-count levels render as plain text, not links, so absence means zero.
    for star in range(1, 6):
        buckets.setdefault(star, 0)

    rating = None
    ld = _jsonld_aggregate(soup)
    if ld:
        rating = ld.get("ratingValue")
        if total is None and ld.get("reviewCount") is not None:
            total = int(ld["reviewCount"])

    if rating is None:
        # Not every listing publishes aggregateRating in its JSON-LD --
        # userway-accessibility doesn't, while accesswidget does. The Twitter
        # card meta tags carry it as "2.4/5" on both. Cosmetic (nothing branches
        # on this value) but a None here makes doctor output look broken.
        rating = _twitter_card_rating(soup)

    if total is None and _looks_like_a_reviews_page(soup, text):
        # An app with no reviews renders "## Reviews" with no "(N)" -- there is no
        # count to find. The per-rating buckets are still rendered (all zeros), so
        # their sum is the count.
        #
        # An earlier version treated the presence of "Counts per rating level" as
        # proof that reviews existed, and therefore reported every review-less app
        # as a parse failure. On a 53-app watchlist where 31 apps have no reviews,
        # that meant 31 false alarms per run -- enough to get the alert channel
        # muted, which would in turn hide a real breakage. Hence the care here.
        total = sum(buckets.values())

    if total == 0:
        # Guard the other direction: if the count says zero but review blocks are
        # visibly present, then the count parsing broke and we must not report a
        # comfortable "no reviews".
        blocks, _ = find_review_blocks(soup)
        if blocks:
            log.warning(
                "review count parsed as 0 but %d review blocks are present", len(blocks)
            )
            total = None

    return {"total": total, "buckets": buckets, "rating": rating}


def _looks_like_a_reviews_page(soup: BeautifulSoup, text: str) -> bool:
    """True if this is a real app reviews page, however many reviews it has.

    Distinguishes "valid page, nothing to report" from "we got served something
    unexpected" -- an error page, a bot challenge, a truncated response. Only the
    latter should trip the dead-man's switch.
    """
    if "No reviews yet" in text:
        return True
    canonical = soup.find("link", rel="canonical")
    if canonical and str(canonical.get("href", "")).rstrip("/").endswith("/reviews"):
        return True
    og = soup.find("meta", property="og:url") or soup.find(
        "meta", attrs={"name": "og:url"}
    )
    if og and "apps.shopify.com" in str(og.get("content", "")):
        return True
    return "Write a review" in text and "Counts per rating level" in text


STAR_FRACTION_RE = re.compile(r"^\s*([0-5](?:\.\d+)?)\s*/\s*5\s*$")


def _twitter_card_rating(soup: BeautifulSoup) -> Optional[float]:
    """Read the overall star rating out of the Twitter card meta tags.

    Shopify renders it as a data/value pair, e.g.
        <meta name="twitter:value2" content="Star rating">
        <meta name="twitter:data2"  content="2.4/5">

    Matching on the "N/5" shape rather than trusting the index means it still
    works if Shopify reorders the pairs (the other pair is pricing, which never
    looks like "N/5").
    """
    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or ""
        if not str(name).startswith("twitter:data"):
            continue
        m = STAR_FRACTION_RE.match(str(tag.get("content", "")))
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _jsonld_aggregate(soup: BeautifulSoup) -> Optional[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            agg = node.get("aggregateRating")
            if isinstance(agg, dict):
                out = {}
                if agg.get("ratingValue") is not None:
                    try:
                        out["ratingValue"] = float(agg["ratingValue"])
                    except (TypeError, ValueError):
                        pass
                if agg.get("reviewCount") or agg.get("ratingCount"):
                    try:
                        out["reviewCount"] = int(
                            agg.get("reviewCount") or agg.get("ratingCount")
                        )
                    except (TypeError, ValueError):
                        pass
                if out:
                    return out
    return None


# --------------------------------------------------------------------------- #
# Review block discovery
# --------------------------------------------------------------------------- #

BLOCK_SELECTORS = [
    "[data-merchant-review]",
    "[data-review-content-id]",
    "div[data-review-id]",
]


def find_review_blocks(soup: BeautifulSoup) -> Tuple[list, str]:
    """Return (blocks, strategy_name).

    Tries known data attributes first, then falls back to a structural heuristic
    that needs no knowledge of Shopify's class names at all.
    """
    for sel in BLOCK_SELECTORS:
        found = soup.select(sel)
        if found:
            return found, f"selector:{sel}"

    # Heuristic fallback: a review block is the SMALLEST element containing both
    # a standalone date line and a "... using the app" line. Taking the smallest
    # matching element prevents grabbing the whole list as one blob.
    candidates = []
    for el in soup.find_all(["div", "li", "article", "section"]):
        lines = _lines(el)
        if not any(DATE_RE.match(l) for l in lines):
            continue
        if not any(USED_FOR_RE.search(l) for l in lines):
            continue
        candidates.append(el)

    minimal = [
        el for el in candidates
        if not any(other is not el and other in el.descendants for other in candidates)
    ]
    if minimal:
        return minimal, "heuristic:date+used-for"
    return [], "none"


def _lines(el) -> List[str]:
    raw = el.get_text("\n", strip=True).split("\n")
    return [l.strip() for l in raw if l.strip()]


# --------------------------------------------------------------------------- #
# Field extraction
# --------------------------------------------------------------------------- #

def parse_review_block(block, *, assumed_rating: Optional[int] = None) -> Optional[dict]:
    """Pull one review's fields out of a block using line-order heuristics."""
    lines = [l for l in _lines(block) if l.lower() not in NOISE_LINES]
    if not lines:
        return None

    # Split merchant content from the developer reply, which always follows it.
    reply_idx = next(
        (i for i, l in enumerate(lines) if REPLIED_RE.search(l) and len(l) < 120), None
    )
    merchant = lines[:reply_idx] if reply_idx is not None else lines
    reply_lines = lines[reply_idx:] if reply_idx is not None else []

    # Anchor: the "... using the app" line. Store name and country sit directly
    # above it; everything above those is the date and the body.
    anchor = next((i for i, l in enumerate(merchant) if USED_FOR_RE.search(l)), None)

    used_for = ""
    country = ""
    store = ""
    country_unrecognized = False
    if anchor is not None:
        used_for = merchant[anchor]
        candidate_country = merchant[anchor - 1] if anchor - 1 >= 0 else ""
        # Observed layout is [.., store, country, "N using the app"]. The only
        # reliable way to tell a country line from the tail of a review body is
        # to check it against a known list -- a permissive regex happily accepts
        # "Solo Merchant" and then eats the store name. So: recognized country
        # means the 3-line layout; anything else means country is absent.
        if _is_known_country(candidate_country) and anchor - 2 >= 0:
            country = candidate_country
            store = merchant[anchor - 2]
            head_end = anchor - 2
        else:
            store = candidate_country
            head_end = max(anchor - 1, 0)
            # Flagged so an unfamiliar country name shows up as a health warning
            # rather than silently shifting every field by one.
            country_unrecognized = bool(candidate_country) and not _is_known_country(
                candidate_country
            )
    else:
        # No anchor (rare layout, or a review from a store with no country shown).
        # Fall back to: last non-date line is the store name.
        head_end = len(merchant)
        for l in reversed(merchant):
            if not DATE_RE.match(l):
                store = l
                break

    date_idx = next((i for i, l in enumerate(merchant) if DATE_RE.match(l)), None)
    review_date = _parse_date(merchant[date_idx]) if date_idx is not None else None

    body_start = (date_idx + 1) if date_idx is not None else 0
    body = " ".join(merchant[body_start:head_end]).strip()

    developer_reply = ""
    developer_reply_date = None
    if reply_lines:
        rdate_idx = next(
            (i for i, l in enumerate(reply_lines) if DATE_RE.match(l)), None
        )
        if rdate_idx is not None:
            developer_reply_date = _parse_date(reply_lines[rdate_idx])
            developer_reply = " ".join(reply_lines[rdate_idx + 1:]).strip()
        else:
            developer_reply = " ".join(reply_lines[1:]).strip()

    dom_rating = _dom_rating(block)
    rating = dom_rating if dom_rating is not None else assumed_rating
    dom_mismatch = (
        dom_rating is not None
        and assumed_rating is not None
        and dom_rating != assumed_rating
    )

    if not body and not store:
        return None

    return {
        "rating": rating,
        "review_date": review_date,
        "review_text": body,
        "reviewer_store_name": store,
        "country": country,
        "used_app_for": used_for,
        "developer_reply": developer_reply,
        "developer_reply_date": developer_reply_date,
        "platform_review_id": _block_id(block),
        "_dom_rating_mismatch": dom_mismatch,
        "_country_unrecognized": country_unrecognized,
    }


def _block_id(block) -> Optional[str]:
    for attr in ("data-review-content-id", "data-review-id", "data-merchant-review", "id"):
        val = block.get(attr)
        if val and str(val).strip() and str(val).strip().lower() != "true":
            return str(val).strip()
    return None


def _dom_rating(block) -> Optional[int]:
    for attr in ("aria-label", "title", "data-rating"):
        for el in block.find_all(attrs={attr: True}):
            raw = str(el.get(attr))
            m = STAR_LABEL_RE.search(raw)
            if m:
                return int(m.group(1))
            if attr == "data-rating" and raw.strip().isdigit():
                return int(raw.strip())
    return None


def _is_known_country(s: str) -> bool:
    """Strict membership test, deliberately not a regex.

    A permissive pattern ("starts uppercase, letters and spaces") matches store
    names like "Solo Merchant" just as happily as "United States", and getting
    this wrong shifts every subsequent field by one line. A miss here is
    self-announcing via the `_country_unrecognized` flag; a false positive is
    silent. So: strict list, and add to it if the doctor reports misses.
    """
    return (s or "").strip() in KNOWN_COUNTRIES


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #

class ShopifySource:
    name = "shopify"

    def reviews_url(self, handle: str) -> str:
        return f"{BASE}/{handle}/reviews"

    def collect(self, target: dict, defaults: dict) -> Tuple[List[Review], SourceHealth]:
        handle = target["handle"]
        name = target.get("name", handle)
        bucket = target.get("bucket", "unknown")
        max_rating = int(target.get("max_rating", defaults.get("max_rating", 3)))
        pages = int(target.get("pages", defaults.get("pages", 1)))

        health = SourceHealth(source=self.name, target=handle)
        url = self.reviews_url(handle)

        try:
            html = http.get(url)
        except http.NotFoundError as exc:
            # A 404 on /reviews does NOT mean the app is gone. Newly launched
            # listings with no reviews can 404 here while the listing itself is
            # live (observed 2026-10: clearpath-accessibility, paperfort,
            # accessproof-2). Probe the listing page: if it exists, this app has
            # no reviews page yet, which is the same as zero reviews. Only a
            # listing that 404s too is a genuine delisting worth alerting on.
            try:
                http.get(f"{BASE}/{handle}")
            except http.NotFoundError:
                health.fail(f"listing no longer exists (delisted?): {exc}")
                return [], health
            except http.FetchError as probe_exc:
                health.fail(
                    f"reviews page 404'd and listing probe failed: {probe_exc}"
                )
                return [], health
            # Log only: any entry in health.notes triggers a Slack alert, and
            # this is an expected state, not something to page about.
            log.info(
                "%s: reviews page 404s but listing is live; treating as no reviews yet",
                handle,
            )
            return [], health
        except http.FetchError as exc:
            health.fail(f"could not fetch reviews page: {exc}")
            return [], health

        summary = parse_summary(html)
        if summary["total"] is None:
            health.fail(
                "could not parse a total review count from the page -- "
                "this almost certainly means Shopify changed the markup"
            )
            return [], health

        health.total_reviews_seen = summary["total"]
        negative_levels = [r for r in range(1, max_rating + 1) if summary["buckets"].get(r, 0) > 0]
        health.negative_expected = sum(summary["buckets"].get(r, 0) for r in negative_levels)

        if not negative_levels:
            return [], health  # normal, common case: no negative reviews exist

        reviews: List[Review] = []
        for rating in negative_levels:
            for page in range(1, pages + 1):
                params = [("ratings[]", str(rating)), ("sort_by", "newest")]
                if page > 1:
                    params.append(("page", str(page)))
                try:
                    page_html = http.get(url, params=params)
                except http.FetchError as exc:
                    health.warn(f"rating {rating} page {page}: {exc}")
                    continue

                soup = BeautifulSoup(page_html, "html.parser")
                blocks, strategy = find_review_blocks(soup)
                if not blocks:
                    # Empty page 1 with reviews promised = something is wrong.
                    # Empty page 2+ just means we ran off the end of the results,
                    # which is the normal case whenever the negatives fit on one
                    # page -- warning about it would be pure noise.
                    if page == 1:
                        health.warn(
                            f"rating {rating}: found 0 review blocks "
                            f"(strategy={strategy}) but buckets promised "
                            f"{summary['buckets'].get(rating)}"
                        )
                    break

                page_count = 0
                for blk in blocks:
                    parsed = parse_review_block(blk, assumed_rating=rating)
                    if not parsed:
                        continue
                    if parsed.pop("_dom_rating_mismatch", False):
                        health.warn(
                            f"rating filter suspect: asked for {rating} but the DOM "
                            f"reports a different rating. Check the ratings[] param."
                        )
                    if parsed.pop("_country_unrecognized", False):
                        health.warn(
                            f"unrecognized country line {parsed['reviewer_store_name']!r} "
                            f"-- if that's a country, add it via tools/gen_countries.py; "
                            f"store name and country may be shifted for this review"
                        )
                    if parsed["rating"] is None or parsed["rating"] > max_rating:
                        continue
                    reviews.append(
                        Review(
                            source=self.name,
                            competitor_handle=handle,
                            competitor_name=name,
                            bucket=bucket,
                            review_url=f"{url}?{urlencode([('ratings[]', rating), ('sort_by', 'newest')])}",
                            **parsed,
                        )
                    )
                    page_count += 1

                if page_count == 0:
                    break  # empty page: stop paginating this rating

        health.negative_parsed = len(reviews)
        if health.negative_expected and health.negative_parsed < health.negative_expected:
            health.warn(
                f"parsed {health.negative_parsed} of {health.negative_expected} "
                f"negative reviews the rating buckets promised"
            )
        return reviews, health
