#!/usr/bin/env python3
"""Turn <name>.patchspec.json + compiled <name>.wasm into <name>.wasm.patchmap.

A template category ships a .wat with sentinel byte patterns inside its
(data ...) sections and a .patchspec.json declaring each sentinel's role. This
step (run by build.sh after wat2wasm) locates each sentinel in the compiled
.wasm and records its byte offset, so the runtime patcher can splice new bytes
in without parsing wasm.

Output <name>.wasm.patchmap:
  {"template": name, "length": <wasm size>,
   "slots": [{"name","offset","length","role","params"}]}

Fails loud if a sentinel is absent or appears more than once (ambiguous).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build_one(spec_path: Path) -> str:
    spec = json.loads(spec_path.read_text())
    name = spec["template"]
    wasm_path = HERE / f"{name}.wasm"
    if not wasm_path.exists():
        raise SystemExit(f"{name}: {wasm_path} not built yet")
    wasm = wasm_path.read_bytes()
    slots = []
    for s in spec["slots"]:
        sentinel = bytes.fromhex(s["sentinel"])
        first = wasm.find(sentinel)
        if first < 0:
            raise SystemExit(f"{name}: sentinel for slot {s['name']!r} not found in wasm")
        if wasm.find(sentinel, first + 1) >= 0:
            raise SystemExit(f"{name}: sentinel for slot {s['name']!r} appears more than once")
        # A slot may patch a region larger than its locating sentinel (e.g. a
        # fixed-capacity array whose first bytes are the sentinel); declare it
        # with "length". Default is the sentinel's own length.
        length = s.get("length", len(sentinel))
        slots.append({
            "name": s["name"],
            "offset": first,
            "length": length,
            "role": s["role"],
            "params": s.get("params", {}),
        })
    out = {"template": name, "length": len(wasm), "slots": slots}
    (HERE / f"{name}.wasm.patchmap").write_text(json.dumps(out, indent=2))
    return f"{name}: {len(slots)} slot(s) -> {[ (s['name'], s['offset']) for s in slots ]}"


def main():
    specs = sorted(HERE.glob("*.patchspec.json"))
    if not specs:
        return
    for spec in specs:
        print("  patchmap", build_one(spec))


if __name__ == "__main__":
    main()
