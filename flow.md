# flow.md — what the soak driver does today

Companion to `examples/NOTES.md`, which holds the facts, decisions and
history. This file describes the **current implementation only**. Code:
`escrow_lib.py` (Worker, categories, Pattern base), `pattern_serial.py`,
`pattern_pipeline.py`, `run_soak.py`, `ledger_ticker.py`. Wasm sources are
in `wats/` (see `wats/README.md`). Run artifacts (soak CSV, log, sampler
CSV) go to `runs/<UTC stamp>/`; accounts are read from
`runs/test_accounts.json` (see `runs/README.md`).

## 1. Lifecycles

Every category declares a `lifecycle`, an `expected_create_result`
(submit-time `engine_result` of the EscrowCreate) and, if it finishes, an
`expected_finish_result` (validated `meta.TransactionResult` of the
EscrowFinish). All escrows are self-escrows (Account == Destination) with
`CancelAfter = validated close_time + --cancel-after-s` (default 30 s). A
step whose result differs from the declared one prints an `[unexpected]`
line on stderr; the CSV row is written either way, and the run continues.

### finish_removes (`return_1`)

| # | tx | expected engine_result / validated result |
|---|---|---|
| 1 | EscrowCreate {Bytecode, CancelAfter} | tesSUCCESS / tesSUCCESS |
| 2 | EscrowFinish {OfferSequence, Gas} | tesSUCCESS / tesSUCCESS — wasm returned > 0, escrow removed |

Done. Any other validated result at step 2 leaves the escrow on the ledger
→ *stranded* (§2, backstop).

### cancel_removes (`return_0`)

| # | tx | expected engine_result / validated result |
|---|---|---|
| 1 | EscrowCreate | tesSUCCESS / tesSUCCESS |
| 2 | EscrowFinish {OfferSequence, Gas} | tecBYTECODE_REJECTED / tecBYTECODE_REJECTED — wasm returned 0, fee charged, escrow stays |
| 3 | wait until validated `close_time > CancelAfter` | — |
| 4 | EscrowCancel {OfferSequence} | tesSUCCESS / tesSUCCESS — escrow removed |

If step 2 unexpectedly validates tesSUCCESS the escrow is already gone and
the cycle ends there. If step 4 does not validate tesSUCCESS → stranded.

### preflight_reject (`unknown_imports`, `disabled_instructions`, `unfunded_account`)

| # | tx | expected engine_result / validated result |
|---|---|---|
| 1 | EscrowCreate | the category's declared reject code / none — not applied |

Done: no escrow, no Finish, no Cancel. If xrpld returns tesSUCCESS instead,
an escrow exists → stranded → backstop Cancel. Confirmed live:
`unknown_imports` and `disabled_instructions` → temINVALID_BYTECODE;
`unfunded_account` → terNO_ACCOUNT.

**`unfunded_account` (A3) quirk — the one non-pool sender.** Its EscrowCreate
is signed by a fresh, unfunded wallet generated per cycle, with the pool
account as Destination, Sequence 1, and no sequence cache touched. Server-side
signing (`submit`+secret) refuses a nonexistent source with srcActNotFound
before the engine runs, so this path signs **client-side** and submits a
pre-signed tx_blob; the blob skips the account lookup and the engine returns
terNO_ACCOUNT. Client-side signing needs the Bytecode / Gas fields, which
xrpl-py 4.5.0 lacks, so `escrow_lib._inject_smart_escrow_fields` adds them to
the binary codec (codes from the branch's server_definitions: Bytecode nth 47
Blob, Gas nth 84 UInt32). Rationale: audit finding 3.4 — the TxQ runs wasm
preflight, allocating arena entries, before checking that the account exists.

### multi_finish (finish_removes variant: `update_data_then_success`)

