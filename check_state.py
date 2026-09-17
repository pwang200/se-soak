#!/usr/bin/env python3
"""Read-only ledger state check across the pool accounts.

Two flavors of pool-owned object coexist during a soak:
  - populated baseline: created by populate_ledger.py before the run. For
    escrows these use a CancelAfter ~1 year out so they never expire mid-soak;
    for the other six types they simply persist. Baseline counts are expected
    to stay CONSTANT across the soak.
  - in-flight soak escrows: created by run_soak.py, CancelAfter ~30 s out,
    removed within a worker cycle. After warm-up these are bounded by the
    active worker/in-flight count.

So this script buckets every account_objects entry by LedgerEntryType, and
splits Escrow objects into baseline vs soak by their CancelAfter. If a
populated/ directory is present, it prints the expected baseline per type and
flags drift.

Run:
    python3 check_state.py
    python3 check_state.py --accounts runs/test_accounts.json --top 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from escrow_lib import DEFAULT_ACCOUNTS_FILE, RUNS_DIR, now_ripple, rpc

# CancelAfter beyond this (ripple seconds from now) marks a populated baseline
# escrow rather than a short-lived soak escrow.
BASELINE_ESCROW_HORIZON_S = 30 * 24 * 3600  # 30 days

POPULATED_DIR = RUNS_DIR.parent / "populated"


def account_objects_all(rpc_url: str, address: str) -> list[dict]:
    """All ledger objects owned by address, following pagination markers."""
    objs: list[dict] = []
    marker = None
    while True:
        params = {"account": address, "limit": 400}
        if marker is not None:
            params["marker"] = marker
        res = rpc(rpc_url, "account_objects", params)
        objs.extend(res.get("account_objects", []))
        marker = res.get("marker")
        if marker is None:
            return objs


def classify(objs: list[dict], baseline_cutoff: int) -> list[tuple[str, str]]:
    """Bucket one account's objects as (bucket, index) pairs. Escrows split
    into Escrow/baseline and Escrow/soak by CancelAfter. Returns the object
    index so the caller can DEDUPE: shared objects (RippleState, an
    owner->destination Escrow, a Credential) appear in both parties'
    account_objects, and summing per-account would double-count them."""
    out: list[tuple[str, str]] = []
    for o in objs:
        t = o.get("LedgerEntryType", "?")
        idx = o.get("index", "")
        if t == "Escrow":
            ca = int(o.get("CancelAfter", 0))
            bucket = "Escrow/baseline" if ca >= baseline_cutoff else "Escrow/soak"
        else:
            bucket = t
        out.append((bucket, idx))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_FILE))
    ap.add_argument("--top", type=int, default=10,
                    help="Show top-N accounts by in-flight (soak) escrows.")
    ap.add_argument("--concurrency", type=int, default=32)
    args = ap.parse_args()

    with open(args.accounts) as f:
        entries = json.load(f)
    addresses = [e["address"] for e in entries]
    print(f"[info] checking {len(addresses)} accounts on {args.rpc_url}")

    baseline_cutoff = now_ripple() + BASELINE_ESCROW_HORIZON_S
    bucket_indices: dict[str, set] = {}          # bucket -> set of keylets (deduped)
    per_account_soak: dict[str, int] = {}
    errors: list[tuple[str, str]] = []

    def task(addr: str):
        try:
            objs = account_objects_all(args.rpc_url, addr)
        except RuntimeError as e:
            return addr, None, str(e)
        return addr, classify(objs, baseline_cutoff), None

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for addr, pairs, err in pool.map(task, addresses):
            if err is not None:
                errors.append((addr, err))
                continue
            soak_here = 0
            for bucket, idx in pairs:
                bucket_indices.setdefault(bucket, set()).add(idx)
                if bucket == "Escrow/soak":
                    soak_here += 1
            per_account_soak[addr] = soak_here

    totals = Counter({b: len(s) for b, s in bucket_indices.items()})

    if errors:
        print(f"[warn] {len(errors)} account lookups failed (first 5):",
              file=sys.stderr)
        for addr, err in errors[:5]:
            print(f"  {addr}  {err}", file=sys.stderr)
    if not per_account_soak:
        sys.exit("[FAIL] no successful account lookups")

    n = len(per_account_soak)
    soak_total = totals.get("Escrow/soak", 0)
    baseline_esc = totals.get("Escrow/baseline", 0)

    print(f"\n=== ledger state across {n} pool accounts ===")
    print(f"  in-flight soak escrows:   {soak_total}   "
          f"(expect <= active workers x in-flight/account after warm-up)")
    print(f"  baseline escrows:         {baseline_esc}   (populated; constant)")
    print(f"\n  all object types (baseline unless noted):")
    for t, v in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"    {t:20s} {v:>10,}")

    # Compare non-escrow baselines to populated/ expectations, if present.
    if os.path.isdir(POPULATED_DIR):
        type_to_entry = {
            "trustlines": "RippleState", "offers": "Offer",
            "nftokens": "NFTokenPage", "mpts": "MPTokenIssuance",
            "escrows": "Escrow/baseline", "oracles": "Oracle",
            "credentials": "Credential",
        }
        print(f"\n  populated baseline vs ledger:")
        for kind, entry in type_to_entry.items():
            p = os.path.join(POPULATED_DIR, f"{kind}.json")
            if not os.path.exists(p):
                continue
            expected = len(json.load(open(p)))
            got = totals.get(entry, 0)
            # NFTokenPage count differs from token count (a page holds many
            # tokens), so don't flag drift for NFTs on a page basis.
            note = ""
            if kind == "nftokens":
                note = f"  (pages hold {expected} tokens)"
            elif got < expected:
                note = f"  DRIFT: {expected - got} missing"
            print(f"    {kind:12s} expected {expected:>8,}  ledger {got:>8,}{note}")

    if args.top > 0 and soak_total:
        print(f"\n  top {args.top} accounts by in-flight soak escrows:")
        ranked = sorted(per_account_soak.items(), key=lambda kv: kv[1],
                        reverse=True)
        for addr, c in ranked[:args.top]:
            if c:
                print(f"    {c:>4}  {addr}")


if __name__ == "__main__":
    main()
