#!/usr/bin/env python3
"""Smart-escrow soak driver.

Threading model:
  --threads T  spawns T worker threads.
  --accounts-per-thread M  gives each thread M accounts to drive.
  Total active accounts = T * M (capped by what's in test_accounts.json).

A scheduling Pattern decides what each thread does with its slice of accounts:
  --pattern serial    one cycle at a time per account (today's behavior).
                      Best with --accounts-per-thread 1.
  --pattern pipeline  round-driven, K in-flight per account.
                      --in-flight-per-account K.
                      Best with --accounts-per-thread >> 1.

A background thread calls `ledger_accept` every --ledger-interval-s so
submitted txns get validated. Standalone xrpld does not auto-close.

Fail-loud policy:
- Any RPC transport error from any thread → stderr log, exit non-zero.
- Two consecutive sequence errors on one account → exit non-zero.
- Any outcome that differs from what the category declares → CSV row plus
  a stderr "[unexpected]" line; whatever escrow it left behind is
  backstop-Cancelled after CancelAfter. Not fatal.

Categories carry a lifecycle (see escrow_lib.CATEGORIES): finish_removes,
cancel_removes (Create → Finish expecting tecBYTECODE_REJECTED → Cancel), or
preflight_reject (Create rejected at preflight; nothing to clean up).

CSV columns:
    ts, thread_id, account, category, action,
    tx_hash, engine_result, final_result

Artifacts: everything a run produces goes to one folder, --run-dir
(default runs/<UTC stamp>/): soak.csv plus run_soak.log, a copy of this
process's stdout+stderr. Accounts are read from runs/test_accounts.json
unless --accounts says otherwise. See runs/README.md.

Run (warm-up, serial):
    python3 run_soak.py --duration 900 --threads 10

Run (long soak, pipeline):
    python3 run_soak.py --duration 604800 --pattern pipeline \\
                        --threads 100 --accounts-per-thread 100 \\
                        --in-flight-per-account 2 \\
                        --run-dir runs/week1
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from xrpl.utils import xrp_to_drops

from escrow_lib import (
    CATEGORIES,
    DEFAULT_ACCOUNTS_FILE,
    Category,
    CsvLog,
    Pattern,
    PatternContext,
    Worker,
    check_categories_against_fees,
    default_run_dir,
    get_ledger_fees,
    rpc,
)
from pattern_pipeline import PipelinePattern
from pattern_serial import SerialPattern


PATTERNS: dict[str, type[Pattern]] = {
    SerialPattern.name: SerialPattern,
    PipelinePattern.name: PipelinePattern,
}


# ---------------------------------------------------------------------------
# stdout/stderr tee → <run-dir>/run_soak.log
# ---------------------------------------------------------------------------

class _Tee:
    """Duplicate writes to the original stream and a line-buffered file.
    Everything else (isatty, encoding, fileno...) is delegated."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        self._stream.write(data)
        self._fh.write(data)

    def flush(self):
        self._stream.flush()
        self._fh.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def tee_std_streams(log_path: Path):
    """Send stdout and stderr to both the terminal and log_path. Worker
    threads print via sys.stderr, which is looked up per call, so their
    [unexpected] / [FATAL] lines land in the log too."""
    fh = open(log_path, "a", buffering=1)
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    return fh


# ---------------------------------------------------------------------------
# Background ledger closer
# ---------------------------------------------------------------------------

def ledger_closer(rpc_url: str, interval_s: float,
                  closer_stop: threading.Event,
                  failure_event: threading.Event,
                  workers_stop: threading.Event) -> None:
    """Call ledger_accept every interval_s until closer_stop is set.

    closer_stop is distinct from the workers' stop_event on purpose: the
    closer must keep ticking until every worker thread has exited,
    otherwise workers mid-wait_for_validated at shutdown hang for the
    full validation timeout and log bogus <TIMEOUT> rows.
    """
    closes = 0
    while not closer_stop.is_set():
        try:
            rpc(rpc_url, "ledger_accept")
            closes += 1
        except RuntimeError as e:
            print(f"[FATAL] ledger_accept failed: {e}", file=sys.stderr)
            failure_event.set()
            workers_stop.set()
            closer_stop.set()
            return
        closer_stop.wait(interval_s)
    print(f"[info] ledger-closer exiting; total closes = {closes}")


# ---------------------------------------------------------------------------
# Account partitioning
# ---------------------------------------------------------------------------

