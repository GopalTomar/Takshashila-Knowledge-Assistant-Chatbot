# Knowledge-base updates

> This file used to describe a weekly refresh driven by Windows Task Scheduler on a
> laptop. That setup is superseded.

The knowledge base now refreshes **daily at 06:00 Asia/Kolkata on GitHub Actions**
(`.github/workflows/kb-refresh.yml`) — incrementally, validated, and promoted
atomically — and the Railway API picks up each new release automatically. No local
machine needs to be on.

* How it works: [ARCHITECTURE.md](ARCHITECTURE.md) §3
* Setup: [DEPLOYMENT.md](DEPLOYMENT.md) §1–2, §6
* Day-to-day commands and status: [OPERATIONS.md](OPERATIONS.md)

Manual/local refresh (same code path as the scheduled job):

```bash
python scripts/refresh_kb.py            # incremental, website + Commit KB
python scripts/refresh_kb.py --dry-run  # show what would change
python scripts/refresh_kb.py --status   # last / next run (IST)
```

`scripts/run_update.bat`, `scripts/setup_windows_task.ps1` and `scripts/scheduler.py`
remain available for self-hosted use only; they call the same refresh implementation.
