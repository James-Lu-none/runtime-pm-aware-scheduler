#!/usr/bin/env python3
"""Construct state-specific latency profile. Energy/power must be supplied from separate measured calibration, not invented."""
import argparse
import csv
import math
import sys
p=argparse.ArgumentParser()
p.add_argument("--row", action="append", nargs=5, metavar=("JOBS_CSV","BACKEND","STATE","ENERGY_MJ","POWER_W"), required=True,
               help="energy/power can be nan; one row per backend/state/cap")
a=p.parse_args()
w=csv.writer(sys.stdout, lineterminator='\n')
w.writerow(["backend","state","cap_khz","p95_ms","energy_mj","power_w","workload"])
keys=set()
for path,backend,state,energy,power in a.row:
    if backend not in ("cpu","gpu") or state not in ("active","suspended","unknown"):
        p.error("invalid backend/state")
    if backend=="cpu" and state!="active":
        p.error("CPU profile state must be active")
    with open(path) as f:
        rows=[r for r in csv.DictReader(f) if r["status"]=="ok" and r["backend"]==backend
              and (backend=="cpu" or r["gpu_state"]==state)]
    caps={int(r["cap_khz"]) for r in rows}
    if len(rows)<10 or len(caps)!=1:
        p.error("need >=10 matching jobs at exactly one CPU cap: "+path)
    workloads={r.get("workload") or "pool" for r in rows}
    shapes={(r.get("width"),r.get("height"),r.get("threads")) for r in rows}
    if len(workloads)!=1 or len(shapes)!=1:
        p.error("profile rows must have one workload, shape and thread count: "+path)
    workload=workloads.pop()
    cap=caps.pop()
    key=(workload,backend,state,cap)
    if key in keys:
        p.error("duplicate profile row")
    keys.add(key)
    values=sorted(float(r["service_ms"]) for r in rows)
    for v in (float(energy),float(power)):
        if not math.isnan(v) and (not math.isfinite(v) or v<0):
            p.error("invalid measured energy/power")
    w.writerow([backend,state,cap,values[math.ceil(.95*len(values))-1],energy,power,workload])