A finish_removes category may set `multi_finish=True`: the driver submits
EscrowFinish repeatedly while the wasm rejects (tecBYTECODE_REJECTED), up to
MAX_FINISH_ATTEMPTS (4), stopping at the first tesSUCCESS. For a stateful wasm
whose first finish writes data and rejects and whose next finish reads that
data and succeeds. `expected_finish_result` is the *terminal* result
(tesSUCCESS); intermediate rejects are expected by construction and not
flagged. Exhausting the cap without success strands the escrow (backstop
Cancel). Both patterns implement it: serial loops in-cycle
(`_finish_until_removed`); pipeline re-Finishes across rounds, tracking
`finish_attempts` on the in-flight entry.

### accumulate_then_drain (opt-in via a case's accumulate_depth)

A meta-lifecycle for probing what scales with the number of *concurrent live
smart escrows*, which the steady-state patterns (bounded near one per worker)
can't reach. Not a new tx sequence: it wraps a case's base lifecycle around a
burst. Per owner:

  1. accumulate: create N live smart escrows, none drained (N = the case's
     `accumulate_depth`, override `--accumulate-depth`; realistic hundreds to
     low thousands).
  2. drain-Finish: Finish each — this is where the wasm runs at depth, logged
     with the count still live. finish_removes cases are removed here;
     cancel_removes cases reject (as their base says) and stay.
  3. cancel_removes only: wait out CancelAfter, then Cancel each.

Run under `--pattern accumulate` (§4a). A case opts in by setting
`accumulate_depth`; preflight_reject cases can't (they never create an escrow).

## 2. Worker and the serial cycle

`Worker` = one funded account + a local sequence cache + Create / Finish /
Cancel primitives. Signing is server-side (`submit` with `secret` +
`tx_json`) because xrpl-py 4.5.0's codec lacks the Bytecode / Gas
fields (xrpld marks this deprecated but still serves it). The one exception
is the A3 unfunded_account category, which signs client-side (§1). Fees: Create =
10×base + 5×bytecode bytes; Finish = base + Gas×GasPrice/1e6 + 1; both
scaled by the open-ledger fee level. GasPrice, GasLimit and
BytecodeSizeLimit are read once at startup from the FeeSettings ledger
entry, and categories are checked against them before any tx is sent. No
`LastLedgerSequence` is set on soak txs.

Per cycle (`SerialPattern._run_one_cycle`, one thread per account):

1. **Backstop sweep.** Read validated `close_time`; for every parked escrow
   with `CancelAfter < close_time − 2`, submit one EscrowCancel
   (`action=cancel_backstop`). One attempt each; a failure is logged, not
   retried.
2. **Pick category**: round-robin `categories[(thread_idx + cycle) % n]`.
3. **Run the lifecycle** (§1). Each submit goes through
   `Pattern.submit_and_log`: submit; if the engine_result is tesSUCCESS or
   tec* (tx was applied) poll `tx` every 0.25 s until `validated:true`
   (120 s cap → `final_result=<TIMEOUT>`); write the CSV row.
4. **Pace**: with `--tps T`, sleep so each cycle takes ≥ threads/T seconds.

**Sequence cache** (`Worker._submit`). First use → `account_info`. After
tesSUCCESS or tec* → `seq + 1` (both consume the sequence). tem/tel/tef/ter
→ unchanged (not applied). On tefPAST_SEQ, terPRE_SEQ, terQUEUED or
tefMAX_LEDGER → log, refresh from `account_info`, retry the same tx once. A
second such code in a row raises; the thread prints `[FATAL]`, sets
`failure_event`, and `run_soak.py` exits 1. terQUEUED caveat: NOTES "Open
question".

**Stalls and retries**

| situation | behaviour |
|---|---|
| transport error or RPC `status:error` on submit / fee / ledger / account_info | RuntimeError → thread `[FATAL]` → run exits 1 |
| RPC error while polling `tx` | swallowed (txnNotFound is normal) until the 120 s cap → `<TIMEOUT>` row, cycle continues; the *next* submit fails loud |
| ledgers stop closing | every validation wait hits 120 s; `wait_for_expiry` gives up after 600 s, the Cancel returns tecNO_PERMISSION → parked |
| non-tesSUCCESS Create (non-preflight category) | row logged, cycle dropped, next cycle |
| Finish / Cancel leaves the escrow behind | parked → backstop at the next cycle start |
| SIGINT | `stop_event` set; each thread finishes its current step, up to one full cycle (~40 s for cancel_removes), then exits |

