#!/usr/bin/env python3
"""
rippled memory sampler for long-running soak tests.

Samples process RSS/VSZ/thread count plus current ledger sequence from
rippled's admin RPC. Cross-platform (Linux + macOS) via psutil.

Designed to:
- survive rippled restarts (rediscovers PID each tick by process name)
- survive transient RPC failures (records sample with empty ledger_seq + note)
- append to its output file (safe across restarts of the sampler itself)
- exit cleanly on SIGINT/SIGTERM
- maintain a drift-free schedule across the full run

Usage:
    pip install psutil
    python3 rippled_memory_sampler.py                            # defaults
    python3 rippled_memory_sampler.py --interval 30 \
        --output soak_week1.csv --rpc-url http://127.0.0.1:5005/

Tail it while it runs:
    tail -f soak_week1.csv
"""

import argparse
import csv
import json
import os
import signal
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

try:
    import psutil
except ImportError:
    sys.exit("psutil is required. Install with: pip install psutil")


FIELDS = [
    "ts_iso",      # human-readable UTC timestamp
    "ts_epoch",    # unix epoch seconds, fractional
    "pid",         # rippled pid at sample time (blank if not found)
    "rss_kb",      # resident set size in KB
    "vsz_kb",      # virtual size in KB
    "threads",     # thread count
    "ledger_seq",  # current ledger from rippled admin RPC (blank on failure)
    "note",        # any anomaly: process_not_found, rpc_unavailable, etc.
]


def find_pid(process_name):
    """Return PID of first matching process by name, or None.

    Rediscovering each tick is intentional: if rippled is restarted mid-soak,
    we pick up the new PID automatically; the gap shows up as
    process_not_found rows, which is useful signal.
    """
    matches = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.info["name"] == process_name:
                matches.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not matches:
        return None
    if len(matches) > 1:
        print(
            f"[warn] {len(matches)} processes named {process_name!r}: "
            f"{matches}; using {matches[0]}",
            file=sys.stderr,
        )
    return matches[0]


def sample_memory(pid):
    """Return (rss_kb, vsz_kb, threads). Raises psutil exceptions on failure."""
    p = psutil.Process(pid)
    mi = p.memory_info()
    return mi.rss // 1024, mi.vms // 1024, p.num_threads()


def query_ledger_seq(rpc_url, timeout=2.0):
    """Return current ledger sequence as int, or None on any failure."""
    body = json.dumps({"method": "ledger_current", "params": [{}]}).encode("utf-8")
    req = urllib.request.Request(
        rpc_url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        result = data.get("result", {})
        seq = result.get("ledger_current_index")
        return int(seq) if seq is not None else None
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        KeyError,
    ):
        return None


def main():
    ap = argparse.ArgumentParser(description="rippled memory sampler (xplatform)")
    ap.add_argument(
        "--process-name",
        default="xrpld",
        help="Process name to sample (default: xrpld)",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Sample interval in seconds (default: 60)",
    )
    ap.add_argument(
        "--output",
        default=None,
        help="Output CSV path (default: xrpld_memory_<utc>.csv in cwd)",
    )
    ap.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:5005/",
        help="rippled admin RPC URL (default: http://127.0.0.1:5005/)",
    )
    ap.add_argument(
        "--no-rpc",
        action="store_true",
        help="Skip ledger_seq RPC queries entirely",
    )
    ap.add_argument(
        "--heartbeat-every",
        type=int,
        default=10,
        help="Print a status line to stderr every N ticks (default: 10; 0 to disable)",
    )
    args = ap.parse_args()

    if args.output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.output = f"xrpld_memory_{stamp}.csv"

    # Append mode: safe across sampler restarts. Write header only if new file.
    new_file = (not os.path.exists(args.output)) or os.path.getsize(args.output) == 0
    f = open(args.output, "a", newline="", buffering=1)  # line-buffered
    writer = csv.DictWriter(f, fieldnames=FIELDS)
    if new_file:
        writer.writeheader()

    # Graceful shutdown.
    stop = {"flag": False}

    def handle_signal(signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(
        f"[info] sampling every {args.interval}s, output={args.output}, "
        f"process={args.process_name!r}, rpc={'off' if args.no_rpc else args.rpc_url}",
        file=sys.stderr,
    )

    next_tick = time.monotonic()
    tick = 0
    while not stop["flag"]:
        now_mono = time.monotonic()
        if now_mono < next_tick:
            # Short sleep keeps SIGINT responsive without burning CPU.
            time.sleep(min(0.5, next_tick - now_mono))
            continue

        ts_epoch = time.time()
        ts_iso = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        row = {
            "ts_iso": ts_iso,
            "ts_epoch": f"{ts_epoch:.3f}",
            "pid": "",
            "rss_kb": "",
            "vsz_kb": "",
            "threads": "",
            "ledger_seq": "",
            "note": "",
        }

        pid = find_pid(args.process_name)
        if pid is None:
            row["note"] = "process_not_found"
        else:
            row["pid"] = pid
            try:
                rss_kb, vsz_kb, threads = sample_memory(pid)
                row["rss_kb"] = rss_kb
                row["vsz_kb"] = vsz_kb
                row["threads"] = threads
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                row["note"] = f"sample_error:{type(e).__name__}"

        if not args.no_rpc:
            seq = query_ledger_seq(args.rpc_url)
            if seq is not None:
                row["ledger_seq"] = seq
            elif not row["note"]:
                row["note"] = "rpc_unavailable"

        writer.writerow(row)

        tick += 1
        if args.heartbeat_every and (tick == 1 or tick % args.heartbeat_every == 0):
            print(
                f"[tick {tick}] pid={row['pid']} rss_kb={row['rss_kb']} "
                f"vsz_kb={row['vsz_kb']} threads={row['threads']} "
                f"ledger_seq={row['ledger_seq']} note={row['note']}",
                file=sys.stderr,
            )

        # Drift-free schedule. If we somehow fell behind by more than one
        # interval (e.g. sampler was paused), don't fire a burst — just resync.
        next_tick += args.interval
        if next_tick < time.monotonic():
            next_tick = time.monotonic() + args.interval

    f.close()
    print("[info] sampler stopped cleanly", file=sys.stderr)


if __name__ == "__main__":
    main()
