# Seven-day discovery-quality pilot

This pilot evaluates the existing discovery system before any JobMatchAI or
Resume-Matcher integration. It runs from **September 11 through September 17,
2026**, using Toronto/GTA, Ontario, and Canada-remote roles that match Murat's
executive Data, AI, Analytics, Finance, Governance, Technology, Platform,
Automation, and Transformation taxonomy.

Daily evidence is private and lives under `pilot/private/` (gitignored). The
method and scripts are version-controlled.

## Daily routine

Use the bundled/current Python 3.10+ interpreter.

```bash
python3 scripts/run_discovery_pilot.py --sources linkedin
python3 scripts/pilot_quality.py queue
python3 scripts/pilot_quality.py label JOB_ID --verdict relevant --status active \
  --scope top_queue --notes "Why this is or is not a fit"
python3 scripts/pilot_quality.py snapshot
```

`linkedin-test` is only a non-production preflight probe over two search terms;
do not use it alone to estimate recall. The daily default is the full configured
LinkedIn watcher. For a wider run, pass one or more of `linkedin`, `indeed`,
`glassdoor`, `ziprecruiter`,
`google-jobs`, or `hiringcafe`. Every source attempt is recorded, including
failures. A failed source must not be silently replaced with another source.

Review the top 10 queue entries each day. Also perform a broad recall check
outside the admitted queue and record plausible omissions with `--scope
recall_check` and `--discovered no`. Use `--scope rejected_sample` for a role
rejected by the discovery filters.

## Labels

- `relevant`: should be near the top of Murat's active executive search.
- `borderline`: potentially useful, but level, mandate, location evidence, or
  domain breadth needs judgment.
- `not_relevant`: should not consume review time.
- `active`: posting appears open and is no more than 30 days old.
- `stale` / `closed`: old or no longer accepting applications.
- `unknown`: posting status could not be verified.

## Measures

- **Strict precision@10** = relevant labelled top-10 roles / labelled top-10
  roles.
- **Useful precision@10** = relevant or borderline labelled top-10 roles /
  labelled top-10 roles.
- **Missed relevant roles** = active relevant roles found in broad recall checks
  or reviewed rejection samples that discovery did not admit. The scorecard
  reports strict (`relevant`) and useful (`relevant` or `borderline`) counts.
- **Duplicate rate** = duplicate raw records / all raw records, using canonical
  URLs and a company-title-location fallback identity.
- **Stale rate** = roles older than 30 days or manually labelled stale/closed /
  unique roles. Unknown dates are reported separately.
- **Source failure rate** = failed source attempts / all recorded attempts.

The final September 17 report should recommend concrete query, geography,
taxonomy, deduplication, freshness, and source changes. Do not integrate either
downstream matching product until Murat reviews that report.
