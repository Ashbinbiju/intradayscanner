#!/usr/bin/env python
"""
Record one screener snapshot to disk.

The screener API has no history -- it only reports what is happening right
now. Backtesting needs the opposite: what was on the list *at the time*, on a
past day. Picking today's movers and running them over last week's charts is
hindsight, and it flatters every result.

So run this on a schedule during market hours and the history builds itself:

    # every 5 minutes, 09:15-15:30 IST, Mon-Fri
    */5 4-10 * * 1-5  cd /path/to/ANGELONE && python record_snapshot.py

On Windows, Task Scheduler with the same command works. Files land in
data/screener/YYYY-MM-DD/HHMM.json and the scanner's "Recorded snapshot"
source reads them back.

    python record_snapshot.py --loop 300     run continuously instead of via cron
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import config
from orbfvg import screener


def once(quiet=False) -> bool:
    try:
        data = screener.fetch()
    except screener.ScreenerError as exc:
        print("%s  fetch failed: %s" % (datetime.now(screener.IST).strftime("%H:%M:%S"), exc))
        return False
    path = screener.save_snapshot(data, config.DATA_DIR)
    if not quiet:
        counts = {b: len(screener.symbols_in(data, b)) for b in screener.bucket_names(data)}
        total = len(screener.details(data))
        print("%s  %d symbols across %d lists -> %s"
              % (datetime.now(screener.IST).strftime("%H:%M:%S"),
                 total, sum(1 for v in counts.values() if v), path))
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", type=int, default=0,
                   help="keep running, capturing every N seconds")
    p.add_argument("--market-hours-only", action="store_true",
                   help="with --loop, skip captures outside 09:00-15:35 IST on weekdays")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    if not args.loop:
        return 0 if once(args.quiet) else 1

    print("Recording every %ds. Ctrl-C to stop." % args.loop)
    try:
        while True:
            now = datetime.now(screener.IST)
            trading = now.weekday() < 5 and "09:00" <= now.strftime("%H:%M") <= "15:35"
            if not args.market_hours_only or trading:
                once(args.quiet)
            elif not args.quiet:
                print("%s  outside market hours, skipping" % now.strftime("%H:%M:%S"))
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
