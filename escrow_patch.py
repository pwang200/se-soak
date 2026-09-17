"""Runtime wasm patching for template-based categories.

Some soak categories don't ship a fixed wasm blob; they ship a *template*
.wasm (built by wats/build.sh) plus a *patchmap* (byte ranges tagged with a
role), and the driver rewrites those ranges with fresh data drawn from the
ledger indexes on every EscrowCreate. Two reasons:

  1. Diversity / no dedup — a unique wasm per cycle defeats any caching keyed
     on identical Bytecode, so we measure the real per-tx cost.
  2. Real ledger access — an account_id or keylet role can point at a genuine
     populated object (expensive "load a real object" path) or a fabricated
     one (the not-found path), a valid/invalid mix chosen per category.

A slot's length never changes, so the module stays structurally valid (only
`(data ...)` bytes move) and the EscrowCreate fee (which scales with byte
count) is stable across cycles.

Roles (extend by adding to Roles._ROLES):
  account_id     20 bytes. A real pool account with probability `valid_ratio`
                 (default 0.5), else random bytes (a non-existent account).
  keylet         32 bytes. The real keylet of a random populated object of
                 `obj_type` (a populate_ledger index file name).
  opaque_random  the slot's own length in random bytes.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from xrpl.core.addresscodec import decode_classic_address

WATS_DIR = Path(__file__).resolve().parent / "wats"
POPULATED_DIR = Path(__file__).resolve().parent / "populated"


@dataclass(frozen=True)
class Template:
    name: str
    body: bytes                    # compiled template wasm
    slots: tuple[dict, ...]        # each: name, offset, length, role, params


def load_template(name: str, wats_dir: Path = WATS_DIR) -> Template:
    body = (wats_dir / f"{name}.wasm").read_bytes()
    pm = json.loads((wats_dir / f"{name}.wasm.patchmap").read_text())
    if pm.get("length") != len(body):
        raise ValueError(f"{name}: patchmap length {pm.get('length')} != "
                         f"wasm length {len(body)} (rebuild wats)")
    return Template(name=name, body=body, slots=tuple(pm["slots"]))


class PatchContext:
    """Data the role handlers draw from, loaded once at driver startup:
    real pool account ids (20-byte) and the populated object keylets
    (32-byte) per type. Thread-safe: patch() takes the caller's rng, so no
    shared mutable state."""

    def __init__(self, account_ids: list[bytes],
                 indexes: dict[str, list[bytes]]):
        self.account_ids = account_ids
        self.indexes = indexes

    # ----- constructors -----

    @classmethod
    def load(cls, accounts_file: str,
             populated_dir: Path = POPULATED_DIR) -> "PatchContext":
        with open(accounts_file) as f:
            entries = json.load(f)
        account_ids = [decode_classic_address(e["address"]) for e in entries]
        indexes: dict[str, list[bytes]] = {}
        if populated_dir.is_dir():
            for p in populated_dir.glob("*.json"):
                recs = json.loads(p.read_text())
                keylets = [bytes.fromhex(r["index"]) for r in recs if r.get("index")]
                if keylets:
                    indexes[p.stem] = keylets
        return cls(account_ids, indexes)

    # ----- role handlers -----

    def _role_account_id(self, slot: dict, params: dict,
                         rng: random.Random) -> bytes:
        valid_ratio = params.get("valid_ratio", 0.5)
        if self.account_ids and rng.random() < valid_ratio:
            return rng.choice(self.account_ids)
        return rng.randbytes(20)

    def _role_keylet(self, slot: dict, params: dict,
                     rng: random.Random) -> bytes:
        obj_type = params.get("obj_type")
        pool = self.indexes.get(obj_type or "")
        if not pool:
            raise RuntimeError(
                f"keylet role needs populated index {obj_type!r}; "
                f"available: {sorted(self.indexes)}. Run populate_ledger.py."
            )
        return rng.choice(pool)

    def _role_opaque_random(self, slot: dict, params: dict,
                            rng: random.Random) -> bytes:
        return rng.randbytes(slot["length"])

    @property
    def _roles(self) -> dict[str, Callable]:
        return {
            "account_id": self._role_account_id,
            "keylet": self._role_keylet,
            "opaque_random": self._role_opaque_random,
        }

    # ----- patch -----

    def patch(self, template: Template, rng: random.Random,
              category_params: Optional[dict] = None) -> bytes:
        """Return a fresh wasm blob: template.body with every slot rewritten.
        category_params maps a role name to overrides merged over the slot's
        own params (e.g. {"account_id": {"valid_ratio": 0.0}})."""
        category_params = category_params or {}
        buf = bytearray(template.body)
        for slot in template.slots:
            role = slot["role"]
            handler = self._roles.get(role)
            if handler is None:
                raise RuntimeError(f"{template.name}: unknown patch role {role!r}")
            params = {**slot.get("params", {}), **category_params.get(role, {})}
            data = handler(slot, params, rng)
            if len(data) != slot["length"]:
                raise RuntimeError(
                    f"{template.name} slot {slot['name']}: role {role} produced "
                    f"{len(data)} bytes, slot is {slot['length']}"
                )
            buf[slot["offset"]:slot["offset"] + slot["length"]] = data
        return bytes(buf)
