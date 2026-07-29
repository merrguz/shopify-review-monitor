"""Parser logic tests. Run: pytest -q

These validate extraction LOGIC against two different markup shapes. They do NOT
validate that today's real Shopify selectors match -- that's `monitor doctor`.
"""

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from monitor.config import State
from monitor.models import Review
from monitor.sources.shopify import (
    find_review_blocks,
    parse_review_block,
    parse_summary,
)

FIXTURES = Path(__file__).parent / "fixtures"
VARIANTS = ["attrs", "generic"]


def load(handle: str, variant: str) -> str:
    path = FIXTURES / f"{handle}.{variant}.html"
    if not path.exists():
        pytest.skip(f"run tests/fixtures/build_fixtures.py first ({path.name} missing)")
    return path.read_text()


# ----------------------------------------------------------------- summaries

@pytest.mark.parametrize("variant", VARIANTS)
def test_summary_userway(variant):
    s = parse_summary(load("userway-accessibility", variant))
    assert s["total"] == 9
    assert s["rating"] == pytest.approx(2.4)
    assert s["buckets"][1] == 3
    assert s["buckets"][2] == 0
    assert s["buckets"][3] == 0
    assert s["buckets"][5] == 6


@pytest.mark.parametrize("variant", VARIANTS)
def test_summary_accessibe(variant):
    s = parse_summary(load("accesswidget", variant))
    assert s["total"] == 15
    assert s["buckets"][1] == 2
    assert s["buckets"][3] == 1
    assert s["buckets"][2] == 0


def test_zero_bucket_is_not_guessed_from_percentage():
    """A level with no reviews renders as plain text, not a link. It must read as
    0 and never be back-filled from a rounded percentage."""
    s = parse_summary(load("userway-accessibility", "attrs"))
    assert s["buckets"][2] == 0
    assert s["buckets"][4] == 0


# -------------------------------------------------------------- block finding

@pytest.mark.parametrize("variant", VARIANTS)
def test_finds_all_blocks(variant):
    soup = BeautifulSoup(load("userway-accessibility", variant), "html.parser")
    blocks, strategy = find_review_blocks(soup)
    assert len(blocks) == 3, f"{strategy} found {len(blocks)}"
    if variant == "attrs":
        assert strategy.startswith("selector:")
    else:
        assert strategy.startswith("heuristic:")


def test_heuristic_does_not_return_the_whole_list_as_one_block():
    soup = BeautifulSoup(load("accesswidget", "generic"), "html.parser")
    blocks, _ = find_review_blocks(soup)
    assert len(blocks) == 2
    for b in blocks:
        assert b.get_text().count("using the app") == 1


# ------------------------------------------------------------ field extraction

@pytest.mark.parametrize("variant", VARIANTS)
def test_field_extraction(variant):
    soup = BeautifulSoup(load("userway-accessibility", variant), "html.parser")
    blocks, _ = find_review_blocks(soup)
    parsed = [parse_review_block(b, assumed_rating=1) for b in blocks]

    first = parsed[0]
    assert first["review_date"] == "2025-07-29"
    assert first["rating"] == 1
    assert first["reviewer_store_name"] == "JCS Wildlife"
    assert first["country"] == "United States"
    assert first["used_app_for"] == "Over 1 year using the app"
    assert "very expensive" in first["review_text"]
    assert "Show more" not in first["review_text"]
    assert first["developer_reply"] == ""

    second = parsed[1]
    assert second["reviewer_store_name"] == "Nutrex Hawaii"
    assert "Zero. None." in second["review_text"]
    assert second["used_app_for"] == "3 days using the app"


@pytest.mark.parametrize("variant", VARIANTS)
def test_developer_reply_is_split_off_from_the_review_body(variant):
    soup = BeautifulSoup(load("userway-accessibility", variant), "html.parser")
    blocks, _ = find_review_blocks(soup)
    third = parse_review_block(blocks[2], assumed_rating=1)

    assert third["reviewer_store_name"] == "Left Field NYC"
    assert third["review_date"] == "2024-11-04"
    assert "lawsuit" in third["review_text"]
    # The reply must not leak into the merchant's text, and vice versa.
    assert "Legal Support Services" not in third["review_text"]
    assert "Legal Support Services" in third["developer_reply"]
    assert third["developer_reply_date"] == "2024-12-15"


@pytest.mark.parametrize("variant", VARIANTS)
def test_rating_comes_from_the_request_not_the_dom(variant):
    """We fetch one page per rating, so `assumed_rating` is authoritative."""
    soup = BeautifulSoup(load("accesswidget", variant), "html.parser")
    blocks, _ = find_review_blocks(soup)
    for b in blocks:
        assert parse_review_block(b, assumed_rating=1)["rating"] == 1


