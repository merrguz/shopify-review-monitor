"""Slack sink tests.

The bug these exist for: on the first real run, a health alert with 30 targets'
notes exceeded Slack's 3000-char section limit and came back
`400 invalid_blocks`, losing every diagnostic.
"""

import pytest

from monitor.models import Review
from monitor.sinks.slack import MAX_BLOCKS_PER_MESSAGE, MAX_SECTION_CHARS, SlackSink
from monitor.sources.base import SourceHealth


class Capturing(SlackSink):
    """Records payloads instead of posting them."""

    def __init__(self):
        super().__init__("https://hooks.slack.example/test")
        self.payloads = []

    def _post(self, payload):
        self.payloads.append(payload)
        return True


def _sections(payload):
    return [b["text"]["text"] for b in payload["blocks"]]


def _all_sections(sink):
    return [s for p in sink.payloads for s in _sections(p)]


def _assert_slack_safe(sink):
    """Every constraint Slack enforces on an incoming-webhook payload."""
    for p in sink.payloads:
        assert len(p["blocks"]) <= MAX_BLOCKS_PER_MESSAGE
        assert p.get("text"), "fallback text is required"
        assert len(p["text"]) <= 500
        for s in _sections(p):
            assert len(s) <= 3000, f"section is {len(s)} chars, Slack rejects >3000"


# ------------------------------------------------------------- health alerts

def _health(target, note, ok=True):
    h = SourceHealth(source="shopify", target=target)
    if ok:
        h.warn(note)
    else:
        h.fail(note)
    return h


def test_health_alert_with_many_targets_stays_within_limits():
    """The exact shape that failed in production: 30 targets, verbose notes."""
    sink = Capturing()
    sink.notify_health([
        _health(f"some-quite-long-app-handle-number-{i}",
                "parsed 0 of 3 negative reviews the rating buckets promised; "
                "unrecognized country line 'Somewhere Odd'")
        for i in range(30)
    ])
    assert sink.payloads
    _assert_slack_safe(sink)


def test_health_alert_survives_one_absurdly_long_note():
    sink = Capturing()
    sink.notify_health([_health("app", "x" * 20000, ok=False)])
    _assert_slack_safe(sink)


def test_health_alert_counts_failures_and_warnings_separately():
    sink = Capturing()
    sink.notify_health([
        _health("broken-app", "could not fetch reviews page", ok=False),
        _health("odd-app", "unrecognized country", ok=True),
    ])
    joined = "\n".join(_all_sections(sink))
    assert "1 failed" in joined
    assert "1 warning" in joined
    assert "broken-app" in joined
    assert "odd-app" in joined


def test_health_alert_mentions_where_full_detail_lives():
    """Slack is a summary; the log and state file hold everything."""
    sink = Capturing()
    sink.notify_health([_health("app", "something")])
    joined = "\n".join(_all_sections(sink))
    assert "workflow log" in joined
    assert "seen.json" in joined


def test_no_alert_when_nothing_is_unhealthy():
    sink = Capturing()
    sink.notify_health([])
    assert sink.payloads == []


# ------------------------------------------------------------ review messages

def _review(**kw):
    base = dict(
        source="shopify", competitor_handle="userway-accessibility",
        competitor_name="UserWay Website Accessibility", bucket="overlay",
        rating=1, review_date="2025-05-19",
        review_text="Paid version literally does nothing for WCAG compliance.",
        reviewer_store_name="Nutrex Hawaii", country="United States",
        used_app_for="3 days using the app",
        review_url="https://apps.shopify.com/userway-accessibility/reviews",
    )
    base.update(kw)
    return Review(**base)


def test_review_message_contains_the_useful_fields():
    sink = Capturing()
    sink.notify_reviews([_review()])
    body = "\n".join(_all_sections(sink))
    assert "Nutrex Hawaii" in body
    assert "United States" in body
    assert "3 days using the app" in body
    assert "WCAG compliance" in body
    assert "apps.shopify.com" in body
    _assert_slack_safe(sink)


def test_very_long_review_is_clipped_not_rejected():
    sink = Capturing()
    sink.notify_reviews([_review(review_text="word " * 4000)])
    _assert_slack_safe(sink)


def test_one_message_per_review():
    sink = Capturing()
    sink.notify_reviews([_review(), _review(reviewer_store_name="TIVVI")])
    assert len(sink.payloads) == 2


def test_multiline_review_is_quoted_on_every_line():
    sink = Capturing()
    sink.notify_reviews([_review(review_text="First line.\nSecond line.")])
    body = "\n".join(_all_sections(sink))
    assert "> First line." in body
    assert "> Second line." in body


def test_missing_store_name_does_not_produce_an_empty_bold_run():
    sink = Capturing()
    sink.notify_reviews([_review(reviewer_store_name="", country="", used_app_for="")])
    body = "\n".join(_all_sections(sink))
    assert "unknown store" in body
    _assert_slack_safe(sink)


def test_dry_run_posts_nothing():
    sink = SlackSink("https://hooks.slack.example/test", dry_run=True)
    sink.notify_reviews([_review()])          # must not raise
    sink.notify_health([_health("app", "n")])


def test_no_webhook_configured_is_treated_as_dry_run():
    sink = SlackSink("")
    assert sink.dry_run is True
    sink.notify_reviews([_review()])
