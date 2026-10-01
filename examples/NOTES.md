Project: smart-escrow soak test driver.

Goal: a small Python project that runs a steady-state smart-escrow workload
against a standalone xrpld for up to a week, so we can detect memory leaks
and DoS-grade long transactions. Fail-loud, no over-engineering — this is
a test harness.

Read the four probe scripts in this directory first; they encode hard-won
facts I don't want you to re-derive:
- probe_fund_accounts.py
- probe_parallel_submit.py
- probe_escrow_wasm.py
- probe_escrow_wasm_failures.py

Facts those scripts already established (2026-09-16: the smart-escrow
branch has since renamed fields/results and changed the Finish fee — see
"Protocol update" below; the probe scripts still speak the May dialect):
- Standalone xrpld at http://127.0.0.1:5005/, manual ledger close via
  ledger_accept. Genesis seed snoPBrXtMeMyMHUVTgbuqAfg1SUTb with
  algorithm=secp256k1 → rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh.
- Per XLS-0100 §6.2: EscrowCreate fee = open_ledger_fee*10 + 5*wasm_bytes.
  EscrowFinish fee = open_ledger_fee*100 + ComputationAllowance.
- EscrowCreate requires CancelAfter even with FinishFunction; FinishFunction
  is an extra gate, not a replacement.
- ComputationAllowance is the gas field on EscrowFinish.
- Result codes: tesSUCCESS (wasm returned nonzero), tecWASM_REJECTED (wasm
  returned 0), tecFAILED_PROCESSING (out of gas).
- Concurrent submit from many accounts works; xrpld processes them in
  parallel.

Notes:
- Genesis: seed snoPBrXtMeMyMHUVTgbuqAfg1SUTb is secp256k1 (xrpl-py defaults 
  to ed25519, which gives the wrong address). Resulting address is 
  rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh.