def partition_accounts(accounts: list[Worker],
                       accounts_per_thread: int) -> list[list[Worker]]:
    """Slice into thread-sized chunks. Last chunk may be smaller."""
    return [accounts[i:i + accounts_per_thread]
            for i in range(0, len(accounts), accounts_per_thread)]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_FILE),
                    help="Accounts JSON from setup_accounts.py. "
                         "Default: runs/test_accounts.json")
    ap.add_argument("--duration", type=int, default=60,
                    help="Seconds to run before stopping.")

    # Threading shape.
    ap.add_argument("--threads", "--workers", type=int, default=10,
                    dest="threads",
                    help="Number of worker threads. (Alias: --workers.)")
    ap.add_argument("--accounts-per-thread", type=int, default=1,
                    help="Accounts each thread drives. For pipeline pattern, "
                         "set this >> 1 (e.g. 100).")

    # Pattern selection.
    ap.add_argument("--pattern", choices=list(PATTERNS), default="serial")

    # Pattern-specific.
    ap.add_argument("--tps", type=float, default=None,
                    help="serial: target cycles/sec across all threads. "
                         "Ignored by pipeline pattern.")
    ap.add_argument("--in-flight-per-account", type=int, default=1,
                    help="pipeline: K in-flight escrows per account.")

    # Escrow shape.
    ap.add_argument("--amount-xrp", type=int, default=1,
                    help="XRP per escrow.")
    ap.add_argument("--cancel-after-s", type=int, default=30,
                    help="Per-escrow CancelAfter in seconds.")
    ap.add_argument("--categories", default="return_1,return_0",
                    help="Comma-separated category names to round-robin.")

    # Ledger cadence + output.
    ap.add_argument("--ledger-interval-s", type=float, default=4.0,
                    help="ledger_accept cadence (used by --manage-ledgers "
                         "thread; also the pipeline pattern's fallback "
                         "sleep when no submits this round).")
    ap.add_argument("--manage-ledgers", action="store_true",
                    help="Run a background ledger_accept thread inside "
                         "this process. Default off — start a separate "
                         "ledger_ticker.py terminal, or point at a real "
                         "network (testnet/mainnet) that closes ledgers "
                         "on its own.")
    ap.add_argument("--run-dir", default=None,
                    help="Folder for this run's artifacts (soak.csv, "
                         "run_soak.log). Default: runs/<UTC stamp>. Pre-create "
                         "it and hand the same path to the memory sampler to "
                         "keep one run's files together.")
    ap.add_argument("--output", default=None,
                    help="CSV log path. Default: <run-dir>/soak.csv")
    args = ap.parse_args()

    # ---- Run directory + log tee (before any other output) ----
    run_dir = Path(args.run_dir) if args.run_dir else default_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output or str(run_dir / "soak.csv")
    log_path = run_dir / "run_soak.log"
    tee_std_streams(log_path)
    print(f"[info] run dir              = {run_dir}")
    print(f"[info] log                  = {log_path}")
    print(f"[info] cmdline              = {' '.join(sys.argv)}")

    # ---- Resolve categories ----
    cat_names = [c.strip() for c in args.categories.split(",") if c.strip()]
    unknown = [n for n in cat_names if n not in CATEGORIES]
    if unknown:
        sys.exit(f"unknown categories: {unknown}; known: {list(CATEGORIES)}")
    categories: list[Category] = [CATEGORIES[n] for n in cat_names]

    # ---- Load accounts ----
    with open(args.accounts) as f:
        entries = json.load(f)
    total_active = args.threads * args.accounts_per_thread
    if len(entries) < total_active:
        sys.exit(
            f"--threads={args.threads} --accounts-per-thread="
            f"{args.accounts_per_thread} needs {total_active} accounts but "
            f"only {len(entries)} in {args.accounts}. Run setup_accounts.py "
            f"with --count {total_active} or larger."
        )
    # ---- Smart-escrow fee parameters from the ledger ----
    # Also the first RPC we make: unreachable xrpld or a node without
    # SmartEscrow enabled/seeded fails loud here, before anything runs.
    try:
        fees = get_ledger_fees(args.rpc_url)
    except RuntimeError as e:
        sys.exit(f"[fail] {e}")
    try:
        check_categories_against_fees(categories, fees)
    except ValueError as e:
        sys.exit(f"[fail] category vs FeeSettings: {e}")

    workers = [
        Worker(args.rpc_url, e["address"], e["seed"],
               cancel_after_seconds=args.cancel_after_s, fees=fees)
        for e in entries[:total_active]
    ]
    chunks = partition_accounts(workers, args.accounts_per_thread)
    assert len(chunks) == args.threads, (
        f"expected {args.threads} chunks, got {len(chunks)}"
    )

    # ---- Construct pattern ----
    pattern: Pattern
    if args.pattern == "serial":
        cycle_period_s = None
        if args.tps is not None and args.tps > 0:
            cycle_period_s = args.threads / args.tps
        pattern = SerialPattern(cycle_period_s=cycle_period_s)
        if args.accounts_per_thread != 1:
            print(f"[warn] serial pattern with --accounts-per-thread="
                  f"{args.accounts_per_thread} > 1: throughput won't scale; "
                  f"consider --pattern pipeline.", file=sys.stderr)
    elif args.pattern == "pipeline":
        pattern = PipelinePattern(
            in_flight_per_account=args.in_flight_per_account,
            ledger_interval_s=args.ledger_interval_s,
        )
        if args.tps is not None:
            print(f"[warn] --tps is ignored by pipeline pattern; cadence is "
                  f"governed by --ledger-interval-s.", file=sys.stderr)
    else:
        sys.exit(f"unknown pattern {args.pattern!r}")

    # ---- CSV + plumbing ----
    csv_log = CsvLog(out_path)

    stop_event = threading.Event()
    failure_event = threading.Event()

    def handle_sigint(signum, frame):
        print("\n[info] SIGINT received; stopping workers...", file=sys.stderr)
        stop_event.set()
    signal.signal(signal.SIGINT, handle_sigint)

    closer_stop = threading.Event()
    closer: threading.Thread | None = None
    if args.manage_ledgers:
        closer = threading.Thread(
            target=ledger_closer,
            args=(args.rpc_url, args.ledger_interval_s,
                  closer_stop, failure_event, stop_event),
            name="ledger-closer",
            daemon=True,
        )
        closer.start()
    else:
        # Sanity check: assert *something* is closing ledgers, otherwise
        # the soak will hang on wait_for_validated. We sample
        # ledger_current twice and require it to advance.
        try:
            i0 = int(rpc(args.rpc_url, "ledger_current")["ledger_current_index"])
            time.sleep(max(args.ledger_interval_s * 1.5, 3.0))
            i1 = int(rpc(args.rpc_url, "ledger_current")["ledger_current_index"])
        except RuntimeError as e:
            sys.exit(f"[fail] xrpld not reachable for ledger probe: {e}")
        if i1 <= i0:
            sys.exit(
                f"[fail] ledger isn't advancing (index stayed at {i0} for "
                f"{args.ledger_interval_s * 1.5:.1f}s). Either start "
                f"ledger_ticker.py in another terminal, or rerun with "
                f"--manage-ledgers."
            )

    amount_drops = int(xrp_to_drops(args.amount_xrp))
    ctx = PatternContext(
        rpc_url=args.rpc_url,
        categories=categories,
        amount_drops=amount_drops,
        csv_log=csv_log,
        stop_event=stop_event,
        failure_event=failure_event,
    )

    # ---- Banner ----
    print(f"[info] rpc                  = {args.rpc_url}")
    print(f"[info] accounts file        = {args.accounts}")
    print(f"[info] pattern              = {pattern.name}")
    print(f"[info] threads              = {args.threads}")
    print(f"[info] accounts/thread      = {args.accounts_per_thread}")
    print(f"[info] active accounts      = {total_active}")
    if args.pattern == "pipeline":
        print(f"[info] in-flight/account    = {args.in_flight_per_account}")
        print(f"[info] steady-state ≤        {total_active * args.in_flight_per_account} "
              f"in-flight escrows")
    print(f"[info] duration             = {args.duration}s")
    print(f"[info] categories           = "
          f"{[f'{c.name} ({c.lifecycle})' for c in categories]}")
    print(f"[info] amount               = {args.amount_xrp} XRP "
          f"({amount_drops} drops)")
    print(f"[info] cancel_after         = {args.cancel_after_s}s")
    print(f"[info] ledger fees          = base={fees.base_fee} drops  "
          f"gas_limit={fees.gas_limit}  gas_price={fees.gas_price} udrops/gas  "
          f"bytecode_size_limit={fees.bytecode_size_limit}")
    if args.manage_ledgers:
        print(f"[info] ledger advance       = internal thread, every "
              f"{args.ledger_interval_s}s")
    else:
        print(f"[info] ledger advance       = external (ticker / network); "
              f"local ledger_interval_s={args.ledger_interval_s} used only "
              f"as pipeline fallback")
    print(f"[info] csv output           = {out_path}")

    # ---- Run thread pool ----
    t_start = time.monotonic()
    deadline = t_start + args.duration

    with ThreadPoolExecutor(max_workers=args.threads,
                            thread_name_prefix="soak") as pool:
        futures = [
            pool.submit(pattern.run_thread, i, chunk, ctx)
            for i, chunk in enumerate(chunks)
        ]

        try:
            while time.monotonic() < deadline:
                if failure_event.is_set() or stop_event.is_set():
                    break
                time.sleep(1.0)
        finally:
            stop_event.set()

        for fut in as_completed(futures):
            exc = fut.exception()
            if exc is not None:
                print(f"[FATAL] thread raised: {exc!r}", file=sys.stderr)
                failure_event.set()

    # All workers have exited; now it's safe to stop closing ledgers.
    closer_stop.set()
    if closer is not None:
        closer.join(timeout=30)

    csv_log.close()
    elapsed = time.monotonic() - t_start

    if failure_event.is_set():
        print(f"[FAIL] soak aborted after {elapsed:.1f}s; "
              f"see {out_path} and stderr above", file=sys.stderr)
        sys.exit(1)
    print(f"[OK] soak complete: {elapsed:.1f}s; rows in {out_path}")


if __name__ == "__main__":
    main()
