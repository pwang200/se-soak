#!/usr/bin/env python3
"""analyze_leak.py — turn a soak run's artifacts into a leak verdict.

Reads one run directory and joins three files it already contains:
  xrpld_memory.csv   RSS/ledger_seq samples (scripts/rippled_memory_sampler.py)
  run_soak.log       [accumulate] phase markers, if an accumulate run
  soak.csv           per-tx rows (to count finishes over time)
and reports whether xrpld RSS is flat (no leak) or growing (leak) once the node
reaches steady state.

Why a verdict needs care (read before trusting the number):
  - Standalone xrpld with online_delete=512 purges old ledgers only after it has
    ~1024 ledgers AND the SHAMapStore rotation thread fires; before that it keeps
    FULL history, so RSS rises with ledger count even for empty ledgers (~25 KB /
    ledger observed). That early ramp is NOT a leak. Pass --warmup-ledgers to drop
    everything before rotation engages (watch complete_ledgers prune, or the log's
    "SHAMapStore ... finished rotation"); the slope AFTER that is the real signal.
  - If rotation never engages (history unbounded), RSS growth is dominated by
    ledger count, not a wasm leak. Compare against a no-wasm / empty-ledger control
    at the same ledger rate: the leak is the DIFFERENCE in bytes-per-ledger, not the
    raw slope. See the "Leak soak" section of flow.md.

The sampler usually keeps running after the load stops (an idle tail), and an idle
node releases memory, which would drag the slope negative. So by default the
analysis is CLIPPED to the load window: from <run-dir>/events.csv (supervisor runs:
first segment_start .. idle_tail_start / last segment_end) if present, else from the
first to the last row of soak.csv. Use --no-clip to analyze every sample.
A supervisor run dir has no top-level soak.csv; its seg*/soak.csv files are used.

Usage:
    python3 analyze_leak.py runs/<run-dir>
    python3 analyze_leak.py runs/<run-dir> --warmup-ledgers 1100   # drop pre-rotation
    python3 analyze_leak.py --mem a.csv --log b.log --soak c.csv --windows 6
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def _lsq(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least-squares slope, intercept for ys ~ slope*xs + b."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return slope, my - slope * mx


def load_mem(path: Path) -> list[tuple[float, int, int | None]]:
    """[(ts_epoch, rss_bytes, ledger_seq or None)] sorted by time, rows with RSS."""
    out = []
    for r in csv.DictReader(open(path)):
        if not r.get("rss_kb"):
            continue
        seq = r.get("ledger_seq")
        out.append((float(r["ts_epoch"]), int(r["rss_kb"]) * 1024,
                    int(seq) if seq else None))
    out.sort()
    return out


def count_finishes_over_time(path: Path) -> list[float]:
    """Sorted epoch timestamps of successful finishes (finish or acc_finish)."""
    ts = []
    rows = list(csv.reader(open(path)))
    if not rows:
        return ts
    hdr = rows[0]
    ti = {c: i for i, c in enumerate(hdr)}
    for r in rows[1:]:
        if r[ti["action"]] in ("finish", "acc_finish") and \
                r[ti.get("final_result", ti["engine_result"])] == "tesSUCCESS":
            try:
                ts.append(datetime.fromisoformat(r[ti["ts"]]).timestamp())
            except Exception:
                pass
    ts.sort()
    return ts


def drain_baselines(log_path: Path, mem: list) -> list[tuple[int, float, int]]:
    """For an accumulate run: (ledger_or_idx, ts, rss) at each drain_done —
    the post-drain baseline. Creep across these = per-burst leak."""
    mk = re.compile(r"ts=(\S+).*phase=drain_done")
    out = []
    for l in open(log_path):
        if "[accumulate]" not in l or "drain_done" not in l:
            continue
        m = mk.search(l)
        if not m:
            continue
        t = datetime.fromisoformat(m.group(1)).timestamp()
        rss = min(mem, key=lambda x: abs(x[0] - t))[1]
        out.append((len(out), t, rss))
    return out


def _last_line(path: Path) -> str:
    """Last non-empty line of a (possibly huge) text file without reading it all."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = min(size, 65536)
        f.seek(size - block)
        lines = f.read().decode("utf-8", "replace").splitlines()
    return next((l for l in reversed(lines) if l.strip()), "")


