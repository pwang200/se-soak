#!/usr/bin/env bash
# Compile every wats/*.wat into a sibling .wasm with wat2wasm (from wabt).
#
# This is an OFFLINE build step. The driver (escrow_lib.load_wasm) only reads
# the pre-built .wasm files and never invokes wat2wasm, so wabt is a
# developer-machine dependency, not a runtime one. Commit the .wasm files.
#
# Usage:  ./wats/build.sh            (from anywhere; cds into wats/)
#
# Install wabt:  brew install wabt   |   apt install wabt
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v wat2wasm >/dev/null 2>&1; then
    echo "wat2wasm not found on PATH; install wabt (brew install wabt)" >&2
    exit 1
fi

# Regenerate any generated .wat first (gen_<name>.py -> <name>.wat).
shopt -s nullglob
for gen in gen_*.py; do
    echo "generating from $gen ..."
    python3 "$gen"
done

shopt -s nullglob
wats=( *.wat )
if [ "${#wats[@]}" -eq 0 ]; then
    echo "no .wat files in $(pwd)" >&2
    exit 1
fi

for wat in "${wats[@]}"; do
    wasm="${wat%.wat}.wasm"
    # No --debug-names: a name section would change the byte size (and so
    # the EscrowCreate fee) without changing behaviour.
    wat2wasm "$wat" -o "$wasm"
    printf '%-26s -> %-26s %5d bytes\n' "$wat" "$wasm" "$(wc -c < "$wasm")"
done

# Template categories: locate patch sentinels in the compiled .wasm and emit
# <name>.wasm.patchmap. No-op if there are no .patchspec.json files.
python3 build_patchmaps.py
