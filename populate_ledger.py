#!/usr/bin/env python3
"""Populate a standalone ledger with real, diverse objects for the soak.

Runs AFTER setup_accounts.py and BEFORE run_soak.py. Uses the funded pool
accounts (runs/test_accounts.json) as actors to create seven object types,
round-robin across owners so every account owns some of every type, with
randomized parameters so the set is diverse (the point — audit-guided DoS
categories that need varied ledger access read these back at soak time).

Object types: trustlines, offers, nftokens, mpts (MPTokenIssuance), escrows
(non-wasm, CancelAfter +1 year so they never vanish mid-soak), oracles,
credentials.

Output: one JSON index per type in populated/. Each record carries the
ingredients you'd need to recompute the keylet AND the node's real keylet
(`index`, taken from the tx metadata's CreatedNode — no client-side keylet
math). At the ~140k/type target a file is ~10 MB; plain JSON, loaded at
driver startup.

Idempotency: if populated/ already has index files, prints counts and exits.
Fresh start = delete the directory.

Fail-loud: any submit that is not tesSUCCESS, or any object whose keylet can't
be read back, aborts with a non-zero exit.

Run:
    python3 populate_ledger.py --count-per-type 50            # dev
    python3 populate_ledger.py --count-per-type 140000        # ~1M total
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time

from xrpl.core.addresscodec import decode_classic_address
from xrpl.core.binarycodec import encode
from xrpl.transaction import sign
from xrpl.wallet import Wallet
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import (
    CredentialCreate,
    EscrowCreate,
    MPTokenIssuanceCreate,
    NFTokenMint,
    OfferCreate,
    OracleSet,
    TrustSet,
)
from xrpl.models.transactions.oracle_set import PriceData

from escrow_lib import (
    DEFAULT_ACCOUNTS_FILE,
    RUNS_DIR,
    now_ripple,
    rpc,
    wait_for_validated,
)

POPULATED_DIR = RUNS_DIR.parent / "populated"
TYPES = ["trustlines", "offers", "nftokens", "mpts", "escrows", "oracles",
         "credentials"]
YEAR_SECONDS = 365 * 24 * 3600
FLAT_FEE = "100"
LAST_LEDGER = 99_999_999   # standalone; effectively no expiry on the tx


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def hex20(address: str) -> str:
    """20-byte account id as uppercase hex."""
    return decode_classic_address(address).hex().upper()


def rand_currency_hex() -> str:
    """40-hex (20-byte) non-standard currency code — always distinct."""
    return secrets.token_hex(20).upper()


class Populator:
    def __init__(self, rpc_url: str, pool: list[Wallet], chunk_size: int):
        self.rpc_url = rpc_url
        self.pool = pool
        self.n = len(pool)
        self.chunk_size = chunk_size
        self._seq: dict[str, int] = {}

    def seq(self, w: Wallet) -> int:
        if w.address not in self._seq:
            info = rpc(self.rpc_url, "account_info", {"account": w.address})
            self._seq[w.address] = int(info["account_data"]["Sequence"])
        return self._seq[w.address]

    def _submit(self, w: Wallet, tx) -> str:
        blob = encode(sign(tx, w).to_xrpl())
        res = rpc(self.rpc_url, "submit", {"tx_blob": blob})
        er = res.get("engine_result", "<missing>")
        if er != "tesSUCCESS":
            raise RuntimeError(
                f"populate submit ({tx.transaction_type}) from {w.address} "
                f"returned {er!r}: {res}"
            )
        self._seq[w.address] += 1
        return (res.get("tx_json") or {}).get("hash") or res.get("tx_hash")

    def _keylet_from_meta(self, tx_hash: str, want_type: str,
                          allow_modified: bool = False) -> tuple[str, dict]:
        """Return (keylet, meta) for the object of want_type. Normally the
        object is newly created (CreatedNode). NFTokenPage is the exception:
        the first mint for an owner creates a page, later mints modify it, so
        allow_modified lets us take the page's LedgerIndex from a ModifiedNode.
        Fail loud if the object isn't present at all."""
        res = wait_for_validated(self.rpc_url, tx_hash, timeout_s=60.0)
        meta = res.get("meta") or {}
        if meta.get("TransactionResult") != "tesSUCCESS":
            raise RuntimeError(f"{tx_hash}: validated {meta.get('TransactionResult')}")
        kinds = ("CreatedNode", "ModifiedNode") if allow_modified else ("CreatedNode",)
        for node in meta.get("AffectedNodes", []):
            for k in kinds:
                nd = node.get(k)
                if nd and nd.get("LedgerEntryType") == want_type:
                    return nd["LedgerIndex"], meta
        raise RuntimeError(
            f"{tx_hash}: no {'/'.join(kinds)} of type {want_type} in meta "
            f"(types: {[list(x.values())[0].get('LedgerEntryType') for x in meta.get('AffectedNodes', [])]})"
        )

    # -- per-type builders: return (owner, tx, ingredients, want_ledger_type) --

    def build(self, kind: str, i: int):
        owner = self.pool[i % self.n]
        other = self.pool[(i + 1) % self.n]
        s = self.seq(owner)
        common = dict(sequence=s, fee=FLAT_FEE, last_ledger_sequence=LAST_LEDGER)

        if kind == "trustlines":
            cur = rand_currency_hex()
            tx = TrustSet(account=owner.address, limit_amount=IssuedCurrencyAmount(
                currency=cur, issuer=other.address,
                value=str(secrets.randbelow(1_000_000) + 1)), **common)
            ing = {"account1": hex20(owner.address), "account2": hex20(other.address),
                   "currency": cur}
            return owner, tx, ing, "RippleState"

        if kind == "offers":
            tx = OfferCreate(account=owner.address,
                taker_gets=str(secrets.randbelow(9_000_000) + 1_000_000),
                taker_pays=IssuedCurrencyAmount(currency=rand_currency_hex(),
                    issuer=other.address, value=str(secrets.randbelow(1000) + 1)),
                **common)
            ing = {"owner": hex20(owner.address), "seq": s}
            return owner, tx, ing, "Offer"

        if kind == "nftokens":
            tx = NFTokenMint(account=owner.address,
                nftoken_taxon=secrets.randbelow(2**32), **common)
            ing = {"owner": hex20(owner.address)}   # nft_id filled from meta
            return owner, tx, ing, "NFTokenPage"

        if kind == "mpts":
            tx = MPTokenIssuanceCreate(account=owner.address,
                asset_scale=secrets.randbelow(16),
                maximum_amount=str(secrets.randbelow(2**60) + 1), **common)
            ing = {"issuer": hex20(owner.address), "seq": s}
            return owner, tx, ing, "MPTokenIssuance"

        if kind == "escrows":
            nr = now_ripple()
            tx = EscrowCreate(account=owner.address, destination=other.address,
                amount=str(secrets.randbelow(9_000_000) + 1_000_000),
                finish_after=nr + 3600, cancel_after=nr + YEAR_SECONDS, **common)
            ing = {"owner": hex20(owner.address), "seq": s}
            return owner, tx, ing, "Escrow"

        if kind == "oracles":
            doc_id = secrets.randbelow(2**32)
            tx = OracleSet(account=owner.address, oracle_document_id=doc_id,
                provider=secrets.token_hex(8).upper(),
                asset_class=secrets.token_hex(4).upper(),
                last_update_time=int(time.time()),
                price_data_series=[PriceData(base_asset="XRP", quote_asset="USD",
                    asset_price=secrets.randbelow(10000) + 1, scale=2)], **common)
            ing = {"owner": hex20(owner.address), "doc_id": doc_id}
            return owner, tx, ing, "Oracle"

        if kind == "credentials":
            ctype = secrets.token_hex(8).upper()
            tx = CredentialCreate(account=owner.address, subject=other.address,
                credential_type=ctype, **common)
            ing = {"subject": hex20(other.address), "issuer": hex20(owner.address),
                   "type": ctype}
            return owner, tx, ing, "Credential"

        raise ValueError(f"unknown kind {kind}")

    def populate_type(self, kind: str, count: int) -> list[dict]:
        records: list[dict] = []
        pending: list[tuple[dict, str, str]] = []   # (ingredients, tx_hash, want_type)
        t0 = time.monotonic()

        is_nft = kind == "nftokens"

        def drain():
            for ing, h, want in pending:
                keylet, meta = self._keylet_from_meta(h, want, allow_modified=is_nft)
                rec = dict(ing)
                rec["index"] = keylet          # NFT: the NFTokenPage keylet
                if is_nft:
                    rec["nft_id"] = meta.get("nftoken_id")
                records.append(rec)
            pending.clear()

        for i in range(count):
            owner, tx, ing, want = self.build(kind, i)
            h = self._submit(owner, tx)
            pending.append((ing, h, want))
            if len(pending) >= self.chunk_size:
                drain()
            if (i + 1) % max(1, count // 10) == 0 or i + 1 == count:
                print(f"  [{kind}] {i + 1:,}/{count:,} "
                      f"({time.monotonic() - t0:.1f}s)")
        drain()
        return records


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_FILE))
    ap.add_argument("--count-per-type", type=int, default=50)
    ap.add_argument("--chunk-size", type=int, default=200,
                    help="Submit this many, then read their keylets back.")
    ap.add_argument("--output-dir", default=str(POPULATED_DIR))
    args = ap.parse_args()

    out = args.output_dir
    existing = (os.path.isdir(out)
                and any(f.endswith(".json") for f in os.listdir(out)))
    if existing:
        print(f"[skip] {out} already populated:")
        for kind in TYPES:
            p = os.path.join(out, f"{kind}.json")
            n = len(json.load(open(p))) if os.path.exists(p) else 0
            print(f"    {kind:12s} {n:,}")
        print("Delete the directory for a fresh start.")
        return

    with open(args.accounts) as f:
        entries = json.load(f)
    pool = [
        Wallet(public_key=e["public_key"], private_key=e["private_key"],
               seed=e.get("seed"))
        if "public_key" in e else Wallet.from_seed(e["seed"])
        for e in entries
    ]
    if not pool:
        sys.exit(f"no accounts in {args.accounts}")
    print(f"[info] {len(pool)} pool accounts, {args.count_per_type:,} per type, "
          f"{len(TYPES)} types → {args.count_per_type * len(TYPES):,} objects")

    os.makedirs(out, exist_ok=True)
    pop = Populator(args.rpc_url, pool, args.chunk_size)
    totals = {}
    t0 = time.monotonic()
    for kind in TYPES:
        print(f"[{kind}] creating {args.count_per_type:,}...")
        records = pop.populate_type(kind, args.count_per_type)
        path = os.path.join(out, f"{kind}.json")
        with open(path, "w") as f:
            json.dump(records, f)
        totals[kind] = len(records)
        print(f"[{kind}] wrote {len(records):,} → {path}")

    print(f"\n[OK] populated {sum(totals.values()):,} objects in "
          f"{time.monotonic() - t0:.1f}s")
    for kind in TYPES:
        print(f"    {kind:12s} {totals[kind]:,}")


if __name__ == "__main__":
    main()
