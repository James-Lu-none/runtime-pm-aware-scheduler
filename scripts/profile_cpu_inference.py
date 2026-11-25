#!/usr/bin/env python3
"""CPU-only, paced MobileNetV4 DVFS characterization on a shared CPUFreq policy."""
import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import statistics
import sys
import time

from profile_dvfs import ROOT, read, write, cool, run_checked, interrupt, percentile, pareto

def csv_rows(path):
    with path.open() as f:
        return list(csv.DictReader(f))

def save_csv(path, rows):
    if not rows:
        return
    with path.open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

def analyze(dest, model, governor, cap, fixed, repeat, a):
    jobs = csv_rows(dest/"jobs.csv")
    samples = csv_rows(dest/"telemetry.csv")
    if len(jobs)!=a.jobs or any(
        j["status"]!="ok" or j["backend"]!="cpu" or j["workload"]!=model or
        int(j["threads"])!=a.threads or int(j["job"])!=i
        for i,j in enumerate(jobs)):
        raise RuntimeError(f"Incomplete or unexpected job stream: {dest}")
    service = [float(j["service_ms"]) for j in jobs]
    response = [float(j["response_ms"]) for j in jobs]
    if any(not math.isfinite(x) or x<0 for x in service+response):
        raise RuntimeError(f"Invalid latency: {dest}")
    if len(samples)<2 or any(not s[k] for s in samples for k in
                            ("monotonic_s","core_w","freq_khz","cap_khz","temp_mc")):
        raise RuntimeError(f"Incomplete telemetry: {dest}")
    t = [float(s["monotonic_s"]) for s in samples]
    w = [float(s["core_w"]) for s in samples]
    freq = [int(s["freq_khz"]) for s in samples]
    temps = [int(s["temp_mc"])/1000 for s in samples]
    if any(not math.isfinite(x) or x<0 for x in w) or any(
        not math.isfinite(x) for x in t) or any(b<=a for a,b in zip(t,t[1:])):
        raise RuntimeError(f"Invalid energy telemetry: {dest}")
    window = t[-1]-t[0]
    # The legacy runner starts its clock after model loading and exits after
    # the final job, so the PMIC window is approximate, not aligned exactly.
    elapsed = float(jobs[-1]["finish_ms"])/1000
    horizon = a.jobs*a.period_ms/1000
    coverage = window/horizon
    energy = sum((w[i]+w[i-1])*.5*(t[i]-t[i-1]) for i in range(1,len(t)))
    match = sum(abs(x-cap)<=cap*.02 for x in freq)/len(freq)
    caps_ok = all(int(s["cap_khz"])==cap for s in samples) and all(
        int(j["cap_khz"])==cap for j in jobs)
    valid = caps_ok and .9<=coverage<=1.1 and elapsed<=1.1*horizon and (
        not fixed or match>=.90)
    return dict(model=model, governor=governor, cap_khz=cap, fixed=int(fixed),
                threads=a.threads, period_ms=a.period_ms, deadline_ms=a.deadline_ms,
                repeat=repeat, jobs=len(jobs), p95_service_ms=percentile(service,.95),
                p95_response_ms=percentile(response,.95), p99_response_ms=percentile(response,.99),
                max_response_ms=max(response), deadline_misses=sum(int(j["deadline_miss"]) for j in jobs),
                rail_w=energy/window, approx_energy_mj_job=energy*1000/len(jobs),
                sampled_s=window, expected_window_s=horizon, coverage_ratio=coverage,
                elapsed_s=elapsed, freq_min_khz=min(freq), freq_median_khz=statistics.median(freq),
                freq_max_khz=max(freq), freq_target_match_pct=100*match,
                max_temp_c=max(temps), valid=int(valid), run_dir=str(dest))

def summarize(rows, repeats):
    groups={}
    for row in rows:
        groups.setdefault((row["model"],row["governor"],row["cap_khz"]),[]).append(row)
    result=[]
    for (model,gov,cap), group in groups.items():
        if len(group)!=repeats or not all(x["valid"] for x in group):
            print(f"Excluded invalid/incomplete: {model}/{gov}/{cap}",file=sys.stderr)
            continue
        row={k:group[0][k] for k in ("model","governor","cap_khz","fixed","threads","period_ms","deadline_ms")}
        row["repeats"]=len(group)
        for key in ("p95_service_ms","p95_response_ms","p99_response_ms",
                    "rail_w","approx_energy_mj_job","freq_median_khz","freq_target_match_pct"):
            row[key]=statistics.median(x[key] for x in group)
        row["max_response_ms"]=max(x["max_response_ms"] for x in group)
        row["deadline_misses"]=sum(x["deadline_misses"] for x in group)
        row["jobs"]=sum(x["jobs"] for x in group)
        result.append(row)
    # Pareto comparisons are only meaningful within the same model.
    for model in {x["model"] for x in result}:
        subset=[x for x in result if x["model"]==model]
        front=pareto(subset)
        for x in subset:
            x["pareto"]=int(x in front)
    return result

