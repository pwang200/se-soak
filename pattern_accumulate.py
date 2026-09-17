"""AccumulatePattern: the accumulate_then_drain lifecycle.

Steady-state patterns (serial, pipeline) keep live smart escrows bounded by
worker count, so they can't reveal anything that scales with the number of
*concurrent live smart escrows*. This pattern deliberately drives that
dimension: each thread, for one owner at a time, creates N live smart escrows
(accumulation), then drains them (drain), and repeats.

What it's probing (all wasm-specific):
  - per-live-object state in the smart-escrow path that isn't freed until the
    last escrow drains (watch RSS across the accumulate ramp / plateau / drain);
  - whether per-Finish wasm cost grows with the concurrent live count on the
    owner (each drain Finish is logged with the count still live at that point,
    join tx_hash -> WASM_TIMING to correlate);
  - anything wasm-specific in the ledger-close path proportional to live count.

N (accumulation depth) comes from the category's accumulate_depth, overridable
with --accumulate-depth. Realistic depths are hundreds to low thousands; small
N won't show accumulation scaling (steady-state already sits near 1 per worker).

Draining uses the case's base lifecycle: finish_removes -> Finish each;
cancel_removes -> (step 3) Finish then Cancel after expiry. Only finish_removes
draining is implemented here; a cancel_removes case under accumulate raises.

Owner reserve is the binding limit: each live escrow locks ReserveIncrement
(2 XRP) + its amount until drained, so peak hold ~ N*(2+amount) XRP. A create
that fails mid-accumulation (e.g. tecINSUFFICIENT_RESERVE) stops the ramp and
the reached depth is reported — that's a wall worth seeing, not an error to
hide, so it's logged and drained normally.

Per-phase detail is written to <run-dir>/accumulate_detail.csv:
    ts, thread_id, account, category, phase, burst_index, live_count,
    tx_hash, engine_result, final_result
plus [accumulate] phase-marker lines on stderr (-> run_soak.log) with
timestamps and depth, so the memory sampler's RSS can be segmented.
"""
from __future__ import annotations

import csv as _csv
import sys
import threading
import time

from escrow_lib import (
    CANCEL_REMOVES,
    FINISH_REMOVES,
    Category,
    Pattern,
    PatternContext,
    Worker,
    get_close_time,
    get_open_ledger_fee,
    is_applied,
    now_iso,
    wait_for_validated,
)

# CancelAfter for finish_removes accumulation escrows: long enough that they
# never expire during a burst (accumulate + drain of thousands can take many
# minutes); they are removed by Finish, not Cancel, so it only needs to be big.
ACCUM_CANCEL_AFTER_S = 6 * 3600

# For cancel_removes cases the drain Finishes each escrow (the wasm reject, the
# measurement) and then Cancels it, which needs it expired. CancelAfter must
# outlast accumulate + drain-Finish for ALL N escrows (a Finish after CancelAfter
# returns tecNO_PERMISSION), so it scales with N; the leftover time before it
# expires is a plateau with N escrows live, which is exactly the RSS we want to
# observe. base + per_n*N seconds; per_n well above the measured ~0.16 s/escrow.
CANCEL_ACCUM_BASE_S = 60
CANCEL_ACCUM_PER_N = 0.5

# Submit this many creates/finishes, then wait for the batch's last tx to
# validate before the next batch. Bounds outstanding work without one
# validation wait per object.
BATCH = 50


class AccumulateDetail:
    """Thread-safe writer for the per-phase detail log."""

    def __init__(self, path: str):
        self._fh = open(path, "w", newline="", buffering=1)
        self._w = _csv.writer(self._fh)
        self._w.writerow(["ts", "thread_id", "account", "category", "phase",
                          "burst_index", "live_count", "tx_hash",
                          "engine_result", "final_result"])
        self._lock = threading.Lock()

    def row(self, *cols):
        with self._lock:
            self._w.writerow(cols)
            self._fh.flush()

    def close(self):
        self._fh.close()


