# Ubuntu leak-soak task prompt

Paste this to a fresh Claude Code session on the Ubuntu box. That session has the
se-soak repo but NONE of the chat context it was written in; everything it needs is
in the repo. Goal: run the smart-escrow memory-leak soak and report whether
xrpld's smart-escrow Finish path leaks memory.

---

You are running on a Ubuntu box in the `se-soak` repo with no prior context. Your
job is to run the smart-escrow memory-leak soak and report a verdict.

## Read first (do not re-derive these)
- `flow.md` section 7, "Leak soak (how to run)" — the method, the exact commands,
  and the `online_delete` gate.
- `examples/NOTES.md`, the "Leak-dimension toolchain" and "online_delete" entries —
  why the gate matters and the full-history confound (RSS grows ~25 KB per EMPTY
  ledger when history is unbounded, so raw RSS growth is NOT a leak).
- `flow.md` sections 1 and 5 for lifecycles and the category registry.

## Assumed environment (verify, do not rebuild)
- xrpld built with the WASM_TIMING patch, running standalone (`xrpld -a --start
  --conf=.../xrpld.cfg`), admin RPC at `http://127.0.0.1:5005/`.
- `xrpld.cfg` `[node_db]` has `type=NuDB`, `online_delete=512`, `advisory_delete=0`.
- A Python venv built from `requirements.txt` (xrpl-py, psutil). Use it for every
  `python3` call. The committed `wats/*.wasm` are used as-is (no wabt needed).
- Confirm before starting: `server_info` shows the node up and fee params present
  (GasLimit/GasPrice/BytecodeSizeLimit); `grep -c WASM_TIMING_FINISH <binary>` > 0
  and a finish log line carries `ledger_seq=` and `open=`; the pool is funded
  (`setup_accounts.py --count 500` if the chain is fresh).

## Step 0 — THE GATE (mandatory, before any long run)
Verify `online_delete` rotation bounds history on THIS build. It fired on earlier
builds and did NOT on the 2026-10-01 build, so it must be checked per-binary.
1. Start `python3 ledger_ticker.py --interval 2` (in tmux).
2. Let the node close past ~1,100 ledgers at that pace.
3. Check `server_info.complete_ledgers`: the earliest must advance past 2
   (e.g. "600-1100"), and the log must show `SHAMapStore ... finished rotation`.
- Rotates → proceed to Step 1, and note the first ledger_seq retained after
  rotation (you pass it to `analyze_leak --warmup-ledgers`).
- Does NOT rotate after ~1,100 ledgers at normal pace → STOP and report. The
  plateau method won't work; use the empty-ledger control fallback in flow.md §7,
  or get rotation working first.

## Step 1 — baseline leak soak (trivial wasm)
Each long-running piece in tmux/nohup (a real soak is hours; your box has no time
cap). Keep the Step 0 ticker running.
```
python3 scripts/rippled_memory_sampler.py --interval 1 \
    --output runs/leak_return1/xrpld_memory.csv
python3 run_soak.py --pattern pipeline --categories return_1 \
    --accounts-per-thread 50 --in-flight-per-account 2 --threads 8 \
    --duration 7200 --run-dir runs/leak_return1
```
Start with 2 h (`--duration 7200`); extend to 6 h (`21600`) if inconclusive. Then:
```
python3 analyze_leak.py runs/leak_return1 --warmup-ledgers <first seq after rotation> --windows 8
```

## Step 2 — heavy soak (isolate a wasm-teardown leak)
Repeat Step 1 with `--categories chain_full_footprint` (and/or `inst_data`) into a
new run dir. Compare bytes/finish against return_1: equal means the growth is in
the tx/ledger path, materially more means a wasm-teardown leak.

## Report back
- Did rotation engage (Step 0)? The complete_ledgers range after the run.
- From `analyze_leak` for each case: post-plateau RSS slope (MB/hour and
  bytes/finish), and whether the per-window slope is flat (leak) or decelerating
  (history/cache settling).
- Thread-count trend from `xrpld_memory.csv` (`threads` column); a climbing thread
  count is its own leak.
- Verdict: leak / no leak / inconclusive, with the numbers, for the trivial and the
  heavy case.

## Notes
- `runs/` is gitignored; don't commit run artifacts.
- If the node or ticker stops, restart it; the sampler re-discovers the pid and
  keeps going.
