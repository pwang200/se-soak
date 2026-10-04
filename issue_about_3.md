# Issue: preflight_reject Creates return temBAD_SIGNATURE ~38% of the time

Found 2026-10-03 while smoke-testing lifecycle 3 (`preflight_reject`) on the Ubuntu box.
Open question: is this a harness bug (driver resubmits an identical tx) or intended
behaviour we should just accept?

## Repro
```
python run_soak.py --accounts <accounts.json> --pattern pipeline \
    --categories unknown_imports,disabled_instructions,unfunded_account \
    --accounts-per-thread 50 --in-flight-per-account 2 --threads 8 --duration 300 \
    --run-dir <dir>
```
Run dir with the full result: `/home/pwang/rippled/run_data/soak/L3_preflight_reject`
(`soak.csv`, `driver.out`). Node: standalone xrpld 3.5.0-b0, branch `se-soak`, ticker at 2 s.

## Observed (300 s run)
| category | engine_result | count |
|---|---|---|
| unfunded_account | terNO_ACCOUNT (expected) | 18,996 |
| disabled_instructions | temINVALID_BYTECODE (expected) | 11,712 |
| disabled_instructions | **temBAD_SIGNATURE** | 7,292 |
| unknown_imports | temINVALID_BYTECODE (expected) | 11,699 |
| unknown_imports | **temBAD_SIGNATURE** | 7,301 |

13,853 `[unexpected]` lines on stderr (the driver logs them and continues).
The ~38% share is the same for every thread and every one of the 400 accounts
(per-account min 33%, median 38.5%, max 40%). `unfunded_account` is never affected.
Valid-bytecode Creates (`return_1`, 336,000 in a 30 min soak) never see it.

## What the node log shows
For a failing tx hash (e.g. `E9FCE84BB9FC903FEA74BD04C39E818963AA77340442557C674745ED48991819`)
in `/home/pwang/rippled/run_data/debug.log`:
```
19:09:11.227 OpenLedger  WASM_TIMING_CREATE_PREFLIGHT tx=E9FC... ledger_seq=474 open=true ... ter=temINVALID_BYTECODE
19:09:11.240 Network     E9FC...: cached bad!
19:09:13.190 OpenLedger  WASM_TIMING_CREATE_PREFLIGHT tx=E9FC... ledger_seq=475 open=true ... ter=temINVALID_BYTECODE
```
(9 log lines mention this hash in total.) So xrpld itself evaluates the tx as
`temINVALID_BYTECODE`; `temBAD_SIGNATURE` appears on a later submission of the same hash,
right after `cached bad!`.

## Hypothesis (not verified)
- A tem result does not consume the account Sequence, so the driver's next
  preflight_reject Create from the same account reuses the same Sequence.
- `escrow_lib.Worker.create` (`/home/pwang/PycharmProjects/se-soak/escrow_lib.py:968`)
  builds the tx from fixed fields: same Bytecode (static blob per category), same
  Sequence, same Fee, and `CancelAfter = close_time + 30`, which is constant within a
  ledger. Submitted again within the same ledger window it is byte-identical, so same
  hash.
- xrpld's HashRouter has already marked that hash bad after the first tem, and answers
  the repeat with `temBAD_SIGNATURE` (the cached-bad path).
- Why ~38% rather than ~100%: only repeats within the same close_time/ledger window are
  identical; not checked in detail.

## To decide
1. Bug in the harness: make each preflight_reject Create unique (vary `CancelAfter`, add a
   Fee offset, or a Memo), so every submit reaches preflight and returns the real code.
2. Intended / acceptable: treat `temBAD_SIGNATURE` as an allowed result for
   `preflight_reject` (but this masks the duplicate).
3. Check whether `cached bad!` -> `temBAD_SIGNATURE` for a repeated tem tx is stock rippled
   behaviour or something specific to this branch.

## Impact
Only the correctness read of lifecycle 3. The rejected txs never create an escrow, never
apply, and cost no fee, so memory soak numbers for the other lifecycles are unaffected.
No code change has been made yet.
