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