def test_dom_rating_mismatch_is_flagged():
    """If we ask for 1-star and the DOM says 5, the ratings[] filter broke.
    That's the canary for the bug observed on accesswidget during research."""
    html = (
        '<div data-merchant-review="true">'
        '<div aria-label="5 out of 5 stars"></div>'
        '<div>March 3, 2026</div><p>Actually a great app.</p>'
        '<div><div>Some Store</div><div>Canada</div><div>2 days using the app</div></div>'
        '</div>'
    )
    soup = BeautifulSoup(html, "html.parser")
    blocks, _ = find_review_blocks(soup)
    parsed = parse_review_block(blocks[0], assumed_rating=1)
    assert parsed["_dom_rating_mismatch"] is True


def test_review_with_no_country_still_yields_a_store_name():
    html = (
        '<div data-merchant-review="true">'
        '<div>April 1, 2026</div><p>Did not work for us at all.</p>'
        '<div><div>Solo Merchant</div><div>5 days using the app</div></div>'
        '</div>'
    )
    soup = BeautifulSoup(html, "html.parser")
    blocks, _ = find_review_blocks(soup)
    p = parse_review_block(blocks[0], assumed_rating=2)
    assert p["used_app_for"] == "5 days using the app"
    assert p["reviewer_store_name"] == "Solo Merchant"
    assert p["country"] == ""
    assert "Did not work" in p["review_text"]


# ------------------------------------------------------------------ dedupe

def _review(**kw) -> Review:
    base = dict(
        source="shopify", competitor_handle="userway-accessibility",
        competitor_name="UserWay", bucket="overlay", rating=1,
        review_date="2025-07-29", review_text="This is very expensive.",
        reviewer_store_name="JCS Wildlife",
    )
    base.update(kw)
    return Review(**base)


def test_content_hash_id_is_stable_across_runs():
    assert _review().review_id == _review().review_id


def test_used_app_for_drift_does_not_change_the_id():
    """'3 days using the app' becomes 'About 1 month using the app' over time.
    If that fed the hash, every review would re-notify forever."""
    a = _review(used_app_for="3 days using the app")
    b = _review(used_app_for="About 1 month using the app")
    assert a.review_id == b.review_id


def test_whitespace_reflow_does_not_change_the_id():
    a = _review(review_text="This is very expensive.")
    b = _review(review_text="This  is very\n expensive.")
    assert a.review_id == b.review_id


def test_platform_id_wins_when_present():
    r = _review(platform_review_id="2186287")
    assert r.review_id.endswith(":2186287")


def test_different_reviews_get_different_ids():
    assert _review().review_id != _review(reviewer_store_name="Nutrex Hawaii").review_id


def test_state_roundtrip(tmp_path):
    path = tmp_path / "seen.json"
    st = State(path)
    r = _review()
    assert st.is_new(r.review_id)
    st.mark(r.review_id)
    st.save()

    reloaded = State(path)
    assert not reloaded.is_new(r.review_id)


# ------------------------------------------------------------------- config

def test_config_rejects_duplicate_handles(tmp_path):
    from monitor.config import Config

    p = tmp_path / "c.yaml"
    p.write_text(
        "defaults: {max_rating: 3}\n"
        "apps:\n"
        "  - {handle: a, name: A, bucket: overlay}\n"
        "  - {handle: a, name: A again, bucket: overlay}\n"
    )
    with pytest.raises(ValueError, match="duplicate handle"):
        Config.load(p)


def test_config_skips_disabled_apps(tmp_path):
    from monitor.config import Config

    p = tmp_path / "c.yaml"
    p.write_text(
        "defaults: {}\n"
        "apps:\n"
        "  - {handle: on1, name: On, bucket: overlay}\n"
        "  - {handle: off1, name: Off, bucket: alt-text, enabled: false}\n"
    )
    cfg = Config.load(p)
    assert [a["handle"] for a in cfg.apps] == ["on1"]


def test_real_config_parses_and_has_no_duplicates():
    from monitor.config import Config

    cfg = Config.load(Path(__file__).parent.parent / "competitors.yaml")
    assert len(cfg.apps) > 30
    handles = [a["handle"] for a in cfg.apps]
    assert len(handles) == len(set(handles))
    for a in cfg.apps:
        assert a.get("bucket"), f"{a['handle']} has no bucket"


# ------------------------------------ apps with no reviews at all (31 of ~55)

# A faithful reproduction of a real zero-review page, transcribed from
# apps.shopify.com/kokua/reviews on 2026-07-29. Note that the rating scaffold IS
# rendered with all zeros, and the heading has no "(N)".
REVIEWLESS_PAGE = (
    '<html><head>'
    '<link rel="canonical" href="https://apps.shopify.com/kokua/reviews">'
    '<meta property="og:url" content="https://apps.shopify.com/kokua">'
    '<meta name="twitter:data2" content="0.0/5">'
    '</head><body>'
    '<h1>K\u014dkua \u2014 Accessibility Scanner</h1>'
    '<h2>Reviews</h2>'
    '<div>Overall rating</div><div>0</div>'
    '<div>Counts per rating level</div>'
    '<ul>'
    + "".join(
        f'<li><div>{s}</div><div>0% of ratings are {s} stars</div><div>0</div></li>'
        for s in (5, 4, 3, 2, 1)
    )
    + '</ul>'
    '<div>Write a review</div>'
    '<div>No reviews yet</div>'
    '</body></html>'
)


