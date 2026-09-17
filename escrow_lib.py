"""Shared module for the smart-escrow soak driver.

Covers: RPC helper, fee formulas, wasm blob loader (pre-built files in
wats/), category registry (each category declares a *lifecycle*), and a
`Worker` class
that owns one account's sequence and exposes create/finish/cancel/
wait_for_expiry.

Facts encoded here (see examples/NOTES.md; protocol names as of the
2026-09 smart-escrow branch, commit e3027675a4):
- Standalone xrpld at http://127.0.0.1:5005/; ledgers close on demand
  via the `ledger_accept` admin RPC.
- Genesis seed snoPBrXtMeMyMHUVTgbuqAfg1SUTb uses secp256k1 (xrpl-py
  defaults to ed25519, which yields the wrong address).
- EscrowCreate carries the wasm in `Bytecode` (Blob) and still requires
  CancelAfter. Fee = 10*base + 5*bytecode_bytes.
- EscrowFinish carries the gas allowance in `Gas` (UInt32, 1..GasLimit);
  required whenever the escrow has Bytecode (else tefBYTECODE_NOT_INCLUDED).
  Fee = base + Gas*GasPrice/1e6 + 1 drops.
- GasLimit, GasPrice (micro-drops per gas) and BytecodeSizeLimit come from
  the ledger's fees: `server_state`.state.validated_ledger (protocol
  defaults unless FeeSettings overrides). Read at startup (get_ledger_fees).
- Validated EscrowFinish results:
    tesSUCCESS              wasm returned > 0, escrow removed
    tecBYTECODE_REJECTED    wasm returned <= 0, escrow stays, fee charged
    tecOUT_OF_GAS           allowance exhausted
    tecFAILED_PROCESSING    wasm trap
  Invalid module at EscrowCreate: temINVALID_BYTECODE / temMALFORMED.
"""

from __future__ import annotations

import csv
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from escrow_patch import PatchContext, Template, load_template


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GENESIS_SEED = "snoPBrXtMeMyMHUVTgbuqAfg1SUTb"
GENESIS_ADDRESS = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"

RIPPLE_EPOCH_OFFSET = 946684800  # 2000-01-01 UTC

# Where run artifacts live (see runs/README.md). Resolved relative to this
# file, not cwd, so every script agrees regardless of where it's launched.
#   runs/test_accounts.json   persistent: written by setup_accounts.py,
#                             read by run_soak.py / check_state.py
#   runs/<UTC stamp>/         one disposable folder per soak run
RUNS_DIR = Path(__file__).resolve().parent / "runs"
DEFAULT_ACCOUNTS_FILE = RUNS_DIR / "test_accounts.json"


