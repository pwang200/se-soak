#!/usr/bin/env python3
"""Fund N test accounts from genesis on standalone xrpld. Scales to ~1M.

Two layouts:
- count <= branch: 1 generation. Genesis funds each leaf directly.
- count >  branch: 2 generations. Genesis funds ceil(count/branch) parents,
  each parent funds up to `branch` children. Parents work in parallel.

The 2-generation case is sized for 1M leaves (e.g., --count 1_000_000
--branch 1000 → 1000 parents × 1000 children each). Submissions are
chunked by --chunk-size and we wait for each chunk's last tx to validate
via the external ledger ticker (or the built-in --manage-ledgers thread).

Output: --output (default runs/test_accounts.json), JSON list of
{address, seed, balance_drops} for leaves only. Intermediate parents
are spent fuel. run_soak.py and check_state.py read the same default.

Run:
    python3 setup_accounts.py --count 50                    # smoke
    python3 setup_accounts.py --count 1_000_000 --branch 1000

Offline keygen first (recommended at scale): generate keys once, fund (or
re-fund) from the file so a million keypairs are not regenerated per run:
    python3 gen_keys.py --count 1_000_000 --output runs/keys.json
    python3 setup_accounts.py --keys-file runs/keys.json --branch 1000
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from xrpl.constants import CryptoAlgorithm
from xrpl.core.binarycodec import encode
from xrpl.models.transactions import Payment
from xrpl.transaction import sign
from xrpl.utils import xrp_to_drops
from xrpl.wallet import Wallet

from escrow_lib import DEFAULT_ACCOUNTS_FILE, GENESIS_SEED, rpc, wait_for_validated


# Funding txs use a generous flat fee — at scale, open_ledger_fee can climb
# under load and we don't want our payments getting terQUEUED.
FUNDING_FEE_DROPS = 1000

# LastLedgerSequence offset wide enough for any reasonable setup run.
# 5000 ledgers at 4s/close ≈ 5.5h of headroom.
LAST_LEDGER_OFFSET = 5000


# ---------------------------------------------------------------------------
# Parallel wallet generation
# ---------------------------------------------------------------------------

def _make_wallet(_ignored):
    # Top-level so ProcessPoolExecutor can pickle the call target.
    from xrpl.wallet import Wallet as _W
    return _W.create()


def generate_wallets(n: int) -> list[Wallet]:
    """Generate n wallets. Uses a process pool for n > 1000 to bypass the GIL
    on the cumulative keygen cost (single-thread ed25519 keygen ≈ 100-500 us
    per wallet, so 1M wallets is multi-minute serially)."""
    if n < 1000:
        return [Wallet.create() for _ in range(n)]
    print(f"[wallet] generating {n:,} wallets in parallel "
          f"({os.cpu_count()} cores)...")
    t0 = time.monotonic()
    chunksize = max(1, n // (os.cpu_count() * 8))
    with ProcessPoolExecutor() as pool:
        wallets = list(pool.map(_make_wallet, range(n), chunksize=chunksize))
    print(f"[wallet] generated {n:,} in {time.monotonic() - t0:.1f}s")
    return wallets


# ---------------------------------------------------------------------------
# Ledger advancement probe (matches run_soak.py's behavior)
# ---------------------------------------------------------------------------

def probe_ledger_advances(rpc_url: str, ledger_interval_s: float) -> None:
    """Confirm something is calling ledger_accept. Fail loud if not."""
    try:
        i0 = int(rpc(rpc_url, "ledger_current")["ledger_current_index"])
        time.sleep(max(ledger_interval_s * 1.5, 3.0))
        i1 = int(rpc(rpc_url, "ledger_current")["ledger_current_index"])
    except RuntimeError as e:
        sys.exit(f"[fail] xrpld not reachable for ledger probe: {e}")
    if i1 <= i0:
        sys.exit(
            f"[fail] ledger isn't advancing (index stayed at {i0} for "
            f"{ledger_interval_s * 1.5:.1f}s). Start ledger_ticker.py in "
            f"another terminal, or rerun with --manage-ledgers."
        )


def ledger_closer_thread(rpc_url: str, interval_s: float,
                         stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            rpc(rpc_url, "ledger_accept")
        except RuntimeError as e:
            print(f"[FATAL] ledger_accept failed: {e}", file=sys.stderr)
            stop_event.set()
            return
        stop_event.wait(interval_s)


# ---------------------------------------------------------------------------
# Submission helpers
# ---------------------------------------------------------------------------

def sign_payment(wallet: Wallet, dest_address: str, amount_drops: int,
                 sequence: int, last_ledger: int) -> str:
    tx = Payment(
        account=wallet.address,
        destination=dest_address,
        amount=str(amount_drops),
        sequence=sequence,
        fee=str(FUNDING_FEE_DROPS),
        last_ledger_sequence=last_ledger,
    )
    signed = sign(tx, wallet)
    return encode(signed.to_xrpl())


def fund_in_chunks(rpc_url: str, source: Wallet, source_start_seq: int,
                   target_addresses: list[str], amount_drops: int,
                   chunk_size: int, last_ledger: int) -> None:
    """Submit Payments from `source` to each target. After every `chunk_size`
    submissions, wait_for_validated on the chunk's last tx hash before
    starting the next chunk. Fail loud on any submit error.

    Caller must ensure last_ledger is far enough out — see LAST_LEDGER_OFFSET.
    """
    n = len(target_addresses)
    seq = source_start_seq
    submitted = 0
    while submitted < n:
        batch = target_addresses[submitted:submitted + chunk_size]
        last_hash: str | None = None
        for target in batch:
            blob = sign_payment(source, target, amount_drops, seq, last_ledger)
            res = rpc(rpc_url, "submit", {"tx_blob": blob})
            er = res.get("engine_result", "<missing>")
            if er not in {"tesSUCCESS", "terQUEUED"}:
                raise RuntimeError(
                    f"submit from {source.address} → {target} (seq={seq}) "
                    f"returned {er!r}; full: {res}"
                )
            last_hash = (res.get("tx_json") or {}).get("hash")
            seq += 1
            submitted += 1
        if last_hash:
            # 120s is overkill for 4s ledger cadence, but cheap insurance
            # against the rare ledger-close stall under heavy load.
            wait_for_validated(rpc_url, last_hash, timeout_s=120.0)


# ---------------------------------------------------------------------------
# Verification (parallel)
# ---------------------------------------------------------------------------

def verify_balance(rpc_url: str, address: str) -> int | None:
    try:
        info = rpc(rpc_url, "account_info", {"account": address})
        return int(info["account_data"]["Balance"])
    except RuntimeError:
        return None


def verify_all_parallel(rpc_url: str, wallets: list[Wallet],
                        label: str, concurrency: int = 32) -> list[dict]:
    n = len(wallets)
    out: list[dict | None] = [None] * n
    not_found: list[str] = []
    nf_lock = threading.Lock()
    progress = [0]

    def task(idx_w):
        idx, w = idx_w
        bal = verify_balance(rpc_url, w.address)
        with nf_lock:
            progress[0] += 1
            if progress[0] % max(1, n // 10) == 0:
                print(f"  [{label}] verified {progress[0]:,}/{n:,}")
            if bal is None:
                not_found.append(w.address)
        out[idx] = {"address": w.address, "seed": w.seed, "balance_drops": bal}

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(task, enumerate(wallets)))
    print(f"  [{label}] verify done in {time.monotonic() - t0:.1f}s")

    if not_found:
        print(f"[FAIL] {label}: {len(not_found):,}/{n:,} accounts missing",
              file=sys.stderr)
        for a in not_found[:10]:
            print(f"  missing: {a}", file=sys.stderr)
        sys.exit(1)
    return out  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Generation funders
# ---------------------------------------------------------------------------

def fund_one_generation(rpc_url: str, source: Wallet, targets: list[Wallet],
                        amount_drops: int, chunk_size: int,
                        label: str) -> None:
    info = rpc(rpc_url, "account_info", {"account": source.address})
    start_seq = int(info["account_data"]["Sequence"])
    last_ledger = (
        int(rpc(rpc_url, "ledger_current")["ledger_current_index"])
        + LAST_LEDGER_OFFSET
    )

    print(f"[{label}] {source.address[:10]}…  → {len(targets):,} targets  "
          f"seq_start={start_seq}  amount={amount_drops} drops  "
          f"chunk_size={chunk_size}")
    t0 = time.monotonic()
    fund_in_chunks(
        rpc_url, source, start_seq, [w.address for w in targets],
        amount_drops, chunk_size, last_ledger,
    )
    print(f"[{label}] submission done in {time.monotonic() - t0:.1f}s")


def fund_parents_to_children_parallel(rpc_url: str,
                                      parents: list[Wallet],
                                      child_assignments: list[list[Wallet]],
                                      amount_drops: int,
                                      chunk_size: int,
                                      max_parallel_parents: int = 50) -> None:
    """Each parents[i] funds child_assignments[i] in chunks of chunk_size,
    waiting for each chunk's last tx to validate. Parents work in parallel
    (up to max_parallel_parents at a time)."""
    assert len(parents) == len(child_assignments)
    parent_count = len(parents)
    total_children = sum(len(c) for c in child_assignments)

    # Snapshot per-parent sequence + a generous LastLedgerSequence once.
    parent_seqs: list[int] = []
    for w in parents:
        info = rpc(rpc_url, "account_info", {"account": w.address})
        parent_seqs.append(int(info["account_data"]["Sequence"]))
    last_ledger = (
        int(rpc(rpc_url, "ledger_current")["ledger_current_index"])
        + LAST_LEDGER_OFFSET
    )

    print(f"[phase B] {parent_count:,} parents → {total_children:,} children "
          f"in parallel  amount={amount_drops} drops  "
          f"chunk_size={chunk_size}  pool={min(parent_count, max_parallel_parents)}")

    progress_lock = threading.Lock()
    submitted_count = [0]

    def parent_task(idx: int):
        children_addrs = [w.address for w in child_assignments[idx]]
        n = len(children_addrs)
        try:
            fund_in_chunks(
                rpc_url, parents[idx], parent_seqs[idx],
                children_addrs, amount_drops, chunk_size, last_ledger,
            )
        except RuntimeError as e:
            return (idx, str(e))
        with progress_lock:
            submitted_count[0] += n
            done = submitted_count[0]
            total = total_children
            # log every ~5% increment
            if done * 20 // total != (done - n) * 20 // total or done == total:
                print(f"  [phase B] {done:,}/{total:,} "
                      f"({100 * done // total}%)")
        return (idx, None)

    t0 = time.monotonic()
    errors = []
    with ThreadPoolExecutor(
        max_workers=min(parent_count, max_parallel_parents),
    ) as pool:
        for idx, err in pool.map(parent_task, range(parent_count)):
            if err is not None:
                errors.append((idx, err))
    print(f"[phase B] submission done in {time.monotonic() - t0:.1f}s")

    if errors:
        print(f"[FAIL] {len(errors)} parent task(s) failed:", file=sys.stderr)
        for idx, err in errors[:5]:
            print(f"  parent[{idx}]: {err}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rpc-url", default="http://127.0.0.1:5005/")
    ap.add_argument("--count", type=int, default=100,
                    help="Number of leaf (worker / cold-pool) accounts. "
                         "Ignored when --keys-file is given (its length wins).")
    ap.add_argument("--keys-file", default=None,
                    help="Pre-generated keys JSON from gen_keys.py. Funds those "
                         "leaves instead of generating fresh ones, so keygen is "
                         "done once offline and reused across funding runs.")
    ap.add_argument("--branch", type=int, default=100,
                    help="Children per parent. With count > branch we use a "
                         "2-generation tree. branch * branch upper-bounds the "
                         "max count (use --branch 1000 for 1M, 1500 for ~2M).")
    ap.add_argument("--fund-xrp", type=int, default=10_000,
                    help="XRP per leaf account. Default sized for the soak "
                         "workload at 1 XRP/escrow (~10K cycles of headroom).")
    ap.add_argument("--chunk-size", type=int, default=100,
                    help="Payments per parent per chunk. Each chunk waits for "
                         "its last tx to validate before the next starts.")
    ap.add_argument("--max-parallel-parents", type=int, default=50,
                    help="Concurrent parent threads in phase B.")
    ap.add_argument("--ledger-interval-s", type=float, default=4.0,
                    help="Used by --manage-ledgers thread, and by the startup "
                         "probe when assuming external ledger advancement.")
    ap.add_argument("--manage-ledgers", action="store_true",
                    help="Run a background ledger_accept thread inside this "
                         "process. Default off — assumes ledger_ticker.py or "
                         "a real network is advancing ledgers.")
    ap.add_argument("--output", default=str(DEFAULT_ACCOUNTS_FILE),
                    help="Where to write the accounts JSON. "
                         "Default: runs/test_accounts.json")
    args = ap.parse_args()

    if args.branch < 1:
        sys.exit("--branch must be >= 1")

    # Resolve the leaf wallets up front so --count reflects reality in the
    # banner and the 1-vs-2 generation decision below.
    preloaded_leaves = None
    if args.keys_file:
        with open(args.keys_file) as f:
            key_entries = json.load(f)
        if not key_entries:
            sys.exit(f"--keys-file {args.keys_file} is empty")
        preloaded_leaves = [
            Wallet(public_key=e["public_key"], private_key=e["private_key"],
                   seed=e.get("seed"))
            for e in key_entries
        ]
        args.count = len(preloaded_leaves)
    if args.count < 1:
        sys.exit("--count must be >= 1")

    leaf_fund_drops = int(xrp_to_drops(args.fund_xrp))
    genesis = Wallet.from_seed(GENESIS_SEED, algorithm=CryptoAlgorithm.SECP256K1)

    print(f"[info] rpc       = {args.rpc_url}")
    print(f"[info] count     = {args.count:,}  branch = {args.branch}  "
          f"fund_xrp = {args.fund_xrp:,}")
    print(f"[info] chunk_size = {args.chunk_size}  "
          f"max_parallel_parents = {args.max_parallel_parents}")
    print(f"[info] genesis   = {genesis.address}")

    # Sanity-check connectivity + genesis funding.
    info = rpc(args.rpc_url, "account_info", {"account": genesis.address})
    print(f"[info] genesis seq = {info['account_data']['Sequence']}  "
          f"balance = {info['account_data']['Balance']}")

    # Ledger advancement plumbing (mirror of run_soak.py).
    stop_event = threading.Event()
    if args.manage_ledgers:
        closer = threading.Thread(
            target=ledger_closer_thread,
            args=(args.rpc_url, args.ledger_interval_s, stop_event),
            name="setup-ledger-closer",
            daemon=True,
        )
        closer.start()
        print(f"[info] ledger advance = internal thread every "
              f"{args.ledger_interval_s}s")
    else:
        probe_ledger_advances(args.rpc_url, args.ledger_interval_s)
        print(f"[info] ledger advance = external (ticker / network)")

    if preloaded_leaves is not None:
        leaves = preloaded_leaves
        print(f"[info] loaded {args.count:,} leaves from {args.keys_file}")
    else:
        leaves = generate_wallets(args.count)

    try:
        if args.count <= args.branch:
            print(f"[plan] 1 generation: genesis → {args.count:,} leaves")
            fund_one_generation(
                args.rpc_url, genesis, leaves, leaf_fund_drops,
                args.chunk_size, label="phase A",
            )
            verified = verify_all_parallel(
                args.rpc_url, leaves, label="phase A",
            )
        else:
            parent_count = math.ceil(args.count / args.branch)
            if parent_count > args.branch:
                sys.exit(
                    f"[fail] count={args.count:,} with branch={args.branch} "
                    f"needs {parent_count:,} parents from genesis, exceeding "
                    f"branch. Raise --branch (try {math.ceil(math.sqrt(args.count))})."
                )

            # Parents pay out (branch * fund + branch * fee) plus their own
            # reserve. ~10x XRPL base reserve as headroom.
            reserve_drops = int(xrp_to_drops(100))
            per_parent_drops = (
                args.branch * (leaf_fund_drops + FUNDING_FEE_DROPS)
                + reserve_drops
            )
            print(f"[plan] 2 generations: genesis → {parent_count:,} parents "
                  f"→ {args.count:,} leaves  (per-parent funding = "
                  f"{per_parent_drops:,} drops)")

            parents = generate_wallets(parent_count)

            # Phase A: genesis → parents.
            fund_one_generation(
                args.rpc_url, genesis, parents, per_parent_drops,
                args.chunk_size, label="phase A",
            )
            # Quick smoke check on parents (full verify only for leaves).
            for w in parents[:3] + parents[-1:]:
                bal = verify_balance(args.rpc_url, w.address)
                if bal is None:
                    sys.exit(f"[fail] parent {w.address} not funded")

            # Phase B: parents → leaves, round-robin assignment.
            assignments: list[list[Wallet]] = [[] for _ in range(parent_count)]
            for i, leaf in enumerate(leaves):
                assignments[i % parent_count].append(leaf)
            fund_parents_to_children_parallel(
                args.rpc_url, parents, assignments, leaf_fund_drops,
                args.chunk_size, args.max_parallel_parents,
            )
            verified = verify_all_parallel(
                args.rpc_url, leaves, label="phase B",
            )

        print(f"[info] writing {len(verified):,} accounts to {args.output}...")
        t0 = time.monotonic()
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(verified, f, indent=2)
        print(f"[OK] saved {len(verified):,} leaves to {args.output} "
              f"({time.monotonic() - t0:.1f}s)")
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