## 3. CancelAfter, close_time and ledger_accept

Standalone xrpld closes a ledger only when something calls `ledger_accept`
(`ledger_ticker.py`, default every 4 s, or `--manage-ledgers`).
`run_soak.py` refuses to start if `ledger_current` doesn't advance within
1.5 × interval. xrpld checks EscrowCancel against the **parent ledger's
close_time**, not the host clock, so the driver keys everything off
close_time: `CancelAfter = close_time(validated) + 30`, and
`wait_for_expiry` sleeps 30 s then polls `ledger` every 2 s until
`close_time > CancelAfter`. Wall-clock CancelAfter produced 24–50 %
tecNO_PERMISSION in early runs (see `Worker.create` docstring, NOTES).

Wall-clock per serial cycle at the defaults (4 s ledgers, 30 s CancelAfter):

| lifecycle | waits on a ledger close for | typical duration |
|---|---|---|
| finish_removes | Create, Finish (0–4 s each, ~2 s avg) | 3–8 s |
| cancel_removes | Create, Finish, then ≥30 s expiry + up to one more close + 2 s poll, then Cancel | 36–46 s |
| preflight_reject | nothing (rejected at submit) | <0.1 s, or threads/tps if paced |

Alternating return_1 / return_0 therefore averages ~22 s per cycle; dryrun4
(pre-Finish-attempt) did 410 creates on 10 threads in 900 s ≈ 41 cycles per
thread. In-flight escrows ≤ threads plus whatever is parked awaiting
backstop.

## 4. Pipeline pattern (brief)

`--pattern pipeline`: each thread drives M accounts in rounds of one ledger
each — validate last round's submits → submit Finish / Cancel for ready
escrows → top up Creates to K per account (category cursor advances per
Create; preflight_reject Creates take no slot) → backstop Cancel anything
stranded past CancelAfter → wait for the round's last *applied* tx to
validate (120 s cap, fatal). State machine and per-stage rules are in the
module docstring. In-flight bound: threads × M × K. A stranded entry gets
at most 3 backstop Cancels, then is dropped with a warning. Validated live
against a standalone node (all 15 categories, 5 threads × 6 accounts, K=2,
5065 rows, 0 unexpected) and against an in-process fake xrpld.

## 4a. Accumulate pattern (accumulate_then_drain)

`--pattern accumulate` (`pattern_accumulate.py`) realizes the burst lifecycle
from §1. Each thread runs one owner at a time through accumulate → drain →
repeat, round-robin over the accumulate-capable categories. Many threads at
once also drive the ledger-wide live count, so both the same-owner and
same-ledger dimensions are exercised.

The binding limit is **owner reserve**, not fee: each live escrow locks
ReserveIncrement (2 XRP) plus its amount until drained, so peak hold is about
N × (2 + amount) XRP. At 10,000 XRP funding and amount 1, one owner reaches
about N=3000. A create that fails mid-ramp (e.g. tecINSUFFICIENT_RESERVE) is
logged and the reached depth reported, then the burst drains what it has —
that wall is a result, not a crash.

Measurement and attribution:
- Every drain Finish is logged to `<run-dir>/accumulate_detail.csv` with the
  concurrent live count at that point; join tx_hash → WASM_TIMING to see
  whether per-Finish wasm cost grows with depth.
- Phase markers (`[accumulate] ... phase=accumulate_start|accumulate_done|
  drain_start|drain_done`) with timestamps and depth go to run_soak.log, so
  the memory sampler's RSS splits into ramp, plateau, and drain, distinct from
  baseline drift.

cancel_removes cases hold a long plateau: they reject at depth (the
measurement) and stay live until CancelAfter, which is sized to outlast
accumulate + drain-Finish for all N (a Finish after CancelAfter would fail
tecNO_PERMISSION). That plateau is where their live-count RSS is observed.

