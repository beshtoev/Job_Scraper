# Local runner: scraping from your Mac

GitHub's servers are a poor place to run some scrapers:

- **Timing.** GitHub's scheduler fires an "hourly" workflow only a few times a day. An exhaustive search
  (2026-09-24) showed the LinkedIn watcher had captured about a quarter of new relevant roles, because each
  run only looks back one hour and most hours had no run.
- **Blocking.** Glassdoor's Cloudflare answers GitHub-hosted runners with an instant `403` on every page.

A Mac on a home connection has neither problem, so a background job runs, most urgent first:

| Source | Every | Why this cadence |
|---|---|---|
| LinkedIn | 20 min | window sized to the time since the last success |
| Indeed | 30 min | 24h window; Indeed dates postings by the day |
| Glassdoor | 60 min | dates by the day and scores requests for bots |
| Company portals | 3 h | a full poll takes ~20 min, so it runs last |

When several sources are due in one tick they run in that order, and the due list is re-read after each one,
so LinkedIn never waits behind a whole portal poll plus everything else. Indeed and the portals moved here on
2026-09-26: GitHub fired the "hourly" Indeed watcher 8 times in 48 hours, with gaps up to 12.9 hours, and
polled the portals once a day. Built In, Google Jobs and the government boards stay on GitHub.

## What each run does

1. Resets a private clone in `~/.job-scraper/repo` to the newest `origin/main` (your dashboard folder is never
   used for this).
2. Runs the source's scraper (`scrape_jobs.py --linkedin-only`, `--indeed-only`, `--glassdoor-only`, or
   `portal_scraper.py`), then adds geography, and stamps the success in `output/scrape_state.json`.
3. Pushes **only `output/` data files** to `main` (code can never be pushed; the runner refuses). If GitHub
   pushed first, it re-merges by job URL instead of overwriting.
4. Copies the fresh jobs into your dashboard folder's `output/`, so `localhost:8080` updates on its own.

LinkedIn's search window is the time since the last **successful** scrape (plus 15 minutes), capped at 7 days.
A sleeping Mac therefore only *delays* discovery; nothing posted meanwhile is lost. The GitHub workflows for
LinkedIn, Indeed and the portals read that stamp (`scripts/skip_if_fresh.py`) and stand down while the Mac is
covering the source (45 min, 75 min and 6 h respectively). When it isn't, they take over as the fallback, now
around the clock: the old 15:00-03:00 UTC window skipped the Toronto morning, which is exactly when a Mac
that slept overnight needs covering.

## Install / check / remove

```bash
scripts/install_local_runner.sh                       # from your checkout; it becomes the dashboard folder
~/.job-scraper/venv/bin/python ~/.job-scraper/repo/scripts/local_runner.py status
tail -f ~/.job-scraper/logs/runner.log
scripts/uninstall_local_runner.sh                     # add --purge to delete ~/.job-scraper too
```

Change cadence in `~/.job-scraper/config.json`, e.g. `{"sources": {"linkedin": {"every_minutes": 15}}}`.
Force a run: `local_runner.py run linkedin`. Bring your dashboard checkout's code up to date without losing its
job data: `local_runner.py sync-dashboard` (GitHub also commits `output/`, so a plain `git pull` collides).

## Limits and trust

- **It runs only while the Mac is awake and online.** A closed lid means no scraping; the next tick after wake
  catches up.
- **It runs whatever is on `main`,** on your Mac, with your GitHub login (for pushing data). Treat merges to
  `main` as code that will run here.
- ZipRecruiter is not covered: its site sits behind an interactive Cloudflare human check.
