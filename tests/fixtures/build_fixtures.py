"""Generates the HTML fixtures used by the tests.

The REVIEW CONTENT here is real -- transcribed from apps.shopify.com on
2026-07-29. The MARKUP is synthetic, in two deliberately different shapes, to
prove the parser doesn't depend on any particular class names:

  variant "attrs"    - review blocks carry `data-merchant-review` (the happy path)
  variant "generic"  - anonymous nested divs, no useful attributes at all
                       (forces the structural heuristic fallback)

If Shopify changes its markup, these tests will still pass -- they validate the
extraction LOGIC. Validating the real selectors is what `python -m monitor doctor`
is for. Both matter; neither substitutes for the other.
"""

from pathlib import Path

HERE = Path(__file__).parent

# (date, rating, body, store, country, used_for, reply_date, reply)
USERWAY_1_STAR = [
    (
        "July 29, 2025", 1,
        "This is very expensive. And we're being told this doesn't work very well.",
        "JCS Wildlife", "United States", "Over 1 year using the app", None, None,
    ),
    (
        "May 19, 2025", 1,
        "Paid version literally does nothing for WCAG compliance. Removed 0 items "
        "from the audit list provided by a third party. Zero. None. Waste of $50 bucks.",
        "Nutrex Hawaii", "United States", "3 days using the app", None, None,
    ),
    (
        "November 4, 2024", 1,
        "We have this and after spending $1300 on a yearly plan and we are currently "
        "involved in a lawsuit that claims this is not compliant and told by my lawyer "
        "widget attracts more lawsuits as there is softwear that lawyers use to detect widgets.",
        "Left Field NYC", "United States", "About 1 month using the app",
        "December 15, 2024",
        "Thank you for sharing your concerns, and we're sorry to hear about your "
        "situation. We offer Legal Support Services designed to help organizations "
        "respond to legal claims and strengthen compliance efforts.",
    ),
]

ACCESSIBE_1_STAR = [
    (
        "July 16, 2026", 1,
        "Beyond the fact that the app dramatically slowed down our website, to the "
        "point that it needed to be uninstalled, the company continues to misrepresent "
        "the legal coverage that its services provide to a website that uses it.",
        "ResilienZ Health", "United States", "3 months using the app", None, None,
    ),
    (
        "June 19, 2024", 1,
        "I set it up on a test URL then changed my URL for launch and they're saying I "
        "have to buy an additional plan for $50 for the new URL and can't use the one I "
        "already paid $50 for.",
        "TIVVI", "United States", "23 days using the app",
        "July 15, 2024",
        "Thank you for your feedback. We truly appreciate you bringing this issue to "
        "our attention.",
    ),
]

SUMMARY_TMPL = """
<h2>Reviews ({total})</h2>
<div>Overall rating</div><div>{rating}</div>
<div>Counts per rating level</div>
<ul>
{levels}
</ul>
<script type="application/ld+json">
{{"@type":"SoftwareApplication","name":"{name}",
 "aggregateRating":{{"@type":"AggregateRating","ratingValue":"{rating}","reviewCount":"{total}"}}}}
</script>
"""


def _levels(handle: str, buckets: dict, total: int) -> str:
    out = []
    for star in (5, 4, 3, 2, 1):
        n = buckets.get(star, 0)
        pct = round(n / total * 100) if total else 0
        if n:
            out.append(
                f'<li><div>{star}</div><div>{pct}% of ratings are {star} stars</div>'
                f'<a href="/{handle}/reviews?ratings%5B%5D={star}">{n}</a></li>'
            )
        else:
            out.append(
                f'<li><div>{star}</div><div>0% of ratings are {star} stars</div>'
                f'<div>0</div></li>'
            )
    return "\n".join(out)


def _review_html(rec, variant: str) -> str:
    date, rating, body, store, country, used_for, rdate, reply = rec
    reply_html = ""
    if reply:
        reply_html = (
            f'<div class="x1"><div>vendor replied</div><div>{rdate}</div>'
            f'<p>{reply}</p><button>Show more</button></div>'
        )
    inner = (
        f'<div aria-label="{rating} out of 5 stars"></div>'
        f'<div>{date}</div>'
        f'<div><p>{body}</p><button>Show more</button></div>'
        f'<div><div>{store}</div><div>{country}</div><div>{used_for}</div></div>'
        f'{reply_html}'
    )
    if variant == "attrs":
        return f'<div data-merchant-review="true" data-review-content-id="{abs(hash(body)) % 10**7}">{inner}</div>'
    return f'<div class="q7z"><div class="k2p">{inner}</div></div>'


def build(handle: str, name: str, total: int, rating: float, buckets: dict,
          reviews: list, variant: str) -> str:
    body = "\n".join(_review_html(r, variant) for r in reviews)
    return f"""<!doctype html><html><head><title>Reviews: {name}</title></head><body>
<nav>Shopify App Store</nav>
{SUMMARY_TMPL.format(total=total, rating=rating, name=name,
                     levels=_levels(handle, buckets, total))}
<section id="reviews">
{body}
</section>
<footer>App categories Top categories Resources Shopify</footer>
<!-- padding to clear the min_bytes soft-failure threshold {'.' * 2200} -->
</body></html>"""


def main() -> None:
    specs = [
        ("userway-accessibility", "UserWay Website Accessibility", 9, 2.4,
         {5: 6, 1: 3}, USERWAY_1_STAR),
        ("accesswidget", "accessiBe Web Accessibility", 15, 3.5,
         {5: 11, 4: 1, 3: 1, 1: 2}, ACCESSIBE_1_STAR),
    ]
    for variant in ("attrs", "generic"):
        for handle, name, total, rating, buckets, reviews in specs:
            html = build(handle, name, total, rating, buckets, reviews, variant)
            (HERE / f"{handle}.{variant}.html").write_text(html)
    print(f"wrote fixtures to {HERE}")


if __name__ == "__main__":
    main()
