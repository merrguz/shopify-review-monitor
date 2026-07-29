"""Runner-level tests: dedupe across runs, dead-man's switch, sink wiring."""

import json
from pathlib import Path

import pytest

from monitor import run as run_mod
from monitor.models import Review
from monitor.sources.base import SourceHealth

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "competitors.yaml"
    p.write_text(
        "defaults: {max_rating: 3, pages: 1}\n"
        "apps:\n"
        "  - {handle: userway-accessibility, name: UserWay, bucket: overlay}\n"
    )
    return p


def _stub_http(monkeypatch):
    from monitor.sources import shopify as shopify_mod

    def fake_get(url, *, params=None, **kw):
        handle = url.rstrip("/").split("/")[-2]
        path = FIXTURES / f"{handle}.attrs.html"
        if not path.exists():
            pytest.skip("run tests/fixtures/build_fixtures.py first")
        return path.read_text()

    monkeypatch.setattr(shopify_mod.http, "get", fake_get)


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _args(cfg, state, **kw):
    base = dict(config=str(cfg), state=str(state), dry_run=True,
                fail_on_error=False, verbose=False)
    base.update(kw)
    return Args(**base)


def test_first_run_reports_all_then_second_run_reports_nothing(
    monkeypatch, cfg_file, tmp_path, caplog
):
    _stub_http(monkeypatch)
    state = tmp_path / "seen.json"

    sent = []
    monkeypatch.setattr(
        run_mod.SlackSink, "notify_reviews",
        lambda self, reviews: sent.extend(reviews),
    )

    assert run_mod.cmd_run(_args(cfg_file, state)) == 0
    assert len(sent) == 3, "first run should surface all three existing 1-stars"

    saved = json.loads(state.read_text())
    assert len(saved["seen"]) == 3
    assert saved["meta"]["shopify"]["userway-accessibility"]["total_reviews_seen"] == 9

    sent.clear()
    assert run_mod.cmd_run(_args(cfg_file, state)) == 0
    assert sent == [], "second run must not re-notify anything"


def test_backfill_marks_seen_without_notifying(monkeypatch, cfg_file, tmp_path):
    _stub_http(monkeypatch)
    state = tmp_path / "seen.json"

    notified = []
    monkeypatch.setattr(
        run_mod.SlackSink, "notify_reviews",
        lambda self, reviews: notified.extend(reviews),
    )

    assert run_mod.cmd_backfill(_args(cfg_file, state)) == 0
    assert notified == []
    assert len(json.loads(state.read_text())["seen"]) == 3

    # A subsequent normal run stays quiet, which is the point of backfill.
    assert run_mod.cmd_run(_args(cfg_file, state)) == 0
    assert notified == []


def test_dead_mans_switch_fires_when_nothing_parses(monkeypatch, cfg_file, tmp_path):
    """Zero NEW reviews is normal. Zero TOTAL reviews across every app is a
    broken parser, and must alert."""
    from monitor.sources import shopify as shopify_mod

    monkeypatch.setattr(
        shopify_mod.http, "get",
        lambda url, **kw: "<html><body>" + ("x" * 3000) + "</body></html>",
    )

    alerts = []
    monkeypatch.setattr(
        run_mod.SlackSink, "notify_health",
        lambda self, unhealthy: alerts.extend(unhealthy),
    )

    rc = run_mod.cmd_run(_args(cfg_file, tmp_path / "seen.json"))
    assert rc == 0
    assert alerts, "expected a health alert"
    assert any(a.target == "ALL" for a in alerts)
    assert any("parser is almost certainly broken" in n
               for a in alerts for n in a.notes)


def test_fail_on_error_exits_nonzero(monkeypatch, cfg_file, tmp_path):
    from monitor.sources import shopify as shopify_mod

    monkeypatch.setattr(
        shopify_mod.http, "get",
        lambda url, **kw: "<html><body>" + ("x" * 3000) + "</body></html>",
    )
    monkeypatch.setattr(run_mod.SlackSink, "notify_health", lambda self, u: None)

    rc = run_mod.cmd_run(_args(cfg_file, tmp_path / "seen.json", fail_on_error=True))
    assert rc == 1


def test_no_alert_when_healthy_and_nothing_new(monkeypatch, cfg_file, tmp_path):
    _stub_http(monkeypatch)
    state = tmp_path / "seen.json"
    monkeypatch.setattr(run_mod.SlackSink, "notify_reviews", lambda self, r: None)

    alerts = []
    monkeypatch.setattr(
        run_mod.SlackSink, "notify_health",
        lambda self, unhealthy: alerts.extend(unhealthy),
    )

    run_mod.cmd_run(_args(cfg_file, state))       # first run: populates state
    alerts.clear()
    run_mod.cmd_run(_args(cfg_file, state))       # steady state: silence
    assert alerts == []


def test_one_broken_app_does_not_kill_the_run(monkeypatch, tmp_path):
    """A single app raising must not abort the other 50."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "defaults: {max_rating: 3, pages: 1}\n"
        "apps:\n"
        "  - {handle: explodes, name: Boom, bucket: overlay}\n"
        "  - {handle: userway-accessibility, name: UserWay, bucket: overlay}\n"
    )

    from monitor.sources import shopify as shopify_mod

    def fake_get(url, *, params=None, **kw):
        if "explodes" in url:
            raise RuntimeError("unexpected boom")
        path = FIXTURES / "userway-accessibility.attrs.html"
        if not path.exists():
            pytest.skip("run tests/fixtures/build_fixtures.py first")
        return path.read_text()

    monkeypatch.setattr(shopify_mod.http, "get", fake_get)

    sent = []
    monkeypatch.setattr(run_mod.SlackSink, "notify_reviews",
                        lambda self, r: sent.extend(r))
    monkeypatch.setattr(run_mod.SlackSink, "notify_health", lambda self, u: None)

    rc = run_mod.cmd_run(_args(cfg, tmp_path / "seen.json"))
    assert rc == 0
    assert len(sent) == 3, "the healthy app should still have been collected"


def test_failed_clay_delivery_is_retried_next_run(monkeypatch, cfg_file, tmp_path):
    """A review that never reached Clay must not be marked as seen."""
    _stub_http(monkeypatch)
    state = tmp_path / "seen.json"

    monkeypatch.setattr(run_mod.SlackSink, "notify_reviews", lambda self, r: None)
    monkeypatch.setattr(run_mod.SlackSink, "notify_health", lambda self, u: None)
    monkeypatch.setattr(run_mod.ClaySink, "send", lambda self, reviews: set())

    run_mod.cmd_run(_args(cfg_file, state, dry_run=False))
    assert json.loads(state.read_text())["seen"] == []

    delivered = []
    monkeypatch.setattr(
        run_mod.ClaySink, "send",
        lambda self, reviews: {r.review_id for r in reviews},
    )
    monkeypatch.setattr(run_mod.SlackSink, "notify_reviews",
                        lambda self, r: delivered.extend(r))
    run_mod.cmd_run(_args(cfg_file, state, dry_run=False))
    assert len(delivered) == 3
    assert len(json.loads(state.read_text())["seen"]) == 3
