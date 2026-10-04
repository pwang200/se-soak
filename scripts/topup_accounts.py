#!/usr/bin/env python3
"""Top up test accounts below a threshold from the standalone genesis account. Needs a ticker running.
usage: topup_accounts.py [--below 5000] [--to 10000]"""
import argparse, json, os, sys, time
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_DATA = os.environ.get("SOAK_RUN_DATA", os.path.expanduser("~/rippled/run_data"))
sys.path.insert(0, REPO)
from escrow_lib import GENESIS_SEED, GENESIS_ADDRESS, rpc, wait_for_validated

ap = argparse.ArgumentParser(); ap.add_argument("--below", type=int, default=5000); ap.add_argument("--to", type=int, default=10000)
a = ap.parse_args()
URL = "http://127.0.0.1:5005/"
accts = json.load(open(f"{RUN_DATA}/soak/test_accounts.json"))
need = []
for x in accts:
    b = int(rpc(URL, "account_info", {"account": x["address"]})["account_data"]["Balance"]) / 1e6
    if b < a.below:
        need.append((x["address"], b))
print(f"{len(need)} accounts below {a.below} XRP")
seq = int(rpc(URL, "account_info", {"account": GENESIS_ADDRESS})["account_data"]["Sequence"])
last = None
for addr, b in need:
    tx = {"TransactionType": "Payment", "Account": GENESIS_ADDRESS, "Destination": addr,
          "Amount": str(int((a.to - b) * 1e6)), "Sequence": seq, "Fee": "100"}
    r = rpc(URL, "submit", {"secret": GENESIS_SEED, "tx_json": tx})
    print(addr, f"{b:.0f} -> {a.to}", r.get("engine_result"))
    if r.get("engine_result") == "tesSUCCESS":
        seq += 1; last = r["tx_json"]["hash"]
if last:
    time.sleep(6)
for addr, _ in need:
    print(addr, int(rpc(URL, "account_info", {"account": addr})["account_data"]["Balance"]) / 1e6, "XRP")
