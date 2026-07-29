# Setup — GitHub Actions, web UI

Nine steps, ~20 minutes. **You don't need Python installed, and with GitHub
Desktop you don't need the terminal either.** Every verification step runs inside
GitHub Actions.

The order matters: the schedule is deliberately disarmed until step 8, so you can
confirm the parser works and swallow the ~27 existing negative reviews before any
alerts fire.

---

## 1. Create the Clay table and webhook

1. In Clay, click **+ Create New** → **Import data from Webhook** → **Create new table**.
2. Name it something like `Competitor Negative Reviews`.
3. Copy the webhook URL Clay shows you. It looks like
   `https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-...`
4. **Skip the auth token for now.** The URL is itself a secret and lives in GitHub
   Secrets; adding a token means matching Clay's exact header name, which is a
   detail you can add later if you want it. If you do add one, the header the code
   sends is `x-clay-webhook-auth` — confirm that matches what Clay expects and
   adjust `monitor/sinks/clay.py` if not.

Clay creates the columns from the first payload it receives, so there's nothing to
define by hand. After the first real review lands, come back and set `review_id`
as the table's unique column.

**Hold onto that URL.** You'll paste it in step 4.

---

## 2. Create the Slack incoming webhook

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Name it `Review Monitor`, pick the TestParty workspace.
3. Left sidebar → **Incoming Webhooks** → toggle **Activate Incoming Webhooks** on.
4. **Add New Webhook to Workspace** → choose the channel (create something like
   `#competitor-signals` first if you want it separate from existing channels).
5. Copy the webhook URL — `https://hooks.slack.com/services/T.../B.../...`

If your workspace restricts app installation you'll need an admin to approve it.
That's the one step here that might need someone else.

---

## 3. Create the repo and push the code

First, **unzip `shopify-review-monitor.zip` somewhere permanent** — `~/Developer/`
or wherever you keep code. Don't work inside the Downloads folder or the temporary
folder Claude wrote it to; that gets cleared.

**Pick ONE of the two paths below and follow it end to end.** They create the
GitHub repo in different places — mixing them leaves you with a half-configured
remote and errors that look unrelated to the real cause.

---

### Path A — GitHub Desktop (recommended)

Handles authentication, repo creation, and the push in one flow. No tokens.

