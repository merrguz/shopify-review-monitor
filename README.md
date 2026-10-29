# Competitor negative-review monitor

Polls the Shopify App Store for new 1–3★ reviews of TestParty's competitors and
pushes each one to a Clay table plus a Slack channel. Built to extend to
Trustpilot and G2 — both have scaffolded adapters with notes.

Runs every 2 hours on GitHub Actions. Costs nothing.

**Deploying it? Follow [SETUP.md](SETUP.md)** — nine steps, no local Python needed.

---

## Read this first: what this signal actually is

**60 apps monitored, ~46 negative reviews in existence, arriving at maybe 2–5 per
month.** accessiBe has 3. UserWay has 3. Thirty of the sixty apps have no reviews
at all.

That's not a bug in the tooling — it's the size of the signal. Each lead is
excellent (a merchant publicly complaining that an overlay didn't deliver
compliance is about as warm as intent gets), but this is a trickle, not a
pipeline.

Worth understanding *why* it's so small: the Shopify App Store is a narrow window
onto these vendors. accessiBe has 15 reviews there in total — that is not a
company with 15 customers, it's a company that sells direct and collected a $1M
FTC order in 2025. Its unhappy customers are on Trustpilot, G2, and in court
filings. So ~46 is not the amount of dissatisfaction with overlay vendors; it's
the amount visible through this one channel. See "Where the volume actually is".

---

## Setup

### 1. Clay webhook

In Clay: **+ Create New → Import data from Webhook → new table**. Copy the
webhook URL. Optionally set an auth token.

The first POST creates the columns automatically. Payload shape:

```json
{
  "review_id": "shopify:userway-accessibility:h3f9c1a...",
  "source": "shopify",
  "competitor_handle": "userway-accessibility",
  "competitor_name": "UserWay Website Accessibility",
  "bucket": "overlay",
  "rating": 1,
  "review_date": "2025-05-19",
  "review_text": "Paid version literally does nothing for WCAG compliance...",
  "reviewer_store_name": "Nutrex Hawaii",
  "country": "United States",
  "used_app_for": "3 days using the app",
  "developer_reply": "",
  "developer_reply_date": null,
  "review_url": "https://apps.shopify.com/userway-accessibility/reviews?...",
  "platform_review_id": null,
  "first_seen_at": "2026-07-29T14:07:11+00:00"
}
```

Set `review_id` as the table's unique/dedupe column. The script already dedupes,
but belt and braces.

### 2. Slack webhook

Create an incoming webhook for whichever channel should get these
(`#competitive-signals` or similar) at
`https://api.slack.com/apps` → your app → **Incoming Webhooks**.

### 3. Repo secrets

`Settings → Secrets and variables → Actions`:

| Secret | Required | Notes |
|---|---|---|
| `CLAY_WEBHOOK_URL` | yes | from step 1 |
| `CLAY_WEBHOOK_AUTH` | no | only if you set a token on the Clay webhook |
| `SLACK_WEBHOOK_URL` | yes | from step 2 |

### 4. Confirm the parser matches today's markup — do not skip this

(All of this can be done from the Actions tab instead — see [SETUP.md](SETUP.md).)

```bash
pip install -r requirements.txt
python -m monitor doctor
```

`doctor` fetches two apps that are known to have 1★ reviews and prints what it
extracted. A healthy run prints real review text, store names and countries, and
ends with `doctor: all checks passed.`

**Why this step exists:** the parser was written without access to a live browser,
so while the extraction logic is covered by 71 unit and integration tests, the
CSS selectors it tries first have not been confirmed against the real DOM. If the
selectors miss, the structural fallback should still work — `doctor` tells you
which strategy matched (`selector:...` vs `heuristic:...`). Either is fine.
If it reports `found 0 review blocks`, that's the one case needing a code fix, and
`monitor/sources/shopify.py` → `find_review_blocks()` is where to look.

### 5. Backfill, so you don't get 27 alerts at once

```bash
python -m monitor backfill
git add state/ && git commit -m "backfill existing reviews" && git push
```

This marks every currently-visible negative review as already-seen without
notifying anyone. Skip it and your first scheduled run will dump all 27 into Slack at once.

Then enable the workflow. That's it.

---

## Commands

```bash
python -m monitor doctor                      # verify the parser against live pages
python -m monitor doctor accesswidget patrol  # ...for specific handles
python -m monitor run --dry-run               # full poll, log instead of send
python -m monitor run                         # the real thing
python -m monitor backfill                    # mark existing as seen, notify nobody
python -m pytest -q                           # 71 tests, no network needed
```

---

## How it works

```
competitors.yaml ──> ShopifySource ──> Review objects ──> dedupe (state/seen.json)
                                                            │
                                                            ├──> ClaySink  (one POST per review)
                                                            └──> SlackSink (one message per review)
                     SourceHealth ─────────────────────────────> dead-man's switch
```

