# Operations runbook

Production: **GitHub Pages** (frontend) → **Render Free** (FastAPI + RAG +
Mattermost `/mattermost/ask`) ← **GitHub Actions** (daily 06:00 IST refresh →
encrypted `kb-latest` bundle). See DEPLOYMENT.md for setup.

## Render service

* Health: `GET /health` (liveness, Render health check), `GET /ready` (200 once the KB
  is loaded), `GET /rag/status` (KB version, bundle sync, refresh health).
* Cold start after the free instance sleeps: ~4 minutes (download + verify + decrypt
  + load at 0.1 CPU); `/ready` is 503 meanwhile. Optional keep-awake: repository
  variable `RENDER_KEEPALIVE_URL` (`.github/workflows/keepalive.yml`).
* Daily KB swap (`KB_LOW_MEMORY=true`): ~40 s of 503 while the new release loads;
  a failed load restores the previous release.
* Logs: Render → service → Logs (one JSON line per request; query text is not
  logged unless `LOG_QUERY_TEXT=true`).
* Smoke test: `python scripts/smoke_test.py --api https://<service>.onrender.com`.

## Daily refresh

* Schedule: `KB_REFRESH_TIME` (default `06:00`) in `KB_REFRESH_TIMEZONE`
  (default `Asia/Kolkata`) — interpreted by `scripts/refresh_gate.py`, never as UTC.
  The GitHub workflow polls every 30 minutes; at most one attempt per local day.
* Where: `.github/workflows/kb-refresh.yml` on GitHub-hosted runners.
* Result: encrypted bundle + manifest on the `kb-latest` release; the API activates
  it within `KB_SYNC_INTERVAL_MINUTES`.

### Status

| What | How |
|---|---|
| Last run / health | `gh release download kb-latest -p kb-status.json -O -` |
| Full report | inside the bundle: `reports/daily_refresh/latest.json`; locally `data/reports/daily_refresh/` |
| Serving version | `GET /api/health` → `kb.version`, `kb_sync` |
| Admin summary | `GET /api/refresh-status` with a staff token; Streamlit Automation tab (admin) |
| Run history | `gh run list --workflow kb-refresh.yml` |

`health` becomes `degraded` after `KB_REFRESH_DEGRADED_AFTER` (3) consecutive
failed/partial runs. Degraded never deletes or rebuilds data; the last good
release keeps serving. GitHub emails the repo owner on every failed run.

### Report fields (`latest.json`)

`run_id, started_at, completed_at, duration_seconds, timezone, mode, website{status,
pages_discovered, pages_fetched, new, modified, unchanged, failed, redirected,
gone_404, removed, removal_pending, by_change_and_type}, commit_kb{…}, merge, index{status,
documents, chunks, embedded, cached, new_chunks, removed_chunks, version}, validation{status,
errors, warnings, counts}, totals, promoted, errors, warnings`.

## Manual commands

```bash
python scripts/refresh_kb.py                  # incremental, both sources (same as the daily job)
python scripts/refresh_kb.py --website        # website only
python scripts/refresh_kb.py --commit-kb      # Commit KB only
python scripts/refresh_kb.py --full           # full crawl
python scripts/refresh_kb.py --dry-run        # report changes, write nothing
python scripts/refresh_kb.py --validate-only  # validate the active release
python scripts/refresh_kb.py --rebuild-only   # re-index current documents (staged + validated)
python scripts/refresh_kb.py --audit-only     # website broken-link/crawl audit
python scripts/refresh_kb.py --status         # health / last / next run (IST)
```

Remote (no laptop): `gh workflow run kb-refresh.yml -f force=true [-f full=true]`.

## Common situations

| Symptom | Action |
|---|---|
| `commit_kb: authentication failed (HTTP 401)` | Commit KB password changed → update the `COMMIT_KB_PASSWORD` secret. Website still refreshes; run is `partial`. |
| `Document count dropped … refusing to promote` | A crawl returned far fewer pages (site outage/restructure). Inspect `reports/crawl/website_latest.json`; if the drop is genuine, run `--full` after confirming, or lower the guard. |
| `crawl stopped at the page cap` | Raise `WEBSITE_MAX_PAGES`. |
| API `kb_sync.last_error` set | Bundle download/verify failed; the old KB keeps serving. Check `KB_BUNDLE_KEY` matches the GitHub secret and the manifest URL. |
| Need to roll back | See DEPLOYMENT.md §7. |

## Key rotation

* `KB_BUNDLE_KEY`: generate a new key, set it in GitHub **and** Render, then run the
  refresh workflow (new bundle is encrypted with the new key).
* `MATTERMOST_SLASH_TOKEN` / `MATTERMOST_ACTION_SECRET`: regenerate in Mattermost,
  update Render; existing buttons on old posts stop working (by design).
* `API_ACCESS_TOKENS`: edit the Render environment variable; remove a token to revoke it.

## Observability

The API logs one JSON line per request (`event`, `request_id`, `path`, `status`, `ms`)
and per query (`scope`, `mode`, `latency`, `confidence`, `n_citations`, `source_ids`,
`retrieved`, `timings`). Query text is not logged unless `LOG_QUERY_TEXT=true`.
Secrets are never logged.
