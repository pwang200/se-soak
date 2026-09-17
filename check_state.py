#!/usr/bin/env python3
"""Read-only state check across worker accounts.

Queries account_objects (filtered to escrow) for every account in
runs/test_accounts.json (or --accounts) and prints:
  - total in-flight escrow count across workers
  - distribution: histogram of "escrows-per-worker"
  - top-N accounts by in-flight count

Used as a warm-up readiness check: after warm-up, in-flight escrow count
should be bounded by the worker count (every Create paired with a
Finish/Cancel in the same cycle).

Run:
    python3 check_state.py
    python3 check_state.py --top 5 > runs/<run>/check_state.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from escrow_lib import DEFAULT_ACCOUNTS_FILE, rpc


def count_escrows(rpc_url: str, address: str) -> int:
    """Return the number of Escrow ledger objects owned by `address`.
    The `type=escrow` filter on account_objects is supported on every
    xrpld version we care about; if it isn't, we'd see them all and
    overcount, which is preferable to silently undercounting."""
    res = rpc(rpc_url, "account_objects",
              {"account": address, "type": "escrow"})
    objs = res.get("account_objects", [])
    # Defensive: in case the type filter is ignored, drop non-Escrow rows.
    return sum(1 for o in objs if o.get("LedgerEntryType") == "Escrow")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_FILE),
                    help="Accounts JSON from setup_accounts.py. "
                         "Default: runs/test_accounts.json")
    ap.add_argument("--top", type=int, default=10,
                    help="Show top-N accounts by in-flight escrow count.")
    ap.add_argument("--concurrency", type=int, default=32)
    args = ap.parse_args()

    with open(args.accounts) as f:
        entries = json.load(f)
    addresses = [e["address"] for e in entries]
    print(f"[info] checking {len(addresses)} accounts on {args.rpc_url}")

    counts: dict[str, int] = {}
    errors: list[tuple[str, str]] = []

    def task(addr: str):
        try:
            return addr, count_escrows(args.rpc_url, addr), None
        except RuntimeError as e:
            return addr, None, str(e)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for addr, c, err in pool.map(task, addresses):
            if err is not None:
                errors.append((addr, err))
            else:
                counts[addr] = c

    if errors:
        print(f"[warn] {len(errors)} account lookups failed (first 5):",
              file=sys.stderr)
        for addr, err in errors[:5]:
            print(f"  {addr}  {err}", file=sys.stderr)

    if not counts:
        sys.exit("[FAIL] no successful account lookups")

    total = sum(counts.values())
    hist = Counter(counts.values())
    n_workers = len(counts)
    busy = sum(1 for v in counts.values() if v > 0)

    print(f"\n=== escrow state ===")
    print(f"  accounts queried:     {n_workers}")
    print(f"  accounts with escrow: {busy}")
    print(f"  total in-flight:      {total}")
    print(f"  per-worker mean:      {total / n_workers:.3f}")
    print(f"  per-worker max:       {max(counts.values())}")

    print(f"\n  distribution (escrows_per_worker  →  worker_count):")
    for k in sorted(hist):
        print(f"    {k:>3}  →  {hist[k]}")

    if args.top > 0:
        print(f"\n  top {args.top} accounts by in-flight escrows:")
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        for addr, c in ranked[:args.top]:
            print(f"    {c:>4}  {addr}")

    # Health hint for warm-up readiness: in-flight should be bounded by
    # worker count. We don't fail the script — it's diagnostic — but flag.
    if total > n_workers:
        print(f"\n[note] total in-flight ({total}) exceeds worker count "
              f"({n_workers}). For steady state, expect total <= workers.")


if __name__ == "__main__":
    main()
