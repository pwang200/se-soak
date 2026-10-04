#!/usr/bin/env python3
"""Long-run supervisor for the se-soak leak runs.

Keeps ledger_ticker + memory sampler alive, runs bounded soak SEGMENTS back to back
(each drains its escrows on exit), health-checks xrpld, and writes STATUS.txt so a
human coming back after hours/days can see what happened in one file.

  python3 scripts/soak_supervisor.py --name long1 --hours 6 --segment-min 30 --schedule rehearsal
Stop early:  touch <run_root>/STOP   (finishes after the current segment)  or SIGTERM.
Never restarts xrpld (a restart = fresh chain = a finding); it stops and says so.
Paths: runs go to $SOAK_RUN_DATA/soak/<name>/ (default ~/rippled/run_data); accounts are
read from $SOAK_RUN_DATA/soak/test_accounts.json; the rotation guard reads
$SOAK_RUN_DATA/db/state.db. Run it with the repo venv python, detached, e.g.
  setsid nohup systemd-inhibit --what=sleep:idle --who=se-soak --why="soak" \\
      .venv/bin/python -u scripts/soak_supervisor.py --name long1 ... &
"""
import argparse, csv, json, os, shutil, signal, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

import psutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
# Where runs, accounts and the xrpld db live (set SOAK_RUN_DATA to override).
RUN_DATA = os.environ.get("SOAK_RUN_DATA", os.path.expanduser("~/rippled/run_data"))
ACCOUNTS = f"{RUN_DATA}/soak/test_accounts.json"
RPC = "http://127.0.0.1:5005/"
PROC_NAMES = ("xrpld-main", "xrpld")   # Linux main thread is xrpld-main

P = ["--pattern", "pipeline", "--accounts-per-thread", "50", "--in-flight-per-account", "2", "--threads", "8"]
CASES = {
    "A_return1":   P + ["--categories", "return_1"],
    "B_cancel":    P + ["--categories", "return_0,oog_execute,oog_compile,trap_div_by_zero"],
    "C_retry":     P + ["--categories", "update_data_then_success"],
    "D_accum":     ["--pattern", "accumulate", "--categories", "return_1,chain_full_footprint", "--threads", "4"],
    "E_heavy":     P + ["--categories", "chain_full_footprint,inst_data,trace_heavy"],
    "H_chain":     P + ["--categories", "chain_full_footprint"],
    "H_inst":      P + ["--categories", "inst_data"],
    "H_trace":     P + ["--categories", "trace_heavy"],
}
IDLE_MIN = 10   # length of an "IDLE" schedule entry (no load; sampler keeps recording)
SCHEDULES = {
    # lifecycles 1,2,4,5 + baseline; 3 (preflight_reject) excluded until issue_about_3.md is resolved
    "rehearsal": ["A_return1", "B_cancel", "C_retry", "D_accum", "E_heavy"] * 2 + ["A_return1", "D_accum"],
    "steady":    ["A_return1"],
    "heavysplit2": ["H_inst", "IDLE", "H_trace", "IDLE"],
    "heavysplit": ["H_chain", "IDLE", "H_inst", "IDLE", "H_trace", "IDLE"],
}

MIN_BALANCE_XRP = 1000
MIN_FREE_GB = 50
MAX_CONSEC_FAIL = 3


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rpc(method, params=None, timeout=10):
    body = {"method": method, "params": [params or {}]}
    req = urllib.request.Request(RPC, json.dumps(body).encode())
    return json.load(urllib.request.urlopen(req, timeout=timeout))["result"]


def stale_rotation_state():
    """online_delete state survives a `--start`: LastRotated from an older chain > current seq
    means rotation will not fire until seq passes it (+online_delete). Return a message or None."""
    import sqlite3
    try:
        last = sqlite3.connect(f"{RUN_DATA}/db/state.db").execute("select LastRotatedLedger from DbState").fetchone()[0]
        seq = rpc("server_info")["info"]["validated_ledger"]["seq"]
    except Exception as e:
        return f"cannot verify rotation state ({e!r}); refusing to start blind"
    if last > seq:
        return (f"stale rotation state: state.db LastRotated={last} > current ledger {seq}; "
                f"rotation would not engage until ledger {last+512}. Stop xrpld, wipe {RUN_DATA}/db, restart, refund accounts.")
    return None