def default_run_dir() -> Path:
    """runs/<YYYYMMDDTHHMMSSZ> for a run starting now (not created here)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return RUNS_DIR / stamp

# Submit-time engine_result codes that mean: refresh sequence from ledger
# and retry once. tefMAX_LEDGER means LastLedgerSequence has elapsed; we
# treat it as a sequence-class error for simplicity (resubmit afresh).
SEQUENCE_RETRY_CODES = frozenset({
    "tefPAST_SEQ", "terPRE_SEQ", "terQUEUED", "tefMAX_LEDGER",
})


def is_applied(engine_result: str) -> bool:
    """True when a submit-time engine_result means the tx was applied to
    the open ledger and will show up validated: tesSUCCESS, or any tec*
    (fee charged, sequence consumed, no other effect). tem/tef/tel/ter
    results were NOT applied."""
    return engine_result == "tesSUCCESS" or engine_result.startswith("tec")


# ---------------------------------------------------------------------------
# WASM blobs. Sources are wats/<name>.wat; wats/build.sh compiles them with
# wat2wasm into sibling .wasm files, which is all the driver ever reads
# (no runtime dependency on wabt). Each exports `escrow_finish() -> i32`.
# See wats/README.md for the contract and how to add one.
# ---------------------------------------------------------------------------

WATS_DIR = Path(__file__).resolve().parent / "wats"


def load_wasm(filename: str) -> bytes:
    """Read a pre-built wasm blob from wats/. Called at import time by the
    category registry, so a missing or stale build fails loud at startup
    rather than mid-soak."""
    path = WATS_DIR / filename
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{path} not found; run wats/build.sh (needs wabt's wat2wasm)"
        ) from None
    if data[:4] != b"\x00asm":
        raise ValueError(f"{path}: not a wasm binary (bad magic bytes)")
    return data


# ---------------------------------------------------------------------------
# RPC + time helpers
# ---------------------------------------------------------------------------

def ripple_time(unix_seconds: float) -> int:
    return int(unix_seconds - RIPPLE_EPOCH_OFFSET)


def rpc(url: str, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
    """Single point of contact with xrpld. Raises RuntimeError on transport
    or RPC-level error. Fail-loud is the policy — callers may catch and log
    but should not paper over."""
    body = json.dumps({"method": method, "params": [params or {}]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.URLError as e:
        raise RuntimeError(f"RPC {method!r} transport error: {e}") from e
    result = data.get("result", {})
    if result.get("status") == "error":
        raise RuntimeError(f"RPC {method!r} error: {result}")
    return result


def get_open_ledger_fee(rpc_url: str) -> int:
    res = rpc(rpc_url, "fee")
    return int(res["drops"]["open_ledger_fee"])


# xrpl-py 4.5.0 does not know the smart-escrow fields, so it cannot encode or
# sign a tx that carries them. We inject Bytecode / Gas into its binary-codec
# definitions once, which is enough for CLIENT-side signing. Only the A3
# ephemeral path needs this (server-side `submit`+secret handles the pool
# accounts); the codes come from the branch's server_definitions.
_SE_FIELDS_INJECTED = False


def _inject_smart_escrow_fields() -> None:
    global _SE_FIELDS_INJECTED
    if _SE_FIELDS_INJECTED:
        return
    from xrpl.core.binarycodec.definitions import definitions as D
    from xrpl.core.binarycodec.definitions.field_info import FieldInfo
    from xrpl.core.binarycodec.definitions.field_header import FieldHeader
    extra = {
        # name: (nth, is_vl_encoded, type_name)
        "Bytecode": (47, True, "Blob"),
        "Gas": (84, False, "UInt32"),
    }
    for name, (nth, vl, type_name) in extra.items():
        if name in D._FIELD_INFO_MAP:
            continue
        D._FIELD_INFO_MAP[name] = FieldInfo(nth, vl, True, True, type_name)
        header = FieldHeader(D._TYPE_ORDINAL_MAP[type_name], nth)
        D._FIELD_HEADER_NAME_MAP[header] = name
    _SE_FIELDS_INJECTED = True


def get_close_time(rpc_url: str) -> int:
    """Return close_time of the latest validated ledger (ripple-epoch seconds).

    EscrowCancel and FinishAfter / CancelAfter checks compare against the
    *parent ledger's close_time*, not wall-clock. In standalone, close_time
    can drift far behind wall-clock (idle gaps between runs accumulate),
    so we must key CancelAfter and the cancel-wait off this clock.
    """
    res = rpc(rpc_url, "ledger", {"ledger_index": "validated"})
    return int(res["ledger"]["close_time"])


def wait_for_validated(rpc_url: str, tx_hash: str, timeout_s: float = 120.0,
                       poll_interval_s: float = 0.25) -> dict:
    """Poll `tx` until validated:true; return the tx response (with meta).
    Raises TimeoutError on timeout."""
    deadline = time.monotonic() + timeout_s
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            res = rpc(rpc_url, "tx", {"transaction": tx_hash})
            if res.get("validated"):
                return res
        except RuntimeError as e:
            # txnNotFound is normal until the tx is in a closed ledger.
            last_err = e
        time.sleep(poll_interval_s)
    raise TimeoutError(
        f"tx {tx_hash} not validated within {timeout_s}s (last={last_err})"
    )


# ---------------------------------------------------------------------------
# Smart-escrow fee parameters (FeeSettings ledger entry) + fee formulas
# ---------------------------------------------------------------------------

# Keylet of the FeeSettings singleton; same on every ledger.
FEE_SETTINGS_INDEX = "4BC50C9B0D8515D3EAAE1E74B29A95804346C491EE1A95BF25E4AAB854A6A651"
MICRO_DROPS_PER_DROP = 1_000_000


@dataclass(frozen=True)
class LedgerFees:
    """What xrpld charges for smart escrows, as read from FeeSettings.
    GasLimit / GasPrice / BytecodeSizeLimit only exist once the SmartEscrow
    amendment is enabled and the fee object has been seeded."""
    base_fee: int              # drops
    gas_limit: int             # max Gas on one EscrowFinish
    gas_price: int             # micro-drops per unit of gas
    bytecode_size_limit: int   # max Bytecode bytes on one EscrowCreate


def get_ledger_fees(rpc_url: str) -> LedgerFees:
    """Smart-escrow fee parameters for the validated ledger.

    Primary source: `server_state` → state.validated_ledger, which xrpld
    fills from the ledger's Fees (protocol defaults when FeeSettings has no
    gas fields, e.g. a [features]-only standalone). Fallback: the
    FeeSettings ledger entry. Fail loud if neither has them — nothing
    escrow-with-wasm will work."""
    st = rpc(rpc_url, "server_state").get("state") or {}
    for key in ("validated_ledger", "closed_ledger"):
        lg = st.get(key) or {}
        if all(k in lg for k in ("gas_limit", "gas_price", "bytecode_size_limit")):
            return LedgerFees(
                base_fee=int(lg.get("base_fee", 10)),
                gas_limit=int(lg["gas_limit"]),
                gas_price=int(lg["gas_price"]),
                bytecode_size_limit=int(lg["bytecode_size_limit"]),
            )
    res = rpc(rpc_url, "ledger_entry",
              {"index": FEE_SETTINGS_INDEX, "ledger_index": "validated"})
    node = res.get("node") or {}
    missing = [k for k in ("GasLimit", "GasPrice", "BytecodeSizeLimit")
               if k not in node]
    if missing:
        raise RuntimeError(
            f"no smart-escrow fee parameters: server_state.validated_ledger "
            f"lacks gas_limit/gas_price/bytecode_size_limit and FeeSettings "
            f"lacks {missing}. SmartEscrow not enabled on this node?"
        )
    if "BaseFeeDrops" in node:                 # post-XRPFees layout
        base_fee = int(node["BaseFeeDrops"])
    else:                                      # legacy layout: hex string
        base_fee = int(str(node.get("BaseFee", "a")), 16)
    return LedgerFees(
        base_fee=base_fee,
        gas_limit=int(node["GasLimit"]),
        gas_price=int(node["GasPrice"]),
        bytecode_size_limit=int(node["BytecodeSizeLimit"]),
    )


def check_categories_against_fees(categories, fees: LedgerFees) -> None:
    """Refuse to start with a category xrpld would reject at preflight
    (temBAD_LIMIT for Gas, temMALFORMED for Bytecode size)."""
    problems = []
    for c in categories:
        if c.runs_finish and not (1 <= c.gas <= fees.gas_limit):
            problems.append(f"{c.name}: gas={c.gas} outside 1..{fees.gas_limit}")
        if c.wasm_size > fees.bytecode_size_limit:
            problems.append(f"{c.name}: wasm {c.wasm_size} bytes > "
                            f"BytecodeSizeLimit {fees.bytecode_size_limit}")
    if problems:
        raise ValueError("; ".join(problems))


def fee_level(open_ledger_fee: int, base_fee: int) -> int:
    """How many multiples of the base fee the open ledger demands (>= 1).
    Under load xrpld scales a tx's whole calculated base fee by this."""
    return max(1, -(-open_ledger_fee // max(base_fee, 1)))


def create_fee_drops(open_ledger_fee: int, wasm_bytes: int) -> int:
    # xrpld: 10 * base + 5 drops per Bytecode byte. Using open_ledger_fee
    # in place of base gives headroom when the open ledger is busy.
    return open_ledger_fee * 10 + 5 * wasm_bytes


def finish_fee_drops(open_ledger_fee: int, gas: int, fees: LedgerFees) -> int:
    # xrpld: base + Gas * GasPrice / 1e6 (+1: server rounds up), scaled by
    # the open-ledger fee level.
    gas_drops = (gas * fees.gas_price) // MICRO_DROPS_PER_DROP + 1
    return fee_level(open_ledger_fee, fees.base_fee) * (fees.base_fee + gas_drops)


def cancel_fee_drops(open_ledger_fee: int) -> int:
    # Plain transaction; matches Payment-class fees with headroom.
    return open_ledger_fee * 10


# ---------------------------------------------------------------------------
# Category registry
#
# A category is one wasm test case. Its *lifecycle* says how the escrow
# (if one is ever created) leaves the ledger:
#
#   finish_removes    Create → Finish (tesSUCCESS removes the escrow) → done.
#   cancel_removes    Create → Finish (wasm rejects; tecBYTECODE_REJECTED,
#                     escrow stays) → wait CancelAfter → Cancel → done.
#                     The Finish attempt is the point: it is the only way
#                     the reject path runs and emits WASM_TIMING_FINISH.
#   preflight_reject  Create is rejected at preflight (tem*). No escrow,
#                     no Finish, no Cancel; the cycle is just the Create.
#
# Both patterns treat any outcome that leaves an escrow on the ledger
# (unexpected Finish result, Cancel failure, or a preflight_reject Create
# that xrpld accepted) as *stranded* and Cancel it once CancelAfter has
# passed, so the in-flight count stays bounded no matter what xrpld does.
# ---------------------------------------------------------------------------

FINISH_REMOVES = "finish_removes"
CANCEL_REMOVES = "cancel_removes"
PREFLIGHT_REJECT = "preflight_reject"
LIFECYCLES = frozenset({FINISH_REMOVES, CANCEL_REMOVES, PREFLIGHT_REJECT})


@dataclass(frozen=True)
class Category:
    """One wasm test case. Extend by adding entries to CATEGORIES.

    expected_create_result  submit-time engine_result of the EscrowCreate.
                            tesSUCCESS for lifecycles that create an escrow;
                            the reject code (e.g. temMALFORMED) for
                            preflight_reject.
    expected_finish_result  validated meta.TransactionResult of the
                            EscrowFinish. None for preflight_reject, which
                            never finishes.
    gas                     `Gas` on the EscrowFinish (1..GasLimit). The fee
                            is charged on this allowance, not on gas used
                            (1 drop/gas at the default GasPrice), so size it
                            at >= 10x the `gas=` that WASM_TIMING_FINISH
                            reports for the module in a short run, no more.
                            return_1 / return_0 use 30 -> 1_000.
    ephemeral_sender        A3 only: sign the EscrowCreate from a fresh,
                            unfunded wallet (Destination = the pool account)
                            instead of the pool account. Expected to be
                            rejected with terNO_ACCOUNT. preflight_reject.
    multi_finish            finish_removes only: keep submitting EscrowFinish
                            until it validates tesSUCCESS, capped at
                            MAX_FINISH_ATTEMPTS. For a stateful wasm whose
                            first finish rejects (writes data) and whose next
                            finish succeeds. expected_finish_result is the
                            TERMINAL result (tesSUCCESS); intermediate
                            tecBYTECODE_REJECTED is expected by construction.

    wasm vs template: a category ships EITHER a static `wasm` blob (fixed for
    every cycle) OR a `template` (escrow_patch.Template) that the driver
    patches per cycle with data drawn from the ledger indexes. `patch_params`
    passes per-role overrides to the patcher (e.g.
    {"account_id": {"valid_ratio": 0.0}}). Exactly one of wasm/template.

    accumulate_depth: opt-in for the accumulate_then_drain lifecycle
                      (--pattern accumulate). If set (an int N), the case can
                      be run as: create N live smart escrows on one owner, then
                      drain them, to probe cost/allocation that scales with
                      concurrent live smart-escrow count. None means the case
                      does not support accumulation (e.g. preflight_reject —
                      nothing accumulates). The base `lifecycle` still supplies
                      how each escrow is drained (finish, or finish+cancel).
    """
    name: str
    gas: int
    lifecycle: str
    wasm: Optional[bytes] = None
    template: Optional[Template] = None
    patch_params: dict = field(default_factory=dict)
    expected_create_result: str = "tesSUCCESS"
    expected_finish_result: Optional[str] = None
    ephemeral_sender: bool = False
    multi_finish: bool = False
    accumulate_depth: Optional[int] = None

    def __post_init__(self):
        if (self.wasm is None) == (self.template is None):
            raise ValueError(
                f"{self.name}: set exactly one of wasm= (static) or "
                f"template= (patched per cycle)"
            )
        if self.ephemeral_sender and self.wasm is None:
            raise ValueError(
                f"{self.name}: ephemeral_sender needs a static wasm"
            )
        if self.accumulate_depth is not None:
            if self.accumulate_depth < 1:
                raise ValueError(f"{self.name}: accumulate_depth must be >= 1")
            if self.lifecycle == PREFLIGHT_REJECT:
                raise ValueError(
                    f"{self.name}: preflight_reject cases never create an "
                    f"escrow, so they cannot accumulate"
                )
        if self.lifecycle not in LIFECYCLES:
            raise ValueError(
                f"{self.name}: unknown lifecycle {self.lifecycle!r}; "
                f"known: {sorted(LIFECYCLES)}"
            )
        if self.runs_finish and self.expected_finish_result is None:
            raise ValueError(
                f"{self.name}: lifecycle {self.lifecycle} runs a Finish and "
                f"needs expected_finish_result"
            )
        if not self.runs_finish and self.expected_finish_result is not None:
            raise ValueError(
                f"{self.name}: lifecycle {self.lifecycle} never finishes; "
                f"expected_finish_result must be None"
            )
        if self.lifecycle == PREFLIGHT_REJECT \
                and self.expected_create_result == "tesSUCCESS":
            raise ValueError(
                f"{self.name}: preflight_reject must declare the expected "
                f"reject code in expected_create_result"
            )
        if self.multi_finish and self.lifecycle != FINISH_REMOVES:
            raise ValueError(
                f"{self.name}: multi_finish only applies to finish_removes"
            )
        if self.ephemeral_sender and self.lifecycle != PREFLIGHT_REJECT:
            raise ValueError(
                f"{self.name}: ephemeral_sender only applies to "
                f"preflight_reject (no escrow is ever created)"
            )

    @property
    def runs_finish(self) -> bool:
        return self.lifecycle in (FINISH_REMOVES, CANCEL_REMOVES)

    @property
    def wasm_size(self) -> int:
        """Byte length of the Bytecode field (drives the EscrowCreate fee).
        Constant for template categories — a patch never changes length."""
        return len(self.wasm) if self.wasm is not None else len(self.template.body)

    def make_wasm(self, patch_ctx: Optional[PatchContext],
                  rng: random.Random) -> bytes:
        """The Bytecode bytes for one EscrowCreate: the static blob, or a
        freshly patched template."""
        if self.wasm is not None:
            return self.wasm
        if patch_ctx is None:
            raise RuntimeError(
                f"{self.name}: template category needs a PatchContext "
                f"(run_soak passes one; did populate_ledger.py run?)"
            )
        return patch_ctx.patch(self.template, rng, self.patch_params)


# EscrowFinish attempts before a multi_finish category is declared stranded.
MAX_FINISH_ATTEMPTS = 4

# Values marked PLACEHOLDER are best guesses; the pattern flags [unexpected]
# with the real code on the first live run, then we lock them in.
CATEGORIES: dict[str, Category] = {
    # -- A: preflight-reject group ------------------------------------------
    "unknown_imports": Category(
        name="unknown_imports",
        wasm=load_wasm("unknown_imports.wasm"),
        gas=1_000,                                   # unused (never finishes)
        lifecycle=PREFLIGHT_REJECT,
        expected_create_result="temINVALID_BYTECODE",   # confirmed live
    ),
    "disabled_instructions": Category(
        name="disabled_instructions",
        wasm=load_wasm("disabled_instructions.wasm"),
        gas=1_000,
        lifecycle=PREFLIGHT_REJECT,
        expected_create_result="temINVALID_BYTECODE",   # confirmed live
    ),
    "unfunded_account": Category(
        name="unfunded_account",
        wasm=load_wasm("return_1.wasm"),             # wasm irrelevant here
        gas=1_000,
        lifecycle=PREFLIGHT_REJECT,
        expected_create_result="terNO_ACCOUNT",
        ephemeral_sender=True,
    ),
    # -- B: cancel-removes group (Finish fails, Cancel cleans up) -----------
    "return_0": Category(
        name="return_0",
        wasm=load_wasm("return_0.wasm"),
        gas=1_000,
        lifecycle=CANCEL_REMOVES,
        expected_finish_result="tecBYTECODE_REJECTED",
    ),
    "oog_execute": Category(
        name="oog_execute",
        wasm=load_wasm("oog_execute.wasm"),
        gas=1_000,
        lifecycle=CANCEL_REMOVES,
        expected_finish_result="tecOUT_OF_GAS",         # confirmed live
    ),
    "oog_compile": Category(
        name="oog_compile",
        wasm=load_wasm("oog_compile.wasm"),
        gas=1_000,
        lifecycle=CANCEL_REMOVES,
        expected_finish_result="tecOUT_OF_GAS",  # confirmed live (translation-OOG, gas=0)
    ),
    "trap_div_by_zero": Category(
        name="trap_div_by_zero",
        wasm=load_wasm("trap_div_by_zero.wasm"),
        gas=1_000,
        lifecycle=CANCEL_REMOVES,
        expected_finish_result="tecFAILED_PROCESSING",  # confirmed live
    ),
    # -- C: finish-removes group (Finish succeeds, removes escrow) ----------
    "return_1": Category(
        name="return_1",
        wasm=load_wasm("return_1.wasm"),
        gas=1_000,
        lifecycle=FINISH_REMOVES,
        expected_finish_result="tesSUCCESS",
    ),
    "update_data_then_success": Category(
        name="update_data_then_success",
        wasm=load_wasm("update_data_then_success.wasm"),
        gas=5_000,
        lifecycle=FINISH_REMOVES,
        expected_finish_result="tesSUCCESS",         # terminal; 1st is reject
        multi_finish=True,
    ),
    "trace_heavy": Category(
        name="trace_heavy",
        wasm=load_wasm("trace_heavy.wasm"),
        gas=300_000,   # 5000 trace calls OOG'd at 200k; raised
        lifecycle=FINISH_REMOVES,
        expected_finish_result="tesSUCCESS",
    ),
    "oom_at_max_page": Category(
        name="oom_at_max_page",
        wasm=load_wasm("oom_at_max_page.wasm"),
        gas=1_000,
        lifecycle=FINISH_REMOVES,
        expected_finish_result="tesSUCCESS",
    ),
    # Single valid cache_le on a real pool account (loads an actual AccountRoot).
    # Retained; the cache_le_pattern _single presets cover the same shape with
    # the patched-array mechanism. (Old unknown_keylet retired — its miss path
    # is now cache_miss_single.)
    "known_keylet": Category(
        name="known_keylet",
        gas=6_000,
        lifecycle=FINISH_REMOVES,
        template=load_template("keylet_probe"),
        patch_params={"valid_ratio": 1.0},
        expected_finish_result="tesSUCCESS",
    ),
    # cache_le_pattern family: one template, five presets differing only in
    # iteration count and hit/miss ratio. Each Finish loops cache_le over
    # distinct keys (fresh reads); the storms probe whether per-call wall time
    # scales with gas and whether hit vs miss is disproportionate. Storm gas
    # ~150 * 5350 + overhead, under the 1M GasLimit. The two _single presets
    # subsume the old unknown_keylet.
    "cache_miss_single": Category(
        name="cache_miss_single",
        gas=8_000,
        lifecycle=FINISH_REMOVES,
        template=load_template("cache_le_pattern"),
        patch_params={"iterations": 1, "hit_ratio": 0.0},
        expected_finish_result="tesSUCCESS",
    ),
    "cache_hit_single": Category(
        name="cache_hit_single",
        gas=8_000,
        lifecycle=FINISH_REMOVES,
        template=load_template("cache_le_pattern"),
        patch_params={"iterations": 1, "hit_ratio": 1.0},
        expected_finish_result="tesSUCCESS",
    ),
    "cache_miss_storm": Category(
        name="cache_miss_storm",
        gas=900_000,
        lifecycle=FINISH_REMOVES,
        template=load_template("cache_le_pattern"),
        patch_params={"iterations": 150, "hit_ratio": 0.0},
        expected_finish_result="tesSUCCESS",
    ),
    "cache_hit_storm": Category(
        name="cache_hit_storm",
        gas=900_000,
        lifecycle=FINISH_REMOVES,
        template=load_template("cache_le_pattern"),
        patch_params={"iterations": 150, "hit_ratio": 1.0},
        expected_finish_result="tesSUCCESS",
    ),
    "cache_mixed_storm": Category(
        name="cache_mixed_storm",
        gas=900_000,
        lifecycle=FINISH_REMOVES,
        template=load_template("cache_le_pattern"),
        patch_params={"iterations": 150, "hit_ratio": 0.5},
        expected_finish_result="tesSUCCESS",
    ),
    # -- D: DoS-shaped finish-removes (time disproportionate to gas) --------
    "boundary_float": Category(
        name="boundary_float",
        wasm=load_wasm("boundary_float.wasm"),
        gas=50_000,
        lifecycle=FINISH_REMOVES,
        expected_finish_result="tesSUCCESS",
    ),
    "many_locals": Category(
        name="many_locals",
        wasm=load_wasm("many_locals.wasm"),
        gas=100_000,
        lifecycle=FINISH_REMOVES,
        expected_finish_result="tesSUCCESS",
    ),
    "home_le_field_bytecode": Category(
        name="home_le_field_bytecode",
        wasm=load_wasm("home_le_field_bytecode.wasm"),
        gas=20_000,
        lifecycle=FINISH_REMOVES,
        expected_finish_result="tesSUCCESS",
    ),
}


# ---------------------------------------------------------------------------
# accumulate_then_drain support (--pattern accumulate)
#
# Every finish_removes / cancel_removes case can be accumulated; preflight_reject
# cases never create an escrow, so they can't. Depth N is the default live count
# to reach per owner before draining (override at run time with
# --accumulate-depth). Declared in one place and applied with dataclasses.replace
# so each case still validates through Category.__post_init__.
# ---------------------------------------------------------------------------

ACCUMULATE_DEPTH_DEFAULT = 500
ACCUMULATE_DEPTH_OVERRIDES = {"return_1": 800}   # per-case tuning
# Single-call presets show no scaling signal, so they don't accumulate.
ACCUMULATE_EXCLUDE = {"cache_miss_single", "cache_hit_single"}

CATEGORIES = {
    name: (replace(cat, accumulate_depth=ACCUMULATE_DEPTH_OVERRIDES.get(
                name, ACCUMULATE_DEPTH_DEFAULT))
           if cat.lifecycle != PREFLIGHT_REJECT and name not in ACCUMULATE_EXCLUDE
           else cat)
    for name, cat in CATEGORIES.items()
}


# ---------------------------------------------------------------------------
# Submit result types
# ---------------------------------------------------------------------------

@dataclass
class SubmitResult:
    engine_result: str
    tx_hash: Optional[str]
    raw: dict = field(repr=False)


@dataclass
class CreateResult(SubmitResult):
    offer_sequence: int = 0          # Sequence used on the EscrowCreate
    cancel_after_ripple: int = 0     # ripple-epoch seconds


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class Worker:
    """One account, one identity, one local sequence counter.

    Server-side signing (secret + tx_json) is used because the Bytecode /
    Gas fields are not in xrpl-py's binary codec (4.5.0). xrpld flags it
    deprecated but it works. Sequence is managed locally and refreshed
    from ledger on any sequence-related submit error.

    Self-escrow (Account == Destination): keeps each worker as one
    independent identity, no pairing required.

    `fees` (LedgerFees) is needed for the Finish fee; pass the one read at
    startup, or leave None and it's fetched on first use.
    """

    def __init__(self, rpc_url: str, address: str, seed: str,
                 cancel_after_seconds: int = 30,
                 fees: Optional[LedgerFees] = None,
                 patch_ctx: Optional[PatchContext] = None):
        self.rpc_url = rpc_url
        self.address = address
        self.seed = seed
        self.cancel_after_seconds = cancel_after_seconds
        self._fees = fees
        self._patch_ctx = patch_ctx
        # Own rng per worker: template patching is thread-local, no shared state.
        self._rng = random.Random()
        self._seq: Optional[int] = None
        # Workers are intended to be owned by one thread, but guard anyway.
        self._lock = threading.Lock()

    @property
    def fees(self) -> LedgerFees:
        if self._fees is None:
            self._fees = get_ledger_fees(self.rpc_url)
        return self._fees

    # -- sequence cache -----------------------------------------------------

    def refresh_sequence(self) -> int:
        info = rpc(self.rpc_url, "account_info", {"account": self.address})
        self._seq = int(info["account_data"]["Sequence"])
        return self._seq

    def _ensure_seq(self) -> int:
        if self._seq is None:
            return self.refresh_sequence()
        return self._seq

    # -- private submit -----------------------------------------------------

    def _submit(self, tx: dict) -> SubmitResult:
        """Submit tx; refresh + retry once on any sequence-related error.
        Fail loud (RuntimeError) on a second consecutive sequence error.

        Sequence-related submit codes and their "is sequence consumed?"
        semantics. We refresh-from-ledger on every retry, so the consumed/
        not-consumed distinction is documentation, not control flow — but
        keep it in mind when deciding whether to bump local _seq after a
        non-tesSUCCESS path.

          tefPAST_SEQ   — submitted seq < account's current seq.
                          Tx NOT applied; seq NOT consumed.
          terPRE_SEQ    — submitted seq > account's current seq.
                          Tx not yet applied; seq NOT yet consumed.
          terQUEUED     — accepted into the TxQ for a future ledger.
                          Seq WILL be consumed when the queued tx applies.
                          (Caveat: at soak load this usually signals a
                          too-low fee, not a stale local seq. See NOTES.)
          tefMAX_LEDGER — LastLedgerSequence elapsed without inclusion.
                          Seq NOT consumed.

        Cache-bump rule: advance _seq on tesSUCCESS AND on tec*. tec
        results are charged-fee outcomes that DO consume the sequence
        (the tx applies to the ledger with no effect). Treating them as
        non-consuming caused a 1-step desync every time a tec hit, and a
        wave of tefPAST_SEQ refreshes on the next submit. tem*, tel*,
        tef* (except listed retry codes) and unhandled ter* don't apply,
        so seq isn't consumed and we leave _seq alone.
        """
        with self._lock:
            for attempt in (0, 1):
                res = rpc(
                    self.rpc_url, "submit",
                    {"secret": self.seed, "tx_json": tx},
                )
                er = res.get("engine_result", "<missing>")
                tx_hash = (res.get("tx_json") or {}).get("hash")
                if is_applied(er):
                    # tes and tec both apply to the ledger and consume seq.
                    self._seq = int(tx["Sequence"]) + 1
                    return SubmitResult(er, tx_hash, res)
                if er in SEQUENCE_RETRY_CODES:
                    if attempt == 0:
                        print(
                            f"[worker {self.address}] sequence error "
                            f"{er} at submitted_seq={tx['Sequence']} "
                            f"cached_seq={self._seq}; "
                            f"refreshing from account_info",
                            file=sys.stderr,
                        )
                        self.refresh_sequence()
                        print(
                            f"[worker {self.address}] refreshed cached_seq"
                            f" → {self._seq}; retrying once",
                            file=sys.stderr,
                        )
                        tx["Sequence"] = self._seq
                        continue
                    # Second consecutive seq error → fail loud. This is a
                    # test harness, not a recovery framework.
                    raise RuntimeError(
                        f"two consecutive sequence errors for "
                        f"{self.address}: latest engine_result={er} at "
                        f"submitted_seq={tx['Sequence']} "
                        f"(post-refresh cached_seq={self._seq}); aborting"
                    )
                # Non-sequence terminal code — return to caller as-is.
                return SubmitResult(er, tx_hash, res)

    # -- public actions -----------------------------------------------------

    def create(self, category: Category, amount_drops: int,
               open_ledger_fee: int,
               cancel_after: Optional[int] = None) -> CreateResult:
        """EscrowCreate { Bytecode, CancelAfter }. CancelAfter is
        mandatory with Bytecode (temBAD_EXPIRATION without it).

        cancel_after (ripple-epoch seconds) may be passed to reuse one value
        across a burst of creates (the accumulate pattern does this to avoid an
        RPC per create); default is validated close_time + cancel_after_seconds.

        CancelAfter is keyed off xrpld's *ledger close_time*, not the host
        wall-clock. xrpld checks parent_close_time vs CancelAfter at apply
        time, and standalone close_time can lag wall-clock by minutes-to-
        hours after idle gaps. Wall-clock-based CancelAfter caused 24-50%
        Cancel-rejection rates (tecNO_PERMISSION) in early runs.
        """
        if category.ephemeral_sender:
            return self._create_ephemeral(category, amount_drops, open_ledger_fee)
        seq = self._ensure_seq()
        if cancel_after is None:
            cancel_after = get_close_time(self.rpc_url) + self.cancel_after_seconds
        # Static blob, or a freshly patched template (unique wasm per cycle).
        wasm_bytes = category.make_wasm(self._patch_ctx, self._rng)
        fee = create_fee_drops(open_ledger_fee, len(wasm_bytes))
        tx = {
            "TransactionType": "EscrowCreate",
            "Account": self.address,
            "Destination": self.address,
            "Amount": str(amount_drops),
            "Bytecode": wasm_bytes.hex().upper(),
            "CancelAfter": cancel_after,
            "Sequence": seq,
            "Fee": str(fee),
        }
        sub = self._submit(tx)
        return CreateResult(
            engine_result=sub.engine_result,
            tx_hash=sub.tx_hash,
            raw=sub.raw,
            offer_sequence=int(tx["Sequence"]),
            cancel_after_ripple=cancel_after,
        )

    def _create_ephemeral(self, category: Category, amount_drops: int,
                          open_ledger_fee: int) -> CreateResult:
        """A3 unfunded_account: sign the EscrowCreate from a fresh, unfunded
        wallet, with the pool account as Destination. No sequence cache is
        touched (the account doesn't exist, so account_info would fail);
        Sequence is 1. Expected engine_result terNO_ACCOUNT — the account
        existence check fires after preflight but before anything applies, so
        no escrow is created and the pool account is unaffected.

        Signed CLIENT-side and submitted as a tx_blob: server-side signing
        (`submit`+secret) refuses a nonexistent source with srcActNotFound
        before it ever reaches the engine, so it can't exercise this path.
        A pre-signed blob skips that lookup; the account-existence check then
        fires in the engine and returns terNO_ACCOUNT."""
        from xrpl.wallet import Wallet
        from xrpl.core.binarycodec import encode, encode_for_signing
        from xrpl.core.keypairs import sign as kp_sign
        _inject_smart_escrow_fields()

        w = Wallet.create()   # throwaway identity, never funded
        cancel_after = get_close_time(self.rpc_url) + self.cancel_after_seconds
        fee = create_fee_drops(open_ledger_fee, len(category.wasm))
        tx = {
            "TransactionType": "EscrowCreate",
            "Account": w.address,
            "Destination": self.address,      # pool account
            "Amount": str(amount_drops),
            "Bytecode": category.wasm.hex().upper(),
            "CancelAfter": cancel_after,
            "Sequence": 1,
            "Fee": str(fee),
            "SigningPubKey": w.public_key,
        }
        tx["TxnSignature"] = kp_sign(bytes.fromhex(encode_for_signing(tx)),
                                     w.private_key)
        blob = encode(tx)
        res = rpc(self.rpc_url, "submit", {"tx_blob": blob})
        er = res.get("engine_result", "<missing>")
        tx_hash = (res.get("tx_json") or {}).get("hash") or res.get("tx_hash")
        return CreateResult(
            engine_result=er, tx_hash=tx_hash, raw=res,
            offer_sequence=1, cancel_after_ripple=cancel_after,
        )

    def finish(self, category: Category, offer_sequence: int,
               open_ledger_fee: int) -> SubmitResult:
        """EscrowFinish { OfferSequence, Gas }. Gas is mandatory when the
        escrow carries Bytecode; the fee scales with Gas * GasPrice."""
        seq = self._ensure_seq()
        fee = finish_fee_drops(open_ledger_fee, category.gas, self.fees)
        tx = {
            "TransactionType": "EscrowFinish",
            "Account": self.address,
            "Owner": self.address,
            "OfferSequence": offer_sequence,
            "Sequence": seq,
            "Fee": str(fee),
            "Gas": category.gas,
        }
        return self._submit(tx)

    def cancel(self, offer_sequence: int, open_ledger_fee: int) -> SubmitResult:
        seq = self._ensure_seq()
        fee = cancel_fee_drops(open_ledger_fee)
        tx = {
            "TransactionType": "EscrowCancel",
            "Account": self.address,
            "Owner": self.address,
            "OfferSequence": offer_sequence,
            "Sequence": seq,
            "Fee": str(fee),
        }
        return self._submit(tx)

    def wait_for_expiry(self, cancel_after_ripple: int,
                        poll_interval_s: float = 2.0,
                        max_wait_s: float = 600.0) -> None:
        """Wait until the validated ledger's close_time > CancelAfter.

        Polls `ledger` every poll_interval_s. CancelAfter was set in
        close_time space by Worker.create, so the comparison is apples
        to apples: as soon as close_time crosses CancelAfter, the next
        EscrowCancel will pass xrpld's permission check.

        Returns silently on timeout (max_wait_s); the subsequent Cancel
        will fail with tecNO_PERMISSION and the pattern's backstop will
        re-try later. max_wait_s exists only to prevent infinite hang if
        close_time stops advancing entirely (xrpld stuck).
        """
        deadline = time.monotonic() + max_wait_s
        # Initial sleep: skip cheap polls when we know close_time can't
        # possibly have advanced enough yet. cancel_after_seconds is the
        # earliest possible expiry; before that, polling is wasted RPCs.
        time.sleep(min(self.cancel_after_seconds, max_wait_s))
        while time.monotonic() < deadline:
            ct = get_close_time(self.rpc_url)
            if ct > cancel_after_ripple:
                return
            time.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# CSV log + time helpers used by every Pattern
# ---------------------------------------------------------------------------

CSV_HEADER = ["ts", "thread_id", "account", "category", "action",
              "tx_hash", "engine_result", "final_result"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def now_ripple() -> int:
    return ripple_time(time.time())


class CsvLog:
    """Thread-safe, line-flushed CSV writer. Cheap; safe to write from many threads."""

    def __init__(self, path: str):
        self._fh = open(path, "w", newline="", buffering=1)
        self._w = csv.writer(self._fh)
        self._w.writerow(CSV_HEADER)
        self._lock = threading.Lock()

    def write(self, row: list) -> None:
        with self._lock:
            self._w.writerow(row)
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# Pattern abstraction
#
# A Pattern is a scheduling strategy: given a slice of accounts, how do we
# stream Create / Finish / Cancel operations through them over time?
# Categories supply the *what* (which wasm, expected result, cleanup
# strategy); patterns supply the *when*.
#
# Adding a new pattern = one new module with a Pattern subclass.
# Adding a new wasm category = one new row in CATEGORIES.
# ---------------------------------------------------------------------------

@dataclass
class PatternContext:
    """Read-only shared context handed to every Pattern.run_thread call."""
    rpc_url: str
    categories: list[Category]
    amount_drops: int
    csv_log: CsvLog
    stop_event: threading.Event
    failure_event: threading.Event


class Pattern(ABC):
    """Abstract base for a soak scheduling strategy.

    Subclass contract:
      * Set `name` (used by the --pattern CLI flag).
      * Implement run_thread(): drive the given accounts until
        ctx.stop_event is set. On any fatal error, set ctx.failure_event
        and return — do NOT raise out of the thread.
    """

    name: str = "abstract"

    @abstractmethod
    def run_thread(self, thread_idx: int, accounts: list[Worker],
                   ctx: PatternContext) -> None: ...

    # ----- shared helpers (used by both serial and pipeline patterns) -----

    def submit_and_log(
        self,
        ctx: PatternContext,
        thread_idx: int,
        account_addr: str,
        category_name: str,
        action: str,
        submit_fn: Callable[[], SubmitResult],
        validate_timeout_s: float = 120.0,
    ) -> tuple[SubmitResult, str]:
        """Synchronous: submit, wait for validation if the tx was applied
        (tesSUCCESS or tec*), log a row. Returns (submit_result,
        final_result_str). final_result is "" when the tx was not applied
        (tem/tef/tel/ter) or no tx_hash came back.

        tec results are waited on too: a cancel_removes Finish is *expected*
        to come back tecBYTECODE_REJECTED, and we want the validated
        meta.TransactionResult in the CSV, not an empty column."""
        sub = submit_fn()
        final_result = ""
        if is_applied(sub.engine_result) and sub.tx_hash:
            try:
                tx_res = wait_for_validated(
                    ctx.rpc_url, sub.tx_hash, timeout_s=validate_timeout_s,
                )
                final_result = (tx_res.get("meta") or {}).get(
                    "TransactionResult", "<missing>"
                )
            except TimeoutError:
                final_result = "<TIMEOUT>"
        ctx.csv_log.write([
            now_iso(), thread_idx, account_addr, category_name, action,
            sub.tx_hash or "", sub.engine_result, final_result,
        ])
        return sub, final_result

    def log_row(
        self,
        ctx: PatternContext,
        thread_idx: int,
        account_addr: str,
        category_name: str,
        action: str,
        sub: SubmitResult,
        final_result: str,
    ) -> None:
        """For patterns that submit-then-validate-later: write a row directly."""
        ctx.csv_log.write([
            now_iso(), thread_idx, account_addr, category_name, action,
            sub.tx_hash or "", sub.engine_result, final_result,
        ])

    def flag_unexpected(
        self,
        thread_idx: int,
        account_addr: str,
        category: Category,
        action: str,
        expected: Optional[str],
        actual: str,
        tx_hash: Optional[str] = None,
    ) -> None:
        """An outcome differed from what the category declares. The CSV row
        already records what happened; this just makes it visible on
        stderr. Not fatal: the patterns clean up whatever was left behind
        and carry on, and a persistent stream of these is the operator's
        signal that xrpld is misbehaving."""
        print(
            f"[unexpected] thread={thread_idx} account={account_addr} "
            f"category={category.name} action={action} "
            f"expected={expected} got={actual} tx={tx_hash or '-'}",
            file=sys.stderr,
        )
