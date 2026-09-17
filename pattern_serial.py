"""SerialPattern: one cycle at a time per account, with a CancelAfter backstop.

A thread drives one (or more, round-robin) accounts. Each cycle is one
category run to ledger-neutral, blocking on every tx:

  finish_removes    Create → Finish (tesSUCCESS) → done
  cancel_removes    Create → Finish (tecBYTECODE_REJECTED, escrow stays)
                    → wait CancelAfter → Cancel → done
  preflight_reject  Create (rejected, tem*) → done. Nothing to clean up.

Backstop: anything that leaves an escrow on the ledger against expectation
— a Finish that didn't remove it, a Cancel that failed, or a
preflight_reject Create that xrpld accepted — is parked on a per-worker
queue. The *next* cycle's start sweeps that queue and Cancels anything
past CancelAfter + grace. This is the only thing that keeps a stranded
escrow from accumulating across a week-long soak.

Multi-account-per-thread is allowed but pointless here — the thread blocks
on each tx, so accounts share that latency serially. For real concurrency
use PipelinePattern.
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from escrow_lib import (
    CANCEL_REMOVES,
    FINISH_REMOVES,
    MAX_FINISH_ATTEMPTS,
    PREFLIGHT_REJECT,
    Category,
    Pattern,
    PatternContext,
    Worker,
    get_close_time,
    get_open_ledger_fee,
)


# Seconds of slack past CancelAfter before we attempt the backstop Cancel.
# The ledger's close_time has to advance past CancelAfter for the cancel
# to be accepted; this gives the ledger ticker a margin.
CANCEL_BACKSTOP_GRACE_S = 2


@dataclass
class PendingCancel:
    offer_sequence: int
    category: Category
    cancel_after_ripple: int


class SerialPattern(Pattern):
    name = "serial"

    def __init__(self, *, cycle_period_s: float | None = None):
        self.cycle_period_s = cycle_period_s

    def run_thread(self, thread_idx: int, accounts: list[Worker],
                   ctx: PatternContext) -> None:
        pending: dict[str, deque[PendingCancel]] = defaultdict(deque)
        cycle = 0
        try:
            while not ctx.stop_event.is_set():
                for worker in accounts:
                    if ctx.stop_event.is_set():
                        break
                    cycle_start = time.monotonic()

                    # 1. Backstop sweep: clean up anything stranded.
                    self._sweep_expired(worker, pending[worker.address],
                                        ctx, thread_idx)

                    # 2. Pick the next category (round-robin, staggered).
                    category = ctx.categories[
                        (thread_idx + cycle) % len(ctx.categories)
                    ]

                    # 3. One cycle.
                    self._run_one_cycle(worker, category, ctx, thread_idx,
                                        pending[worker.address])

                    cycle += 1

                    # 4. Pace.
                    if self.cycle_period_s:
                        elapsed = time.monotonic() - cycle_start
                        if elapsed < self.cycle_period_s:
                            ctx.stop_event.wait(self.cycle_period_s - elapsed)
        except RuntimeError as e:
            print(f"[FATAL] serial thread {thread_idx}: {e}", file=sys.stderr)
            ctx.failure_event.set()
            ctx.stop_event.set()
        except Exception as e:  # noqa: BLE001 — log + fail loud
            print(f"[FATAL] serial thread {thread_idx} unexpected: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            ctx.failure_event.set()
            ctx.stop_event.set()

    def _sweep_expired(self, worker: Worker, q: deque[PendingCancel],
                       ctx: PatternContext, thread_idx: int) -> None:
        """Cancel any pending entries past CancelAfter + grace.

        Comparison uses ledger close_time, not wall-clock — CancelAfter
        is in close_time space (see Worker.create). Entries are appended
        in chronological order, so we can stop scanning as soon as we
        hit one that hasn't matured yet. One attempt per entry: if the
        backstop Cancel itself fails, the row is in the CSV and we move
        on rather than retry forever.
        """
        cutoff = get_close_time(ctx.rpc_url) - CANCEL_BACKSTOP_GRACE_S
        while q and q[0].cancel_after_ripple < cutoff:
            entry = q.popleft()
            olf = get_open_ledger_fee(ctx.rpc_url)
            _sub, final = self.submit_and_log(
                ctx, thread_idx, worker.address, entry.category.name,
                "cancel_backstop",
                lambda e=entry, f=olf: worker.cancel(e.offer_sequence, f),
            )
            if final != "tesSUCCESS":
                self.flag_unexpected(thread_idx, worker.address,
                                     entry.category, "cancel_backstop",
                                     "tesSUCCESS", final or _sub.engine_result,
                                     _sub.tx_hash)

    def _run_one_cycle(self, worker: Worker, category: Category,
                       ctx: PatternContext, thread_idx: int,
                       pending_q: deque[PendingCancel]) -> None:
        # ---- Create ----
        olf = get_open_ledger_fee(ctx.rpc_url)
        create_sub, _create_final = self.submit_and_log(
            ctx, thread_idx, worker.address, category.name, "create",
            lambda: worker.create(category, ctx.amount_drops, olf),
        )
        if create_sub.engine_result != category.expected_create_result:
            self.flag_unexpected(thread_idx, worker.address, category,
                                 "create", category.expected_create_result,
                                 create_sub.engine_result, create_sub.tx_hash)
        if create_sub.engine_result != "tesSUCCESS":
            # Nothing was escrowed. Expected for preflight_reject; a
            # (flagged) failure for the others. Either way, cycle over.
            return

        if category.lifecycle == PREFLIGHT_REJECT:
            # xrpld accepted a Create it should have rejected. An escrow
            # now exists; park it so the backstop Cancels it.
            pending_q.append(PendingCancel(
                offer_sequence=create_sub.offer_sequence,
                category=category,
                cancel_after_ripple=create_sub.cancel_after_ripple,
            ))
            return

        # ---- finish_removes: Finish (possibly repeated) removes the escrow ----
        if category.lifecycle == FINISH_REMOVES:
            if not self._finish_until_removed(worker, category, create_sub,
                                              ctx, thread_idx):
                # Not removed after the allowed attempts — stranded.
                pending_q.append(PendingCancel(
                    offer_sequence=create_sub.offer_sequence,
                    category=category,
                    cancel_after_ripple=create_sub.cancel_after_ripple,
                ))
            return

        # ---- cancel_removes: one Finish (expected to fail), wait, Cancel ----
        assert category.lifecycle == CANCEL_REMOVES, category.lifecycle
        olf2 = get_open_ledger_fee(ctx.rpc_url)
        finish_sub, finish_final = self.submit_and_log(
            ctx, thread_idx, worker.address, category.name, "finish",
            lambda: worker.finish(category, create_sub.offer_sequence, olf2),
        )
        if finish_final != category.expected_finish_result:
            self.flag_unexpected(thread_idx, worker.address, category,
                                 "finish", category.expected_finish_result,
                                 finish_final or finish_sub.engine_result,
                                 finish_sub.tx_hash)
        if finish_final == "tesSUCCESS":
            # wasm unexpectedly succeeded; escrow already gone, no Cancel.
            return

        worker.wait_for_expiry(create_sub.cancel_after_ripple)
        if ctx.stop_event.is_set():
            return
        olf3 = get_open_ledger_fee(ctx.rpc_url)
        cancel_sub, cancel_final = self.submit_and_log(
            ctx, thread_idx, worker.address, category.name, "cancel",
            lambda: worker.cancel(create_sub.offer_sequence, olf3),
        )
        if cancel_final != "tesSUCCESS":
            self.flag_unexpected(thread_idx, worker.address, category,
                                 "cancel", "tesSUCCESS",
                                 cancel_final or cancel_sub.engine_result,
                                 cancel_sub.tx_hash)
            pending_q.append(PendingCancel(
                offer_sequence=create_sub.offer_sequence,
                category=category,
                cancel_after_ripple=create_sub.cancel_after_ripple,
            ))

    def _finish_until_removed(self, worker: Worker, category: Category,
                              create_sub, ctx: PatternContext,
                              thread_idx: int) -> bool:
        """Submit EscrowFinish for a finish_removes category; return True if
        one validated tesSUCCESS (escrow removed).

        A normal category runs exactly one Finish. A multi_finish category
        (C2) retries while the wasm rejects — its first Finish writes data and
        returns tecBYTECODE_REJECTED, its next returns success — capped at
        MAX_FINISH_ATTEMPTS. Intermediate tecBYTECODE_REJECTED is expected by
        construction and not flagged; any other non-success result, or failing
        to succeed within the cap, is flagged and leaves the escrow stranded.
        """
        attempts = MAX_FINISH_ATTEMPTS if category.multi_finish else 1
        for i in range(attempts):
            olf = get_open_ledger_fee(ctx.rpc_url)
            sub, final = self.submit_and_log(
                ctx, thread_idx, worker.address, category.name, "finish",
                lambda o=olf: worker.finish(category, create_sub.offer_sequence, o),
            )
            if final == "tesSUCCESS":
                return True
            if category.multi_finish and final == "tecBYTECODE_REJECTED" \
                    and i < attempts - 1:
                continue        # expected intermediate; retry
            self.flag_unexpected(thread_idx, worker.address, category,
                                 "finish", category.expected_finish_result,
                                 final or sub.engine_result, sub.tx_hash)
            return False
        return False
