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

Facts those scripts already established:
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