### Five decisions worth knowing about

**1. There is no event trigger. There cannot be.** Shopify publishes no reviews
API, no webhook, no RSS. Hourly polling is as close to real-time as this data
source gets. The pages are server-rendered, so no browser is needed.

**2. `sort_by=newest` is always sent.** The site default is "Most relevant",
which will silently hide new reviews. This is the single easiest way to build a
monitor that appears to work and doesn't.

**3. Ratings come from the request, not the DOM.** We fetch one page per rating
(`?ratings[]=1`), so a review's rating is known before parsing and we never
depend on counting star glyphs. Multi-value filters are never used —
`ratings[]=1&ratings[]=2&ratings[]=3` was observed collapsing to just
`ratings[]=1` against `accesswidget`, silently dropping the 3★ review. If the DOM
does expose a rating and it contradicts what we asked for, that's a health
warning: it's the canary for that filter changing behavior again.

**4. One request per app in the common case.** The unfiltered reviews page
carries per-rating bucket counts, so we read those first and only fetch filtered
pages for ratings that actually have reviews. Most apps have none, so ~55 apps
costs ~60 requests per run rather than 165. Those bucket counts also give us an
authoritative "how many negatives should exist" number to check the parse
against.

**5. Field extraction is line-based, not CSS-based.** Shopify churns class names;
the visual order of a review does not:

```
July 29, 2025                       <- date
This is very expensive. And ...     <- body
JCS Wildlife                        <- store name
United States                       <- country
Over 1 year using the app           <- duration
userway.org replied                 <- reply, if any
```

The parser anchors on the `... using the app` line and walks outward. That
survives restyling in a way `.tw-text-fg-secondary` does not.

### Dedupe

`review_id` prefers a real platform ID and falls back to a content hash of
`(source, handle, date, rating, store, first 200 chars of body)`. The hash
deliberately **excludes** `used_app_for`, because "3 days using the app" becomes
"About 1 month using the app" over time — include it and every review re-notifies
forever. Whitespace is normalized for the same reason.

A review is only marked as seen once Clay confirms delivery, so a failed POST is
retried next run rather than lost.

### The dead-man's switch

The failure mode that matters isn't a crash — it's a scraper that returns zero
reviews forever after Shopify changes its markup, while everyone assumes the
competitors just stopped getting complaints.

So: **zero new reviews is normal and silent. Zero *total* reviews is an alert.**
Specifically it alerts when

- no total review count can be parsed for an app (markup changed),
- zero total reviews parse across *every* app (parser broken),
- fewer negatives parse than the rating buckets promised,
- an unrecognized country name appears (which would shift fields by one line),
- the DOM rating contradicts the requested rating filter.

It deliberately does **not** alert on an app that simply has no reviews. 30 of the
53 monitored apps have none, and their pages render `## Reviews` with no `(N)` —
there is no count to parse. Reading that as a failure fired 30 false alarms on the
very first live run, which is how an alert channel gets muted and then ignored.

Getting this right was fiddly enough to be worth stating: Shopify renders the
"Counts per rating level" scaffold *with all zeros* on review-less pages, so its
presence does **not** prove reviews exist. `parse_summary()` instead treats a page
as valid-but-empty when it carries reviews-page furniture (a canonical URL ending
in `/reviews`, or the literal string "No reviews yet") and takes the bucket sum as
the count. It guards the opposite direction too: if the count parses as zero but
review blocks are visibly present on the page, that's a hard failure rather than a
comfortable zero.

Health notes are written into `state/seen.json` under `meta`, so the git history
of that file is a running record of scraper health.

`monitor/http.py` also treats a 200 response with a suspiciously small body as a
soft failure and retries with backoff. Shopify serves empty-but-200 shells under
load; taking those at face value is exactly how a scraper silently dies.

### State in git

`state/seen.json` is committed back to the repo after every run. Two nice side
effects: it needs no external datastore, and the commit history becomes a
permanent, diffable audit log of every review ever seen. It also keeps the repo
"active", which matters because GitHub disables scheduled workflows in repos with
60 days of no activity.

---

## Tuning

`competitors.yaml` holds 55 apps, every handle verified live on 2026-07-29,
grouped by `bucket`:

| bucket | what it is | default |
|---|---|---|
| `overlay` | overlay/widget vendors — the core competitor set | on |
| `code-fix` | source-code remediation, TestParty's direct positioning | on |
| `scanner` | audit/scan/monitoring only | on |
| `alt-text` | image alt-text SEO tools | on (trial) |
| `gdpr-bundle` | cookie apps that bundle an a11y widget | **off** |

**gdpr-bundle is disabled** — Consentmo and Pandectes have ~4,700 reviews between
them, nearly all about cookie banners. That's ~25 negatives of pure noise.

