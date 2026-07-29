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

REVIEWLESS_PAGE = (
    '<html><head>'
    '<link rel="canonical" href="https://apps.shopify.com/kokua/reviews">'
    '<meta property="og:url" content="https://apps.shopify.com/kokua">'
    '</head><body>'
    '<h1>Kokua - Accessibility Scanner</h1>'
    '<h2>Reviews</h2>'          # note: no "(N)"
    '<div>Write a review</div>'
    '</body></html>'
)


def test_app_with_zero_reviews_reports_total_zero_not_failure():
    """31 of the monitored apps have no reviews. Their pages show a bare
    "Reviews" heading with no count. If that read as a parse failure the
    dead-man's switch would scream on most of the watchlist every hour."""
    s = parse_summary(REVIEWLESS_PAGE)
    assert s["total"] == 0
    assert all(v == 0 for v in s["buckets"].values())


def test_genuinely_broken_page_still_reports_none():
    """No count, no structural furniture -> indeterminate, must fail loudly."""
    s = parse_summary("<html><body><p>Something went wrong</p></body></html>")
    assert s["total"] is None


def test_missing_count_alongside_rating_buckets_is_a_failure():
    """"Counts per rating level" only renders when reviews exist, so its
    presence without a parseable total means the markup really did change."""
    html = (
        '<html><head><link rel="canonical" '
        'href="https://apps.shopify.com/accesswidget/reviews"></head><body>'
        '<h2>Reviews</h2><div>Counts per rating level</div>'
        '<div>73% of ratings are 5 stars</div>'
        '</body></html>'
    )
    assert parse_summary(html)["total"] is None