class AccumulatePattern(Pattern):
    name = "accumulate"

    def __init__(self, *, depth_override: int | None = None,
                 detail_path: str | None = None,
                 amount_xrp_per_escrow: int = 1):
        self.depth_override = depth_override
        self.detail_path = detail_path
        self.amount_xrp = amount_xrp_per_escrow
        self._detail: AccumulateDetail | None = None

    # ----- thread entry -----

    def run_thread(self, thread_idx: int, accounts: list[Worker],
                   ctx: PatternContext) -> None:
        caps = [c for c in ctx.categories if c.accumulate_depth is not None]
        if not caps:
            print("[FATAL] accumulate pattern: no category has "
                  "accumulate_depth set", file=sys.stderr)
            ctx.failure_event.set(); ctx.stop_event.set(); return
        if self._detail is None and self.detail_path:
            # First thread to arrive opens the shared detail writer.
            self._detail = AccumulateDetail(self.detail_path)

        cycle = 0
        try:
            while not ctx.stop_event.is_set():
                for worker in accounts:
                    if ctx.stop_event.is_set():
                        break
                    category = caps[(thread_idx + cycle) % len(caps)]
                    self._accumulate_then_drain(worker, category, ctx,
                                                thread_idx)
                    cycle += 1
        except RuntimeError as e:
            print(f"[FATAL] accumulate thread {thread_idx}: {e}",
                  file=sys.stderr)
            ctx.failure_event.set(); ctx.stop_event.set()
        except Exception as e:  # noqa: BLE001
            print(f"[FATAL] accumulate thread {thread_idx} unexpected: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            ctx.failure_event.set(); ctx.stop_event.set()

    # ----- one accumulate -> drain burst -----

    def _accumulate_then_drain(self, worker: Worker, category: Category,
                               ctx: PatternContext, thread_idx: int) -> None:
        if category.lifecycle not in (FINISH_REMOVES, CANCEL_REMOVES):
            raise RuntimeError(
                f"{category.name}: lifecycle {category.lifecycle} cannot drain")
        is_cancel = category.lifecycle == CANCEL_REMOVES
        N = self.depth_override or category.accumulate_depth
        amount_drops = self.amount_xrp * 1_000_000
        acct = worker.address
        after_s = (CANCEL_ACCUM_BASE_S + int(CANCEL_ACCUM_PER_N * N)
                   if is_cancel else ACCUM_CANCEL_AFTER_S)

        # ---- ACCUMULATE: create N live escrows, no draining ----
        self._mark(thread_idx, acct, category.name, "accumulate_start", N, 0)
        t0 = time.monotonic()
        cancel_after = get_close_time(ctx.rpc_url) + after_s
        offer_seqs: list[int] = []
        last_hash = None
        wall = None
        olf = get_open_ledger_fee(ctx.rpc_url)
        for i in range(N):
            if ctx.stop_event.is_set():
                break
            if i % BATCH == 0:
                olf = get_open_ledger_fee(ctx.rpc_url)
                if last_hash:                       # let the last batch land
                    wait_for_validated(ctx.rpc_url, last_hash, timeout_s=120.0)
            sub = worker.create(category, amount_drops, olf,
                                cancel_after=cancel_after)
            self._log_main(ctx, thread_idx, acct, category.name, "acc_create",
                           sub.engine_result, sub.tx_hash)
            if self._detail:
                self._detail.row(now_iso(), thread_idx, acct, category.name,
                                 "accumulate", i, len(offer_seqs) + 1,
                                 sub.tx_hash or "", sub.engine_result, "")
            if sub.engine_result == "tesSUCCESS":
                offer_seqs.append(sub.offer_sequence)
                last_hash = sub.tx_hash
            else:
                # A create failed mid-ramp — almost always the reserve wall.
                wall = sub.engine_result
                self.flag_unexpected(thread_idx, acct, category, "acc_create",
                                     "tesSUCCESS", sub.engine_result,
                                     sub.tx_hash)
                break
        if last_hash:
            wait_for_validated(ctx.rpc_url, last_hash, timeout_s=120.0)
        depth = len(offer_seqs)
        self._mark(thread_idx, acct, category.name, "accumulate_done", N, depth,
                   extra=f"reached={depth} wall={wall or '-'} "
                         f"secs={time.monotonic() - t0:.1f}")

        # ---- DRAIN, part 1: Finish each (this is where the wasm runs at depth).
        # For finish_removes the Finish removes the escrow; for cancel_removes it
        # is expected to reject/oog and the escrow stays for the Cancel pass.
        expected = category.expected_finish_result
        self._mark(thread_idx, acct, category.name, "drain_start", N, depth)
        td = time.monotonic()
        removed = self._drain(worker, category, ctx, thread_idx, offer_seqs,
                              depth, "acc_finish", "drain",
                              lambda seq, f: worker.finish(category, seq, f),
                              expected)

        if not is_cancel:
            self._mark(thread_idx, acct, category.name, "drain_done", N, 0,
                       extra=f"drained={removed} secs={time.monotonic() - td:.1f}")
            return

        # ---- DRAIN, part 2 (cancel_removes only): wait out CancelAfter, Cancel.
        worker.wait_for_expiry(cancel_after)
        if ctx.stop_event.is_set():
            return
        cancelled = self._drain(worker, category, ctx, thread_idx, offer_seqs,
                                depth, "acc_cancel", "drain_cancel",
                                lambda seq, f: worker.cancel(seq, f),
                                "tesSUCCESS")
        self._mark(thread_idx, acct, category.name, "drain_done", N, 0,
                   extra=f"drained={cancelled} secs={time.monotonic() - td:.1f}")

    def _drain(self, worker, category, ctx, thread_idx, offer_seqs, depth,
               action, phase, submit_fn, expected) -> int:
        """Submit submit_fn for each accumulated escrow in a batched pass,
        logging each with the concurrent live count. Returns the count that
        matched `expected`. Used for both the Finish pass and the Cancel pass."""
        acct = worker.address
        last_hash = None
        ok = 0
        olf = get_open_ledger_fee(ctx.rpc_url)
        for j, offer_seq in enumerate(offer_seqs):
            if ctx.stop_event.is_set():
                break
            if j % BATCH == 0:
                olf = get_open_ledger_fee(ctx.rpc_url)
                if last_hash:
                    wait_for_validated(ctx.rpc_url, last_hash, timeout_s=120.0)
            live_now = depth - j            # this escrow + others still live
            sub = submit_fn(offer_seq, olf)
            self._log_main(ctx, thread_idx, acct, category.name, action,
                           sub.engine_result, sub.tx_hash)
            if self._detail:
                self._detail.row(now_iso(), thread_idx, acct, category.name,
                                 phase, j, live_now, sub.tx_hash or "",
                                 sub.engine_result, "")
            if sub.engine_result == expected:
                ok += 1
            elif not is_applied(sub.engine_result):
                self.flag_unexpected(thread_idx, acct, category, action,
                                     expected, sub.engine_result, sub.tx_hash)
            if is_applied(sub.engine_result):
                last_hash = sub.tx_hash
        if last_hash:
            wait_for_validated(ctx.rpc_url, last_hash, timeout_s=120.0)
        return ok

    # ----- helpers -----

    def _log_main(self, ctx, thread_idx, acct, cat_name, action,
                  engine_result, tx_hash):
        ctx.csv_log.write([now_iso(), thread_idx, acct, cat_name, action,
                           tx_hash or "", engine_result, ""])

    def _mark(self, thread_idx, acct, cat_name, phase, N, live, extra=""):
        print(f"[accumulate] ts={now_iso()} thread={thread_idx} "
              f"account={acct} category={cat_name} phase={phase} N={N} "
              f"live={live} {extra}", file=sys.stderr)
