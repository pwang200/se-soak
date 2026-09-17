#!/usr/bin/env python3
"""Offline key generation: create N wallets and write them to a keys file.

No node required. This is split from funding (setup_accounts.py) so the
expensive keygen for a large account set is done ONCE and reused across
funding runs — e.g. after an xrpld restart, re-fund the same accounts without
regenerating a million keypairs.

Each record stores address, seed, public_key and private_key, so
`setup_accounts.py --keys-file` rebuilds each wallet from the stored keypair
(no re-derivation).

Run:
    python3 gen_keys.py --count 50                         # smoke
    python3 gen_keys.py --count 1_000_000 --output runs/keys.json
"""
from __future__ import annotations

import argparse
import json
import os
import time

from escrow_lib import RUNS_DIR
from setup_accounts import generate_wallets  # process-pool generator


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, required=True,
                    help="Number of wallets to generate.")
    ap.add_argument("--output", default=str(RUNS_DIR / "keys.json"),
                    help="Where to write the keys JSON. Default: runs/keys.json")
    args = ap.parse_args()
    if args.count < 1:
        raise SystemExit("--count must be >= 1")

    print(f"[info] generating {args.count:,} wallets (offline, no node)...")
    t0 = time.monotonic()
    wallets = generate_wallets(args.count)
    data = [
        {"address": w.address, "seed": w.seed,
         "public_key": w.public_key, "private_key": w.private_key}
        for w in wallets
    ]
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(data, f)
    print(f"[OK] wrote {len(data):,} keys to {args.output} "
          f"in {time.monotonic() - t0:.1f}s")
    print(f"[next] fund them: python3 setup_accounts.py --keys-file "
          f"{args.output} --branch <B>")


if __name__ == "__main__":
    main()