**alt-text is on as a trial.** It roughly doubles volume (~21 more negatives,
16 of them on `alttext-ai` alone). The case for it: alt text is WCAG 1.1.1, and a
merchant who bought an alt-text tool has solved images and nothing else — partial
coverage is an opening. The case against: reading their actual negative reviews,
most are about credit consumption and billing rather than accessibility failure.
They arrive in Slack tagged `alt-text tool`, so judge for yourself and disable the
block if it's noise.

Per-app overrides: `max_rating` (default 3) and `pages` (default 1; one page
comfortably covers apps with single-digit negative counts).

Handle gotchas already baked in, since these are easy to guess wrong:

- accessiBe is **`accesswidget`**, not `accessibe`
- Clvyr is **`clevyre-accessibility-widget`** (slug spells out the "e"s)
- Amrita is **`accessibility-widget`** (deceptively generic)
- `accessproof-2` was a separate listing from `accessproof-1`; it was delisted
  (404) on 2026-10-29 and removed
- **AudioEye's listing is delisted** and **EqualWeb has no Shopify listing** —
  neither is monitorable here. They only show up via Trustpilot/G2.

---

## Adding Trustpilot and G2

Both adapters exist as documented scaffolds in `monitor/sources/`. Implement
`collect()` and register in `sources/__init__.py`; nothing downstream changes.

**Trustpilot** — do this one first. It has an official Business Unit API
(`api.trustpilot.com/v1/business-units/{id}/reviews?stars=1&stars=2&stars=3`) with
proper multi-value filtering and stable review IDs. If you scrape instead, parse
the `__NEXT_DATA__` JSON blob rather than the DOM. This is where the volume is,
and it's the only way to see AudioEye and EqualWeb at all.

**G2** — hard. Aggressive Cloudflare bot management; a plain request from a GitHub
Actions runner gets a challenge page, and datacenter IPs are blocked essentially
always. Don't burn a day on headers and sleeps. Realistic paths are a paid
unblocker (~$50–100/mo) or G2's own Content/Buyer Intent API — and if you're
spending money anyway, the intent feed is a better product than scraped reviews.

**One important modelling caveat for both:** Trustpilot and G2 reviewers are
individuals, frequently anonymized ("Verified User in Retail"). You get the
complaint but not the company, so they're competitive intel and messaging
research rather than leads. Only Shopify reviews give you a store name you can
resolve to a domain. Tag those rows accordingly in Clay so nobody tries to
prospect a ghost.

---

## Clay enrichment after ingestion

The script delivers the review. Turning it into a lead needs a few Clay columns:

1. **`store_domain`** — "Find company from name" on `reviewer_store_name`,
   falling back to a Claygent search for `"<store name>" shopify store`. Expect
   70–85% resolution; single-store merchants are the misses.
2. **Widget-still-installed check** — an HTTP column fetching the candidate
   homepage and grepping for `acsbapp.com` / `userway.org` / `equalweb.com`. This
   both confirms the domain match *and* confirms they're still a customer. Make
   it a required gate before anyone emails — plenty of these merchants already
   churned off the competitor.
3. **`pain_category`** — AI column: `no compliance results` / `still got sued` /
   `pricing` / `support` / `bugs` / `performance`.
4. **`fit_score`** — 1–10, weighted toward the compliance-failure and lawsuit
   categories.
5. **Contacts + email waterfall** — Head of Ecommerce, Head of Digital, VP
   Marketing, Legal/Compliance.

Leave unresolved rows in a `Needs manual lookup` view. At 1–3 leads a month,
hand-resolving stragglers costs someone five minutes.

---

## Data quality notes

- **Category-grid review counts are stale and wrong.** Observed: ALT SEO showed
  4.1/20 in the grid vs 4.8/47 on its `/reviews` page. This code only ever reads
  `/reviews`.
- **Displayed star averages are recency-weighted** and don't match the rating
  buckets — accessiBe shows 3.5 where the buckets compute to ~4.3. Don't quote
  these averages in outbound copy without checking the math.
- **Reviews get edited and deleted.** Re-check a review still exists before
  quoting it back at a merchant.
- **accessiBe carries the "Built for Shopify" badge** despite the 3.5 rating and
  the 2025 FTC order. Shopify's endorsement is not on your side here.

---

## Where the volume actually is

Ranked by leads-per-unit-effort, this monitor is not first on the list:

1. **Overlay install detection.** Any store running an overlay script is a
   prospect whether or not they reviewed it. BuiltWith or Store Leads filtered on
   `acsbapp.com` / `userway.org` / `equalweb.com` gives thousands of accounts
   instead of three. Lower intent each, vastly more of them. If you build one
   more thing, build this.
2. **ADA litigation filings.** Public Title III web-accessibility suits are the
   highest-intent signal in this market and publish continuously.
3. **Trustpilot/G2 negative reviews.** More volume than the Shopify App Store,
   weaker lead data.
4. **This monitor.** Best intent, lowest volume.

Use it to prioritize within a bigger list rather than as the list itself.
