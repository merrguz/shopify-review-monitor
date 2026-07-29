"""Runner + CLI.

Commands
--------
run       Poll every enabled target, send new negative reviews to Clay and Slack.
doctor    Diagnostic. Fetch a handful of pages and report which parse strategy
          matched and what it extracted. Run this first, and any time the
          dead-man's switch fires.
backfill  Mark everything currently visible as already-seen without notifying.
          Run once after setup so you don't get 25 historical reviews at once.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List

from bs4 import BeautifulSoup

from . import http
from .config import Config, State, env
from .models import Review
from .sinks import ClaySink, SlackSink
from .sources import REGISTRY
from .sources.base import SourceHealth
from .sources.shopify import ShopifySource, find_review_blocks, parse_review_block, parse_summary

log = logging.getLogger("monitor")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "competitors.yaml"
DEFAULT_STATE = ROOT / "state" / "seen.json"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def collect_all(cfg: Config, source_name: str = "shopify"):
    source = REGISTRY[source_name]
    all_reviews: List[Review] = []
    healths: List[SourceHealth] = []

    for i, app in enumerate(cfg.apps, 1):
        log.info("[%d/%d] %s", i, len(cfg.apps), app["handle"])
        try:
            reviews, health = source.collect(app, cfg.defaults)
        except Exception as exc:  # noqa: BLE001 - one bad app must not kill the run
            health = SourceHealth(source=source_name, target=app["handle"])
            health.fail(f"unhandled error: {type(exc).__name__}: {exc}")
            reviews = []
            log.exception("collect failed for %s", app["handle"])
        all_reviews.extend(reviews)
        healths.append(health)

    return all_reviews, healths


def cmd_run(args) -> int:
    cfg = Config.load(args.config)
    state = State(args.state)

    reviews, healths = collect_all(cfg)

    new = [r for r in reviews if state.is_new(r.review_id)]
    log.info(
        "%d negative reviews visible across %d apps, %d of them new",
        len(reviews), len(cfg.apps), len(new),
    )

    clay = ClaySink(env("CLAY_WEBHOOK_URL"), env("CLAY_WEBHOOK_AUTH"), dry_run=args.dry_run)
    slack = SlackSink(env("SLACK_WEBHOOK_URL"), dry_run=args.dry_run)

    delivered = clay.send(new)
    slack_batch = [r for r in new if r.review_id in delivered]
    slack.notify_reviews(slack_batch)
    for r in slack_batch:
        state.mark(r.review_id)

    # --- dead-man's switch -------------------------------------------------
    # A healthy run finds zero NEW reviews most of the time. What it must never
    # find is zero TOTAL reviews on an app that has some.
    unhealthy = [h for h in healths if not h.ok or h.notes]
    total_seen = sum(h.total_reviews_seen for h in healths)
    if healths and total_seen == 0:
        broken = SourceHealth(source="shopify", target="ALL")
        broken.fail(
            f"parsed 0 total reviews across all {len(healths)} apps. "
            "The parser is almost certainly broken, not the internet."
        )
        unhealthy.insert(0, broken)

    for h in healths:
        state.record_health(h.source, h.target, {
            "ok": h.ok,
            "total_reviews_seen": h.total_reviews_seen,
            "negative_expected": h.negative_expected,
            "negative_parsed": h.negative_parsed,
            "notes": h.notes,
        })

    if unhealthy:
        # Log every note BEFORE attempting Slack. The first real run lost all 30
        # diagnostics because the Slack alert was rejected and the notes lived
        # nowhere else -- a diagnostic channel that vanishes precisely when
        # something is wrong is worse than none.
        log.warning("%d of %d targets reported problems:", len(unhealthy), len(healths))
        for h in unhealthy:
            level = log.error if not h.ok else log.warning
            for note in (h.notes or ["(no detail recorded)"]):
                level(
                    "  %s %-45s %s",
                    "FAIL" if not h.ok else "warn", h.target, note,
                )
        log.warning(
            "counts per target: %s",
            ", ".join(
                f"{h.target}={h.negative_parsed}/{h.negative_expected}"
                for h in unhealthy
                if h.negative_expected
            ) or "(none had expected negatives)",
        )
        slack.notify_health(unhealthy)

    state.save()
    log.info("state saved to %s (%d ids tracked)", args.state, len(state.seen))

    hard_failures = [h for h in unhealthy if not h.ok]
    if hard_failures and args.fail_on_error:
        log.error("%d hard failures; exiting non-zero", len(hard_failures))
        return 1
    return 0


def cmd_backfill(args) -> int:
    """Mark everything currently visible as seen, notifying nobody."""
    cfg = Config.load(args.config)
    state = State(args.state)
    reviews, _ = collect_all(cfg)
    for r in reviews:
        state.mark(r.review_id)
    state.save()
    log.info(
        "backfilled %d existing reviews into state; future runs will only "
        "report genuinely new ones", len(reviews),
    )
    return 0


def cmd_doctor(args) -> int:
    """Verify the parser against live pages and print what it found.

    Defaults to two apps that are known to have negative reviews, so a healthy
    doctor run should print real review text. If it prints nothing, the parser
    needs attention -- start with the reported block strategy.
    """
    handles = args.handles or ["userway-accessibility", "accesswidget"]
    src = ShopifySource()
    problems = 0

    for handle in handles:
        url = src.reviews_url(handle)
        print(f"\n{'=' * 72}\n{handle}\n{'=' * 72}")
        try:
            html = http.get(url)
        except http.FetchError as exc:
            print(f"  FETCH FAILED: {exc}")
            problems += 1
            continue

        summary = parse_summary(html)
        print(f"  total reviews : {summary['total']}")
        print(f"  overall rating: {summary['rating']}")
        print(f"  buckets       : {summary['buckets']}")
        if summary["total"] is None:
            print("  !! could not parse a total count -- markup likely changed")
            problems += 1

        negatives = {r: c for r, c in summary["buckets"].items() if r <= 3 and c}
        if not negatives:
            print("  no negative reviews on this app (expected for most)")
            continue

        for rating, expected in negatives.items():
            params = [("ratings[]", str(rating)), ("sort_by", "newest")]
            try:
                page = http.get(url, params=params)
            except http.FetchError as exc:
                print(f"  rating {rating}: FETCH FAILED: {exc}")
                problems += 1
                continue
            soup = BeautifulSoup(page, "html.parser")
            blocks, strategy = find_review_blocks(soup)
            print(f"\n  rating {rating}: expected {expected}, "
                  f"found {len(blocks)} blocks via {strategy}")
            if len(blocks) < expected:
                problems += 1
            for blk in blocks:
                p = parse_review_block(blk, assumed_rating=rating)
                if not p:
                    print("    - (block yielded nothing)")
                    problems += 1
                    continue
                snippet = (p["review_text"] or "")[:110].replace("\n", " ")
                print(f"    - {p['review_date']} | {p['rating']}* | "
                      f"{p['reviewer_store_name']!r} ({p['country']!r}) | "
                      f"{p['used_app_for']!r}")
                print(f"      text: {snippet}...")
                if p["developer_reply"]:
                    print(f"      reply: {p['developer_reply'][:70]}...")
                missing = [k for k in ("review_date", "review_text", "reviewer_store_name")
                           if not p.get(k)]
                if missing:
                    print(f"      !! missing fields: {missing}")
                    problems += 1

    print(f"\n{'=' * 72}")
    if problems:
        print(f"doctor found {problems} problem(s). Fields commonly break in this "
              f"order: country -> store name -> body. Check monitor/sources/shopify.py "
              f"parse_review_block().")
    else:
        print("doctor: all checks passed.")
    return 1 if problems else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="monitor", description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--state", default=str(DEFAULT_STATE))
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="poll and notify")
    r.add_argument("--dry-run", action="store_true", help="log instead of sending")
    r.add_argument("--fail-on-error", action="store_true",
                   help="exit non-zero if any target hard-failed")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("backfill", help="mark existing reviews as seen, notify nobody")
    b.set_defaults(func=cmd_backfill, dry_run=True, fail_on_error=False)

    d = sub.add_parser("doctor", help="verify the parser against live pages")
    d.add_argument("handles", nargs="*", help="app handles (default: two known-negative apps)")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
