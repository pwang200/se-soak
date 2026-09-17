# wats/ — wasm Bytecode sources for the soak categories

Each smart-escrow category in `escrow_lib.CATEGORIES` points at one
pre-built `.wasm` in this folder. The `.wat` next to it is the source. The
bytes go on the wire as the EscrowCreate `Bytecode` field.

| file | role |
|---|---|
| `<name>.wat` | hand-written WebAssembly text; the thing you edit |
| `<name>.wasm` | built artifact, read by the driver at import time via `escrow_lib.load_wasm("<name>.wasm")`; commit it |
| `build.sh` | runs `wat2wasm` on every `.wat` here |

The driver never calls `wat2wasm`. If a `.wasm` is missing, `escrow_lib`
fails at import with a message telling you to run `build.sh`.

## Build

Needs [wabt](https://github.com/WebAssembly/wabt) on PATH
(`brew install wabt`).

```
./wats/build.sh
```

It prints each output's byte size. Size matters twice: the EscrowCreate
fee is `10*base_fee + 5*wasm_bytes`, and xrpld rejects modules larger than
the ledger's `BytecodeSizeLimit` (FeeSettings entry; the driver checks this
at startup). Don't pass `--debug-names`; a name section adds bytes for
nothing.

## Contract every module must satisfy

- Export a function named `escrow_finish` with signature `() -> i32`
  (`escrowFunctionName` in xrpld's `include/xrpl/tx/wasm/WasmVM.h`). A
  module without it is refused at EscrowCreate with `temINVALID_BYTECODE`
  ("no entry point 'escrow_finish'").
- Imports, if any, must come from module `host_lib` and name a real host
  function; anything else is `temINVALID_BYTECODE`. Exporting `memory` is
  optional; if present, at most 128 pages.
- Return value semantics at EscrowFinish (from `examples/NOTES.md`):

  | `escrow_finish()` returns | validated result | escrow |
  |---|---|---|
  | > 0 | `tesSUCCESS` | removed, funds released |
  | <= 0 | `tecBYTECODE_REJECTED` | stays; fee charged; value recorded in meta `VMReturnCode` |
  | out of gas | `tecOUT_OF_GAS` | stays; fee charged |
  | trap | `tecFAILED_PROCESSING` | stays; fee charged |

- Gas is bounded by the category's `gas` (the `Gas` field on the
  EscrowFinish, 1..`GasLimit`). Finish fee is `base + Gas*GasPrice/1e6 + 1`
  drops and is charged on the **allowance**, not on gas used (1 drop per
  gas at the default GasPrice). Rule: run the module once, read `gas=` from
  its WASM_TIMING_FINISH line, set `gas` to at least 10x that. The trivial
  return_1 / return_0 modules use 30 gas, so they declare 1_000. A module
  that runs out returns `tecOUT_OF_GAS`, which the driver flags as
  unexpected.
- A module that fails xrpld's preflight validation (e.g. an unknown
  import) is rejected at EscrowCreate with `temINVALID_BYTECODE` /
  `temMALFORMED`, before anything is written to the ledger.

## Adding a category

1. **Write the source**: `wats/<name>.wat`, exporting `escrow_finish`. Put a
   comment header at the top stating the intended lifecycle and expected
   results (see `return_1.wat`).
2. **Build**: `./wats/build.sh`. Check the printed size is what you expect.
3. **Register** in `escrow_lib.CATEGORIES`:

   ```python
   "<name>": Category(
       name="<name>",
       wasm=load_wasm("<name>.wasm"),
       gas=1_000,        # >= 10x the gas= WASM_TIMING_FINISH reports; fee is on the allowance
       lifecycle=FINISH_REMOVES,            # or CANCEL_REMOVES / PREFLIGHT_REJECT
       expected_finish_result="tesSUCCESS", # None for PREFLIGHT_REJECT
       # expected_create_result="temMALFORMED",  # PREFLIGHT_REJECT only
   ),
   ```

   `Category.__post_init__` rejects inconsistent combinations (e.g. a
   `preflight_reject` with an `expected_finish_result`). Lifecycle
   semantics are described in `flow.md` §1.
4. **Run it**: `--categories return_1,return_0,<name>` on `run_soak.py`.
   Both patterns pick it up automatically; nothing else to wire.
5. **Verify** with a short run: the CSV `final_result` column for the
   category's Finish rows should equal `expected_finish_result`, and
   stderr should show no `[unexpected]` lines for it.

Inspect a built module with `wasm-objdump -x wats/<name>.wasm` (also from
wabt) or `xxd wats/<name>.wasm`.

## Host function ABI reference

_TODO (pwang): fill in from the C++ source._

Placeholder for the host functions an `escrow_finish()` may import from xrpld:
module name, function name, signature, semantics, gas cost, and which
xrpld branch/commit the table was taken from.

| import (module.name) | signature | semantics | gas |
|---|---|---|---|
| | | | |
