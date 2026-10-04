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

### retry_finish_until_success (`update_data_then_success`)

| # | tx | expected engine_result / validated result |
|---|---|---|
| 1 | EscrowCreate | tesSUCCESS / tesSUCCESS |
| 2 | EscrowFinish {OfferSequence, Gas} | tecBYTECODE_REJECTED — wasm wrote its Data and returned 0, escrow stays |
| 3 | EscrowFinish {OfferSequence, Gas} | tesSUCCESS — wasm read its Data and returned 1, escrow removed |

A lifecycle for a stateful wasm whose Finish rejects until state it writes
accumulates, then succeeds. The driver re-submits EscrowFinish while the wasm
returns tecBYTECODE_REJECTED, up to MAX_FINISH_ATTEMPTS (4), stopping at the
first tesSUCCESS. `expected_finish_result` is the *terminal* result
(tesSUCCESS); the intermediate rejects are expected by construction and not
flagged. Two fall-throughs keep the ledger clean:
  - a *true* Finish failure (out-of-gas / trap — neither success nor the
    intermediate reject) drops to the cancel_removes cleanup: wait CancelAfter,
    then Cancel;
  - exhausting MAX_FINISH_ATTEMPTS without a success strands the escrow for the
    backstop Cancel (§2).

The one live case is update_data_then_success (C2): Finish #1 reads its own
sfData (full SField code (7<<16)|27 = 458779), finds it absent, writes one byte
with set_data, and returns 0 (reject, escrow stays); Finish #2 sees the Data
present and returns 1 (escrow removed). Confirmed live: reject then success in
two Finishes at gas=5000. Both patterns implement it: serial loops in-cycle
(`_drive_finishes`); pipeline re-Finishes across rounds, tracking
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

Twenty-eight categories (escrow_lib.CATEGORIES). Each declares a base `lifecycle`, a
`lifecycles` set (the base plus `accumulate_then_drain` if it opts into the
burst pattern), a `purpose` set (informational: baseline / correctness / dos /
leak), and its own `gas` allowance; the fee is charged on the allowance, not on
gas used. Which cases accumulate and their default N (`accumulate_depth`) are
declared centrally in escrow_lib so support and defaults are read/tuned in one
place.

| category | base lifecycle | accumulate | purpose | gas |
|---|---|---|---|---|
| unknown_imports | preflight_reject | — | correctness | 1,000 |
| disabled_instructions | preflight_reject | — | correctness | 1,000 |
| unfunded_account | preflight_reject | — | correctness | 1,000 |
| return_0 | cancel_removes | yes | baseline, correctness | 1,000 |
| oog_execute | cancel_removes | yes | correctness | 1,000 |
| oog_compile | cancel_removes | — | correctness | 1,000 |
| trap_div_by_zero | cancel_removes | — | correctness | 1,000 |
| return_1 | finish_removes | yes | baseline | 1,000 |
| update_data_then_success | retry_finish_until_success | — | correctness | 5,000 |
| trace_heavy | finish_removes | — | correctness | 300,000 |
| oom_at_max_page | finish_removes | — | correctness | 1,000 |
| known_keylet | finish_removes | — | correctness, dos | 6,000 |
| cache_miss_single | finish_removes | — | correctness, dos | 8,000 |
| cache_hit_single | finish_removes | — | correctness, dos | 8,000 |
| cache_miss_storm | finish_removes | yes | dos, leak | 900,000 |
| cache_hit_storm | finish_removes | yes | dos, leak | 900,000 |
| cache_mixed_storm | finish_removes | yes | dos, leak | 900,000 |
| boundary_float | finish_removes | yes | dos | 50,000 |
| many_locals | finish_removes | yes | dos | 100,000 |
| home_le_field_bytecode | finish_removes | yes | dos | 20,000 |
| dos_large_finish_linear | finish_removes | yes | dos | 1,000,000 |
| dos_large_finish_looped | finish_removes | yes | dos | 1,000,000 |
| dos_large_finish_many_helpers | finish_removes | yes | dos | 1,000,000 |
| inst_data | finish_removes | yes | dos | 10,000 |
| inst_elem | finish_removes | yes | dos | 10,000 |
| inst_locals | finish_removes | yes | dos | 10,000 |
| chain_L1_resident | finish_removes | yes | dos | 1,000,000 |
| chain_full_footprint | finish_removes | yes | dos | 1,000,000 |

