#!/usr/bin/env python3
"""
Probes 4 and 5: failure modes of smart escrow.

Probe 4 — return_0 wasm.
  The wasm's finish() returns 0. Expect tecWASM_REJECTED: the tx applies,
  fee is burned, no payout to destination.

Probe 5 — infinite-loop wasm with tight ComputationAllowance.
  The wasm runs (loop br) forever. Expect tecWASM_REJECTED (or another
  out-of-gas code) once the allowance is consumed.

Both cases reuse the EscrowCreate + EscrowFinish path from probe 3.
Each case takes its own creator/receiver pair from test_accounts.json so
the cases stay independent.

Run:
    python3 probe_escrow_wasm_failures.py
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from xrpl.utils import xrp_to_drops


RIPPLE_EPOCH_OFFSET = 946684800

def ripple_time(unix_seconds):
    return int(unix_seconds - RIPPLE_EPOCH_OFFSET)


# ---------------------------------------------------------------------------
# Wasm blobs. Both export `finish() -> i32`. No imports, no memory.
# ---------------------------------------------------------------------------

# Returns the constant 0. Same shape as the return_1 blob from probe 3 with
# i32.const value flipped. 39 bytes.
RETURN_0_WASM = bytes.fromhex(
    "0061736d01000000"
    "01" "05" "01" "60" "00" "01" "7f"
    "03" "02" "01" "00"
    "07" "0a" "01" "06" "66696e697368" "00" "00"
    "0a" "06" "01" "04" "00" "41" "00" "0b"
)

# (module (func (export "finish") (result i32)
#   (loop $L (br $L))
#   i32.const 1))
# Loop branches back to itself unconditionally — never exits. The
# i32.const 1 / end after it is dead code, but wasm's polymorphic stack
# rule for unreachable instructions makes it valid. 44 bytes.
INFINITE_LOOP_WASM = bytes.fromhex(
    "0061736d01000000"
    "01" "05" "01" "60" "00" "01" "7f"
    "03" "02" "01" "00"
    "07" "0a" "01" "06" "66696e697368" "00" "00"
    "0a" "0b" "01" "09" "00" "03" "40" "0c" "00" "0b" "41" "01" "0b"
)


def rpc(url, method, params=None, timeout=10):
    body = json.dumps({"method": method, "params": [params or {}]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp).get("result", {})
    except urllib.error.URLError as e:
        raise RuntimeError(f"RPC {method!r} transport error: {e}") from e


def run_case(name, wasm, gas_allowance, expected_result, creator, receiver,
             rpc_url, amount_xrp):
    """One EscrowCreate + EscrowFinish; verify engine_result and metadata."""
    print()
    print("=" * 64)
    print(f"  {name}")
    print("=" * 64)
    print(f"  creator:        {creator['address']}")
    print(f"  receiver:       {receiver['address']}")
    print(f"  wasm bytes:     {len(wasm)}")
    print(f"  gas allowance:  {gas_allowance}")
    print(f"  expected result for finish: {expected_result}")

    # Snapshots.
    cinfo = rpc(rpc_url, "account_info", {"account": creator["address"]})
    rinfo = rpc(rpc_url, "account_info", {"account": receiver["address"]})
    create_seq = cinfo["account_data"]["Sequence"]
    creator_bal0 = int(cinfo["account_data"]["Balance"])
    receiver_bal0 = int(rinfo["account_data"]["Balance"])

    fee_res = rpc(rpc_url, "fee")
    olf = int(fee_res["drops"]["open_ledger_fee"])
    create_fee = olf * 10 + 5 * len(wasm)
    finish_fee = olf * 100 + gas_allowance

    # ---------- EscrowCreate ----------
    cancel_after = ripple_time(time.time() + 365 * 86400)
    create_tx = {
        "TransactionType": "EscrowCreate",
        "Account": creator["address"],
        "Destination": receiver["address"],
        "Amount": xrp_to_drops(amount_xrp),
        "FinishFunction": wasm.hex().upper(),
        "CancelAfter": cancel_after,
        "Sequence": create_seq,
        "Fee": str(create_fee),
    }
    print(f"\n  [create] fee={create_fee}  seq={create_seq}")
    res = rpc(rpc_url, "submit",
              {"secret": creator["seed"], "tx_json": create_tx})
    print(f"           engine_result={res.get('engine_result')}  "
          f"hash={res.get('tx_json', {}).get('hash')}")
    if res.get("engine_result") != "tesSUCCESS":
        print(f"\n  [FAIL] EscrowCreate did not return tesSUCCESS for {name}")
        print(f"  full response: {json.dumps(res, indent=2)}")
        return False
    rpc(rpc_url, "ledger_accept")

    # ---------- EscrowFinish ----------
    finfo = rpc(rpc_url, "account_info", {"account": creator["address"]})
    finish_seq = finfo["account_data"]["Sequence"]
    finish_tx = {
        "TransactionType": "EscrowFinish",
        "Account": creator["address"],
        "Owner": creator["address"],
        "OfferSequence": create_seq,
        "Sequence": finish_seq,
        "Fee": str(finish_fee),
        "ComputationAllowance": str(gas_allowance),
    }
    print(f"\n  [finish] fee={finish_fee}  allowance={gas_allowance}  "
          f"seq={finish_seq}")
    t0 = time.monotonic()
    res = rpc(rpc_url, "submit",
              {"secret": creator["seed"], "tx_json": finish_tx})
    submit_ms = (time.monotonic() - t0) * 1000
    finish_engine = res.get("engine_result")
    finish_hash = res.get("tx_json", {}).get("hash")
    print(f"           engine_result={finish_engine}  hash={finish_hash}")
    print(f"           submit_ms={submit_ms:.1f}  (driver-side wall clock)")
    rpc(rpc_url, "ledger_accept")

    # ---------- Read back validated metadata ----------
    tx_info = rpc(rpc_url, "tx", {"transaction": finish_hash})
    meta = tx_info.get("meta") or tx_info.get("metaData") or {}
    final_result = meta.get("TransactionResult", "<missing>")
    print(f"\n  [validated] meta.TransactionResult = {final_result}")

    # ---------- Verify balances ----------
    cinfo2 = rpc(rpc_url, "account_info", {"account": creator["address"]})
    rinfo2 = rpc(rpc_url, "account_info", {"account": receiver["address"]})
    creator_bal1 = int(cinfo2["account_data"]["Balance"])
    receiver_bal1 = int(rinfo2["account_data"]["Balance"])
    recv_delta = receiver_bal1 - receiver_bal0
    create_delta = creator_bal1 - creator_bal0

    # Receiver should get amount on tesSUCCESS, 0 on tecWASM_REJECTED.
    expected_recv = (
        int(xrp_to_drops(amount_xrp)) if expected_result == "tesSUCCESS" else 0
    )

    print(f"\n  creator delta:  {create_delta}")
    print(f"  receiver delta: {recv_delta}  (expected {expected_recv} for "
          f"{expected_result})")

    ok_result = (final_result == expected_result)
    ok_payout = (recv_delta == expected_recv)
    print(f"\n  result match:   {'OK ' if ok_result else 'BAD'}  "
          f"(got {final_result}, want {expected_result})")
    print(f"  payout match:   {'OK ' if ok_payout else 'BAD'}")
    return ok_result and ok_payout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--accounts", default="test_accounts.json")
    ap.add_argument("--amount-xrp", type=int, default=100)
    args = ap.parse_args()

    with open(args.accounts) as f:
        entries = json.load(f)
    if len(entries) < 4:
        sys.exit("need at least 4 funded accounts in test_accounts.json")

    cases = [
        # (name, wasm, gas_allowance, expected_result for the finish)
        ("PROBE 4: return_0 wasm",
         RETURN_0_WASM, 10000, "tecWASM_REJECTED"),
        ("PROBE 5: infinite-loop wasm (small allowance)",
         INFINITE_LOOP_WASM, 1000, "tecWASM_REJECTED"),
    ]

    results = []
    for i, (name, wasm, gas, expected) in enumerate(cases):
        creator = entries[2 * i]
        receiver = entries[2 * i + 1]
        ok = run_case(name, wasm, gas, expected, creator, receiver,
                      args.rpc_url, args.amount_xrp)
        results.append((name, ok))

    print("\n" + "=" * 64)
    print("  SUMMARY")
    print("=" * 64)
    for name, ok in results:
        print(f"  [{'OK ' if ok else 'BAD'}]  {name}")

    if not all(ok for _, ok in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