def find_xrpld():
    for p in psutil.process_iter(["pid", "name"]):
        if p.info["name"] in PROC_NAMES:
            return p
    return None


class Sup:
    def __init__(self, a):
        self.a = a
        self.root = f"{RUN_DATA}/soak/{a.name}"
        os.makedirs(self.root, exist_ok=True)
        self.logf = open(f"{self.root}/supervisor.log", "a", buffering=1)
        self.ticker = self.sampler = self.driver = None
        self.state = "STARTING"
        self.detail = ""
        self.seg_i = 0
        self.seg_name = ""
        self.seg_t0 = 0
        self.fails = 0
        self.consec_fail = 0
        self.restarts = {"ticker": 0, "sampler": 0}
        self.segments = []
        self.t0 = time.time()
        self.xpid = None
        self.last_seq = (0, time.time())
        self.stopping = False
        self.events = open(f"{self.root}/events.csv", "a", buffering=1)
        if self.events.tell() == 0:
            self.events.write("ts_epoch,ts_iso,event,detail\n")
        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)

    def _sig(self, *_):
        self.stopping = True
        self.log("signal received, stopping")

    def log(self, msg):
        self.logf.write(f"{now()} {msg}\n")

    def event(self, ev, detail=""):
        self.events.write(f"{time.time():.1f},{now()},{ev},{detail}\n")
        self.log(f"EVENT {ev} {detail}")

    # ---- child processes -------------------------------------------------
    def start_ticker(self):
        out = open(f"{self.root}/ticker.log", "a")
        self.ticker = subprocess.Popen([PY, "-u", f"{REPO}/ledger_ticker.py", "--interval", "2"],
                                       stdout=out, stderr=out, cwd=REPO)

    def start_sampler(self):
        out = open(f"{self.root}/sampler.log", "a")
        self.sampler = subprocess.Popen(
            [PY, "-u", f"{REPO}/scripts/rippled_memory_sampler.py", "--interval", "1",
             "--output", f"{self.root}/xrpld_memory.csv"],
            stdout=out, stderr=out, cwd=REPO)

    def keepalive(self):
        for name, attr, starter in (("ticker", "ticker", self.start_ticker),
                                    ("sampler", "sampler", self.start_sampler)):
            p = getattr(self, attr)
            if p is None or p.poll() is not None:
                if p is not None:
                    self.restarts[name] += 1
                    self.event(f"{name}_restart", f"rc={p.returncode}")
                starter()

    # ---- health ------------------------------------------------------------
    def health(self):
        """Return None if ok, else a fatal reason string."""
        x = find_xrpld()
        if x is None:
            return "xrpld process is gone"
        if self.xpid is None:
            self.xpid = x.pid
        elif x.pid != self.xpid:
            return f"xrpld pid changed {self.xpid}->{x.pid} (restarted = fresh chain)"
        try:
            info = rpc("server_info")["info"]
        except Exception as e:
            self.rpc_fail = getattr(self, "rpc_fail", 0) + 1
            if self.rpc_fail >= 6:  # ~3 min
                return f"xrpld RPC unresponsive ({e})"
            return None
        self.rpc_fail = 0
        seq = info.get("validated_ledger", {}).get("seq", 0)
        if seq != self.last_seq[0]:
            self.last_seq = (seq, time.time())
        elif time.time() - self.last_seq[1] > 180 and self.ticker and self.ticker.poll() is None:
            self.event("ledger_stalled", f"seq={seq}; restarting ticker")
            self.ticker.kill()
        self.info = info
        earliest = str(info.get("complete_ledgers", "")).split("-")[0].split(",")[0]
        if seq >= 1300 and earliest == "2":
            if not getattr(self, "rot_warned", False):
                self.event("WARN_rotation_not_engaged", f"seq={seq} complete_ledgers={info.get('complete_ledgers')}")
            self.rot_warned = True
            self.detail = "WARN: online_delete rotation not engaged (history unbounded)"
        else:
            self.rot_warned = False
            if self.detail.startswith("WARN: online_delete"):
                self.detail = ""
        free = shutil.disk_usage(RUN_DATA).free / 1e9
        self.free_gb = free
        if free < MIN_FREE_GB:
            return f"disk free {free:.0f} GB < {MIN_FREE_GB} GB"
        return None

    def min_balance(self):
        try:
            accts = json.load(open(ACCOUNTS))
            lo = min(int(rpc("account_info", {"account": a["address"]})["account_data"]["Balance"])
                     for a in accts[:400]) / 1e6
            return lo
        except Exception as e:
            self.log(f"balance check failed: {e}")
            return None

    # ---- status --------------------------------------------------------------
    def write_status(self, final=False):
        x = find_xrpld()
        rss = thr = "?"
        if x:
            try:
                rss = f"{x.memory_info().rss/2**20:.0f} MB"
                thr = x.num_threads()
            except Exception:
                pass
        info = getattr(self, "info", {})
        lines = [
            f"STATE: {self.state} {self.detail}",
            f"updated: {now()}   started: {datetime.fromtimestamp(self.t0, timezone.utc):%Y-%m-%dT%H:%M:%SZ}   "
            f"elapsed: {(time.time()-self.t0)/3600:.2f} h of {self.a.hours} h",
            f"segment: #{self.seg_i} (schedule cycles every {len(self.plan)}) {self.seg_name}  failed_segments={self.fails}  consec_fail={self.consec_fail}",
            f"xrpld pid={self.xpid} rss={rss} threads={thr} complete_ledgers={info.get('complete_ledgers')} "
            f"free_disk={getattr(self,'free_gb',0):.0f} GB",
            f"restarts: {self.restarts}",
            "", "segments:"]
        for s in self.segments:
            lines.append("  " + s)
        tmp = f"{self.root}/STATUS.txt.tmp"
        open(tmp, "w").write("\n".join(lines) + "\n")
        os.replace(tmp, f"{self.root}/STATUS.txt")

    def tick(self):
        """One supervision beat; returns fatal reason or None."""
        self.keepalive()
        why = self.health()
        self.write_status()
        return why

    def shutdown(self):
        for p in (self.driver, self.sampler, self.ticker):
            if p and p.poll() is None:
                p.terminate()
        time.sleep(2)
        for p in (self.driver, self.sampler, self.ticker):
            if p and p.poll() is None:
                p.kill()

    # ---- a segment -------------------------------------------------------------
    def run_segment(self, name, secs):
        d = f"{self.root}/seg{self.seg_i:02d}_{name}"
        os.makedirs(d, exist_ok=True)
        cmd = [PY, "-u", f"{REPO}/run_soak.py", "--accounts", ACCOUNTS, "--duration", str(secs),
               "--run-dir", d] + CASES[name]
        out = open(f"{d}/driver.out", "w")
        self.event("segment_start", f"{self.seg_i} {name} secs={secs}")
        self.driver = subprocess.Popen(cmd, stdout=out, stderr=out, cwd=REPO)
        deadline = time.time() + secs + 900
        fatal = None
        while self.driver.poll() is None:
            time.sleep(15)
            fatal = self.tick()
            if fatal or self.stopping:
                self.driver.terminate()
                break
            if time.time() > deadline:
                self.event("segment_hang", name)
                self.driver.kill()
                break
        rc = self.driver.wait()
        unexpected = sum(1 for l in open(f"{d}/driver.out", errors="replace") if l.startswith("[unexpected]"))
        tracebacks = sum(1 for l in open(f"{d}/driver.out", errors="replace") if "Traceback" in l)
        rows = 0
        try:
            rows = sum(1 for _ in open(f"{d}/soak.csv")) - 1
        except Exception:
            pass
        ok = (rc == 0 and unexpected == 0 and tracebacks == 0 and not fatal)
        self.event("segment_end", f"{self.seg_i} {name} rc={rc} unexpected={unexpected} rows={rows} ok={ok}")
        self.segments.append(f"{self.seg_i:02d} {name:10s} rc={rc} rows={rows} unexpected={unexpected} {'OK' if ok else 'FAIL'}")
        return ok, fatal

    def run_idle(self, secs):
        self.event("segment_start", f"{self.seg_i} IDLE secs={secs}")
        t = time.time(); fatal = None
        while time.time() - t < secs and not self.stopping and not fatal:
            time.sleep(15); fatal = self.tick()
        self.event("segment_end", f"{self.seg_i} IDLE ok={not fatal}")
        self.segments.append(f"{self.seg_i:02d} IDLE       {secs}s {'OK' if not fatal else 'FAIL'}")
        return not fatal, fatal

    # ---- main ------------------------------------------------------------------
    def main(self):
        a = self.a
        self.plan = SCHEDULES[a.schedule]
        x = find_xrpld()
        if x is None:
            self.state, self.detail = "FAILED", "xrpld not running at start"
            self.write_status(); return 2
        self.xpid = x.pid
        stale = stale_rotation_state()
        if stale:
            self.state, self.detail = "FAILED", stale
            self.write_status(); return 2
        lo = self.min_balance()
        self.log(f"start: xrpld pid {x.pid}, min sampled balance {lo}")
        if lo is not None and lo < MIN_BALANCE_XRP:
            self.state, self.detail = "FAILED", f"account balance low ({lo:.0f} XRP) at start"
            self.write_status(); return 2
        self.start_ticker(); self.start_sampler()
        self.state = "RUNNING"
        self.event("run_start", f"{a.name} hours={a.hours} segment_min={a.segment_min} schedule={a.schedule}")
        end = self.t0 + a.hours * 3600
        fatal = None
        while not self.stopping and time.time() < end and not fatal:
            if os.path.exists(f"{self.root}/STOP"):
                self.event("stop_file"); break
            remaining = end - time.time()
            name = self.plan[self.seg_i % len(self.plan)]
            secs = int(min((IDLE_MIN if name == "IDLE" else a.segment_min) * 60, remaining))
            if secs < 120:
                break
            lo = self.min_balance()
            if lo is not None and lo < MIN_BALANCE_XRP:
                fatal = f"account balance low ({lo:.0f} XRP)"; break
            self.seg_name = name
            ok, fatal = self.run_idle(secs) if name == "IDLE" else self.run_segment(name, secs)
            self.seg_i += 1
            if ok:
                self.consec_fail = 0
            else:
                self.fails += 1; self.consec_fail += 1
                if self.consec_fail >= MAX_CONSEC_FAIL and not fatal:
                    fatal = f"{MAX_CONSEC_FAIL} consecutive segment failures"
            # brief breather; keeps supervision alive between segments
            for _ in range(2):
                time.sleep(5); fatal = fatal or self.tick()
        if fatal:
            self.state, self.detail = "STOPPED_ON_PROBLEM", fatal
            self.event("fatal", fatal)
        elif self.stopping:
            self.state, self.detail = "STOPPED_BY_SIGNAL", ""
        else:
            self.state, self.detail = ("DONE_CLEAN" if self.fails == 0 else "DONE_WITH_FAILED_SEGMENTS"), ""
            # idle tail (sampler keeps recording post-load RSS), marked in events.csv
            self.event("idle_tail_start", "sampler continues; analysis should clip before this")
            t = time.time()
            while time.time() - t < a.tail_min * 60 and not self.stopping:
                time.sleep(15); self.tick()
        self.event("run_end", self.state)
        self.write_status()
        self.shutdown()
        return 0 if self.state.startswith("DONE") else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--segment-min", type=float, default=30)
    ap.add_argument("--schedule", choices=list(SCHEDULES), default="rehearsal")
    ap.add_argument("--tail-min", type=float, default=15)
    sys.exit(Sup(ap.parse_args()).main())