**Do not create the repo on github.com first** — Desktop creates it for you, and
it will refuse to publish if a repo with that name already exists. If you already
made an empty one, delete it (repo → **Settings** → bottom of the page → **Delete
this repository**; it's empty, nothing is lost).

1. Install [GitHub Desktop](https://desktop.github.com), open it, sign in. It
   sends you through the browser — that's your auth done.
2. **File → Add Local Repository**
3. Choose the unzipped folder → **Add Repository**. If you already ran `git init`
   and committed, Desktop picks that up; if not, it offers to create the repo.
4. Click **Publish repository** in the top bar.
5. Name it `shopify-review-monitor`, leave **Keep this code private** ticked,
   **Publish repository**.

Refresh github.com — all 30 files should be there. Skip Path B entirely.

---

### Path B — Terminal

First create the repo on github.com: **+** (top right) → **New repository**.

- Owner: your TestParty org, or your personal account
- Name: `shopify-review-monitor`
- **Private** ← important, the state file will contain scraped review data
- Do **not** add a README, .gitignore, or licence — the folder already has them
- Leave the page open; you'll need the URL in a moment

Then:

```bash
cd ~/Documents/Developer/shopify-review-monitor    # wherever you actually unzipped it
git config --global user.name  "Your Name"
git config --global user.email "you@testparty.ai"
git init
git add .
git commit -m "Competitor negative-review monitor"
git branch -M main
```

Now set the remote. **Don't copy the URL below** — go to your new empty repo on
github.com and copy the HTTPS URL from the green **Code** button, then:

```bash
git remote set-url origin <paste-the-url-here>   # or `git remote add origin ...` the first time
git push -u origin main
```

**Authentication: your GitHub password will not work.** GitHub disabled password
auth for git in 2021. If you see
`remote: Invalid username or token. Password authentication is not supported`,
that's this — you need a token:

1. github.com → avatar → **Settings** → **Developer settings** (bottom of the left
   sidebar) → **Personal access tokens** → **Tokens (classic)** →
   **Generate new token (classic)**
2. Tick the top-level **`repo`** scope. Nothing else is needed.
3. **Generate token** and copy it — it's not shown again.
4. `git config --global credential.helper osxkeychain` so you only do this once.
5. Push again. Username = your GitHub username, **password = the token**.

### Common mistakes here

| What you'll see | Cause |
|---|---|
| `cd: no such file or directory` | You're not where you unzipped it. `pwd` to check, or drag the folder onto the Terminal icon in Finder. |
| `Authentication failed` after entering your password | Password auth is dead. Use a token (above). |
| `error: remote origin already exists` | Use `git remote set-url origin ...` instead of `git remote add`. |
| `fatal: 'origin' does not appear to be a git repository` | There's no remote set. You're mixing Path A and Path B — run `git remote -v`; if it's empty, either finish in GitHub Desktop or `git remote add origin <url>`. |
| `Repository not found` | The remote URL has a typo, or you never created the repo, or it's under a different owner than the URL says. |
| `nothing to commit, working tree clean` | Already committed. Harmless — skip ahead to the push. |

> **Avoid the drag-and-drop uploader on github.com.** It silently skips the
> `.github/` folder because it starts with a dot, and without that folder no
> workflow exists and nothing past step 4 will work.

---

## 4. Add the two secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **Secrets** tab →
**New repository secret**. Add both:

| Name | Value |
|---|---|
| `CLAY_WEBHOOK_URL` | the URL from step 1 |
| `SLACK_WEBHOOK_URL` | the URL from step 2 |

(`CLAY_WEBHOOK_AUTH` is optional and only needed if you set a token in Clay.)

Secrets are write-only — you won't be able to read them back, which is expected.

---

## 5. Verify the parser against live pages — don't skip this

Repo → **Actions** tab. If you see "Workflows aren't being run on this forked
repository", click the button to enable them.

Click **review-monitor** in the left sidebar → **Run workflow** (top right) →
set **command** to `doctor` → **Run workflow**.

Wait ~1 minute, click into the run, open the **Run monitor** step.

**What a healthy run looks like:**

```
========================================================================
userway-accessibility
========================================================================
  total reviews : 9
  overall rating: 2.4
  buckets       : {5: 6, 1: 3, 4: 0, 3: 0, 2: 0}
  rating 1: expected 3, found 3 blocks via selector:[data-merchant-review]
    - 2025-07-29 | 1* | 'JCS Wildlife' ('United States') | 'Over 1 year using the app'
      text: This is very expensive. And we're being told this doesn't work very well....
...
doctor: all checks passed.

Confirmed working live on 2026-07-29 against both apps.
```

The two things to check: **real review text appears**, and the numbers after
`expected` and `found` match.

`via heuristic:date+used-for` instead of `via selector:...` is completely fine —
that's the structural fallback doing its job, which is exactly why it exists.

**If it says `found 0 review blocks`:** the parser needs a fix. Paste me the log
output and I'll patch `find_review_blocks()`. This is the one plausible failure,
because the CSS selectors were written without access to a live browser. Don't
continue to step 6 until doctor passes.

---

## 6. Do a dry run

**Run workflow** again → command `run` → tick **dry_run** → **Run workflow**.

This walks all 53 apps and logs what it *would* send, touching neither Clay nor
Slack. Takes ~2 minutes. You should see lines like:

```
[1/53] accesswidget
[2/53] userway-accessibility
...
27 negative reviews visible across 53 apps, 27 of them new
[dry-run] would POST to Clay: {"source": "shopify", ...}
```

The count should land somewhere near 27. Wildly lower means something's off —
check the log for `WARNING` lines.

Note that a dry run still writes to `state/seen.json` and commits it, so the
reviews it saw are now marked as seen. That's harmless; step 7 is what makes it
deliberate.

---

## 7. Backfill

> **Already ran `run` without ticking dry_run?** Then this is already done — the
> reviews were delivered and recorded, and you can skip to step 9. Check Clay and
> Slack to confirm they arrived.

**Run workflow** → command `backfill` → **Run workflow**.

This marks every currently-visible negative review as already-seen and notifies
nobody. Afterwards you'll see a `state: 2026-07-29T...` commit on the repo, and
`state/seen.json` will hold ~27 ids.

**Skipping this means your first scheduled run posts all 27 historical reviews to
Slack in one burst** — including complaints from 2024 that are useless as leads.

---

## 8. Send one real message end-to-end

You've now suppressed everything, so a normal run will be silent — which is
correct but proves nothing about your webhooks. To test the delivery path, unsuppress
one review:

1. In the repo, open `state/seen.json` → pencil icon (**Edit this file**).
2. Delete any one string from the `"seen"` array (keep the JSON valid — mind the
   commas).
3. **Commit changes** directly to `main`.
4. **Actions** → **review-monitor** → **Run workflow** → command `run`, dry_run
   **unticked**.

Within a minute you should get exactly one Slack message and one new row in Clay.
If the Slack message arrives but Clay stays empty, the Clay URL is wrong — the log
will show `Clay rejected` with the status code.

---

## 9. Arm the schedule

The cron is gated on a repo variable, so nothing runs on a timer until you flip it.

**Settings** → **Secrets and variables** → **Actions** → **Variables** tab →
**New repository variable**:

| Name | Value |
|---|---|
| `MONITOR_ENABLED` | `true` |

Done. It now runs every 2 hours at :07.

To change cadence, edit the one `cron:` line in
`.github/workflows/monitor.yml` — the alternatives and their minute costs are
listed in a comment right above it. To pause it, set `MONITOR_ENABLED` to `false`
rather than deleting anything.

---

## Cost

Free. Private repos get 2,000 Actions minutes/month, jobs bill rounded up to the
whole minute, and at ~3 minutes a run every 2 hours you'll use ~1,080 — about 54%.
Daily would be ~90 minutes (5%). Hourly is the only cadence that overruns the free
tier, at ~2,160.

Watch **Settings → Billing → Actions** for the first month to see your real
per-run duration, then re-tune if you want.

---

## Once it's live

**Where things show up**

- New negative review → Slack message + Clay row, within 2 hours
- Scraper broken → Slack alert prefixed `⚠️ Review monitor needs attention`
- Every run → a `state:` commit, so the repo history is an audit log of what was
  seen when
- Each run's **Summary** tab shows the command, whether the schedule is armed, and
  how many review ids are tracked

**Expect long silences.** 1–3 negative reviews a month across the whole category is
normal. Silence is the product working, not failing — which is exactly why the
dead-man's switch exists: it alerts when the scraper stops seeing *any* reviews,
so you never have to wonder whether the quiet is real.

**When Shopify changes its markup** (it will, eventually), you'll get the health
alert. Run `doctor` from the Actions tab, see which strategy is matching, and fix
`monitor/sources/shopify.py`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| No **Run workflow** button | Workflow file isn't on the default branch. Check `.github/workflows/monitor.yml` exists on `main`. |
| Scheduled runs never fire | `MONITOR_ENABLED` variable missing or not exactly `true`. Note it's a *Variable*, not a Secret. |
| `Permission to ... denied` on the commit step | Repo → Settings → Actions → General → Workflow permissions → set **Read and write permissions**. |
| Slack works, Clay doesn't | Wrong Clay URL, or an auth token set in Clay that the code isn't sending. Log shows `Clay rejected` + status. |
| Everything silent after weeks | Expected. Confirm with a manual `doctor` run. |
| Cron stops after ~2 months | GitHub disables schedules in inactive repos — but each run commits state, which counts as activity, so this shouldn't happen. If it does, there's a "re-enable" button on the Actions tab. |
| Health alert naming one app | Probably an unrecognized country name. The alert says which; add it via `tools/gen_countries.py`. |
