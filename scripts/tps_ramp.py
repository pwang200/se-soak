#!/usr/bin/env python3
"""Short TPS ramp: step up in-flight-per-account K (txs/ledger ~= K * 400 accounts) with return_1.
Per step: achieved tx/s, txs/ledger, real ledger interval (ticker sleep + close time), xrpld CPU/RSS,
fee-escalation + queue depth, non-tesSUCCESS share, min account balance. Stops at the first bad step.
usage: tps_ramp.py [--steps 2,4,6,10] [--secs 240]   (needs xrpld up, NO other soak/ticker running)
Note: standalone xrpld escalates the open-ledger fee above minimum_txn_in_ledger_standalone
(default 1000 txs/ledger), and run_soak.py aborts on terQUEUED, so steps past ~1000 txs/ledger
fail unless that cfg value is raised ([transaction_queue]) first."""
import argparse, collections, csv, json, os, subprocess, sys, time
import psutil, urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
RUN_DATA = os.environ.get("SOAK_RUN_DATA", os.path.expanduser("~/rippled/run_data"))
ROOT = f"{RUN_DATA}/soak/ramp"
ACCTS = f"{RUN_DATA}/soak/test_accounts.json"
URL = "http://127.0.0.1:5005/"
THREADS, APT = 16, 25          # 400 accounts


def rpc(m, p=None, timeout=15):
    r = urllib.request.Request(URL, json.dumps({"method": m, "params": [p or {}]}).encode())
    return json.load(urllib.request.urlopen(r, timeout=timeout))["result"]


def seq():
    return rpc("server_info")["info"]["validated_ledger"]["seq"]


def min_balance(accts):
    return min(int(rpc("account_info", {"account": a["address"]})["account_data"]["Balance"]) for a in accts[:400:5]) / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="2,4,6,10"); ap.add_argument("--secs", type=int, default=240)
    a = ap.parse_args()
    os.makedirs(ROOT, exist_ok=True)
    accts = json.load(open(ACCTS))
    x = next(p for p in psutil.process_iter(["name"]) if p.info["name"] in ("xrpld-main", "xrpld"))
    tk = subprocess.Popen([PY, "-u", f"{REPO}/ledger_ticker.py", "--interval", "2"],
                          stdout=open(f"{ROOT}/ticker.log", "a"), stderr=subprocess.STDOUT, cwd=REPO)
    out = open(f"{ROOT}/summary.csv", "a", buffering=1)
    hdr = "K,planned_per_ledger,rows,ledgers,interval_s,txs_per_ledger,tx_per_s,tx_per_s_at_3s,non_success_pct,unexpected,cpu_avg_pct,cpu_max_pct,rss_max_mb,ledger_size_max,queue_max,open_fee_max_drops,min_balance_xrp,rc"
    if out.tell() == 0:
        out.write(hdr + "\n")
    try:
        for K in [int(s) for s in a.steps.split(",")]:
            # wait for a quiet node between steps
            for _ in range(18):
                f = rpc("fee")
                if int(f["current_queue_size"]) == 0 and int(f["current_ledger_size"]) < 50:
                    break
                time.sleep(5)
            d = f"{ROOT}/K{K:02d}"; os.makedirs(d, exist_ok=True)
            cmd = [PY, "-u", f"{REPO}/run_soak.py", "--accounts", ACCTS, "--pattern", "pipeline", "--categories", "return_1",
                   "--threads", str(THREADS), "--accounts-per-thread", str(APT), "--in-flight-per-account", str(K),
                   "--duration", str(a.secs), "--run-dir", d]
            s0, t0 = seq(), time.time()
            drv = subprocess.Popen(cmd, stdout=open(f"{d}/driver.out", "w"), stderr=subprocess.STDOUT, cwd=REPO)
            cpu, rss, lsz, que, fee, bal = [], [], [0], [0], [0], 1e9
            x.cpu_percent(None)
            nxt = time.time()
            while drv.poll() is None:
                time.sleep(5)
                try:
                    cpu.append(x.cpu_percent(None)); rss.append(x.memory_info().rss / 2**20)
                    f = rpc("fee", timeout=10)
                    lsz.append(int(f["current_ledger_size"])); que.append(int(f["current_queue_size"]))
                    fee.append(int(f["drops"]["open_ledger_fee"]))
                    if time.time() >= nxt:
                        bal = min(bal, min_balance(accts)); nxt = time.time() + 30
                        if bal < 1500:
                            print(f"balance guard {bal:.0f} XRP, killing step"); drv.terminate()
                except Exception as e:
                    print("monitor:", e)
            rc = drv.wait(); s1, t1 = seq(), time.time()
            res = collections.Counter()
            for r in csv.DictReader(open(f"{d}/soak.csv")):
                res[r["final_result"] or r["engine_result"]] += 1
            rows = sum(res.values()); ok = res.get("tesSUCCESS", 0)
            unexp = sum(1 for l in open(f"{d}/driver.out", errors="replace") if l.startswith("[unexpected]"))
            led = max(s1 - s0, 1); dur = t1 - t0
            tpl = rows / led
            line = [K, K * THREADS * APT, rows, led, round(dur / led, 2), round(tpl), round(rows / dur), round(tpl / 3),
                    round(100 * (rows - ok) / max(rows, 1), 2), unexp, round(sum(cpu) / max(len(cpu), 1)), round(max(cpu or [0])),
                    round(max(rss or [0])), max(lsz), max(que), max(fee), round(bal), rc]
            out.write(",".join(map(str, line)) + "\n")
            print(dict(zip(hdr.split(","), line)), dict(res))
            bad = (rc != 0) or (rows - ok) / max(rows, 1) > 0.02 or dur / led > 8 or bal < 1500
            if bad:
                print("STOP: step degraded -> this is the knee"); break
    finally:
        tk.terminate(); time.sleep(1)


if __name__ == "__main__":
    main()