- WASM_TIMING log format (in xrpld's main journal): 
  WASM_TIMING_CREATE_PREFLIGHT tx=<hash> time=<us> code_sz=<bytes> result=<ok|err>, WASM_TIMING_CREATE_APPLY tx=<hash> time=<us> code_sz=<bytes>, and WASM_TIMING_FINISH tx=<hash> time=<us> gas=<used> result=<i32> code_sz=<bytes>.
- Each tx triggers multiple invocations (preflight runs on submission, consensus, 
  and finalization), so leak analysis must normalize per-invocation, not per-tx.
- Must scale to ~10K accounts. Genesis funds 100 accounts, 
  each of those funds the next generation (~100 accounts) in parallel. 
  Within each generation, batch submits before ledger_accept 
  since the open ledger absorbs many queued txns per close (but I don't know exact number). 
  Show progress.

Build:
1. escrow_lib.py — shared module: RPC helper, fee formulas, wasm blob
   constants, category registry, Worker class with create/finish/cancel/
   wait_for_expiry methods. Start with two categories: return_1
   (cleanup=finish) and return_0 (cleanup=cancel-after-expiry).
2. setup_accounts.py — fund N accounts from genesis with a generous
   balance (e.g. 1M XRP each, configurable). Writes test_accounts.json.
3. run_soak.py — threadpool of N workers. Each worker picks a category
   (round-robin or weighted), runs it to ledger-neutral, logs per-tx
   outcome to a CSV (ts, worker_id, category, action, tx_hash,
   engine_result, final_result). Configurable --duration, --workers,
   --tps, --output. Same script for warm-up and long run.
4. check_state.py — read-only. Queries account_objects across worker
   accounts, prints in-flight escrow count and distribution. Used as a
   warm-up readiness check.

Constraints:
- Fail-loud: if xrpld stops responding, log and exit non-zero, don't try
  to be heroic.
- Sequence cache per worker, with refresh-from-ledger on any sequence-
  related error (tefPAST_SEQ, terPRE_SEQ, terQUEUED, tefMAX_LEDGER).
- Steady-state object count: every Create must end in a removal (Finish
  or Cancel) within the same worker cycle. After warm-up, in-flight
  escrow count should be bounded by worker count.
- Category registry must be extensible — we'll add ~8 more wasm cases
  later. Each category specifies: wasm blob, expected finish result,
  cleanup strategy.
- CancelAfter should be short (e.g. 30s) so cancel-cleanup cycles
  complete inside the test, not stretched over months.

Out of scope for this pass: post-soak analysis script, additional wasm
categories beyond return_1/return_0, fancy resilience features.

Start with escrow_lib.py. Show me the structure before filling in
implementations.

Refine 1:
Update the Worker class in escrow_lib.py to handle sequence-related
errors more completely. Reference: xrpl4j's SmartEscrowSoakTest treats
these engine_results as "sequence errors" that require refreshing the
local sequence cache from the ledger via account_info:

  tefPAST_SEQ   — submitted sequence is below the account's current
                  sequence. Tx not applied, sequence not consumed.
  terPRE_SEQ    — submitted sequence is above current. Tx may queue or
                  be rejected. Sequence not yet consumed.
  terQUEUED     — accepted into the TxQ for a future ledger. Sequence
                  will be consumed when applied.
  tefMAX_LEDGER — LastLedgerSequence has passed without the tx being
                  included. Sequence not consumed.

Behavior to add on any of these codes:
1. Log the engine_result and the worker's cached sequence.
2. Call account_info to fetch the authoritative sequence and update
   the cache.
3. Retry the same operation once with the refreshed sequence.
4. If it fails again with another sequence error, log and exit
   non-zero — this is fail-loud, not heroic recovery.

Don't add this to the category functions; it belongs in the lower-level
submit helper so every Create / Finish / Cancel benefits without
duplication.

Also add a brief comment block at the top of the submit helper listing
these four codes and their "is sequence consumed?" semantics, since
that distinction matters when deciding whether to bump the local
sequence after the call.

In case you want to see how SmartEscrowSoakTest works: 
https://github.com/XRPLF/xrpl4j/blob/df/support-smart-escrow/xrpl4j-integration-tests/src/test/java/org/xrpl/xrpl4j/tests/SmartEscrowSoakTest.java

Refine 2:
Scheduling is now an abstraction. Three concerns are decoupled:

- Worker  (escrow_lib.py): one account's primitives — Create/Finish/Cancel
  + sequence cache. Never changes when we add patterns or categories.
- Category (escrow_lib.py CATEGORIES): one wasm test case — blob, gas
  allowance, expected finish result, cleanup strategy. Adding a new
  category = one row.
- Pattern (pattern_*.py): *when* to submit *what* across a slice of
  accounts. Adding a new pattern = one new module subclassing
  escrow_lib.Pattern. Selected at runtime with --pattern.

Two patterns are shipped:

- SerialPattern (--pattern serial). One thread per account; one cycle
  at a time (Create → wait → cleanup → wait). Today's behavior.
  In-flight bound: ≤ threads.

- PipelinePattern (--pattern pipeline). Each thread owns M accounts
  (--accounts-per-thread) and runs round-driven submission. Per round
  (one ledger close):
    A. validate the previous round's pending submits (CSV-log them).
    B. submit Finish/Cancel for every in-flight escrow that's ready.
    C. top up with new Creates until each account has K in-flight
       (--in-flight-per-account; default 1).
    D. wait for the round's last submit hash to validate.
    E. backstop sweep: force-Cancel any CREATED entry past CancelAfter.
  Create and its Finish are always in different ledgers. No same-ledger
  trickery (option A from the design discussion was rejected).
  In-flight bound: ≤ threads × accounts_per_thread × K.

Backstop (both patterns):
Every escrow has a CancelAfter (default 30s). If the happy-path cleanup
doesn't validate as expected, the escrow is parked. At the next sweep,
anything past CancelAfter + 2s grace is Cancelled. This is what keeps
in-flight count bounded across a week-long soak even if some Finishes
behave unexpectedly. Logged as action="cancel_backstop" in the CSV.

CSV columns are now:
    ts, thread_id, account, category, action,
    tx_hash, engine_result, final_result
("worker_id" was renamed to "thread_id"; "account" was added so rows from
the same thread driving multiple accounts can be disambiguated.)

Adding a new wasm category: append to CATEGORIES with name, wasm blob,
computation_allowance, expected_finish_result, cleanup ("finish" or
"cancel"). Both patterns pick it up automatically.

Adding a new pattern: write pattern_<name>.py with a subclass of
escrow_lib.Pattern, register it in PATTERNS in run_soak.py.

Refine 3 (lifecycles):
`cleanup` on Category is replaced by `lifecycle`, and a third lifecycle is
added. Each category now declares what it expects at every step:

  finish_removes    Create(tes) → Finish(tes; removes escrow) → done.
  cancel_removes    Create(tes) → Finish(tecWASM_REJECTED; escrow stays)
                    → wait CancelAfter → Cancel(tes) → done.
  preflight_reject  Create rejected at preflight (expected_create_result
                    is the tem* code). No escrow, no Finish, no Cancel.

Fields: lifecycle, expected_create_result (default tesSUCCESS),
expected_finish_result (None for preflight_reject). Category.__post_init__
rejects inconsistent combinations.

Behaviour change for return_0: it now submits the Finish (expected
tecWASM_REJECTED) before waiting for CancelAfter. Before this, cancel
categories never ran their wasm at all — no WASM_TIMING_FINISH lines,
no reject-path coverage. One extra tx per return_0 cycle.

Unexpected outcomes (actual ≠ declared) print an "[unexpected] ..." line
to stderr in addition to the CSV row. Not fatal. Anything that leaves an
escrow on the ledger against expectation — Finish that didn't remove it,
Cancel that failed, a preflight_reject Create that xrpld accepted — is
backstop-Cancelled after CancelAfter, so the in-flight bound holds even
when xrpld misbehaves.

submit_and_log now waits for validation on tec* results too (they are
applied and validate), so cancel_removes Finish rows carry the validated
meta result instead of an empty final_result.

Pipeline pattern fixes made while adding the lifecycle branches (the
pattern had never been run):
- PENDING_CLEANUP entries were removed from the queue regardless of
  result, so the stage-E backstop could never see a failed cleanup. Now a
  non-removing result goes to STRANDED and stage E Cancels it.
- Stage D waited on the last submitted hash even when that submit was not
  applied (tel/tef), which would time out and abort the soak. Now only
  applied (tes/tec) hashes are tracked.
- Category rotation is per Create (per-account cursor), not per round, so
  preflight_reject Creates take a rotation slot but no in-flight slot and
  the account still fills its K real escrows. Loop bounded at K + #cats.
- New states: PENDING_FINISH, AWAIT_CANCEL (cancel_removes waiting out
  CancelAfter), STRANDED, PENDING_CANCEL. tecNO_TARGET on a Cancel counts
  as "escrow gone". MAX_BACKSTOP_ATTEMPTS=3 then drop with a warning.

Protocol update (2026-09-16, xrpld 3.4.0-rc1 @ e3027675a4, branch se-soak):
The first dry run against the rebuilt node aborted with
"Field 'tx_json.FinishFunction' is unknown". Read from the source:
- EscrowCreate: `FinishFunction` → `Bytecode` (Blob). CancelAfter still
  required (temBAD_EXPIRATION). Fee unchanged: 10*base + 5*bytes. Size
  capped by FeeSettings.BytecodeSizeLimit (temMALFORMED above it).
- EscrowFinish: `ComputationAllowance` → `Gas` (UInt32). Mandatory when the
  escrow has Bytecode (tefBYTECODE_NOT_INCLUDED), forbidden otherwise
  (tefNO_BYTECODE). 1 <= Gas <= FeeSettings.GasLimit else temBAD_LIMIT.
  Fee = base + Gas*GasPrice/1e6 + 1 (GasPrice in micro-drops per gas,
  from FeeSettings). Was: base*100 + allowance.
- Results: tecWASM_REJECTED → tecBYTECODE_REJECTED (202, wasm returned
  <= 0; return value lands in meta VMReturnCode). New: tecOUT_OF_GAS (201),
  temINVALID_BYTECODE. GasLimit == 0 or BytecodeSizeLimit == 0 in
  FeeSettings → temTEMP_DISABLED ("WASM runtime deactivated by fee voting").
- Wasm entry point renamed: the module must export `escrow_finish`
  (`() -> i32`), not `finish`; otherwise EscrowCreate fails at preflight
  with temINVALID_BYTECODE and the log says "no entry point
  'escrow_finish'". Imports must come from module `host_lib`. Both
  wats/*.wat updated (now 46 bytes each).
- Observed on the unfixed node (SmartEscrow NOT enabled): EscrowCreate
  with Bytecode returned temINVALID_BYTECODE, i.e. the wasm validator ran
  before the amendment gate refused the tx. Worth a look on the xrpld side.
- FeeSettings gained GasLimit=1,000,000 / GasPrice=1,000,000 /
  BytecodeSizeLimit=100,000 at ledger 257 (first flag ledger), not at
  genesis, on that node. A freshly started node has none for ~17 min at
  4 s ledgers unless genesis seeds them.
- WASM_TIMING_FINISH `result=` is now the wasm return value on success or
  the tec name on reject/failure; `gas=` is the engine's cost.
- Server-side signing (`submit` + `secret`) still works, with a
  "deprecated" note in the response. xrpl-py 4.5.0 has no Bytecode/Gas.
- Driver: escrow_lib.get_ledger_fees reads gas_limit/gas_price/
  bytecode_size_limit from `server_state`.state.validated_ledger at startup
  (FeeSettings ledger_entry as fallback) and fails loud if neither has
  them; Category.computation_allowance → Category.gas.
- Gas sizing rule (2026-09-16): allowance >= 10x measured `gas=` from
  WASM_TIMING_FINISH. return_1/return_0 measure 30 -> gas=1_000 (Finish fee
  1,011 drops instead of 10,011 at gas=10_000; the dry run of 20:37 UTC ran
  with 10_000).
- Node rebuilt at 38f99136af with PR 8228 (fee limits from the ledger; fixes
  the "no gas fields until the first flag ledger" issue). With
  [features]-only config FeeSettings still has no gas fields — the values
  are protocol defaults surfaced via server_state. `feature` RPC keeps
  saying enabled:false; that is expected in standalone.
- Node side (separate fix in xrpld): with `[features] SmartEscrow` the
  rebuilt node still came up with SmartEscrow vetoed and no gas fields in
  FeeSettings.

All-categories build (2026-09-16, live against xrpld 3.4.0-rc1 @ 33b530ab):
15 categories registered. Live end-to-end confirmed 13; 2 open findings.
- Confirmed create/finish codes (locked in the registry):
    unknown_imports, disabled_instructions -> temINVALID_BYTECODE
    unfunded_account -> terNO_ACCOUNT
    oog_execute -> tecOUT_OF_GAS (not tecFAILED_PROCESSING; probe 5 predated
      the tecOUT_OF_GAS code)
    trap_div_by_zero -> tecFAILED_PROCESSING
    return_0 -> tecBYTECODE_REJECTED; return_1 / trace_heavy / oom_at_max_page
      / unknown_keylet / boundary_float / many_locals / home_le_field_bytecode
      -> tesSUCCESS
- trace_heavy: 5000 trace calls cost ~225k gas (~45 gas/call; trace is NOT
  free), so its allowance is 300000 not 200000.
- A3 unfunded_account needs CLIENT-side signing: server-side submit+secret
  fails srcActNotFound for a nonexistent account. escrow_lib injects Bytecode
  (nth47 Blob) / Gas (nth84 UInt32) into xrpl-py's codec and submits a signed
  tx_blob. That is the sole use of client-side signing; pool accounts still
  sign server-side.
- multi_finish: finish_removes categories may retry Finish while the wasm
  rejects, capped at MAX_FINISH_ATTEMPTS=4 (for update_data_then_success).
- oog_compile: RESOLVED. Wasmi LazyTranslation only translates CALLED
  functions; the uncalled noise cost nothing (gas=30). escrow_finish now calls
  all noise fns (early return + dead body) -> tecOUT_OF_GAS with gas=0, i.e.
  runs out in TRANSLATION before executing. gas=0 distinguishes translation-OOG
  (B3) from execution-OOG (B2). update_data_then_success:
  RESOLVED. home_le_field takes the FULL SField code (type<<16)|nth, not the
  bare nth (invokeWithField -> SField::getKnownCodeToField). sfData is
  (7<<16)|27=458779, not 27; with 27 it never matched so every Finish rejected.
  Fixed -> converges in 2 Finishes. Same fix for home_le_field_bytecode
  (sfBytecode (7<<16)|47=458799). All 15 categories now behave as intended.

Ledger population (populate_ledger.py, 2026-09-17, Part A of the audit-guided
DoS work):
Runs after setup_accounts, before run_soak. Uses the funded pool accounts as
actors to create seven object types with randomized parameters, round-robin
across owners: trustlines (RippleState), offers (Offer), nftokens (NFTokenPage),
mpts (MPTokenIssuance), escrows (plain, CancelAfter +1yr so they never vanish
mid-soak), oracles (Oracle), credentials (Credential). All seven confirmed
creatable on the standalone node once the amendments are in [features]
(NonFungibleTokensV1_1, MPTokensV1, PriceOracle, Credentials, plus Escrow /
SmartEscrow) — the feature RPC still reports them "disabled" but the transactors
work, same preset behaviour as SmartEscrow.
  python3 populate_ledger.py --count-per-type 50           # dev
  python3 populate_ledger.py --count-per-type 140000       # ~1M total
Output: one JSON index per type in populated/ (gitignored). Each record has the
ingredients to recompute the keylet AND the node's real keylet in `index`, read
from the tx meta CreatedNode (NFTs: the NFTokenPage keylet + nft_id) — no
client-side keylet math. Idempotent: a non-empty populated/ makes it print
counts and exit; delete the dir for a fresh start. Fail-loud on any non-tes
submit or missing keylet.

Pool-account object convention + check_state:
A pool account owns two flavours of object: populated baseline (constant across
a soak; escrows use CancelAfter ~1yr) and in-flight soak escrows (CancelAfter
~30s, removed within a cycle). check_state.py buckets every account_objects
entry by LedgerEntryType, splits Escrow into Escrow/baseline vs Escrow/soak by
CancelAfter (cutoff now+30 days), and DEDUPES by keylet because shared objects
(RippleState, an owner->dest Escrow, a Credential) appear in both parties'
account_objects. With populated/ present it prints expected-vs-ledger per type
and flags a baseline DROP (missing) as drift.

TODO (deferred, discuss before implementing):
- Cat 6 (cache_le storm): template model, mix of valid keylets from populated
  indexes + fabricated invalid ones. Discuss merging with C5 first.
- Random-field-code categories: need a way to enumerate valid field codes per
  object type; naive random codes hit FieldNotFound ~always and measure the
  wrong path.
- Template + per-cycle wasm patching (Part B) and its patchmap/patcher-role
  vocabulary will be documented here when built.

accumulate_then_drain lifecycle (2026-09-17):
A burst lifecycle for what scales with concurrent live smart-escrow count,
which steady-state patterns can't reach. New --pattern accumulate
(pattern_accumulate.py): per owner, create N live escrows, then drain. N is a
per-case accumulate_depth (default 500, return_1 800; override
--accumulate-depth); preflight_reject cases can't accumulate. Support is
declared centrally in escrow_lib via dataclasses.replace so it is one place to
read/tune.
  python3 run_soak.py --pattern accumulate --categories return_1 \
                      --accumulate-depth 500 --threads N ...
Drain uses the case's base lifecycle: finish_removes -> Finish each (removed);
cancel_removes -> Finish each (rejects at depth = the measurement), wait
CancelAfter, Cancel each. cancel_removes CancelAfter scales with N so a Finish
never lands after expiry (tecNO_PERMISSION); the pre-expiry gap is a plateau
holding N live escrows.
Binding limit is OWNER RESERVE not fee: each live escrow locks 2 XRP
(ReserveIncrement) + amount until drained, so ~N*(2+amount) XRP at peak;
10k funding reaches ~N=3000 at amount 1. A create failing mid-ramp
(tecINSUFFICIENT_RESERVE) is logged and the reached depth reported.
Attribution: <run-dir>/accumulate_detail.csv logs every create/finish with the
concurrent live count; join tx_hash -> WASM_TIMING. Phase markers in
run_soak.log ([accumulate] phase=...) segment sampler RSS into ramp/plateau/
drain. Additive to populate_ledger baseline; check_state already separates
Escrow/soak (includes accumulation) from Escrow/baseline and non-escrow types.
Validated: return_1 N=20 and N=500 (per-Finish time flat ~50us across live
0-500 = baseline); return_0 rejects at depth then cancels after expiry.

cache_le_pattern family (2026-09-17): merged old C5 (unknown_keylet, now
retired) with the cache_le-storm idea. One template (wats/cache_le_pattern.wat,
generated; MAX_N=150) whose Finish loops `count` times: accountroot_id(account)
-> keylet -> cache_le(slot 1 reused, so each call does a fresh view().read of a
distinct object = the disk-read driver). Two patch slots: u32_count and
account_id_array (fills the first `count` 20-byte entries with a shuffled
hit/miss mix; hits distinct real accounts, misses random). patch_params is now a
FLAT dict read by all roles ({"iterations":N,"hit_ratio":r}); known_keylet moved
to {"valid_ratio":1.0}. Five presets: cache_{miss,hit}_single (iters=1),
cache_{miss,hit,mixed}_storm (iters=150, gas 900k, accumulate 500); the two
_single are excluded from accumulate. iterations cap ~180/Finish (each ~5350 gas
vs 1M GasLimit). DoS signal (wall vs gas, hit vs miss) needs disk reads: misses
hit disk anywhere (random SHAMap paths), hits only with a large cold funded pool
(soak scale). Live dev (20 accts, warm): all 5 tesSUCCESS; singles ~5874 gas
~300us, storms ~806749 gas ~500us (hit storm ~640us) -> wall small vs gas at dev;
the inversion at soak scale is the watch item.

Open question — terQUEUED vs the other three:
The other three codes (tefPAST_SEQ, terPRE_SEQ, tefMAX_LEDGER) all mean
"your local seq is wrong, refresh fixes it." terQUEUED is different:
the seq is fine, but the fee was below open_ledger_fee and xrpld parked
the tx in the TxQ. Refreshing from account_info doesn't help — the
queued tx hasn't applied, so account_info returns the same seq we
already used. A retry would resubmit with the same seq + same fee and
either hit terQUEUED again or tefPAST_SEQ (if the queued copy applied
first), tripping the fail-loud exit on the second attempt.

At soak load with open_ledger_fee jumping, terQUEUED is the most likely
"sequence-class" error to actually hit, and the correct response is
"bump fee and retry," not "refresh seq." Current behavior treats it the
same as the other three → fail-loud exit on the second hit, which is
the operator's signal to raise fee headroom. Acceptable for now;
revisit if we see terQUEUED churn during real runs.

Revisit pass (2026-09-17): retry lifecycle, per-case lifecycles+purpose, B3 rewrite:
Promoted the old `multi_finish` bool to a named lifecycle retry_finish_until_success
and added a true-failure fall-through. Every case now declares a `lifecycles` SET
(base lifecycle + accumulate_then_drain when it opts into the burst pattern) and a
`purpose` SET (informational only: baseline / correctness / dos / leak). Declared
centrally in escrow_lib via dataclasses.replace so support + intent read in one place.

Case x lifecycles x purpose (20 categories):
  category                    base lifecycle              accum  purpose
  unknown_imports             preflight_reject            -      correctness
  disabled_instructions       preflight_reject            -      correctness
  unfunded_account            preflight_reject            -      correctness
  return_0                    cancel_removes              yes    baseline,correctness
  oog_execute                 cancel_removes              yes    correctness
  oog_compile                 cancel_removes              -      correctness
  trap_div_by_zero            cancel_removes              -      correctness
  return_1                    finish_removes              yes    baseline
  update_data_then_success    retry_finish_until_success  -      correctness
  trace_heavy                 finish_removes              -      correctness
  oom_at_max_page             finish_removes              -      correctness
  known_keylet                finish_removes              -      correctness,dos
  cache_miss_single           finish_removes              -      correctness,dos
  cache_hit_single            finish_removes              -      correctness,dos
  cache_miss_storm            finish_removes              yes    dos,leak
  cache_hit_storm             finish_removes              yes    dos,leak
  cache_mixed_storm           finish_removes              yes    dos,leak
  boundary_float              finish_removes              yes    dos
  many_locals                 finish_removes              yes    dos
  home_le_field_bytecode      finish_removes              yes    dos
accumulate_then_drain is a run-mode carried in the lifecycles set (not a base
lifecycle); only the six cases the task named plus the three storm presets opt in.
oog_compile, trap_div_by_zero, trace_heavy, oom_at_max_page, known_keylet, the two
_single presets, and update_data_then_success do NOT accumulate. preflight_reject
cases never create an escrow, so they can't accumulate at all.

retry_finish_until_success (was multi_finish): re-submit Finish while the wasm
returns tecBYTECODE_REJECTED, cap MAX_FINISH_ATTEMPTS=4, stop at the first
tesSUCCESS. NEW fall-through: a *true* Finish failure (out-of-gas / trap, i.e.
neither success nor the intermediate reject) now drops to the cancel_removes
cleanup (wait CancelAfter, Cancel) instead of stranding; only cap-exhaustion
strands (backstop). One live case: update_data_then_success (C2). Verified live
end-to-end (2026-09-17): create tesSUCCESS, Finish #1 tecBYTECODE_REJECTED (writes
its own sfData = (7<<16)|27 = 458779 via set_data), Finish #2 tesSUCCESS. gas=5000.

B3 oog_compile rewritten (deliverable 3): the generator (wats/gen_oog_compile.py)
now emits ONE huge escrow_finish body — an early `return (i32.const 1)` guard then
PAIRS copies of `(i32.const 0)(drop)` — instead of many small helper functions.
Under Wasmi LazyTranslation the function is translated (and charged) only on first
entry, and the huge body exhausts the Gas allowance during that translation before
a single body instruction runs, so WASM_TIMING_FINISH shows tecOUT_OF_GAS with
gas=0 (translation-OOG, distinct from B2's execution-OOG gas~=allowance). Live-
confirmed at gas=1000: PAIRS=20000 -> 60053 B wasm -> tecOUT_OF_GAS gas=0 reliably;
PAIRS=30000 -> 90053 B -> also OOGs but nears the 100 KB BytecodeSizeLimit. Committed
build is PAIRS=20000 (~60 KB, comfortable margin). Reliable size to report: 20000
pairs.

Audit 3.4 structurally fixed in a pending PR — leak weight now LOW for A*/B3:
Audit finding 3.4 (the TxQ / preflight path allocates wasm arena entries before
cheaper checks — e.g. account existence — so rejected or aborted-early Finishes
still churn allocations) is being fixed structurally in a pending rippled PR. With
that fix landed, the per-invocation allocation churn on the preflight / lazy-
translation path is bounded, so the LEAK-signal weight for the cases that mainly
exercise that path is now LOW:
  - A1 unknown_imports, A2 disabled_instructions, A3 unfunded_account
    (preflight_reject — rejected in preflight, never execute a body);
  - B3 oog_compile (translation-OOG — dies during lazy translation on entry).
Keep these cases: they are still correctness coverage (the reject/OOG codes must
stay stable) and A3 still exercises the audit-3.4 ordering directly. But do NOT
read RSS growth across an A*/B3-heavy run as a leak once the PR is in — that path
is the one the PR bounds. The leak watch shifts to the storm/accumulate cases
(cache_*_storm, the D-group under accumulate), which exercise live-object and
per-execution allocation, not preflight churn.

Large-wasm cost dimensions (2026-09-21): dos_large_finish_{linear,looped,many_helpers}:
Three new DoS categories that FORK the question the D-group only opens — which
specific Wasmi cost dimension is underpriced, if any. All three are finish_removes,
purpose=dos, accumulate-capable (depth 500), template-based (so each carries a
zero-fuel opaque_random pad). Staged for a later xrpld build; NOT run live yet.

Cost model background (all as understood on this branch):
- Lazy translation: a function body translates to Wasmi IR on its FIRST call and
  is billed against the caller's gas allowance at that call. Unreferenced functions
  never translate (this is the B3 oog_compile mechanism).
- Instantiation (data/elem sections, memory init) is charged ZERO fuel (verified
  externally). That is why the opaque_random dedup-defeating pad lives in a (data
  ...) section — it changes the module bytes without adding any measured fuel.
- Anchor: another session measured a sustained pure-wasm cost of ~8.9 ns per unit
  of fuel (gas) using a dependent cache-missing pointer chase. Treat it as ONE
  data point, machine-specific, order-of-magnitude useful — the trio is partly a
  cross-check on it, not a fit to it. Anything above ~8.9 ns/gas is stronger DoS
  than a pure wasm loop already is; below is weaker.

The three shapes (default sizes in parens):
- linear (COUNT=5000, ~15 KB): one huge finish body, COUNT straight-line
  (i32.const 1)(i32.add) units on one accumulator, no loop/branch. Every
  instruction is TRANSLATED on entry AND EXECUTED once -> isolates per-instruction
  (translation + execution) cost, summed. Size-capped by the 100 KB limit.
- looped (ITERS=100 x BODY_UNITS=50 = 5000 executed units, ~0.5 KB): small body in
  a hot loop, executed count comparable to linear's but translated ONCE ->
  isolates per-executed-instruction cost, translation amortized. Capped only by
  gas, not size.
- many_helpers (N=5000, ~50 KB): finish calls N tiny one-instruction helpers once
  each; each helper first-call-translates + one function entry inside the single
  Finish -> isolates per-function-entry setup cost (as a differential over linear).
  Size-capped by the 100 KB limit (~12 bytes/helper).

Comparison framing (measure time_us/gas per shape from WASM_TIMING_FINISH; the
relative comparison across the three matters more than absolute values):
- linear elevated, looped NOT -> per-instruction TRANSLATION dominant (looped
  would also be elevated if execution dominated, since it isolates execution).
- looped elevated -> per-executed-instruction EXECUTION dominant.
- many_helpers elevated over linear -> per-function-ENTRY setup dominant.
- all three elevated vs ~8.9 -> Wasmi cost model is globally low.
- none elevated vs ~8.9 -> cost model is honest at this shape.
Caveat: many_helpers also translates+executes N tiny bodies, so it isolates entry
cost only DIFFERENTIALLY against linear (helper bodies are one instruction each so
entry overhead dominates the delta). And linear vs looped will NOT have equal gas
even at equal executed counts — linear also pays translation fuel for all COUNT
units — but time/gas normalizes that; that's the point of the ratio.

Gas sizing: set to GasLimit max (1,000,000) for all three as STAGING headroom,
because translation cost is unmeasured and a first-run OOG would waste a live
cycle. Allowance does not distort the measurement (WASM_TIMING gas= is used, not
allowed). After the first live run, recalibrate down to ~10x measured gas per the
project's gas rule to keep Finish fees sane (1,000,000 gas ~= 1 XRP/Finish).

Offline verification done (no live node): all three build via wat2wasm and pass
wasm-validate; sizes linear 15093 B, looped 463 B, many_helpers 49971 B (all <
100 KB). The driver's own path (Category.make_wasm -> PatchContext.patch) produces
a valid module each cycle, two cycles differ (dedup defeated), and only the 32-byte
pad slot changes. run_soak wires PatchContext whenever any selected category is a
template (run_soak.py needs_patch), so these plumb through identically to the
cache_le_pattern / keylet_probe templates; opaque_random needs no populated/ index
or pool accounts. Live measurement (the time/gas comparison + gas recalibration)
is deferred to the updated xrpld binary.

Expensive instantiation (2026-09-21): dos_expensive_instantiation = inst_data /
inst_elem / inst_locals:
Three DoS categories that probe INSTANTIATION cost — work Wasmi charges ZERO fuel
and that xrpld repeats on every EscrowFinish (run() builds a fresh engine + module
+ store and calls instantiate_and_start per invocation; vm.rs). Each pairs a
maximally-expensive-at-instantiation module with trivial finish() { return 1 }, so
WASM_TIMING time= is instantiation-dominant. All finish_removes, purpose=dos,
accumulate depth 500, template-based (opaque_random pad for per-cycle uniqueness).
Staged for a later xrpld build; NOT run live yet.

Caps looked up (deliverable 1) — the two the prompt asked for are NOT xrpld
constants; xrpld sets no max-locals/max-functions. The real binding limits:
- MAX_MEMORY_PAGES = 128 (8 MiB), MAX_TABLE_ELEMENTS = 1024: crates/xrpl-wasm-vm/
  src/vm.rs (store_limits + preflight). The vm.rs MAX_TABLE_ELEMENTS comment is the
  exact "wasmi materializes every one inside instantiate_and_start, before the
  guest's first instruction, so no gas charge can reach the cost" line the task
  quotes — inst_elem reproduces that shape directly.
- max locals per function = 30,000. wasmi 2.0.0 hard translator limit
  LocalsRegistry::LOCAL_VARIABLES_MAX (engine/translator/func/locals.rs), enforced
  in register() at translation time, config-independent (strict-greater, so 30000
  itself is allowed). NOT the 50,000 seen in wasmparser limits.rs / wasmi
  limits.rs MAX_FUNC_LOCAL_COUNT — that 50,000 is an OPT-IN EnforcedLimit and
  xrpld does not enable enforced limits (vm.rs sets only ignore_custom_sections
  etc.). So 50,000 is inert; 30,000 binds. (User caught this — I had cited 50,000.)
- max functions per module = no active wasmi limit in xrpld. wasmi has
  MAX_FUNC_COUNT = 1,000,000 but only as an EnforcedLimit (inert here); the
  effective ceiling is the 100 KB module-size cap (~30k functions). So "90% of the
  functions cap" is not a usable sizing rule; the 100 KB module size is the real
  constraint for function-/data-heavy blobs.

Sizing (all offline-built, wat2wasm + wasm-validate, < 100 KB):
- inst_data: 90000-byte active (data ...) (~90% of the 100 KB cap) copied to linear
  memory (min 2 pages) at instantiation. wasm 90063 B, Create fee ~450,415 drops
  (~0.45 XRP at base=10). Whole data segment IS the opaque_random slot (payload +
  dedup). Isolates memory-init copy cost.
- inst_elem: (table 1024 funcref) + elem filling all 1024 -> ONE no-op dummy (elem
  indices may repeat; wasmi materializes each slot regardless of target, so N dummy
  functions required = 1). wasm 1139 B, Create fee ~5,795 drops. 32-byte pad.
  Isolates table-materialization cost.
- inst_locals: 30000 i32 locals in finish, trivial body, locals NOT touched. wasm
  95 B (locals collapse to a few bytes in the binary; the cost is runtime frame
  zeroing, not size), Create fee ~575 drops. Do NOT add local.get+drop per local:
  declared locals are frame-zeroed at entry whether used or not (and wat2wasm never
  drops them), so touching them would inject ~2*30000 translated+executed body
  instructions and make time= translation-dominant, not frame-init-dominant — plus
  blow past 100 KB. Overlaps existing many_locals (also 30000) but that one calls a
  helper in a loop 100x; inst_locals is a single-entry regression test for the
  in-flight per-frame-local-init fuel PR (once it lands, time_us/gas here drops).
  Isolates per-frame local-slot-zeroing cost. 32-byte pad.

Reference / read-out: measure time_us/gas per sub-case from WASM_TIMING_FINISH and
compare against the ~8.9 ns/gas pure-wasm anchor; above 8.9 is stronger DoS than a
pure wasm loop. Gas allowance is small (10,000) because finish is trivial and
instantiation is unbilled — gas used is near-zero; time= is the whole finding.

WASM_TIMING interpretation aside (deliverable check): WASM_TIMING_FINISH does NOT
split instantiation from execution. The timing patch (commit e3027675a4 on branch
se-soak / 929b1d8215 on supported_May_2_time_log; EscrowFinish.cpp) wraps the
whole apply window (t_apply_start..t_apply_end around the wasm run()) into one
time= microsecond number, combining compile + instantiate + execute. For these
sub-cases the trivial finish makes the combined number instantiation-dominant, so
no immediate action. FUTURE INSTRUMENTATION IMPROVEMENT: split instantiate vs
execute (and vs lazy-translation) time in WASM_TIMING_FINISH so instantiation cost
is attributable directly rather than inferred from a trivial-finish control.

Cache-miss chain (2026-09-21): dos_cache_miss_chain = chain_L1_resident /
chain_full_footprint:
Two DoS categories reproducing the dependent-pointer-chase shape (the "8.9 ns/gas"
memory-hierarchy attack from another session). finish builds a random permutation
in linear memory and chases it: each load's address is the previous load's value,
so no prefetch and no memory-level parallelism — cost tracks the cache level the
working set lands in. All finish_removes, purpose=dos, accumulate depth 500,
template-based (opaque_random SEED slot, not a shipped permutation). Staged; NOT
run live yet. Behavior confirmed offline against a Python reference of the exact
algorithm (single Sattolo cycle, one-pass covers all N, returns >0 -> tesSUCCESS);
the wasm itself is wat2wasm + wasm-validate clean (no offline wasm runtime here).

Shape: three phases in finish, all rebuilt every Finish (fresh zeroed memory):
  1. identity fill p[i]=i.
  2. Sattolo shuffle (uniform random SINGLE cycle): for i=N-1 downto 1, j=rand()%i,
     swap p[i],p[j]. Single n-cycle guarantees one pass touches every node once.
  3. chase: cur=p[cur], T times. full_footprint T=N (one pass, each load a fresh
     line); L1_resident T>>N (many warm passes, the floor).
xorshift32 seeded from a 32-byte opaque_random slot at offset 0 that the patcher
rewrites per cycle -> fresh permutation each Finish (defeats cross-Finish address
learning). No new patcher role — opaque_random on the seed is enough.
Presets: chain_L1_resident N=2048 stride=4 (~8 KB, L1) T=50000; chain_full_footprint
N=10000 stride=64 (~0.6 MB) T=10000. Blobs ~300 B (array built at runtime, not
shipped), Create fee ~1,600 drops. Gas at the 1M ceiling (chase as long as
possible); WASM_TIMING time= is the measurement, not gas.

KEY FINDING — DRAM-latency wasm DoS is effectively UNREACHABLE on this build:
The wasmi 2.0.0 fuel model (verified in source) is 1 fuel per operator (loads,
stores, arith, const, local.get/set, br_if all = 1; only nop/drop/block/loop/
return/else/end = 0; costs.rs default_cost macro), gas maps 1:1 to fuel, ceiling
1,000,000, and bulk memory is disabled (vm.rs wasm_bulk_memory(false)) so the array
must be built with per-element stores. Building the chain costs ~78 fuel/node
(identity fill + Sattolo swap's random accesses + one chase step), so the buildable
working set caps at ~12,000 nodes (~0.6-0.8 MB) under the 1M ceiling. Shipping a
bigger array as a data segment doesn't help (100 KB module cap -> ~90 KB array),
and the 8 MiB memory cap can't be FILLED within gas anyway. On the test hosts
(M4 Pro ~24 MB L3, Threadripper 9960X ~128 MB L3) a sub-MB footprint is an L2/LLC
chase, never DRAM. To read ~8.9 ns/gas you need ~130 ns dependent loads (DRAM),
which requires a working set past the LLC — and the attacker cannot afford the
gas to build one. So the real answer to "does this shape reach 8.9 ns/gas on our
hosts" is NO: the gas ceiling caps the attacker's working set below any modern
LLC. That is itself the useful result — this pure-wasm shape is bounded here.

OPEN QUESTION — where did 8.9 ns/gas come from? It is inconsistent with the above.
Possibilities (flagged, not chased): the other session assumed a different (higher
or absent) gas ceiling; ran on a smaller-cache machine where a ~0.1-0.6 MB working
set already exceeds the LLC; used a shape we have not seen (e.g. a data-segment
array on a small-LLC host, or measured translation/instantiation rather than the
chase); or a different fuel/gas mapping. Worth reconciling before treating 8.9 as
a target for this build. For our hosts, expect chain_full_footprint well below 8.9
ns/gas and chain_L1_resident far below that (the floor); the L1-vs-full ratio is
the deliverable, and the absolute confirmation waits for the live run.

First live measurements (2026-10-01, Oct 1 release build @ rippled HEAD b545391d0c,
WASM_TIMING patch; node repointed to cmake-build-release/xrpld):
Environment confirmed: node up on the new binary, fee params present (GasLimit 1M,
GasPrice 1M, BytecodeSizeLimit 100K, base 10), 20 pool accounts refunded on the
fresh chain, ledger ticker advancing.

WASM_TIMING_FINISH is logged TWICE per EscrowFinish tx (confirmed by pwang): xrpld
applies the tx once against the OPEN ledger (to judge whether it's good) and again
AFTER consensus (to build the next ledger). Both run the wasm, so two lines. For
return_1 the two are equal (~141 and ~137 us), so the earlier 935 us was just the
first-ever finish (cold start). Analysis joins by tx hash and currently pools both
passes (median). PENDING (pwang): add ledger_seq and an open flag to the
WASM_TIMING_FINISH line so the open-ledger check and the consensus build can be
separated (pick one as canonical, or sum both for true per-tx node cost).

Baseline return_1 (serial, 4 threads, 5 min, 152 cycles, 0 unexpected): gas=30
constant; finish time median 138 us (min 38, p90 277, max 427). KEY: there is a
~140 us FIXED per-finish-invocation overhead (instantiate + compile + apply
machinery) independent of work done. Every measurement reads against this floor.
time/gas is only meaningful when gas is large; for small-gas cases (inst_*) the
signal is ABSOLUTE time above the floor, not time/gas.

Single-shot DoS gate (one create+finish each): 7 of 8 run tesSUCCESS with my
staged gas; results (gas / time_us, pooled 2 passes):
  dos_large_finish_looped        23,520 /  267   (~11 ns/gas, above anchor)
  dos_large_finish_linear       115,030 /  838   (~7 ns/gas)
  inst_data                          30 /  231   (floor; signal is absolute time)
  inst_elem                          30 /  303
  inst_locals                        58 /  330
  chain_full_footprint          851,487 / 3907   (~4.6 ns/gas)
  chain_L1_resident             942,757 / 3914   (~4.2 ns/gas)
  dos_large_finish_many_helpers  REJECTED at create (see guard below), now reworked

xrpld GUARD — min average bytes per function = 40 (found live). The node rejects a
module whose average code-bytes/function < 40 once total function bytes exceed
1000: AvgBytesPerFunctionLimit{req_funcs_bytes:1000, min_avg_bytes_per_function:40}
at crates/xrpl-wasm-vm/src/vm.rs; log "wasm: compile: the Wasm module failed to
meet the minimum average bytes per function of 40: avg=8, ter: temINVALID_BYTECODE".
A defense against exactly the many-tiny-functions translation-cost attack. The
original dos_large_finish_many_helpers (5000 one-instruction helpers, avg 8 B) was
blocked. REWORKED to 250 helpers x 20 (i32.const 1)(i32.add) units (~72 B avg/func,
clears the guard; 5000 body units match dos_large_finish_linear). Re-run single
shot: create tesSUCCESS, finish tesSUCCESS, gas=136,671 time~1662 us. Differential
vs linear (115,030 / ~838) = 250 entries add ~824 us for ~21,641 gas = ~3.3 us and
~87 gas per entry = ~38 ns/gas on the delta — the SHARPEST underbilled dimension
found, well above the 8.9 anchor. So: the guard bounds the many-tiny-functions
attack, but per-function-entry cost (first-call translation + frame setup) is still
strongly underbilled in the regime the guard allows. (Single-shot, floor-
contaminated, both passes pooled; a short trial run will tighten it.)

RESOLVED (same day) — chain pair reworked from Sattolo to a cheap LCG build.
First symptom: chain_L1_resident (~8 KB) and chain_full_footprint (~640 KB)
finished in ~3914 vs ~3907 us — essentially equal despite the 80x working-set
difference. Cause: the Sattolo build (identity + N random-index swaps, itself
random-access) dominated the ~3900 us and buried the chase. Fix: replace Sattolo
with a full-period LCG single cycle, next[i] = (a*i + c) mod N (N a power of two,
a == 1 mod 4 and a != 1, c odd; a,c from the opaque_random seed). The build is now
a SEQUENTIAL (prefetchable, near-free) write pass, so the random chase dominates.
Verified single-cycle over 200 seeds offline. Resized so both chase T=16384 loads
(same count -> time difference is per-load latency): L1 N=2048 stride=4 (8 KB,
warm, ~8 passes), full N=16384 stride=64 (1 MB, one cold pass). First attempt
(N=32768 full, T=32768) OOG'd — measured ~16.4 gas/loop iteration, so 2N=65536
iters x 16.4 > 1M; N=16384 keeps build+chase ~537K, safe. Live single-shot
(2026-10-01): L1 308,143 gas / 794 us; full 623,563 gas / 2,937 us -> 3.7x time
ratio at equal chase count = the memory-hierarchy signal. gen_cache_miss_chain.py
now emits the LCG build (the 2026-09-21 Sattolo description above is historical).
Caveat: full also does more build writes (higher gas), but the build is sequential
and fast so the 3.7x is chase-dominated; ledger_seq/open flags will let a trial run
isolate the consensus pass cleanly.

Trial DoS measurements + gas recalibration (2026-10-01, rebuilt binary @ HEAD
6973159c7e with ledger_seq+open flags):
WASM_TIMING_FINISH now logs each finish TWICE, tagged: open=true (apply vs the open
ledger, when the tx is first judged) and open=false (apply after consensus, building
the next ledger), both with ledger_seq. open=false is the canonical per-tx cost
(deterministic ledger-build pass); for heavy cases it's the COSTLIER of the two
(e.g. chain_full open=true 1978 us vs open=false 2636 us), so using it is also the
conservative choice. (pwang note: preflight/open-ledger apply can run MORE than
twice per tx; join by ledger_seq+open, don't assume a fixed count.)

Trial run: serial, 8 threads, 5 min, 8 DoS cases round-robin, 304 finishes (38/case),
0 unexpected. Per-case open=false median time / gas:
  case                           gas      t_false  t_true   ns/gas(raw)
  dos_large_finish_many_helpers  136,671  1167 us   845 us   8.5
  dos_large_finish_looped         23,520   214 us   192 us   9.1
  dos_large_finish_linear        115,030   728 us   478 us   6.3
  chain_full_footprint           623,563  2636 us  1978 us   4.2
  chain_L1_resident              308,143  1141 us   690 us   3.7
  inst_locals                        58    262 us   214 us   (gas tiny)
  inst_elem                          30    220 us   194 us   (gas tiny)
  inst_data                          30    196 us   236 us   (gas tiny)
There is a ~196 us FIXED per-finish-invocation floor (min open=false median), so RAW
ns/gas is floor-contaminated for small/medium-gas cases. The real signal is in the
DIFFERENTIALS (the trio was designed for exactly this):

RANKED underbilled cost dimensions (wall time not reflected in gas):
  1. per-function-ENTRY ~20 ns/gas  (many_helpers - linear: dt=439us, dgas=21,641).
     SHARPEST, ~2.3x the 8.9 anchor. First-call translation + frame setup per entry
     is the most underbilled pure-wasm dimension. (Guarded above avg<40 B/func, but
     within the allowed regime this holds.)
  2. per-instruction TRANSLATION ~5.6 ns/gas  (linear - looped: dt=514us, dgas=91,510).
  3. cache-miss CHASE ~4 ns/gas  (chain absolute; 1 MB L2-bound, below the 8.9 DRAM
     figure — DRAM unreachable here, see "cache-miss chain"). L1-vs-full ratio 3.7x.
  4. per-executed-instruction ~0.8 ns/gas  (looped, floor-corrected). CHEAPEST;
     raw execution is well-billed.
  5. INSTANTIATION (inst_*): unbilled ABSOLUTE microseconds, gas ~= 0. frame-local
     init +65 us over floor (30000 locals, gas 58), table materialization +24 us
     (1024 entries), data-copy ~0 (90 KB is fast). Near-infinite ns/gas but small
     absolute; frame-local-init supports the in-flight per-frame-local fuel PR.
Takeaway: the two sharp findings are per-function-ENTRY (~20 ns/gas) and
frame-local-INIT (unbilled us); both are "work the gas model doesn't charge for."

Gas recalibration (from the staging max-headroom values to ~2x measured; gas is
DETERMINISTIC for these cases — fixed instruction/loop counts — so 2x is safe;
capped at the 1M ceiling, floor 1000). All 8 re-confirmed tesSUCCESS at the new
allowances:
  dos_large_finish_linear  1M -> 250K    dos_large_finish_looped  1M -> 50K
  dos_large_finish_many_helpers 1M -> 300K
  inst_data/elem/locals    10K -> 1K     chain_L1_resident  1M -> 650K
  chain_full_footprint     1M (kept; 2x exceeds ceiling)
This cuts per-finish fees (1 drop/gas) for accumulate/soak runs where fee x N matters.
