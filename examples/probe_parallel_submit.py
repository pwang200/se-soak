#!/usr/bin/env python3
"""
Probe: concurrent submission from N funded accounts on standalone xrpld.

What this validates:
- N concurrent submit RPCs from distinct accounts succeed simultaneously
- All apply on a single ledger_accept (no drops, no out-of-order races
  that would prevent application)
- The RPC layer is concurrency-safe under modest fan-out (10 threads)
- Rough per-submit latency, so we know how to size the driver's account pool
  later: pool_size ≈ target_TPS × submit_latency × safety_factor

Loads accounts from test_accounts.json (produced by probe_fund_accounts.py).
Each account submits one Payment back to genesis, concurrently via a
ThreadPoolExecutor. Then ledger_accept, then verify balances dropped by
exactly (amount + fee).

Run:
    python3 probe_parallel_submit.py
    # or override:
    python3 probe_parallel_submit.py --workers 10 --rpc-url http://127.0.0.1:5005/
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from xrpl.constants import CryptoAlgorithm
from xrpl.core.binarycodec import encode
from xrpl.models.transactions import Payment
from xrpl.transaction import sign
from xrpl.utils import xrp_to_drops
from xrpl.wallet import Wallet


GENESIS_SEED = "snoPBrXtMeMyMHUVTgbuqAfg1SUTb"
PAYMENT_AMOUNT_XRP = 10
FEE_DROPS = 10


def rpc(url, method, params=None, timeout=10):
    """Single point of contact with xrpld."""
    body = json.dumps({"method": method, "params": [params or {}]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.URLError as e:
        raise RuntimeError(f"RPC {method!r} transport error: {e}") from e
    result = data.get("result", {})
    if result.get("status") == "error":
        raise RuntimeError(f"RPC {method!r} error: {result}")
    return result


def build_sign_submit(rpc_url, wallet, dest_address, sequence, current_ledger):
    """One unit of work for a worker thread. Times each stage."""
    t0 = time.monotonic()
    tx = Payment(
        account=wallet.address,
        destination=dest_address,
        amount=xrp_to_drops(PAYMENT_AMOUNT_XRP),
        sequence=sequence,
        fee=str(FEE_DROPS),
        last_ledger_sequence=current_ledger + 100,
    )
    signed = sign(tx, wallet)
    blob = encode(signed.to_xrpl())
    t_signed = time.monotonic()
    res = rpc(rpc_url, "submit", {"tx_blob": blob})
    t_done = time.monotonic()
    return {
        "address": wallet.address,
        "sequence": sequence,
        "engine_result": res.get("engine_result", "<missing>"),
        "tx_hash": res.get("tx_json", {}).get("hash"),
        "sign_ms": (t_signed - t0) * 1000,
        "submit_ms": (t_done - t_signed) * 1000,
        "total_ms": (t_done - t0) * 1000,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--accounts", default="test_accounts.json")
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()

    # 1. Load funded accounts and reconstruct wallets.
    with open(args.accounts) as f:
        entries = json.load(f)
    print(f"[info] loaded {len(entries)} accounts from {args.accounts}")

    wallets = []
    for e in entries:
        w = Wallet.from_seed(e["seed"])
        if w.address != e["address"]:
            sys.exit(
                f"[fail] seed/address mismatch for {e['address']} "
                f"(got {w.address}). Algorithm drift?"
            )
        wallets.append(w)

    genesis = Wallet.from_seed(GENESIS_SEED, algorithm=CryptoAlgorithm.SECP256K1)
    print(f"[info] each account → {PAYMENT_AMOUNT_XRP} XRP → {genesis.address}")

    # 2. Snapshot per-account sequence + balance, and current ledger index.
    seqs, balances_before = {}, {}
    for w in wallets:
        info = rpc(args.rpc_url, "account_info", {"account": w.address})
        seqs[w.address] = info["account_data"]["Sequence"]
        balances_before[w.address] = int(info["account_data"]["Balance"])
    cur = rpc(args.rpc_url, "ledger_current")
    current_ledger = cur["ledger_current_index"]
    print(f"[info] current ledger: {current_ledger}")

    # 3. Concurrent submit.
    t_batch = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                build_sign_submit,
                args.rpc_url,
                w,
                genesis.address,
                seqs[w.address],
                current_ledger,
            )
            for w in wallets
        ]
        for fut in as_completed(futures):
            results.append(fut.result())
    batch_ms = (time.monotonic() - t_batch) * 1000

    results.sort(key=lambda r: r["address"])
    print(f"[info] concurrent batch wall time: {batch_ms:.1f} ms "
          f"(workers={args.workers})")
    for r in results:
        print(
            f"  {r['address']}  seq={r['sequence']}  "
            f"result={r['engine_result']}  "
            f"submit_ms={r['submit_ms']:>6.1f}  total_ms={r['total_ms']:>6.1f}"
        )

    submit_ok = sum(1 for r in results if r["engine_result"] == "tesSUCCESS")
    print(f"[info] {submit_ok}/{len(results)} returned tesSUCCESS at submit time")

    # 4. Manual ledger close.
    print("[info] calling ledger_accept...")
    accept = rpc(args.rpc_url, "ledger_accept")
    print(f"[ledger_accept] ledger_current_index={accept['ledger_current_index']}")

    # 5. Verify each balance dropped by exactly (amount + fee).
    expected_delta = int(xrp_to_drops(PAYMENT_AMOUNT_XRP)) + FEE_DROPS
    bad = []
    print(f"[info] verifying balance drop of {expected_delta} drops per account...")
    for w in wallets:
        info = rpc(args.rpc_url, "account_info", {"account": w.address})
        after = int(info["account_data"]["Balance"])
        before = balances_before[w.address]
        delta = before - after
        ok = delta == expected_delta
        print(f"  [{'OK' if ok else 'BAD'}] {w.address}  "
              f"before={before}  after={after}  delta={delta}")
        if not ok:
            bad.append((w.address, delta))

    if bad:
        for addr, delta in bad:
            print(f"  unexpected delta {delta} on {addr}", file=sys.stderr)
        sys.exit(f"[FAIL] {len(bad)} of {len(wallets)} did not apply cleanly")
    print(f"[OK] all {len(wallets)} concurrent submits applied on one ledger close")


if __name__ == "__main__":
    main()
