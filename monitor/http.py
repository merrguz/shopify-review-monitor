"""Polite HTTP with retries. Shared by all source adapters."""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(RuntimeError):
    pass


class NotFoundError(FetchError):
    """The server answered 404. Callers decide whether that means "delisted"."""


def get(
    url: str,
    *,
    params: Optional[list] = None,
    timeout: int = 30,
    retries: int = 4,
    min_bytes: int = 2000,
    throttle: float = 0.7,
) -> str:
    """GET a URL, returning HTML text.

    Treats a 200 with a suspiciously small body as a soft failure and retries.
    Shopify occasionally serves an empty-but-200 shell under load; taking that
    at face value is how a scraper silently reports "no reviews" forever.

    `params` is a list of (key, value) tuples, not a dict, because Shopify's
    rating filter uses repeated `ratings[]` keys.
    """
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                url, params=params, headers=DEFAULT_HEADERS, timeout=timeout
            )
            if resp.status_code == 429:
                last_err = "429 rate limited"
            elif resp.status_code >= 500:
                last_err = f"{resp.status_code} server error"
            elif resp.status_code == 404:
                # Don't burn retries on it. Whether a 404 means "delisted" or just
                # "no reviews page yet" is the caller's call -- see ShopifySource.
                raise NotFoundError(f"404 not found: {resp.url}")
            elif resp.status_code != 200:
                last_err = f"{resp.status_code}"
            elif len(resp.text) < min_bytes:
                last_err = f"200 but only {len(resp.text)} bytes (soft failure)"
            else:
                time.sleep(throttle)
                return resp.text
        except FetchError:
            raise
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"

        backoff = min(2 ** attempt, 30) + random.uniform(0, 1.5)
        log.warning(
            "fetch attempt %d/%d failed for %s (%s); retrying in %.1fs",
            attempt, retries, url, last_err, backoff,
        )
        time.sleep(backoff)

    raise FetchError(f"gave up on {url} after {retries} attempts: {last_err}")
