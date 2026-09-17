# runs/ — everything the soak driver reads or writes at run time

Nothing in this folder is source. It is gitignored except this file.

| path | lifetime | produced by | read by |
|---|---|---|---|
| `test_accounts.json` | **persistent** until you re-run setup | `setup_accounts.py` | `run_soak.py`, `check_state.py` |
| `<UTC stamp>/` | **one soak run; safe to delete** | `run_soak.py --run-dir` (default `runs/<stamp>`) | you, post-run analysis |
| `<UTC stamp>/soak.csv` | | `run_soak.py` | |
| `<UTC stamp>/run_soak.log` | | `run_soak.py` (tee of its stdout + stderr) | |
| `<UTC stamp>/xrpld_memory.csv` | | `scripts/rippled_memory_sampler.py --output` | |
| `<UTC stamp>/sampler.err` | | sampler heartbeats, if you redirect them there | |
| `<UTC stamp>/check_state.txt` | | `check_state.py > ...`, if you redirect | |
| `xrpld_memory_<stamp>.csv` | disposable | sampler started without `--output` | |
| `archive/<date>/` | keep | hand-moved results worth keeping | |

`test_accounts.json` can be large (the 1M-account version is 141 MB). Never
commit it; regenerate with `setup_accounts.py`.

## One run, one folder

The sampler usually starts before `run_soak.py`, so pick the folder up
front and hand it to everything:

```
RUN=runs/$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$RUN"

python3 scripts/rippled_memory_sampler.py --interval 30 \
        --output "$RUN/xrpld_memory.csv" 2> "$RUN/sampler.err" &

python3 run_soak.py --run-dir "$RUN" --threads 10 --tps 5 --duration 1800

python3 check_state.py > "$RUN/check_state.txt"     # any time, read-only
```

`run_soak.py` without `--run-dir` creates `runs/<stamp>/` itself and prints
the path in its banner.

## Cleaning up

- `rm -rf runs/<stamp>` removes one run completely.
- Delete `test_accounts.json` only if you intend to re-fund accounts; the
  escrows those accounts own on the ledger are not affected either way.
- `archive/` is the exception: it holds results someone chose to keep
  (`archive/2026-05-15/` = the four May dry runs, their logs, and the two
  memory-sampler traces, including the 120 MB → 3.4 GB RSS climb).
