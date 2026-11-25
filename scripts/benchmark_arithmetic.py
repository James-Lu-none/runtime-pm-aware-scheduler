#!/usr/bin/env python3
"""Saturated single-dispatch FP32 microbenchmark; verify all elements before timing."""
import argparse
import csv
import math
import os
from pathlib import Path
import subprocess
import sys
import time

p=argparse.ArgumentParser()
p.add_argument("--runner",default="./build-micro/pm_microbench")
p.add_argument("--elements",type=int,default=262144)
p.add_argument("--iterations",type=int,nargs="+",default=[1,16,64,256,1024])
p.add_argument("--threads",type=int,default=2)
p.add_argument("--seconds",type=int,default=5)
p.add_argument("--gpu-index",type=int,default=0)
p.add_argument("--gpu-device",default=str(Path("/sys/class/drm/renderD128/device").resolve()))
p.add_argument("--out",default="results/arithmetic-"+time.strftime("%Y%m%d-%H%M%S"))
a=p.parse_args()
out=Path(a.out)
out.mkdir(parents=True,exist_ok=False)
env=dict(os.environ)
env["OMP_WAIT_POLICY"]="PASSIVE"
print("iterations,nominal_flops_per_byte,backend,jobs,p95_service_ms,throughput_jobs_s,GFLOPS,rail_w,approx_energy_mj_job",flush=True)
with (out/"summary.csv").open("w") as summary:
    writer=csv.writer(summary)
    writer.writerow(["iterations","nominal_flops_per_byte","backend","jobs","p95_service_ms","throughput_jobs_s","GFLOPS","rail_w","approx_energy_mj_job"])
    for rounds in a.iterations:
        common=["--elements",str(a.elements),"--iterations",str(rounds),"--threads",str(a.threads),
                "--gpu-index",str(a.gpu_index),"--seconds",str(a.seconds)]
        parity=subprocess.check_output([a.runner,"--mode","verify",*common],env=env,text=True)
        (out/f"verify-{rounds}.txt").write_text(parity)
        for mode in ("cpu","gpu"):
            dest=out/f"{rounds}-{mode}"
            subprocess.run([sys.executable,"scripts/run.py","--out",str(dest),"--interval","0.137",
                            "--gpu-device",a.gpu_device,"--",a.runner,"--mode",mode,*common],
                           env=env,check=True)
            with (dest/"jobs.csv").open() as file:
                rows=list(csv.DictReader(file))
            if not rows:
                raise RuntimeError("No completed jobs")
            lat=sorted(float(r["service_ms"]) for r in rows)
            elapsed=(float(rows[-1]["finish_ms"])-float(rows[0]["dispatch_ms"]))/1000
            throughput=len(rows)/elapsed
            gflops=throughput*a.elements*rounds*2/1e9
            with (dest/"telemetry.csv").open() as file:
                samples=list(csv.DictReader(file))
            power=energy_job=float("nan")
            if len(samples)>=2 and all(s["core_w"] for s in samples):
                t=[float(s["monotonic_s"]) for s in samples]
                w=[float(s["core_w"]) for s in samples]
                energy=sum((w[i]+w[i-1])*.5*(t[i]-t[i-1]) for i in range(1,len(t)))
                if t[-1]>t[0]:
                    power=energy/(t[-1]-t[0])
                    energy_job=energy*1000/len(rows)
            row=[rounds,rounds/4,mode,len(rows),lat[math.ceil(.95*len(lat))-1],throughput,gflops,power,energy_job]
            writer.writerow(row)
            summary.flush()
            print(",".join(str(x) for x in row),flush=True)
print("Results:",out)