def test_app_with_zero_reviews_reports_total_zero_not_failure():
    """31 of the 53 monitored apps have no reviews. Reporting those as parse
    failures produced 31 false alarms per run on the first live run -- enough to
    get the alert channel muted, which would then hide a real breakage."""
    s = parse_summary(REVIEWLESS_PAGE)
    assert s["total"] == 0
    assert all(v == 0 for v in s["buckets"].values())
    assert s["rating"] == pytest.approx(0.0)


def test_rating_scaffold_alone_does_not_imply_reviews_exist():
    """The regression that caused those false alarms: "Counts per rating level"
    renders on zero-review pages too, so it cannot be used as proof."""
    assert "Counts per rating level" in REVIEWLESS_PAGE
    assert parse_summary(REVIEWLESS_PAGE)["total"] == 0


def test_genuinely_broken_page_still_reports_none():
    """No count, no reviews-page furniture -> indeterminate, must fail loudly."""
    s = parse_summary("<html><body><p>Something went wrong</p></body></html>")
    assert s["total"] is None


def test_bot_challenge_page_reports_none():
    html = "<html><body><h1>Just a moment...</h1><p>Checking your browser</p></body></html>"
    assert parse_summary(html)["total"] is None


def test_zero_count_contradicted_by_visible_reviews_is_a_failure():
    """The other direction: if the count parses as 0 but review blocks are right
    there on the page, the count parsing broke. Never report a comfortable zero."""
    html = (
        '<html><head>'
        '<link rel="canonical" href="https://apps.shopify.com/accesswidget/reviews">'
        '</head><body><h2>Reviews</h2><div>Counts per rating level</div>'
        '<div data-merchant-review="true">'
        '<div>March 3, 2026</div><p>Terrible.</p>'
        '<div><div>A Store</div><div>Canada</div><div>2 days using the app</div></div>'
        '</div></body></html>'
    )
    assert parse_summary(html)["total"] is None


def test_missing_count_with_nonzero_buckets_uses_the_bucket_sum():
    html = (
        '<html><head>'
        '<link rel="canonical" href="https://apps.shopify.com/x/reviews">'
        '</head><body><h2>Reviews</h2><div>Counts per rating level</div>'
        '<a href="/x/reviews?ratings%5B%5D=5">7</a>'
        '<a href="/x/reviews?ratings%5B%5D=1">2</a>'
        '</body></html>'
    )
    assert parse_summary(html)["total"] == 9


# ------------------------------------------- overall rating fallback

def test_rating_falls_back_to_twitter_card_when_jsonld_lacks_it():
    """userway-accessibility publishes no aggregateRating in its JSON-LD but does
    expose "2.4/5" in the Twitter card meta tags. Observed live 2026-07-29."""
    html = (
        '<html><head>'
        '<link rel="canonical" href="https://apps.shopify.com/userway-accessibility/reviews">'
        '<meta name="twitter:value1" content="Pricing">'
        '<meta name="twitter:data1" content="Free plan available">'
        '<meta name="twitter:value2" content="Star rating">'
        '<meta name="twitter:data2" content="2.4/5">'
        '</head><body><h2>Reviews (9)</h2>'
        '<div>Counts per rating level</div>'
        '<a href="/userway-accessibility/reviews?ratings%5B%5D=1">3</a>'
        '</body></html>'
    )
    s = parse_summary(html)
    assert s["total"] == 9
    assert s["rating"] == pytest.approx(2.4)


def test_jsonld_rating_wins_over_twitter_card():
    html = (
        '<html><head>'
        '<meta name="twitter:data2" content="1.1/5">'
        '<script type="application/ld+json">'
        '{"aggregateRating":{"ratingValue":"3.5","reviewCount":"15"}}'
        '</script></head><body><h2>Reviews (15)</h2></body></html>'
    )
    assert parse_summary(html)["rating"] == pytest.approx(3.5)


def test_pricing_meta_is_not_mistaken_for_a_rating():
    """The other twitter:data pair is pricing text. Must not parse as a rating."""
    html = (
        '<html><head>'
        '<meta name="twitter:data1" content="Free to install">'
        '<meta name="twitter:data2" content="From $19/month">'
        '<link rel="canonical" href="https://apps.shopify.com/kokua/reviews">'
        '</head><body><h2>Reviews</h2><div>Write a review</div></body></html>'
    )
    s = parse_summary(html)
    assert s["rating"] is None
    assert s["total"] == 0
