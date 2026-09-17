#!/usr/bin/env python3
"""Tick `ledger_accept` against a standalone xrpld on a fixed cadence.

Standalone xrpld does not close ledgers on its own; close_time only
advances when something calls `ledger_accept`. Run this in its own
terminal alongside xrpld so:
  - close_time stays glued to wall_clock (no drift between runs).
  - run_soak.py / setup_accounts.py / check_state.py don't have to
    manage ledger advancement themselves and can target real networks
    (testnet/mainnet) without modification — just don't start the
    ticker.

Heartbeat: prints a one-line summary every --heartbeat-every closes,
including the gap between close_time and wall_clock so you can spot
catch-up bursts or stalls.

Run:
    python3 ledger_ticker.py
    python3 ledger_ticker.py --interval 4 --rpc-url http://127.0.0.1:5005/
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from escrow_lib import get_close_time, now_ripple, rpc


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--interval", type=float, default=4.0,
                    help="Seconds between ledger_accept calls.")
    ap.add_argument("--heartbeat-every", type=int, default=15,
                    help="Print a status line every N closes.")
    args = ap.parse_args()

    stop = False
    def handle_sigint(signum, frame):
        nonlocal stop
        print("\n[info] SIGINT received; stopping ticker.", file=sys.stderr)
        stop = True
    signal.signal(signal.SIGINT, handle_sigint)

    print(f"[info] ticker rpc={args.rpc_url}  interval={args.interval}s  "
          f"heartbeat every {args.heartbeat_every} closes")

    closes = 0
    t_start = time.monotonic()
    while not stop:
        try:
            rpc(args.rpc_url, "ledger_accept")
        except RuntimeError as e:
            print(f"[FATAL] ledger_accept failed: {e}", file=sys.stderr)
            sys.exit(1)
        closes += 1
        if closes % args.heartbeat_every == 0:
            ct = get_close_time(args.rpc_url)
            drift = now_ripple() - ct
            elapsed = time.monotonic() - t_start
            print(f"[tick {closes:>5}] elapsed={elapsed:>7.1f}s  "
                  f"close_time={ct}  drift_behind_wall={drift}s")
        time.sleep(args.interval)

    print(f"[info] ticker exiting; total closes = {closes}")


if __name__ == "__main__":
    main()
