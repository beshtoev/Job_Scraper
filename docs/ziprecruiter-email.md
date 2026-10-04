# ZipRecruiter via email alerts

ZipRecruiter's site sits behind an interactive Cloudflare check, so no scheduled scraper —
GitHub's or the Mac's — can search it. Its **email alerts carry the same postings**, so the
pipeline reads those instead: saved searches on ziprecruiter.com email their results, and the
local runner reads that mailbox hourly over IMAP (**read-only**: it never marks, moves or
deletes anything) and feeds the job cards through the normal title/location filters into
`output/ziprecruiter_jobs.json`, exactly as if a scraper had found them.

## One-time setup (about 10 minutes, needs you)

1. **Create the alerts.** Sign in at ziprecruiter.com and save a search for each term you
   care about (the list in `config.json` under `search_terms.ziprecruiter` is the menu:
   *VP Data AI, Head of Data, VP Analytics, Data Governance, Finance Transformation, VP FP&A,
   Enterprise AI, Digital Transformation, Chief Financial Officer*), location Toronto, ON /
   Canada-remote, with **email alerts on** (daily is fine; instant is fresher).
2. **Make an app password** for the Gmail account that receives them:
   Google Account → Security → 2-Step Verification → App passwords (2-Step must be on).
   The account's real password will not work and must not be used.
3. **Fill in the mailbox config** at `~/.job-scraper/email.json`:

   ```json
   {
     "user": "you@gmail.com",
     "app_password": "xxxx xxxx xxxx xxxx"
   }
   ```

   Optional keys: `host` (default `imap.gmail.com`), `folder` (`INBOX`),
   `since_days` (`7` — how far back each run looks; emails older than that are simply
   left alone). Keep the file private: `chmod 600 ~/.job-scraper/email.json`.
   It lives outside the repo and is never committed or pushed.

Until the file is filled in, the hourly slot is a no-op that keeps the previous results and
logs "Mailbox not configured" — nothing breaks, nothing is overwritten.

## How it behaves

- **A quiet week keeps the data.** No alert emails, an unreadable mailbox, or a missing
  config all preserve the previous `ziprecruiter_jobs.json` (the same rule the blocked
  scrapers follow), so results never vanish because ingestion hiccupped.
- **Repeat alerts dedupe.** Alert links carry ZipRecruiter's own job id (`lvk`), which the
  pipeline already uses as the job's identity, so the same posting in Monday's and
  Tuesday's digest is one job.
- **Layout drift is reported, not swallowed.** An email that parses to zero job cards is
  logged with its subject line ("the layout may have changed") so the parser can be fixed.

Test it by hand after setup:

```bash
~/.job-scraper/venv/bin/python ~/.job-scraper/repo/scrape_jobs.py --ziprecruiter-email
```