Validated: return_1 (finish_removes) accumulates and drains at N=20 and N=500
with no wall; its per-Finish time is flat (~50 µs) across live counts 0–500,
the baseline for heavier cases. return_0 (cancel_removes) rejects at depth then
Cancels after expiry.

## 5. Category registry and WASM_TIMING interpretation

Sixteen categories (escrow_lib.CATEGORIES), grouped by lifecycle:
preflight_reject (unknown_imports, disabled_instructions, unfunded_account),
cancel_removes (return_0, oog_execute, oog_compile, trap_div_by_zero),
finish_removes (return_1, update_data_then_success, trace_heavy,
oom_at_max_page, unknown_keylet, known_keylet, boundary_float, many_locals,
home_le_field_bytecode). Each declares its own `gas` allowance; the fee is
charged on the allowance, not on gas used. Every non-preflight case also sets
`accumulate_depth` (§1, §4a), declared centrally in escrow_lib so support and
default N are in one place.

The D-group (boundary_float, many_locals, home_le_field_bytecode) probes DoS
shapes where wall time is disproportionate to gas charged (audit findings
3.9-3.11). Read it off WASM_TIMING_FINISH: compare `time=` (microseconds)
against `gas=` (units charged). many_locals is the sharpest live example —
100 calls zeroing 30000 locals ran in ~900 µs for gas=1447. A category whose
`time=` per `gas=` stands out from the trivial return_1 baseline is the
signal.

All fifteen behave as intended. Two earlier issues were resolved: oog_compile
runs out of translation fuel (§6), and update_data_then_success converges once
its home_le_field field code was corrected (§6).

## 6. Known limitations / open questions

- **oog_compile (resolved).** Wasmi uses CompilationMode::LazyTranslation:
  a function is translated (and charged) only when first CALLED, and
  unreferenced functions are never translated. The first version left the
  noise functions uncalled, so they cost nothing (gas=30, Finish tesSUCCESS).
  Now escrow_finish calls all of them (each an early `return` then a large dead
  body), so their full bodies translate on first entry: Finish returns
  tecOUT_OF_GAS with gas=0 in WASM_TIMING_FINISH — it exhausts the allowance in
  translation before executing any body instruction. That gas=0 is how
  translation-OOG (B3) reads apart from execution-OOG (B2, gas≈allowance).
- **home_le_field takes the full SField code, not the bare nth.** invokeWithField
  (HostContext.cpp:195) looks the argument up in SField::getKnownCodeToField(),
  keyed by (type<<16)|nth. update_data_then_success passed 27 for sfData and
  never matched, so every Finish read "absent", wrote data, and rejected —
  looking like a non-convergence bug but really a wrong field code. Fixed to
  458779 = (VL 7<<16)|27; C2 now converges in two Finishes (reject-then-
  success). home_le_field_bytecode had the same flaw (47 vs (7<<16)|47 =
  458799); with it fixed the module actually reads its own 30 KB sfBytecode, so
  the audit-3.11 unaccounted-copy path is genuinely exercised (gas stays ~85
  per call regardless of field size — that flat gas IS the finding).

- **Crash or abort mid-cycle.** Parked and in-flight escrows exist only in
  thread memory. After a `[FATAL]` exit, SIGINT or a crash they stay on
  the ledger and no later run knows about them. `check_state.py` shows
  them; removal is a manual EscrowCancel after CancelAfter.
- **Fail-loud is delayed during validation waits** (up to 120 s, §2).
- **One driver per accounts file.** Sequence caches assume exclusive use;
  two drivers on the same accounts → tefPAST_SEQ storms → exit 1.
- **Unexpected results are not fatal.** A systematically misbehaving xrpld
  strands up to (cycles per CancelAfter window) escrows per worker before
  the backstop catches up.
- **`wait_for_expiry` ignores `stop_event`**, so shutdown can take a full
  expiry wait.
- **terQUEUED** is handled as a sequence error (NOTES "Open question").
- **`check_state.py` queries every account in `test_accounts.json`**, not
  just the active slice; slow with the 1M-account file.
- **tefMAX_LEDGER cannot occur** (no LastLedgerSequence is set); it is in
  the retry set for completeness only.