Purpose is informational only — it labels intent (baseline = a trivial control,
correctness = a specific result/behaviour under test, dos = a wall-time-vs-gas
probe, leak = a concurrent-live-count probe) and does not change how a case
runs. A case can carry more than one tag.

**cache_le_pattern family** (one template, wats/cache_le_pattern.wat; retires
the old unknown_keylet). Each Finish loops `iterations` times, computing an
AccountRoot keylet from a patched account id and calling cache_le (reusing one
slot, so every call does a fresh read of a distinct object). The patcher fills
a per-cycle hit/miss mix in random order — hits are distinct real pool accounts
(cache_le finds them), misses are random ids (not found). Presets:

| preset | iterations | hit_ratio | gas | accumulate |
|---|---|---|---|---|
| cache_miss_single | 1 | 0.0 | 8,000 | no |
| cache_hit_single | 1 | 1.0 | 8,000 | no |
| cache_miss_storm | 150 | 0.0 | 900,000 | 500 |
| cache_hit_storm | 150 | 1.0 | 900,000 | 500 |
| cache_mixed_storm | 150 | 0.5 | 900,000 | 500 |

iterations caps at ~180 per Finish (each ~5350 gas against the 1M GasLimit);
150 completes with margin. The point is DoS: whether per-cache_le wall time
scales with gas and whether hit or miss is disproportionate. That signal needs
disk reads, which need a source pool larger than cache: misses hit disk on any
ledger (random SHAMap paths), hits only when the funded pool is large and cold
(soak scale, not dev). Live first cycles (dev, 20 accounts, warm): all five
finish tesSUCCESS; singles ~5,874 gas ~300 µs, storms ~806,749 gas ~500 µs
(hit storm ~640 µs), so at dev scale wall time is small relative to gas — the
inversion at soak scale is the thing to watch.

The D-group (boundary_float, many_locals, home_le_field_bytecode) probes DoS
shapes where wall time is disproportionate to gas charged (audit findings
3.9-3.11). Read it off WASM_TIMING_FINISH: compare `time=` (microseconds)
against `gas=` (units charged). many_locals is the sharpest live example —
100 calls zeroing 30000 locals ran in ~900 µs for gas=1447. A category whose
`time=` per `gas=` stands out from the trivial return_1 baseline is the
signal.

**dos_large_finish family** (three templates: dos_large_finish_linear,
dos_large_finish_looped, dos_large_finish_many_helpers). A controlled trio that
forks the DoS question the D-group only opens: *which* Wasmi cost dimension is
underpriced. Under lazy translation a function body translates (and is billed)
on first call; unreferenced functions never translate; instantiation (data /
memory init) is charged zero fuel. The three isolate one dimension each by
holding the others roughly constant:

