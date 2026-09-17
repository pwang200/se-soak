"""PipelinePattern: round-driven, M accounts per thread, K in-flight per account.

Each thread owns a slice of accounts. The thread runs rounds; one round
corresponds roughly to one ledger close. Per round:

  STAGE A — validate last round's pending submits.
    For every entry in a PENDING_* state, fetch the tx, read
    meta.TransactionResult, write a CSV row, advance the state machine.

  STAGE B — submit the next step for escrows that exist.
    CREATED       → submit Finish (finish_removes, cancel_removes).
    AWAIT_CANCEL  → if close_time > CancelAfter, submit Cancel.

  STAGE C — top-up.
    While an account has fewer than K entries, rotate to the next
    category and submit an EscrowCreate. preflight_reject Creates are
    logged straight away and take no slot (nothing was created); the
    loop just moves on to the next category. Bounded per round so a
    category list that never creates anything can't spin.

  STAGE E — backstop sweep.
    Cancel any CREATED / STRANDED entry past CancelAfter + grace.

  STAGE D — wait one ledger close.
    Poll `tx` for the round's last *applied* submit until validated. If
    nothing was applied this round, sleep `ledger_interval_s`.

Entry state machine — one InFlight per escrow that exists or might:

  PENDING_CREATE  ─tes─▶ CREATED ─Finish─▶ PENDING_FINISH ─tes─▶ (removed)
        │other                                   │other
        ▼                                        ├─ cancel_removes ─▶ AWAIT_CANCEL ─Cancel─▶ PENDING_CANCEL
    (dropped)                                    └─ finish_removes ─▶ STRANDED     ─Cancel─▶ PENDING_CANCEL
                                                                                          │
  PENDING_CANCEL ─tes / tecNO_TARGET─▶ (removed)   PENDING_CANCEL ─other─▶ STRANDED  ◀────┘
  CREATED for a preflight_reject category (xrpld accepted it) ─▶ STRANDED

Create and its Finish are NEVER in the same ledger — Finish is submitted
in the round AFTER the Create's validation. The OfferSequence is the
local seq we used for Create, so no extra RPC is needed to learn it.

Configuration:
  in_flight_per_account (K): steady-state entries per account.
  ledger_interval_s:         fallback sleep when nothing applied this round.
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
from typing import Optional

from escrow_lib import (
    CANCEL_REMOVES,
    FINISH_REMOVES,
    MAX_FINISH_ATTEMPTS,
    PREFLIGHT_REJECT,
    Category,
    Pattern,
    PatternContext,
    SubmitResult,
    Worker,
    get_close_time,
    get_open_ledger_fee,
    is_applied,
    wait_for_validated,
)


# Stage-A per-tx validation cap. The round's last-hash wait at stage D
# means previous-round txns should already be validated, so this rarely
# polls more than once. Keep it tight to bound stage A.
STAGE_A_VALIDATE_TIMEOUT_S = 5.0

# Backstop grace past CancelAfter before we force a Cancel.
CANCEL_BACKSTOP_GRACE_S = 2

# Give up on a stranded entry after this many backstop Cancels that did
# not validate as tesSUCCESS/tecNO_TARGET. Bounds retry loops; the rows
# are all in the CSV.
MAX_BACKSTOP_ATTEMPTS = 3


# -- entry state machine ----------------------------------------------------

PENDING_CREATE = "pending_create"
CREATED = "created"
PENDING_FINISH = "pending_finish"
AWAIT_CANCEL = "await_cancel"
STRANDED = "stranded"
PENDING_CANCEL = "pending_cancel"

PENDING_STATES = frozenset({PENDING_CREATE, PENDING_FINISH, PENDING_CANCEL})

# Validated Cancel results meaning "the escrow is not on the ledger".
ESCROW_GONE = frozenset({"tesSUCCESS", "tecNO_TARGET"})


@dataclass
class InFlight:
    offer_sequence: int
    category: Category
    cancel_after_ripple: int
    status: str = PENDING_CREATE
    last_sub: Optional[SubmitResult] = None
    last_action: str = "create"   # create | finish | cancel | cancel_backstop
    create_round: int = 0         # round in which Create was submitted
    backstop_attempts: int = 0
    finish_attempts: int = 0      # EscrowFinish submits (for multi_finish cap)


# -- pattern ----------------------------------------------------------------

class PipelinePattern(Pattern):
    name = "pipeline"

    def __init__(self, *,
                 in_flight_per_account: int = 1,
                 ledger_interval_s: float = 4.0):
        if in_flight_per_account < 1:
            raise ValueError("in_flight_per_account must be >= 1")
        self.K = in_flight_per_account
        self.ledger_interval_s = ledger_interval_s

    # ----- thread entry -----

    def run_thread(self, thread_idx: int, accounts: list[Worker],
                   ctx: PatternContext) -> None:
        # Per-account queue of entries; appended at Create, removed when
        # the escrow is confirmed gone.
        queues: dict[str, deque[InFlight]] = {
            w.address: deque() for w in accounts
        }
        # Per-account category cursor, staggered so accounts in the same
        # thread work on different categories at the same time.
        cat_cursor: dict[str, int] = {
            w.address: i for i, w in enumerate(accounts)
        }
        round_idx = 0

        try:
            while not ctx.stop_event.is_set():
                self._stage_a_validate(thread_idx, accounts, queues, ctx)

                last_hash = self._stage_b_next_step(
                    thread_idx, accounts, queues, ctx,
                )
                h = self._stage_c_topup(
                    thread_idx, accounts, queues, ctx, round_idx, cat_cursor,
                )
                last_hash = h or last_hash

                # Stage E before D so straggler cancels also ride the
                # round-end ledger close wait.
                h = self._stage_e_backstop(thread_idx, accounts, queues, ctx)
                last_hash = h or last_hash

                # STAGE D — wait for the round to close.
                if last_hash:
                    try:
                        wait_for_validated(ctx.rpc_url, last_hash,
                                           timeout_s=120.0)
                    except TimeoutError as e:
                        print(f"[FATAL] pipeline thread {thread_idx}: "
                              f"round {round_idx} last-hash {last_hash} "
                              f"not validated ({e})", file=sys.stderr)
                        ctx.failure_event.set()
                        ctx.stop_event.set()
                        return
                else:
                    # Nothing applied this round; idle through one ledger.
                    ctx.stop_event.wait(self.ledger_interval_s)

                round_idx += 1

        except RuntimeError as e:
            print(f"[FATAL] pipeline thread {thread_idx}: {e}", file=sys.stderr)
            ctx.failure_event.set()
            ctx.stop_event.set()
        except Exception as e:  # noqa: BLE001
            print(f"[FATAL] pipeline thread {thread_idx} unexpected: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            ctx.failure_event.set()
            ctx.stop_event.set()

    # ----- stage A: validate previous round's pending submits -----

    def _stage_a_validate(self, thread_idx: int, accounts: list[Worker],
                          queues: dict[str, deque[InFlight]],
                          ctx: PatternContext) -> None:
        for worker in accounts:
            q = queues[worker.address]
            for entry in list(q):
                if entry.status not in PENDING_STATES:
                    continue
                sub = entry.last_sub
                if sub is None or not sub.tx_hash:
                    final = "<NO_HASH>"          # shouldn't happen; guard
                    sub = sub or SubmitResult("<NO_SUB>", None, {})
                else:
                    final = self._fetch_final_result(ctx, sub.tx_hash)

                self.log_row(ctx, thread_idx, worker.address,
                             entry.category.name, entry.last_action,
                             sub, final)
                cat = entry.category

                if entry.status == PENDING_CREATE:
                    if final == "tesSUCCESS":
                        # preflight_reject Create that got in (flagged at
                        # submit): an escrow exists, backstop it.
                        entry.status = (STRANDED
                                        if cat.lifecycle == PREFLIGHT_REJECT
                                        else CREATED)
                    elif final == "<TIMEOUT>":
                        # Unknown whether it landed. Assume it did; a
                        # backstop Cancel on a non-existent escrow comes
                        # back tecNO_TARGET and drops the entry.
                        entry.status = STRANDED
                    else:
                        q.remove(entry)  # not applied; nothing created

                elif entry.status == PENDING_FINISH:
                    if final == "tesSUCCESS":
                        if final != cat.expected_finish_result:
                            self.flag_unexpected(thread_idx, worker.address,
                                                 cat, "finish",
                                                 cat.expected_finish_result,
                                                 final, sub.tx_hash)
                        q.remove(entry)  # Finish removed the escrow
                    elif cat.multi_finish and final == "tecBYTECODE_REJECTED" \
                            and entry.finish_attempts < MAX_FINISH_ATTEMPTS:
                        # Expected intermediate reject (wasm wrote data);
                        # re-Finish next round. Not flagged.
                        entry.status = CREATED
                    elif cat.lifecycle == CANCEL_REMOVES:
                        if final != cat.expected_finish_result:
                            self.flag_unexpected(thread_idx, worker.address,
                                                 cat, "finish",
                                                 cat.expected_finish_result,
                                                 final, sub.tx_hash)
                        entry.status = AWAIT_CANCEL  # expected path
                    else:
                        # finish_removes that didn't remove the escrow, or a
                        # multi_finish that exhausted its attempts.
                        self.flag_unexpected(thread_idx, worker.address, cat,
                                             "finish",
                                             cat.expected_finish_result,
                                             final, sub.tx_hash)
                        entry.status = STRANDED

                elif entry.status == PENDING_CANCEL:
                    if final in ESCROW_GONE:
                        if final != "tesSUCCESS":
                            self.flag_unexpected(thread_idx, worker.address,
                                                 cat, entry.last_action,
                                                 "tesSUCCESS", final,
                                                 sub.tx_hash)
                        q.remove(entry)
                    else:
                        self.flag_unexpected(thread_idx, worker.address, cat,
                                             entry.last_action, "tesSUCCESS",
                                             final, sub.tx_hash)
                        entry.status = STRANDED

    def _fetch_final_result(self, ctx: PatternContext, tx_hash: str) -> str:
        try:
            tx_res = wait_for_validated(ctx.rpc_url, tx_hash,
                                        timeout_s=STAGE_A_VALIDATE_TIMEOUT_S)
            return (tx_res.get("meta") or {}).get(
                "TransactionResult", "<missing>"
            )
        except TimeoutError:
            return "<TIMEOUT>"
        except RuntimeError as e:
            # tx lookup transport failure — bubble up as fatal.
            raise RuntimeError(f"stage A tx lookup failed for {tx_hash}: {e}")

    # ----- stage B: next step for escrows that exist -----

    def _stage_b_next_step(self, thread_idx: int, accounts: list[Worker],
                           queues: dict[str, deque[InFlight]],
                           ctx: PatternContext) -> Optional[str]:
        last_hash: Optional[str] = None
        # Sample close_time once per round; cancel-after readiness is
        # checked against this, not wall-clock.
        now_ct = get_close_time(ctx.rpc_url)

        for worker in accounts:
            for entry in queues[worker.address]:
                cat = entry.category
                if entry.status == CREATED:
                    if not cat.runs_finish:
                        entry.status = STRANDED   # defensive; A routes here
                        continue
                    olf = get_open_ledger_fee(ctx.rpc_url)
                    entry.finish_attempts += 1
                    sub = worker.finish(cat, entry.offer_sequence, olf)
                    self._post_submit(entry, sub, "finish", PENDING_FINISH,
                                      ctx, thread_idx, worker.address)
                elif entry.status == AWAIT_CANCEL \
                        and now_ct > entry.cancel_after_ripple:
                    olf = get_open_ledger_fee(ctx.rpc_url)
                    sub = worker.cancel(entry.offer_sequence, olf)
                    self._post_submit(entry, sub, "cancel", PENDING_CANCEL,
                                      ctx, thread_idx, worker.address)
                else:
                    continue
                if sub.tx_hash and is_applied(sub.engine_result):
                    last_hash = sub.tx_hash
        return last_hash

    # ----- stage C: top up with new creates -----

    def _stage_c_topup(self, thread_idx: int, accounts: list[Worker],
                       queues: dict[str, deque[InFlight]],
                       ctx: PatternContext, round_idx: int,
                       cat_cursor: dict[str, int]) -> Optional[str]:
        last_hash: Optional[str] = None
        n_cats = len(ctx.categories)

        for worker in accounts:
            q = queues[worker.address]
            # Every queued entry is (or may be) an escrow on the ledger,
            # so len(q) is the in-flight count. Bound the loop: K real
            # creates plus one pass through categories that create nothing.
            attempts = 0
            while len(q) < self.K and attempts < self.K + n_cats:
                attempts += 1
                category = ctx.categories[cat_cursor[worker.address] % n_cats]
                cat_cursor[worker.address] += 1

                olf = get_open_ledger_fee(ctx.rpc_url)
                create_sub = worker.create(category, ctx.amount_drops, olf)
                if create_sub.engine_result != category.expected_create_result:
                    self.flag_unexpected(thread_idx, worker.address, category,
                                         "create",
                                         category.expected_create_result,
                                         create_sub.engine_result,
                                         create_sub.tx_hash)

                if create_sub.engine_result != "tesSUCCESS":
                    # Nothing escrowed; log now with engine_result as final.
                    self.log_row(ctx, thread_idx, worker.address,
                                 category.name, "create", create_sub,
                                 create_sub.engine_result)
                    if create_sub.tx_hash and is_applied(create_sub.engine_result):
                        last_hash = create_sub.tx_hash   # tec: fee charged
                    if category.lifecycle == PREFLIGHT_REJECT:
                        continue   # expected; move on to the next category
                    break          # unexpected; don't hammer this account

                q.append(InFlight(
                    offer_sequence=create_sub.offer_sequence,
                    category=category,
                    cancel_after_ripple=create_sub.cancel_after_ripple,
                    status=PENDING_CREATE,
                    last_sub=create_sub,
                    last_action="create",
                    create_round=round_idx,
                ))
                if create_sub.tx_hash:
                    last_hash = create_sub.tx_hash

        return last_hash

    def _post_submit(self, entry: InFlight, sub: SubmitResult,
                     action: str, next_status: str, ctx: PatternContext,
                     thread_idx: int, account_addr: str) -> None:
        """Common bookkeeping after a Finish/Cancel submit."""
        entry.last_sub = sub
        entry.last_action = action
        if is_applied(sub.engine_result):
            # tes or tec: it's in the open ledger; stage A validates and
            # logs it next round.
            entry.status = next_status
        else:
            # Not applied (tem/tef/tel/ter). Log now; leave the status
            # alone so we retry next round, or stage E backstops it once
            # CancelAfter has passed.
            self.log_row(ctx, thread_idx, account_addr,
                         entry.category.name, action, sub,
                         sub.engine_result)
            self.flag_unexpected(thread_idx, account_addr, entry.category,
                                 action, "<applied>", sub.engine_result,
                                 sub.tx_hash)

    # ----- stage E: backstop sweep -----

    def _stage_e_backstop(self, thread_idx: int, accounts: list[Worker],
                          queues: dict[str, deque[InFlight]],
                          ctx: PatternContext) -> Optional[str]:
        """Cancel any CREATED / STRANDED entry whose CancelAfter has passed.

        CREATED here means the Finish never got applied for a whole
        CancelAfter window; STRANDED means a step left the escrow behind.
        Forcing a Cancel reclaims the slot and the XRP — without this,
        stragglers would accumulate.
        """
        # close_time-based cutoff; see Worker.create's docstring for why
        # wall-clock comparisons here cause tecNO_PERMISSION storms.
        cutoff = get_close_time(ctx.rpc_url) - CANCEL_BACKSTOP_GRACE_S
        last_hash: Optional[str] = None
        for worker in accounts:
            q = queues[worker.address]
            for entry in list(q):
                if entry.status not in (CREATED, STRANDED):
                    continue
                if entry.cancel_after_ripple >= cutoff:
                    continue
                if entry.backstop_attempts >= MAX_BACKSTOP_ATTEMPTS:
                    print(f"[warn] thread {thread_idx} account "
                          f"{worker.address} category {entry.category.name} "
                          f"offer_seq={entry.offer_sequence}: giving up after "
                          f"{entry.backstop_attempts} backstop cancels",
                          file=sys.stderr)
                    q.remove(entry)
                    continue
                entry.backstop_attempts += 1
                olf = get_open_ledger_fee(ctx.rpc_url)
                sub = worker.cancel(entry.offer_sequence, olf)
                self._post_submit(entry, sub, "cancel_backstop",
                                  PENDING_CANCEL, ctx, thread_idx,
                                  worker.address)
                if sub.tx_hash and is_applied(sub.engine_result):
                    last_hash = sub.tx_hash
        return last_hash
