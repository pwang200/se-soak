#!/usr/bin/env python3
"""
Probe: fund N accounts from genesis on a standalone xrpld node, advancing
the ledger manually via the `ledger_accept` admin command.

What this validates:
- xrpld admin RPC is reachable at the given URL
- genesis seed unlocks the master account
- N pending txns from a single account can be queued before a ledger close
  and all apply on one `ledger_accept` (this is the assumption parallel
  submission will rely on)
- `account_info` returns each funded account after the ledger closes

Output: JSON file with funded (address, seed, balance_drops) for reuse by
later probes.

Run:
    pip install xrpl-py
    python3 probe_fund_accounts.py
    # or with overrides:
    python3 probe_fund_accounts.py --rpc-url http://127.0.0.1:5005/ --count 10
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

from xrpl.constants import CryptoAlgorithm
from xrpl.core.binarycodec import encode
from xrpl.models.transactions import Payment
from xrpl.transaction import sign
from xrpl.utils import xrp_to_drops
from xrpl.wallet import Wallet


# Well-known standalone master account. Funded with ~100B XRP at genesis.
GENESIS_SEED = "snoPBrXtMeMyMHUVTgbuqAfg1SUTb"


def rpc(url, method, params=None, timeout=10):
    """Single point of contact with xrpld. Raises on transport or RPC error."""
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


def sign_and_submit(url, tx, wallet):
    """Sign locally, submit as tx_blob. Returns (engine_result, tx_hash, raw)."""
    signed = sign(tx, wallet)
    blob = encode(signed.to_xrpl())
    res = rpc(url, "submit", {"tx_blob": blob})
    return (
        res.get("engine_result", "<missing>"),
        res.get("tx_json", {}).get("hash"),
        res,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--fund-xrp", type=int, default=1000)
    ap.add_argument("--output", default="test_accounts.json")
    args = ap.parse_args()

    print(f"[info] rpc={args.rpc_url}  count={args.count}  fund={args.fund_xrp} XRP")

    genesis = Wallet.from_seed(GENESIS_SEED, algorithm=CryptoAlgorithm.SECP256K1)
    print(f"[info] genesis address: {genesis.address}")

    # 1. Probe the node and pin down starting state.
    try:
        info = rpc(args.rpc_url, "account_info", {"account": genesis.address})
    except RuntimeError as e:
        sys.exit(f"[fail] cannot reach xrpld or genesis missing: {e}")
    start_seq = info["account_data"]["Sequence"]
    print(f"[info] genesis starting sequence: {start_seq}")

    cur = rpc(args.rpc_url, "ledger_current")
    current_ledger = cur["ledger_current_index"]
    print(f"[info] current ledger index: {current_ledger}")

    # 2. Generate N wallets locally (random keys).
    wallets = [Wallet.create() for _ in range(args.count)]

    # 3. Submit N payments from genesis, with manually-incremented sequences,
    #    BEFORE any ledger close. This is what the real driver will do.
    submitted = []
    for i, w in enumerate(wallets):
        tx = Payment(
            account=genesis.address,
            destination=w.address,
            amount=xrp_to_drops(args.fund_xrp),
            sequence=start_seq + i,
            fee="10",
            last_ledger_sequence=current_ledger + 100,
        )
        engine_result, tx_hash, _ = sign_and_submit(args.rpc_url, tx, genesis)
        submitted.append((w, engine_result, tx_hash))
        print(
            f"[submit {i + 1:>2}/{args.count}] "
            f"dest={w.address}  seq={start_seq + i}  "
            f"result={engine_result}  hash={tx_hash}"
        )
        # Anything other than tesSUCCESS / terQUEUED at submit time is worth
        # noting, but we keep going to see the full picture.

    # 4. Manually close the ledger.
    print("[info] calling ledger_accept...")
    accept = rpc(args.rpc_url, "ledger_accept")
    print(f"[ledger_accept] {json.dumps(accept, indent=2)}")

    # 5. Verify each account exists and is funded.
    funded = []
    not_found = []
    for i, (w, _engine, _hash) in enumerate(submitted):
        try:
            info = rpc(args.rpc_url, "account_info", {"account": w.address})
            bal = int(info["account_data"]["Balance"])
            print(f"  [{i + 1:>2}] {w.address}  balance={bal} drops")
            funded.append(
                {"address": w.address, "seed": w.seed, "balance_drops": bal}
            )
        except RuntimeError as e:
            print(f"  [{i + 1:>2}] {w.address}  NOT FOUND ({e})", file=sys.stderr)
            not_found.append(w.address)

    # 6. Summary + persistence.
    if not_found:
        print(
            f"[FAIL] {len(not_found)} of {args.count} accounts not funded "
            f"after one ledger_accept",
            file=sys.stderr,
        )
        # Still save what did make it through — useful for inspection.
        if funded:
            with open(args.output, "w") as f:
                json.dump(funded, f, indent=2)
            print(f"[partial] saved {len(funded)} funded accounts to {args.output}")
        sys.exit(1)

    with open(args.output, "w") as f:
        json.dump(funded, f, indent=2)
    print(f"[OK] funded {len(funded)} accounts; saved to {args.output}")


if __name__ == "__main__":
    main()