def soak_span(paths: list[Path]) -> tuple[float, float] | None:
    """(first, last) row timestamp across soak.csv files, or None."""
    lo, hi = None, None
    for p in paths:
        try:
            with open(p) as f:
                f.readline()
                first = f.readline()
            t0 = datetime.fromisoformat(first.split(",")[0]).timestamp()
            t1 = datetime.fromisoformat(_last_line(p).split(",")[0]).timestamp()
        except Exception:
            continue
        lo = t0 if lo is None else min(lo, t0)
        hi = t1 if hi is None else max(hi, t1)
    return (lo, hi) if lo is not None else None


def events_window(path: Path) -> tuple[float, float] | None:
    """Load window from a supervisor events.csv: first segment_start to the
    idle_tail_start (or the last segment_end)."""
    if not path.exists():
        return None
    starts, ends, tail = [], [], None
    for r in csv.DictReader(open(path)):
        t = float(r["ts_epoch"])
        if r["event"] == "segment_start":
            starts.append(t)
        elif r["event"] == "segment_end":
            ends.append(t)
        elif r["event"] == "idle_tail_start" and tail is None:
            tail = t
    if not starts:
        return None
    end = tail if tail is not None else (max(ends) if ends else None)
    return (min(starts), end) if end else None


MB = 1048576


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", nargs="?", help="Run folder (expects xrpld_memory.csv, "
                    "run_soak.log, soak.csv). Or pass --mem/--log/--soak.")
    ap.add_argument("--mem")
    ap.add_argument("--log")
    ap.add_argument("--soak")
    ap.add_argument("--warmup-ledgers", type=int, default=0,
                    help="Drop samples at/below this ledger_seq (pre-rotation ramp).")
    ap.add_argument("--warmup-s", type=float, default=0.0,
                    help="Drop the first N seconds (alternative to --warmup-ledgers).")
    ap.add_argument("--no-clip", action="store_true",
                    help="Do not clip samples to the load window (events.csv / "
                         "soak.csv span); analyze everything the sampler recorded.")
    ap.add_argument("--windows", type=int, default=5,
                    help="Split the steady region into N windows to see if the slope "
                         "is decelerating (plateau) or steady (leak).")
    args = ap.parse_args()

    if args.run_dir:
        d = Path(args.run_dir)
        mem_p = Path(args.mem) if args.mem else d / "xrpld_memory.csv"
        log_p = Path(args.log) if args.log else d / "run_soak.log"
        soak_p = Path(args.soak) if args.soak else d / "soak.csv"
        events_p = d / "events.csv"
    else:
        mem_p, log_p, soak_p = Path(args.mem), Path(args.log), Path(args.soak)
        events_p = mem_p.parent / "events.csv"
    # Supervisor run dir: no top-level soak.csv, the segments each have one.
    soak_paths = [soak_p] if soak_p.exists() else sorted(soak_p.parent.glob("seg*/soak.csv"))

    if not mem_p.exists():
        raise SystemExit(f"no memory CSV at {mem_p} (run scripts/"
                         f"rippled_memory_sampler.py --output {mem_p})")
    mem = load_mem(mem_p)
    if len(mem) < 3:
        raise SystemExit(f"only {len(mem)} RSS samples; need a longer run")

    if not args.no_clip:
        win = events_window(events_p) or soak_span(soak_paths)
        if win:
            kept = [m for m in mem if win[0] <= m[0] <= win[1] + 5]
            fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")
            print(f"clipped to load window {fmt(win[0])}..{fmt(win[1])} "
                  f"({len(mem) - len(kept)} idle/other samples dropped; --no-clip to disable)")
            mem = kept
            if len(mem) < 3:
                raise SystemExit("load window contains <3 RSS samples")

    t0 = mem[0][0]
    # Apply warmup cutoff.
    cut = [m for m in mem
           if (args.warmup_ledgers == 0 or (m[2] or 0) > args.warmup_ledgers)
           and (m[0] - t0) >= args.warmup_s]
    if len(cut) < 3:
        raise SystemExit("warmup cutoff left <3 samples; lower it")

    span_s = mem[-1][0] - mem[0][0]
    peak = max(m[1] for m in mem)
    lo = min(m[1] for m in mem)
    seqs = [m[2] for m in mem if m[2] is not None]
    print(f"run: {mem_p.parent.name}")
    print(f"samples: {len(mem)}  span: {span_s/60:.1f} min  "
          f"ledgers: {seqs[0] if seqs else '?'}..{seqs[-1] if seqs else '?'}")
    print(f"RSS: start {mem[0][1]/MB:.1f}  min {lo/MB:.1f}  "
          f"peak {peak/MB:.1f}  end {mem[-1][1]/MB:.1f} MB")

    # Finishes.
    fin_ts = sorted(t for p in soak_paths for t in count_finishes_over_time(p))
    nfin = len(fin_ts)

    # Steady-region slopes (after warmup).
    ts = [m[0] for m in cut]
    rss = [m[1] for m in cut]
    slope_s, _ = _lsq([t - ts[0] for t in ts], rss)      # bytes/sec
    print(f"\nsteady region (after warmup): {len(cut)} samples, "
          f"{(ts[-1]-ts[0])/60:.1f} min")
    print(f"  RSS slope: {slope_s*3600/MB:+.2f} MB/hour  "
          f"({slope_s:+.0f} bytes/sec)")
    if nfin:
        # finishes within the steady window
        fin_win = [t for t in fin_ts if ts[0] <= t <= ts[-1]]
        if fin_win:
            per_fin = slope_s * (ts[-1] - ts[0]) / len(fin_win)
            print(f"  finishes in window: {len(fin_win)}  "
                  f"-> {per_fin:+.0f} bytes/finish")
    if seqs:
        seq_cut = [(m[2], m[1]) for m in cut if m[2] is not None]
        if len(seq_cut) >= 2:
            sl_ledger, _ = _lsq([s for s, _ in seq_cut], [r for _, r in seq_cut])
            print(f"  RSS slope: {sl_ledger:+.0f} bytes/ledger")

    # Windowed slopes — decelerating slope => plateau (no leak); steady => leak.
    print(f"\nper-window RSS slope (decelerating = plateau/no-leak, "
          f"steady = leak), {args.windows} windows:")
    w = len(cut) // args.windows or 1
    for i in range(args.windows):
        seg = cut[i*w: (i+1)*w] if i < args.windows-1 else cut[i*w:]
        if len(seg) < 2:
            continue
        s, _ = _lsq([m[0]-seg[0][0] for m in seg], [m[1] for m in seg])
        print(f"  window {i+1}: {s*3600/MB:+.2f} MB/hour  "
              f"(RSS {seg[0][1]/MB:.1f}->{seg[-1][1]/MB:.1f} MB)")

    # Accumulate drain-to-baseline creep.
    if log_p.exists():
        dbs = drain_baselines(log_p, mem)
        if dbs:
            print(f"\naccumulate post-drain baselines (creep across bursts):")
            base0 = dbs[0][2]
            for i, (_, t, rss_b) in enumerate(dbs):
                print(f"  burst {i+1}: t={(t-t0)/60:4.1f}min  RSS {rss_b/MB:7.1f} MB"
                      f"  ({(rss_b-base0)/MB:+.1f} vs first)")
            print("  NOTE: on a full-history node this creep is mostly retained "
                  "ledgers, not a leak — needs rotation or a control (see docstring).")

    print("\nverdict hint: a near-zero MB/hour that does NOT shrink window-over-"
          "window after the rotation plateau is the leak signal; a decelerating "
          "slope is the cache/history filling and settling.")


if __name__ == "__main__":
    main()