def sha256(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--models",nargs="+",choices=["small","medium","large"],default=["small","medium","large"])
    p.add_argument("--freqs",nargs="+",type=int,default=[1500000,2000000,2400000])
    p.add_argument("--threads",type=int,default=4)
    p.add_argument("--jobs",type=int,default=60)
    p.add_argument("--period-ms",type=int,default=500)
    p.add_argument("--deadline-ms",type=int,default=250)
    p.add_argument("--repeats",type=int,default=1)
    p.add_argument("--native-governors",nargs="*",default=["original"],
                   help="Native baselines at original cap; empty list disables them")
    p.add_argument("--policy",type=Path,default=Path("/sys/devices/system/cpu/cpufreq/policy0"))
    p.add_argument("--zone",type=Path,default=Path("/sys/class/thermal/thermal_zone0"))
    p.add_argument("--runner",type=Path,default=ROOT/"build/pm_runner")
    p.add_argument("--model-dir",type=Path,default=ROOT/"models/mobilenetv4")
    p.add_argument("--out",type=Path,default=ROOT/("results/cpu-inference-"+time.strftime("%Y%m%d-%H%M%S")))
    p.add_argument("--start-temp",type=float,default=60)
    p.add_argument("--max-temp",type=float,default=75)
    a=p.parse_args()
    if os.geteuid()!=0:
        p.error("Run with sudo for CPUFreq control")
    if not (1<=a.threads<=4 and 20<=a.jobs<=10000 and a.period_ms>=100 and
            0<a.deadline_ms<=a.period_ms and a.jobs*a.period_ms>=10000 and
            1<=a.repeats<=10 and 0<a.start_temp<a.max_temp<=75 and
            len(set(a.models))==len(a.models) and len(set(a.freqs))==len(a.freqs)):
        p.error("Invalid workload, timing, frequency list or thermal limits")
    for key in ("policy","zone","runner","model_dir","out"):
        setattr(a,key,getattr(a,key).resolve())
    if not a.runner.is_file():
        p.error("Build pm_runner first")
    if read(a.zone/"type")!="cpu-thermal":
        p.error("Expected cpu-thermal zone")
    hashes={}
    for model in a.models:
        for ext in ("param","bin"):
            path=a.model_dir/f"{model}.{ext}"
            if not path.is_file():
                p.error(f"Missing exported model: {path}")
            hashes[path.name]=sha256(path)
    with open("/run/lock/pm-dvfs-profile.lock","w") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        sweep(a,hashes)

def sweep(a, hashes):
    capfile=a.policy/"scaling_max_freq"
    govfile=a.policy/"scaling_governor"
    minfile=a.policy/"scaling_min_freq"
    orig_cap=int(read(capfile))
    orig_min=int(read(minfile))
    orig_gov=read(govfile)
    available={int(x) for x in read(a.policy/"scaling_available_frequencies").split()}
    governors=set(read(a.policy/"scaling_available_governors").split())
    natives=list(dict.fromkeys(orig_gov if x=="original" else x for x in a.native_governors))
    if "performance" not in governors or any(g not in governors for g in natives):
        raise RuntimeError(f"Requested governor unavailable; available: {sorted(governors)}")
    if any(f not in available or not orig_min<=f<=orig_cap for f in a.freqs):
        raise RuntimeError(f"Requested cap outside supported original limits {orig_min}..{orig_cap}")
    configs=[("performance",f,True) for f in a.freqs]
    for g in natives:
        if not any(g==gov and orig_cap==cap for gov,cap,_ in configs):
            configs.append((g,orig_cap,g=="performance"))
    a.out.mkdir(parents=True,exist_ok=False)
    snapshot=dict(policy=str(a.policy),governor=orig_gov,min_khz=orig_min,max_khz=orig_cap,
                  related_cpus=read(a.policy/"related_cpus"),models=a.models,model_sha256=hashes,
                  configurations=configs,threads=a.threads,jobs=a.jobs,period_ms=a.period_ms,
                  deadline_ms=a.deadline_ms,repeats=a.repeats,sample_interval_s=.137,
                  start_temp_c=a.start_temp,max_temp_c=a.max_temp,runner=str(a.runner),
                  runner_sha256=sha256(a.runner),kernel=os.uname().release)
    (a.out/"original_settings.json").write_text(json.dumps(snapshot,indent=2)+"\n")
    print("Results:",a.out,flush=True)
    print("Original settings:",json.dumps(snapshot),flush=True)
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
        signal.signal(sig,interrupt)
    env=dict(os.environ,OMP_WAIT_POLICY="PASSIVE")
    rows=[]
    try:
        for repeat in range(1,a.repeats+1):
            plan=[(m,g,c,f) for m in a.models for g,c,f in configs]
            if repeat%2==0:
                plan.reverse()
            for model,gov,cap,fixed in plan:
                write(capfile,orig_cap)
                write(govfile,orig_gov)
                cool(a.zone,a.start_temp)
                write(capfile,cap)
                write(govfile,gov)
                if int(read(minfile))!=orig_min or int(read(capfile))!=cap:
                    raise RuntimeError("CPUFreq limits changed unexpectedly")
                dest=a.out/f"r{repeat}-{model}-{gov}-{cap}"
                print(f"=== {model} {gov} {cap} kHz (repeat {repeat}) ===",flush=True)
                cmd=[sys.executable,str(ROOT/"scripts/run.py"),"--out",str(dest),
                     "--policy",str(a.policy),"--zone",str(a.zone),"--interval","0.137",
                     "--",str(a.runner),"--mode","cpu","--gpu","0","--workload",model,
                     "--model-dir",str(a.model_dir),"--threads",str(a.threads),
                     "--jobs",str(a.jobs),"--period-ms",str(a.period_ms),
                     "--deadline-ms",str(a.deadline_ms),"--cpufreq-policy",str(a.policy),
                     "--thermal-zone",str(a.zone),"--thermal","0"]
                run_checked(cmd,env,a.zone,a.max_temp,a.out/f"{dest.name}.stderr.log")
                if read(govfile)!=gov or int(read(capfile))!=cap or int(read(minfile))!=orig_min:
                    raise RuntimeError("CPUFreq settings changed during measurement")
                row=analyze(dest,model,gov,cap,fixed,repeat,a)
                rows.append(row)
                save_csv(a.out/"runs.csv",rows)
                print(f'P95 service {row["p95_service_ms"]:.3f} ms; '
                      f'response {row["p95_response_ms"]:.3f} ms; '
                      f'energy {row["approx_energy_mj_job"]:.3f} mJ; '
                      f'misses {row["deadline_misses"]}/{row["jobs"]}; valid={row["valid"]}',flush=True)
    finally:
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
            signal.signal(sig,signal.SIG_IGN)
        failures=[]
        for path,value in ((capfile,orig_cap),(govfile,orig_gov)):
            try:
                write(path,value)
            except Exception as e:
                failures.append(str(e))
        if failures:
            raise RuntimeError("RESTORE FAILED: "+"; ".join(failures))
        print(f"Restored cap={orig_cap}, governor={orig_gov}; min unchanged",flush=True)
    result=summarize(rows,a.repeats)
    if not result:
        raise RuntimeError("No valid configurations; inspect runs.csv")
    save_csv(a.out/"summary.csv",result)
    save_csv(a.out/"pareto.csv",[x for x in result if x["pareto"]])
    # Only fixed-frequency measurements are candidate costs for a DVFS selector;
    # use a separate schema, never overwrite the legacy heterogeneous profile.
    profile=[{k:x[k] for k in ("model","cap_khz","threads","period_ms","p95_service_ms",
                              "approx_energy_mj_job","rail_w","repeats")}
             for x in result if x["fixed"]]
    save_csv(a.out/"cpu_costs.csv",profile)
    print("\nmodel,governor,cap_khz,p95_service_ms,p95_response_ms,energy_mj,misses,jobs,pareto")
    for x in result:
        print(f'{x["model"]},{x["governor"]},{x["cap_khz"]},{x["p95_service_ms"]:.3f},'
              f'{x["p95_response_ms"]:.3f},{x["approx_energy_mj_job"]:.3f},'
              f'{x["deadline_misses"]},{x["jobs"]},{x["pareto"]}')

if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Profiling interrupted",file=sys.stderr)
        sys.exit(130)
    except (OSError,ValueError,RuntimeError) as e:
        print(f"profile_cpu_inference: {e}",file=sys.stderr)
        sys.exit(1)