| case | shape | isolates | translation | execution |
|---|---|---|---|---|
| linear | one huge finish body, COUNT straight-line `(i32.const 1)(i32.add)` units, no loop | per-instruction (translate + execute), summed | all COUNT units, once | all COUNT units, once |
| looped | small body of BODY_UNITS units in a loop of ITERS (executed ≈ linear's COUNT) | per-executed-instruction, translation amortized | body once | body × ITERS |
| many_helpers | finish calls N helpers once each, summing; each helper = HELPER_UNITS of linear's unit so body work matches linear | per-function-entry setup (the gap vs linear) | N helper bodies, once each | N entries + N helper bodies |

The read (all vs the ~8.9 ns/gas anchor in NOTES): linear elevated but looped
not → translation-dominant per-instruction cost; looped elevated →
execution-dominant; many_helpers elevated over linear → per-function-entry cost;
all three elevated → the cost model is globally low; none elevated → honest at
this shape. Absolute numbers are secondary; the comparison across the three is
the finding.

Each is a template only to carry a 32-byte `opaque_random` pad in a `(data ...)`
section, rewritten every EscrowCreate so each cycle's Bytecode is unique
(defeats any module-store dedup keyed on identical bytes). Data-section init
costs zero fuel, so the pad does not touch the time/gas measurement. Gas is set
to the GasLimit max (1,000,000) for staging headroom — translation cost is not
yet measured — and should be recalibrated down to ~10× measured gas after the
first live run. Default sizes: linear COUNT=5000 (~15 KB), looped ITERS=100 ×
BODY_UNITS=50 (~0.5 KB), many_helpers N_HELPERS=250 × HELPER_UNITS=20 (~18 KB, so
its 5000 body units match linear's); all comfortably under the 100 KB
BytecodeSizeLimit. linear is size-capped by the byte limit; looped is capped only
by gas.

**many_helpers hits an xrpld defense (found live 2026-10-01).** The node rejects
a module whose average code-bytes-per-function is below 40 once total function
bytes exceed 1000 (`AvgBytesPerFunctionLimit{req_funcs_bytes:1000,
min_avg_bytes_per_function:40}` at crates/xrpl-wasm-vm/src/vm.rs) — a defense
against exactly the many-tiny-functions translation-cost attack. The first
version used 5000 one-instruction helpers (avg 8 bytes) and was rejected
temINVALID_BYTECODE. The rework uses 250 helpers of 20 units each (~62 B/body,
avg ~72 B/function), which clears the guard and keeps body work matched to linear.
Single-shot result: linear 115,030 gas / ~838 µs vs many_helpers 136,671 gas /
~1,662 µs, so the 250 entries add ~824 µs for ~21,641 gas — about 3.3 µs and
87 gas per entry, ~38 ns/gas on the delta, the sharpest underbilled dimension
found (well above the 8.9 anchor). So the guard bounds the attack, but within the
regime it allows, per-function-entry cost is still strongly underbilled.

**dos_expensive_instantiation family** (three templates: inst_data, inst_elem,
inst_locals). Where dos_large_finish probes translation and execution, this trio
probes *instantiation*: work that Wasmi charges **zero fuel** and that xrpld
repeats on every Finish, because `run()` builds a fresh engine, module, and
store and calls `instantiate_and_start` per invocation. Each pairs a maximally
expensive-at-instantiation module with a trivial `finish() { return 1 }`, so the
combined WASM_TIMING `time=` is instantiation-dominant.

| case | packs | isolates | ~size |
|---|---|---|---|
| inst_data | a ~90 KB active `(data ...)` copied into linear memory at instantiation | memory-init copy cost | 90 KB (~90% of the 100 KB module cap) |
| inst_elem | `(table 1024 funcref)` with an elem segment filling every entry (all → one no-op dummy) | table-entry materialization (1024 slots) | ~1.1 KB |
| inst_locals | 30,000 i32 locals in finish, trivial body, locals untouched | per-frame local-slot zeroing at entry | ~0.1 KB |

The caps are xrpld's / Wasmi's real ones: memory 128 pages, table 1024 elements
(both at crates/xrpl-wasm-vm/src/vm.rs), and locals 30,000 (wasmi 2.0.0
`LocalsRegistry::LOCAL_VARIABLES_MAX`, a hard translator limit — not the inert
50,000 EnforcedLimit). inst_elem needs only **one** dummy function: an elem
segment is a list of indices that may repeat and Wasmi materializes every slot
regardless of target. inst_locals does **not** touch its locals on purpose —
declared locals are frame-zeroed whether used or not, so touching them would add
translated/executed body instructions and make the number translation-dominant
instead (it also overlaps the existing many_locals, which sits at the same 30,000
but calls a helper in a loop; inst_locals is the single-entry regression test for
the in-flight per-frame-local-init fuel PR). Read all three against the ~8.9
ns/gas anchor: above it is stronger DoS than a pure wasm loop. Gas is small
(10,000) since finish is trivial and instantiation is unbilled; `time=` is the
measurement. Each carries an `opaque_random` pad for per-cycle uniqueness —
inst_data's whole data segment is the pad, the other two carry a 32-byte pad.
WASM_TIMING_FINISH does **not** split instantiate from execute time (one combined
`time=`); trivial finish keeps it instantiation-dominant, so no immediate action
(see NOTES for the instrumentation-improvement flag).

**dos_cache_miss_chain family** (two presets: chain_L1_resident,
chain_full_footprint). A data-dependent pointer chase — the strongest known
pure-wasm DoS shape. finish builds a single-cycle permutation in linear memory and
then follows it, so each load's address comes from the previous load: no prefetch,
no memory-level parallelism, cost set by the memory-hierarchy level the working set
lands in. Two phases, rebuilt every Finish (fresh zeroed memory): a cheap
SEQUENTIAL build of a full-period LCG single cycle (`next[i] = (a*i + c) mod N`
written in order, N a power of two, a ≡ 1 mod 4 and a ≠ 1, c odd), then the random
chase. a and c come from a 32-byte `opaque_random` seed the patcher rewrites per
cycle, so every Finish gets a fresh cycle order — no new patcher role. (An earlier
version used a Sattolo shuffle; its random-index swaps were themselves
random-access and dominated the wall time, hiding the chase — see NOTES
2026-10-01. The sequential LCG build is near-free, so the chase dominates.)

| preset | nodes × stride | footprint | chase | probes |
|---|---|---|---|---|
| chain_L1_resident | 2048 × 4 B | ~8 KB | T=16384 (warm, ~8 passes) | warm-cache floor (L1 on both hosts) |
| chain_full_footprint | 16384 × 64 B | ~1 MB | T=16384 (one cold pass) | cold working set the gas ceiling allows |

Both chase the same 16,384 loads, so the time difference is per-load latency, not
count. Live single-shot (2026-10-01): L1 794 µs vs full 2,937 µs, a **3.7x** ratio
— the memory-hierarchy signal. Naming is by shape, not cache level: on the test
hosts (M4 Pro ~16 MB L2, Threadripper 9960X 1 MB L2 / 128 MB L3) full's ~1 MB is an
L2/L3 chase, **not** DRAM-evicting. That bound is itself a finding: wasmi charges
~16 gas per loop iteration (1 fuel/operator) and forbids bulk memory, so
build + chase caps the buildable working set near 30K nodes (~2 MB) under the 1M
gas ceiling — below any modern LLC. So the ~8.9 ns/gas DRAM shape from the earlier
session is effectively unreachable on this build: the attacker cannot afford the
memory to make it work (see NOTES "cache-miss chain"). Gas is at the 1M ceiling;
WASM_TIMING `time=` is the measurement, not gas. Blobs are ~220 bytes (the array
is built at runtime, not shipped), so Create fees are ~1,200 drops.

All twenty-eight behave as intended. Two earlier issues were resolved: oog_compile
runs out of translation fuel (§6), and update_data_then_success converges once its
home_le_field field code was corrected (§6). The eight DoS cases
(dos_large_finish ×3, dos_expensive_instantiation ×3, dos_cache_miss_chain ×2) are
now **single-shot confirmed live** against the Oct 1 release build: each returns
tesSUCCESS with its staged gas, except that many_helpers first hit the xrpld
min-40-bytes/function guard (reworked, above) and the chain pair was reworked from
Sattolo to the LCG build to surface the hierarchy signal. Full trial measurements
(stable medians, ranking vs the 8.9 ns/gas anchor, gas recalibration from the
max-headroom staging values) are the next step; see NOTES 2026-10-01.

## 6. Known limitations / open questions

- **oog_compile (resolved).** Wasmi uses CompilationMode::LazyTranslation:
  a function is translated (and charged) only when first CALLED. The cost is put
  on escrow_finish's OWN body: it is one huge function — an early
  `return (i32.const 1)` guard followed by PAIRS copies of `(i32.const 0)(drop)`
  (default 20,000, ~60 KB wasm, under the 100 KB BytecodeSizeLimit). Translating
  that body on first entry exhausts the Gas allowance before a single body
  instruction executes: Finish returns tecOUT_OF_GAS with gas=0 in
  WASM_TIMING_FINISH. That gas=0 is how translation-OOG (B3) reads apart from
  execution-OOG (B2, gas≈allowance). (An earlier version used many small helper
  functions called from escrow_finish; one huge body is simpler and puts the
  cost squarely on the function actually entered.) Live-confirmed: 20,000 pairs
  reliably OOG at gas=1,000; 30,000 (~90 KB) also OOGs but nears the size cap.
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

## 7. Leak soak (how to run)

The DoS work (§5) measures per-tx wall time. The leak dimension asks a different
question: does xrpld RSS grow without bound over a long run? Three tools, all in
this repo, no chat context needed:

- `scripts/rippled_memory_sampler.py` — samples xrpld RSS / VSZ / thread count /
  ledger_seq to a CSV (`<run-dir>/xrpld_memory.csv`). Cross-platform (psutil),
  survives xrpld and sampler restarts, drift-free schedule.
- `run_soak.py` — drives the workload (the same patterns/categories as the DoS
  work).
- `analyze_leak.py` — joins the sampler CSV + run_soak.log + soak.csv and reports
  the RSS slope once the node reaches steady state, plus a per-window slope
  (decelerating = settling, steady = leak) and the accumulate drain baselines.

**The gate: history must be bounded, or RSS growth is meaningless.** Standalone
xrpld keeps FULL ledger history until `online_delete` (see xrpld.cfg `[node_db]`,
default 512) rotates — and rotation only fires once the node has ~2×online_delete
ledgers AND the SHAMapStore thread runs at a normal pace. Before that, RSS rises
with ledger count even for empty ledgers (~25 KB/ledger observed), which is NOT a
leak. **Verify rotation engages on your build before trusting a soak**: close past
~1,100 ledgers at normal pace and confirm `server_info.complete_ledgers` prunes
(earliest advances past 2) and the log shows `SHAMapStore ... finished rotation`.
Rotation was seen working on earlier builds and NOT firing on the 2026-10-01 build
(see NOTES), so this check is mandatory per-binary.

**Gotcha — wipe `db/` before every `--start`.** The SHAMapStore rotation point
(`LastRotatedLedger`) is persisted in `<db>/state.db` and survives a `--start`, which
begins a fresh chain at ledger 2. If the previous chain had rotated at ledger N,
the new chain will not rotate until ledger N+512, so the gate silently fails for
hours. Stop xrpld, `rm -rf <run_data>/db`, start it, then re-run
`setup_accounts.py` (the old accounts are gone with the chain).
`scripts/soak_supervisor.py` refuses to start in that state.

**Process name.** On Linux xrpld's main thread is named `xrpld-main`, so the
process is `xrpld-main` to psutil / `pgrep -x`, not `xrpld`. The sampler matches
`xrpld-main`, `xrpld` and `rippled` by default. (Binary check: `grep -a -c
WASM_TIMING_FINISH <binary>`; plain `grep -c` reports 0 on a binary.)

**Run it** (each long-running piece in tmux/nohup; a real soak is hours):

```
# 1. ledgers advancing
python3 ledger_ticker.py --interval 2

# 2. RSS sampler (1 Hz is plenty; writes into the run dir)
python3 scripts/rippled_memory_sampler.py --interval 1 \
    --output runs/leak1/xrpld_memory.csv

# 3. the workload — pipeline for per-invocation leak at high throughput...
python3 run_soak.py --pattern pipeline --categories return_1 \
    --accounts-per-thread 50 --in-flight-per-account 2 --threads 8 \
    --duration 21600 --run-dir runs/leak1
#    ...or accumulate for per-live-object state (create N, drain N, repeat):
# python3 run_soak.py --pattern accumulate --categories return_1,chain_full_footprint \
#     --accumulate-depth 2000 --threads 4 --duration 21600 --run-dir runs/leak1

# 4. verdict (drop the pre-rotation ramp with --warmup-ledgers)
python3 analyze_leak.py runs/leak1 --warmup-ledgers 1100 --windows 8
```

**Reading the result.** After warmup (past rotation), a flat slope that does not
shrink window-over-window is the leak; a decelerating slope is caches/history
filling and settling. Run a trivial case (`return_1`) and a heavy one
(`chain_full_footprint`, `inst_data`) separately: equal bytes/finish means the leak
is in the tx/ledger path, more for the heavy case means a wasm teardown leak.

**If rotation will not engage on your build** (history stays unbounded), fall back
to a control: run an *empty-ledger baseline* (sampler + ticker, no soak) to get
bytes/ledger with zero txs, then the escrow soak at the same ledger rate; the leak
is the DIFFERENCE in bytes/ledger, not the raw slope. (A no-wasm Payment control
would be cleaner but the driver is escrow-only today.)

**Unattended / multi-hour runs: `scripts/soak_supervisor.py`.** Runs bounded segments
(each drains its escrows on exit) from a schedule (`rehearsal` = lifecycles 1,2,4,5 + baseline,
`steady` = `return_1`, `heavysplit*` = one heavy category per segment with idle gaps), keeps the
ticker and sampler alive, health-checks xrpld, and writes `<run>/STATUS.txt` plus
`events.csv`. It stops (never restarts xrpld) if xrpld dies or restarts, the disk fills, rotation
state is stale, or an account balance drops under 1000 XRP; `touch <run>/STOP` stops it after the
current segment. Detach it so closing the IDE/terminal does not kill it and block sleep:
`setsid nohup systemd-inhibit --what=sleep:idle --who=se-soak --why=soak .venv/bin/python -u
scripts/soak_supervisor.py --name long1 --hours 4 --schedule rehearsal &`.
Paths come from `$SOAK_RUN_DATA` (default `~/rippled/run_data`). `analyze_leak.py <run>` reads
its `events.csv` and clips to the load window, so the idle tail does not skew the slope.
Other helpers in `scripts/`: `run_case.sh` (one 5-minute case with sampler),
`topup_accounts.py` (refill drained accounts from genesis), `tps_ramp.py` (txs/ledger ramp).

**Measured on the Ubuntu box (2026-10-03/04), for planning:**
- Throughput with the pipeline at 400 accounts x 2 in flight: 800 txs/ledger (400 Create +
  400 Finish), ~375 tx/s at a 2.14 s ledger (~267 tx/s at 3 s); xrpld used ~0.4 core, so the
  limit is the driver. Past ~1000 txs/ledger standalone fee escalation
  (`minimum_txn_in_ledger_standalone`, default 1000) kicks in and `run_soak.py` aborts on
  `terQUEUED`; raise that cfg value to measure further.
- Fees: `chain_full_footprint` costs ~1 XRP per Finish (gas 1M x 1 drop/gas), ~1,550
  XRP per account per hour at 400 accounts. The accumulate pattern routes all fees through
  one owner per thread (4 accounts); size funding (`setup_accounts.py --fund-xrp`) or top up.
- RSS (live node): light cases plateau at ~1.3-1.9 GB; `chain_full_footprint` and `trace_heavy`
  stay near that; `inst_data` swings 7-14 GB under load and takes ~20 min to return to
  baseline after the load stops (released, not leaked). Threads stay at 40 (brief 47-71 spikes).
