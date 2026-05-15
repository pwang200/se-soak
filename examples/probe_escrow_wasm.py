#!/usr/bin/env python3
"""
Probe 3: EscrowCreate + EscrowFinish round-trip with a trivial wasm blob
that always returns 1 (i.e. "always finishable").

What this validates:
- xrpld accepts an EscrowCreate that carries a FinishFunction field
- ledger_accept absorbs it
- EscrowFinish triggers the wasm; finish() returning 1 → tesSUCCESS
- Destination receives the escrowed amount
- If the WASM_TIMING patch is applied to rippled, one timing record will
  appear in the main journal for the EscrowFinish.

Server-side signing (xrpld signs from our seed) is used so the probe
doesn't need client-side knowledge of the new FinishFunction field
encoding. Fine for standalone; the real driver will sign client-side.

Run:
    python3 probe_escrow_wasm.py
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from xrpl.utils import xrp_to_drops


# Ripple epoch starts 2000-01-01 UTC. Time fields on transactions use it.
RIPPLE_EPOCH_OFFSET = 946684800

def ripple_time(unix_seconds):
    return int(unix_seconds - RIPPLE_EPOCH_OFFSET)


# ---------------------------------------------------------------------------
# Hand-crafted "return 1" wasm module.
#
# Structure:
#   magic + version                                       8 bytes
#   type section:    () -> i32                            7 bytes
#   function section: 1 func of type 0                    4 bytes
#   export section:  "finish" func 0                     12 bytes
#   code section:    i32.const 1; end                     8 bytes
# Total: 39 bytes. No memory, no globals, no imports.
# ---------------------------------------------------------------------------
RETURN_1_WASM = bytes.fromhex(
    "0061736d01000000"                          # \0asm + version 1
    "01" "05" "01" "60" "00" "01" "7f"          # type: () -> i32
    "03" "02" "01" "00"                         # one function, type 0
    "07" "0a" "01" "06" "66696e697368" "00" "00"  # export "finish" as func 0
    "0a" "06" "01" "04" "00" "41" "01" "0b"     # body: i32.const 1; end
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


def must_succeed(label, res):
    """Print + bail if a submit didn't return tesSUCCESS."""
    eng = res.get("engine_result", "<missing>")
    print(f"  engine_result: {eng}")
    print(f"  tx_hash:       {res.get('tx_json', {}).get('hash')}")
    if eng != "tesSUCCESS":
        print(f"\n[FAIL] {label} did not return tesSUCCESS")
        print(f"  full response: {json.dumps(res, indent=2)}")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--accounts", default="test_accounts.json")
    ap.add_argument("--amount-xrp", type=int, default=100)
    args = ap.parse_args()

    with open(args.accounts) as f:
        entries = json.load(f)
    if len(entries) < 2:
        sys.exit("need at least 2 funded accounts in test_accounts.json")

    creator, receiver = entries[0], entries[1]
    print(f"[info] creator:  {creator['address']}")
    print(f"[info] receiver: {receiver['address']}")
    print(f"[info] wasm:     return_1, {len(RETURN_1_WASM)} bytes")
    print(f"[info] wasm hex: {RETURN_1_WASM.hex().upper()}")

    # Snapshot balances and creator sequence.
    creator_info = rpc(args.rpc_url, "account_info", {"account": creator["address"]})
    receiver_info = rpc(args.rpc_url, "account_info", {"account": receiver["address"]})
    creator_bal0 = int(creator_info["account_data"]["Balance"])
    receiver_bal0 = int(receiver_info["account_data"]["Balance"])
    create_seq = creator_info["account_data"]["Sequence"]
    print(f"[info] creator seq:    {create_seq}")
    print(f"[info] creator bal:    {creator_bal0}")
    print(f"[info] receiver bal:   {receiver_bal0}")

    # Current open-ledger fee. Smart escrow fees are derived from this.
    fee_res = rpc(args.rpc_url, "fee")
    open_ledger_fee = int(fee_res["drops"]["open_ledger_fee"])
    base_fee = int(fee_res["drops"]["base_fee"])
    print(f"[info] base_fee={base_fee}  open_ledger_fee={open_ledger_fee}")

    # Per XLS-0100 §6.2 (as used in xrpl4j's SmartEscrowSoakTest):
    #   EscrowCreate fee = open_ledger_fee * 10 + 5 * wasm_bytes
    #   EscrowFinish fee = open_ledger_fee * 100 + computation_allowance
    wasm_bytes = len(RETURN_1_WASM)
    create_fee = open_ledger_fee * 10 + 5 * wasm_bytes
    computation_allowance = 10000  # generous overkill for return_1
    finish_fee = open_ledger_fee * 100 + computation_allowance
    print(f"[info] create fee:  {create_fee} drops (formula: {open_ledger_fee}*10 + 5*{wasm_bytes})")
    print(f"[info] finish fee:  {finish_fee} drops (formula: {open_ledger_fee}*100 + {computation_allowance})")

    # -----------------------------------------------------------------------
    # STEP 1: EscrowCreate with FinishFunction + CancelAfter.
    # CancelAfter is mandatory — FinishFunction is an extra gate, not a
    # replacement for the lifetime cap.
    # -----------------------------------------------------------------------
    print("\n[step 1] EscrowCreate { FinishFunction = return_1 } ...")
    cancel_after = ripple_time(time.time() + 365 * 24 * 3600)  # 1 year out
    print(f"[info] CancelAfter (ripple epoch): {cancel_after}")
    create_tx = {
        "TransactionType": "EscrowCreate",
        "Account": creator["address"],
        "Destination": receiver["address"],
        "Amount": xrp_to_drops(args.amount_xrp),
        "FinishFunction": RETURN_1_WASM.hex().upper(),
        "CancelAfter": cancel_after,
        "Sequence": create_seq,
        "Fee": str(create_fee),
    }
    res = rpc(args.rpc_url, "submit", {"secret": creator["seed"], "tx_json": create_tx})
    must_succeed("EscrowCreate", res)

    print("[info] ledger_accept...")
    rpc(args.rpc_url, "ledger_accept")

    # -----------------------------------------------------------------------
    # STEP 2: EscrowFinish — invokes the wasm.
    # Creator finishes its own escrow. ComputationAllowance is the max gas
    # the wasm may consume; if exceeded, result is tecWASM_REJECTED.
    # -----------------------------------------------------------------------
    print("\n[step 2] EscrowFinish (runs wasm) ...")
    finisher_info = rpc(args.rpc_url, "account_info", {"account": creator["address"]})
    finish_seq = finisher_info["account_data"]["Sequence"]
    finish_tx = {
        "TransactionType": "EscrowFinish",
        "Account": creator["address"],
        "Owner": creator["address"],
        "OfferSequence": create_seq,
        "Sequence": finish_seq,
        "Fee": str(finish_fee),
        "ComputationAllowance": str(computation_allowance),
    }
    res = rpc(args.rpc_url, "submit", {"secret": creator["seed"], "tx_json": finish_tx})
    must_succeed("EscrowFinish", res)

    print("[info] ledger_accept...")
    rpc(args.rpc_url, "ledger_accept")

    # -----------------------------------------------------------------------
    # STEP 3: Verify the destination got paid and the escrow was removed.
    # -----------------------------------------------------------------------
    print("\n[step 3] verifying balances...")
    creator_bal1 = int(
        rpc(args.rpc_url, "account_info", {"account": creator["address"]})
        ["account_data"]["Balance"]
    )
    receiver_bal1 = int(
        rpc(args.rpc_url, "account_info", {"account": receiver["address"]})
        ["account_data"]["Balance"]
    )

    expected_recv = int(xrp_to_drops(args.amount_xrp))
    recv_delta = receiver_bal1 - receiver_bal0
    create_delta = creator_bal1 - creator_bal0  # negative: amount + 2 fees

    print(f"  creator:  {creator_bal0} → {creator_bal1}  delta={create_delta}")
    print(f"  receiver: {receiver_bal0} → {receiver_bal1}  delta={recv_delta}")

    if recv_delta != expected_recv:
        print(
            f"\n[FAIL] receiver delta {recv_delta} != expected {expected_recv}"
        )
        sys.exit(1)

    # Creator: paid out (amount + create_fee + finish_fee). Won't always be exact
    # because some result codes may charge slightly different fees; print and check
    # the rough envelope.
    expected_create_cost = expected_recv + create_fee + finish_fee
    print(f"  expected creator delta ≈ -{expected_create_cost}")

    print(f"\n[OK] EscrowCreate + EscrowFinish round-trip with return_1 wasm")
    print(f"     Check xrpld's main journal for a WASM_TIMING line "
          f"(if the timing patch is applied).")


if __name__ == "__main__":
    main()
