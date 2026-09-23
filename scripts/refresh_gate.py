#!/usr/bin/env python3
"""
refresh_gate.py — Is the daily KB refresh due right now?

GitHub Actions cron is UTC-only and can start late, so the workflow polls
frequently and asks this gate. The gate is the single place where the schedule
is interpreted — always in KB_REFRESH_TIMEZONE (default Asia/Kolkata), never UTC:

    due  ⇔  KB_REFRESH_ENABLED
            and local_now >= today's KB_REFRESH_TIME (in KB_REFRESH_TIMEZONE)
            and no refresh attempt has started yet on today's local date

The "attempted today" check reads the status JSON published by the previous run
(``last_run`` / ``last_attempt_date``). A failed run is therefore not retried
every 30 minutes; the next day's run retries normally (transient HTTP errors
are already retried with backoff inside the run).

    python scripts/refresh_gate.py --status-file kb-status.json [--now 2026-09-23T00:31:00Z] [--force]

Prints ``due=true|false`` and ``reason=...``; appends both to $GITHUB_OUTPUT when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def decide(now_utc: datetime, refresh_time: str, tz_name: str, enabled: bool,
           last_attempt_local_date: str = "") -> tuple:
    if not enabled:
        return False, "KB_REFRESH_ENABLED is false"
    tz = ZoneInfo(tz_name)
    local = now_utc.astimezone(tz)
    hh, _, mm = refresh_time.partition(":")
    scheduled = local.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
    today = local.date().isoformat()
    if local < scheduled:
        return False, f"not yet {refresh_time} {tz_name} (local time {local:%Y-%m-%d %H:%M})"
    if last_attempt_local_date == today:
        return False, f"already attempted today ({today} {tz_name})"
    return True, f"due: {local:%Y-%m-%d %H:%M} {tz_name} >= {refresh_time}"


def _last_attempt_date(status_file: str, tz_name: str) -> str:
    if not status_file or not Path(status_file).exists():
        return ""
    try:
        st = json.loads(Path(status_file).read_text(encoding="utf-8"))
    except Exception:
        return ""
    raw = st.get("last_attempt_date") or ""
    if raw:
        return raw
    last = st.get("last_run") or ""
    try:
        return datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(ZoneInfo(tz_name)).date().isoformat()
    except ValueError:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status-file", default="")
    ap.add_argument("--now", default="", help="ISO UTC time (testing)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    tz_name = os.getenv("KB_REFRESH_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata"
    refresh_time = os.getenv("KB_REFRESH_TIME", "06:00") or "06:00"
    enabled = (os.getenv("KB_REFRESH_ENABLED", "true") or "true").lower() in ("1", "true", "yes", "on")
    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now
           else datetime.now(timezone.utc))
    if args.force:
        due, reason = True, "forced (manual dispatch)"
    else:
        due, reason = decide(now, refresh_time, tz_name, enabled,
                             _last_attempt_date(args.status_file, tz_name))
    print(f"due={'true' if due else 'false'}")
    print(f"reason={reason}")
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"due={'true' if due else 'false'}\nreason={reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
