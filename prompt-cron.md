# Weekly Cron — Implementation Plan

## Goal

Run `billboard-to-ytmusic-sync -v` every **Wednesday 04:00 KST** without standing up any servers, and email the full terminal log to **nambin.heo@gmail.com** afterwards. Free.

## Platform: GitHub Actions

GitHub Actions wins for this use case:

- **Free for private repos** up to 2,000 minutes/month on the standard plan. A weekly run takes ~1–2 minutes, so we'll use 4–8 minutes/month — comfortably free.
- **Native cron trigger** built into the `on:` block of a workflow file. No external scheduler.
- **No infra to maintain.** Runners are ephemeral, provisioned per-run, torn down after. Zero ops.

Alternatives considered and rejected:

| Option | Why not |
| --- | --- |
| AWS Lambda + EventBridge | User explicitly said no AWS. Also extra packaging work for the Python deps. |
| Cloudflare Workers / Vercel Cron | Native runtime is JS/TS; running Python is awkward. |
| Render / Fly.io free dynos | Always-on infra to keep alive between runs; adds maintenance. |
| Local Task Scheduler (Windows) | Only fires when laptop is on and awake. Unreliable for a weekly cadence. |

## Schedule: cron expression

- Korea Standard Time = **UTC+9**, no daylight savings (Korea doesn't observe DST), so the conversion is stable year-round.
- Wed 04:00 KST = **Tue 19:00 UTC**.
- Cron expression: `0 19 * * 2` (POSIX cron; day-of-week 2 = Tuesday).
- Also expose `workflow_dispatch:` so the user can trigger a run manually from the Actions UI for testing without waiting for Wednesday.

### GitHub Actions cron caveats

- **Delay tolerance**: scheduled workflows can fire 5–30 minutes late under load. Irrelevant for a weekly playlist sync.
- **60-day inactivity rule**: scheduled workflows are automatically disabled if the repo has had no commits or workflow runs in 60 days. A weekly cron *does* count as activity, so this only bites if the workflow itself fails for 60 consecutive days. Worth knowing but not solving up-front.

## Secrets management

The sync needs three secrets at runtime:

1. **`browser.json`** — YouTube Music session cookies.
2. **`GEMINI_API_KEY`** — for the LLM rescue.
3. **`MAIL_PASSWORD`** — Gmail app password for sending email (see below).

The user has said it's acceptable to commit `browser.json` and `.env` since the repo is private. I'd recommend against it anyway, and use GitHub Secrets instead:

### Option A — GitHub Secrets (recommended)

Store the three values in **Settings → Secrets and variables → Actions** in the GitHub repo. The workflow reads them at runtime as env vars; nothing sensitive ever lives in the repo or git history.

**Pros:**
- Repo stays clean. `.gitignore` doesn't have to change.
- Secrets are encrypted at rest, masked in logs by GitHub.
- Rotating a key is a one-click UI edit, no commit needed.
- If the repo's visibility ever flips to public (or you grant a collaborator), nothing is exposed.

**Cons:**
- One-time copy/paste setup in the GitHub UI (3 secrets).
- The workflow needs a couple of extra lines to materialize `browser.json` from the secret value.

### Option B — Commit `browser.json` and `.env`

Force-add the files (override `.gitignore`) and let the workflow pick them up by `actions/checkout`.

**Pros:**
- Workflow is one line shorter.

**Cons:**
- Secrets live in git history forever; rotation requires a commit.
- If the repo is ever made public, secrets leak.
- Rotating cookies (which expires monthly-ish) means a commit each time.

**My recommendation: A.** The setup cost is ~2 minutes once. After that, the operational overhead is the same as Option B (rotate via GitHub UI vs. via commit), but the security posture is meaningfully better.

## Email delivery

**Provider:** Gmail SMTP using an **app-specific password**.

Why Gmail SMTP rather than a third-party email API (Resend, Mailgun, SendGrid, SES)?

- The user already has `nambin.heo@gmail.com`; no new account needed.
- 4 emails/month is well below Gmail SMTP's casual-use limits.
- App-specific passwords are stable and don't require OAuth2 setup.

**Requirements (one-time setup):**

1. Enable 2-Factor Authentication on the user's Google account (if not already).
2. Visit <https://myaccount.google.com/apppasswords>, create a password for "Mail" + "Other (Custom name)" — call it e.g. "billboard-sync GitHub Action".
3. Copy the 16-character password and store it as the `MAIL_PASSWORD` GitHub Secret.

**Action:** `dawidd6/action-send-mail@v3` — well-maintained, supports Gmail SMTP, attachments, HTML body.

**Email shape:**

- **From:** `nambin.heo@gmail.com`
- **To:** `nambin.heo@gmail.com` (sending to self)
- **Subject:** `Billboard sync — success` or `Billboard sync — failed (exit 3)` depending on outcome
- **Body:** the full terminal log wrapped in HTML `<pre>` tags so column alignment is preserved in the Gmail viewer
- **Attachment:** `output.log` (same content, for archival / mobile reading)

The workflow captures the binary's stdout/stderr to `output.log` via `tee`, then `dawidd6/action-send-mail@v3` ships it.

## Workflow shape

```yaml
name: Weekly Billboard sync

on:
  schedule:
    - cron: "0 19 * * 2"   # Tue 19:00 UTC = Wed 04:00 KST
  workflow_dispatch:        # manual trigger for testing

jobs:
  sync:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -e .

      - name: Materialize browser.json from secret
        env:
          BROWSER_JSON: ${{ secrets.BROWSER_JSON }}
        run: printf '%s' "$BROWSER_JSON" > browser.json

      - name: Run sync
        id: sync
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: billboard-to-ytmusic-sync -v 2>&1 | tee output.log
        continue-on-error: true

      - name: Capture log into env for email body
        run: |
          {
            echo "MAIL_BODY<<EOF_LOG"
            echo "<pre>"
            cat output.log
            echo "</pre>"
            echo "EOF_LOG"
          } >> "$GITHUB_ENV"

      - name: Email result
        uses: dawidd6/action-send-mail@v3
        with:
          server_address: smtp.gmail.com
          server_port: 465
          secure: true
          username: nambin.heo@gmail.com
          password: ${{ secrets.MAIL_PASSWORD }}
          from: nambin.heo@gmail.com
          to: nambin.heo@gmail.com
          subject: "Billboard sync — ${{ steps.sync.outcome }}"
          html_body: ${{ env.MAIL_BODY }}
          attachments: output.log
```

**Key behavior:**

- `continue-on-error: true` on the sync step ensures the email always fires — including when the sync fails (exit 1/2/3/4). The subject embeds `steps.sync.outcome` (`success` / `failure`) so the user can triage from the inbox without opening.
- The job has `permissions: contents: read` — no write needed since the workflow doesn't commit anything.

## GitHub Secrets to configure (one-time)

| Secret name | Value |
| ----------- | ----- |
| `BROWSER_JSON` | Paste the entire contents of the local `browser.json` file. |
| `GEMINI_API_KEY` | The Gemini AI Studio key currently in `.env`. |
| `MAIL_PASSWORD` | The Gmail app password generated above. |

Set them at: **Repo Settings → Secrets and variables → Actions → New repository secret**.

## Cost

| Component | Cost |
| --------- | ---- |
| GitHub Actions runner time (~8 min/month) | $0 (free tier covers 2,000 min/month for private repos) |
| Gmail SMTP outbound | $0 |
| Gemini API (LLM rescue, free-tier) | $0 |
| **Total** | **$0/month** |

## Failure modes and behavior

| Failure | Sync exit code | Email behavior |
| ------- | -------------- | -------------- |
| YT Music cookies expired | 3 | Email arrives with subject `Billboard sync — failure`, full log shows the auth-failure message and instruction to regenerate `browser.json`. |
| `GEMINI_API_KEY` missing/invalid | 1 | Email with `failure`, log shows the LLM init error. (Shouldn't happen if the secret is set; just covered.) |
| Billboard parser breaks | 2 | Email with `failure`, log names the parser module. |
| Billboard or YT Music network outage | 4 | Email with `failure`, log shows the retry-exhausted message. |
| Workflow itself errors (e.g. SMTP send fails) | n/a | GitHub's built-in failure notification fires (email to repo admin's GitHub account). Worst case: silent miss for one week — next week's run still produces a fresh email. |

## Test plan

1. Push the workflow file.
2. Set the three secrets in the GitHub UI.
3. Trigger the workflow manually via **Actions → Weekly Billboard sync → Run workflow**.
4. Verify: workflow completes, email arrives in `nambin.heo@gmail.com` with full log as body + attachment.
5. Wait for the next Wed 04:00 KST to confirm the cron schedule actually fires (don't trust the schedule until it's fired once).

## Non-goals (this iteration)

- No multi-recipient email.
- No alternate scheduler.
- No SMS/Slack/Discord notification.
- No retry on workflow failure beyond GitHub's built-in transient-failure retries.
- No commit-bumping cadence to keep the cron warm — the 60-day inactivity rule isn't a problem for a successful weekly cron.

## Open questions

1. **Secrets management** — Option A (GitHub Secrets, recommended) or Option B (commit `browser.json` and `.env` to the private repo)?
2. **Email on every run, or only on failure / when there are skipped songs?** Default is every run; user said "I'd like to get an email" without qualifier, so I'm going with every-run. Easy to flip to conditional later.
3. **Time zone confirmation** — I'm reading "Wednesday 4 AM Korea time" as 04:00 KST (= Tue 19:00 UTC). Please confirm.

Once these are answered, the actual change is small: one new file (`.github/workflows/sync.yml`) plus the GitHub Secrets setup. No code changes inside `billboard_sync/`.
