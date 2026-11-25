#!/usr/bin/env python3
import argparse,csv,math,pathlib
p=argparse.ArgumentParser()
p.add_argument("run")
a=p.parse_args()
r=pathlib.Path(a.run)
with (r/"jobs.csv").open() as f: jobs=list(csv.DictReader(f))
with (r/"telemetry.csv").open() as f: samples=list(csv.DictReader(f))
ok=[j for j in jobs if j["status"]=="ok"]
lat=sorted(float(j["response_ms"]) for j in ok)
def percentile(q):
    return lat[math.ceil(q*len(lat))-1] if lat else None
print("jobs",len(jobs),"completed",len(ok),"misses_including_rejected",sum(int(j["deadline_miss"]) for j in jobs))
print("p95_response_ms",percentile(.95),"p99_response_ms",percentile(.99))
for d in ("cpu","gpu"):
    print(d,sum(j["backend"]==d for j in ok))
if len(samples)>=2 and all(s["core_w"] for s in samples):
    t=[float(s["monotonic_s"]) for s in samples]
    w=[float(s["core_w"]) for s in samples]
    energy=sum((w[i]+w[i-1])*.5*(t[i]-t[i-1]) for i in range(1,len(t)))
    print("sampled_core_rail_window_s",t[-1]-t[0],"energy_j",energy)
    print("mean_core_rail_w",energy/(t[-1]-t[0]))
    print("window_energy_per_completed_job_mj_approx",energy*1000/len(ok) if ok else None)
else:
    print("core rail energy unavailable; do not substitute zero")
