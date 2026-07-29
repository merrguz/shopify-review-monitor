"""End-to-end collect() tests with HTTP stubbed out.

These are the tests that catch integration bugs the unit tests can't see:
a parser field that never reaches the Review constructor, a rating filter that
returns the wrong page, a dead-man's switch that doesn't fire.
"""

from pathlib import Path

import pytest

from monitor import http as http_mod
from monitor.sources import shopify as shopify_mod
from monitor.sources.shopify import ShopifySource

FIXTURES = Path(__file__).parent / "fixtures"
DEFAULTS = {"max_rating": 3, "pages": 1}
UW = {"handle": "userway-accessibility", "name": "UserWay", "bucket": "overlay"}


def _stub(monkeypatch, variant="attrs", *, fail_filtered=False, empty=False):
    """Serve fixtures in place of live HTTP.

    Fixtures are per-app, not per-rating, so a filtered request returns the same
    document. Every fixture review is 1-star, which matches what the real
    `?ratings[]=1` page would return.
    """
    calls = []

    def fake_get(url, *, params=None, **kw):
        calls.append((url, params))
        if empty:
            raise http_mod.FetchError("simulated soft failure")
        if params and fail_filtered:
            raise http_mod.FetchError("simulated filtered-page failure")
        handle = url.rstrip("/").split("/")[-2]
        path = FIXTURES / f"{handle}.{variant}.html"
        if not path.exists():
            pytest.skip("run tests/fixtures/build_fixtures.py first")
        return path.read_text()

    monkeypatch.setattr(shopify_mod.http, "get", fake_get)
    return calls


@pytest.mark.parametrize("variant", ["attrs", "generic"])
def test_collect_returns_populated_reviews(monkeypatch, variant):
    _stub(monkeypatch, variant)
    reviews, health = ShopifySource().collect(UW, DEFAULTS)

    assert health.ok
    assert health.total_reviews_seen == 9
    assert health.negative_expected == 3
    assert len(reviews) == 3

    r = reviews[0]
    assert r.source == "shopify"
    assert r.competitor_handle == "userway-accessibility"
    assert r.bucket == "overlay"
    assert r.rating == 1
    assert r.reviewer_store_name == "JCS Wildlife"
    assert r.country == "United States"
    assert r.review_date == "2025-07-29"
    assert r.review_url.startswith("https://apps.shopify.com/userway-accessibility/reviews?")
    assert r.first_seen_at
    assert r.review_id


def test_collect_payload_is_json_serializable(monkeypatch):
    import json

    _stub(monkeypatch)
    reviews, _ = ShopifySource().collect(UW, DEFAULTS)
    for r in reviews:
        payload = json.loads(json.dumps(r.to_payload()))
        assert payload["review_id"] == r.review_id
        # No leaked internal diagnostic keys in the Clay payload.
        assert not [k for k in payload if k.startswith("_")]


def test_review_ids_are_unique_within_a_run(monkeypatch):
    _stub(monkeypatch)
    reviews, _ = ShopifySource().collect(UW, DEFAULTS)
    ids = [r.review_id for r in reviews]
    assert len(ids) == len(set(ids))


def test_only_negative_rating_levels_are_fetched(monkeypatch):
    """UserWay has 3 one-stars and zero 2s and 3s. We should request only
    ratings[]=1, never 2 or 3 -- that's the request-budget optimization."""
    calls = _stub(monkeypatch)
    ShopifySource().collect(UW, DEFAULTS)

    filtered = [p for _, p in calls if p]
    assert len(filtered) == 1
    assert ("ratings[]", "1") in filtered[0]
    assert ("sort_by", "newest") in filtered[0]


def test_sort_by_newest_is_always_sent(monkeypatch):
    """The site default is 'Most relevant', which hides new reviews."""
    calls = _stub(monkeypatch)
    ShopifySource().collect(UW, DEFAULTS)
    for _, params in calls:
        if params:
            assert ("sort_by", "newest") in params


def test_ratings_filter_is_never_multi_valued(monkeypatch):
    """Multi-value ratings[] was observed collapsing to one value. One request
    per rating, always."""
    calls = _stub(monkeypatch)
    ShopifySource().collect(UW, DEFAULTS)
    for _, params in calls:
        if params:
            assert len([1 for k, _ in params if k == "ratings[]"]) == 1


def test_app_with_no_negative_reviews_costs_one_request(monkeypatch):
    calls = _stub(monkeypatch)
    clean = {"handle": "userway-accessibility", "name": "UserWay",
             "bucket": "overlay", "max_rating": 0}
    reviews, health = ShopifySource().collect(clean, {"max_rating": 0, "pages": 1})
    assert reviews == []
    assert health.ok
    assert len(calls) == 1


def test_unfetchable_summary_page_is_a_hard_failure(monkeypatch):
    _stub(monkeypatch, empty=True)
    reviews, health = ShopifySource().collect(UW, DEFAULTS)
    assert reviews == []
    assert health.ok is False
    assert any("could not fetch" in n for n in health.notes)


def test_unparseable_summary_is_a_hard_failure(monkeypatch):
    """A 200 that yields no review count means the markup changed. That must
    fail loudly rather than report 'no new reviews'."""
    monkeypatch.setattr(
        shopify_mod.http, "get",
        lambda url, **kw: "<html><body>" + ("x" * 3000) + "</body></html>",
    )
    reviews, health = ShopifySource().collect(UW, DEFAULTS)
    assert reviews == []
    assert health.ok is False
    assert any("markup" in n for n in health.notes)


def test_short_parse_against_promised_buckets_warns(monkeypatch):
    """Buckets promise 3 negatives; if the filtered page can't be fetched we
    should end up with a warning, not silent success."""
    _stub(monkeypatch, fail_filtered=True)
    reviews, health = ShopifySource().collect(UW, DEFAULTS)
    assert reviews == []
    assert health.negative_expected == 3
    assert health.notes


def test_developer_reply_reaches_the_review_object(monkeypatch):
    _stub(monkeypatch)
    reviews, _ = ShopifySource().collect(UW, DEFAULTS)
    replied = [r for r in reviews if r.developer_reply]
    assert len(replied) == 1
    assert replied[0].reviewer_store_name == "Left Field NYC"
    assert replied[0].developer_reply_date == "2024-12-15"


def test_reviewless_app_is_healthy_and_silent(monkeypatch):
    """The common case across the watchlist: a live listing, zero reviews.
    Must be one request, no reviews, no warnings, ok=True."""
    calls = []

    def fake_get(url, *, params=None, **kw):
        calls.append((url, params))
        return (
            '<html><head><link rel="canonical" '
            'href="https://apps.shopify.com/kokua/reviews"></head><body>'
            '<h1>Kokua</h1><h2>Reviews</h2><div>Write a review</div>'
            + ("x" * 2500) + '</body></html>'
        )

    monkeypatch.setattr(shopify_mod.http, "get", fake_get)
    reviews, health = ShopifySource().collect(
        {"handle": "kokua", "name": "Kokua", "bucket": "scanner"}, DEFAULTS
    )
    assert reviews == []
    assert health.ok is True
    assert health.notes == []
    assert health.total_reviews_seen == 0
    assert len(calls) == 1
