#!/usr/bin/env bash
# usage: scripts/run_case.sh <name> <duration_s> <run_soak.py args...>
# Runs sampler + soak into run_data/soak/<name>, stops the sampler when the soak ends.
set -u
NAME=$1; DUR=$2; shift 2
REPO=$(cd "$(dirname "$0")/.." && pwd)
S=${SOAK_RUN_DATA:-$HOME/rippled/run_data}/soak
D=$S/$NAME
mkdir -p "$D"
cd "$REPO"
.venv/bin/python -u scripts/rippled_memory_sampler.py --interval 1 \
    --output "$D/xrpld_memory.csv" > "$D/sampler.log" 2>&1 &
SP=$!
.venv/bin/python -u run_soak.py --accounts "$S/test_accounts.json" --duration "$DUR" \
    --run-dir "$D" "$@" > "$D/driver.out" 2>&1
RC=$?
kill $SP 2>/dev/null
echo "rc=$RC"
tail -3 "$D/driver.out" | cut -c1-200
echo "--- unexpected / errors:"
grep -c -i "unexpected" "$D/driver.out"
grep -i "unexpected\|traceback\|error" "$D/driver.out" | head -8 | cut -c1-220
echo "--- result counts (action result):"
awk -F, 'NR>1{c[$4" "$5" "$8]++} END{for(k in c)print c[k],k}' "$D/soak.csv" | sort -rn
