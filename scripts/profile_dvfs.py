#!/usr/bin/env python3
"""Controlled CPUFreq sweep against a saturated Vulkan baseline (Pi 5)."""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time
import fcntl

ROOT = Path(__file__).resolve().parents[1]

def read(path):
    return path.read_text().strip()

def write(path, value):
    path.write_text(str(value) + "\n")
    if read(path) != str(value):
        raise RuntimeError(f"Readback mismatch: {path}: expected {value}, got {read(path)}")

def percentile(values, q):
    return sorted(values)[math.ceil(q * len(values))-1]

def interrupt(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")

def stop_child(child):
    # run.py handles SIGTERM and terminates its runner before exiting.
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()

def run_checked(cmd, env, zone, max_temp, stderr_path):
    with stderr_path.open("w") as log:
        child = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True,
                                 stderr=log)
        try:
            while child.poll() is None:
                if int(read(zone / "temp")) / 1000 >= max_temp:
                    raise RuntimeError(f"Thermal guard reached {max_temp} C; stopped {cmd}")
                time.sleep(.25)
            if child.returncode:
                raise RuntimeError(f"Exit {child.returncode}: {cmd}; see {stderr_path}")
        finally:
            stop_child(child)

def cool(zone, limit, timeout=180):
    start = time.monotonic()
    while int(read(zone / "temp")) / 1000 > limit:
        if time.monotonic()-start >= timeout:
            raise RuntimeError(f"Cannot cool below {limit} C; check cooling or adjust --start-temp")
        time.sleep(1)
    return int(read(zone / "temp")) / 1000

def parse_run(dest, backend, cap, threads, iterations, elements, repeat, governor, start_temp):
    with (dest/"jobs.csv").open() as file:
        jobs = list(csv.DictReader(file))
    with (dest/"telemetry.csv").open() as file:
        samples = list(csv.DictReader(file))
    if not jobs or any(j["status"]!="ok" or j["backend"]!=backend for j in jobs):
        raise RuntimeError(f"Incomplete benchmark: {dest}")
    lat = [float(j["service_ms"]) for j in jobs]
    elapsed = (float(jobs[-1]["finish_ms"])-float(jobs[0]["dispatch_ms"]))/1000
    if elapsed <= 0 or len(samples) < 2:
        raise RuntimeError(f"Insufficient timing/telemetry: {dest}")
    t = [float(s["monotonic_s"]) for s in samples]
    if any(not s["core_w"] or not s["freq_khz"] or not s["cap_khz"] or not s["temp_mc"] for s in samples):
        raise RuntimeError(f"Missing PMIC/frequency/thermal telemetry: {dest}")
    w = [float(s["core_w"]) for s in samples]
    if any(not math.isfinite(x) or x < 0 for x in w) or any(b<=a for a,b in zip(t,t[1:])):
        raise RuntimeError(f"Invalid PMIC/timestamps: {dest}")
    freq = [int(s["freq_khz"]) for s in samples]
    caps = {int(s["cap_khz"]) for s in samples}
    temps = [int(s["temp_mc"])/1000 for s in samples]
    window = t[-1]-t[0]
    energy = sum((w[i]+w[i-1])*.5*(t[i]-t[i-1]) for i in range(1,len(t)))
    coverage = window/elapsed
    match = sum(abs(x-cap)<=cap*.02 for x in freq)/len(freq)
    # scaling_cur_freq is a cpufreq report, not a hardware cycle counter.
    # Allow minor sampling loss/initialization; reject gross window mismatch.
    valid = caps == {cap} and .90 <= coverage <= 1.10 and (backend=="gpu" or match>=.90)
    throughput = len(jobs)/elapsed
    return dict(repeat=repeat, backend=backend, governor=governor, cap_khz=cap,
                threads=threads if backend=="cpu" else 0, iterations=iterations,
                elements=elements, jobs=len(jobs), timed_s=elapsed, sampled_s=window,
                coverage_ratio=coverage, p95_service_ms=percentile(lat,.95),
                throughput_jobs_s=throughput, GFLOPS=throughput*elements*iterations*2/1e9,
                rail_w=energy/window, approx_energy_mj_job=energy*1000/len(jobs),
                freq_min_khz=min(freq), freq_median_khz=statistics.median(freq),
                freq_max_khz=max(freq), freq_target_match_pct=match*100,
                start_temp_c=start_temp, max_sample_temp_c=max(temps),
                valid=int(valid), run_dir=str(dest))

def pareto(rows):
    return [x for x in rows if not any(
        y["p95_service_ms"]<=x["p95_service_ms"] and
        y["approx_energy_mj_job"]<=x["approx_energy_mj_job"] and
        (y["p95_service_ms"]<x["p95_service_ms"] or
         y["approx_energy_mj_job"]<x["approx_energy_mj_job"])
        for y in rows if y is not x)]

def summarize(rows, out, repeats):
    grouped = {}
    for x in rows:
        grouped.setdefault((x["backend"],x["cap_khz"],x["threads"],x["governor"]),[]).append(x)
    medians = []
    for (backend,cap,threads,governor), group in grouped.items():
        good = [x for x in group if x["valid"]]
        if len(good)!=repeats:
            print(f"Excluded incomplete/invalid configuration {backend}/{cap}/{threads}",file=sys.stderr)
            continue
        row = dict(backend=backend, cap_khz=cap, threads=threads, governor=governor, repeats=len(good))
        for k in ("p95_service_ms","approx_energy_mj_job","rail_w","GFLOPS",
                  "freq_median_khz","freq_target_match_pct"):
            row[k] = statistics.median(x[k] for x in good)
        medians.append(row)
    if not medians:
        raise RuntimeError("No valid configurations; inspect runs.csv")
    frontier = pareto(medians)
    for row in medians:
        row["pareto"] = int(row in frontier)
    for name, data in (("summary.csv",medians),("pareto.csv",frontier)):
        with (out/name).open("w") as f:
            writer=csv.DictWriter(f,fieldnames=list(medians[0]))
            writer.writeheader()
            writer.writerows(data)
    print("\nMedian results (Pareto=1 means non-dominated):")
    print("backend,cap_khz,threads,p95_ms,energy_mj,rail_w,pareto")
    for row in medians:
        print(f'{row["backend"]},{row["cap_khz"]},{row["threads"]},'
              f'{row["p95_service_ms"]:.3f},{row["approx_energy_mj_job"]:.3f},'
              f'{row["rail_w"]:.3f},{row["pareto"]}')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--freqs",nargs="+",type=int,default=[1500000,2000000,2400000],help="CPU frequency caps in kHz")
    p.add_argument("--threads",type=int,default=4)
    p.add_argument("--iterations",type=int,default=1024)
    p.add_argument("--elements",type=int,default=262144)
    p.add_argument("--seconds",type=int,default=30)
    p.add_argument("--repeats",type=int,default=1)
    p.add_argument("--policy",type=Path,default=Path("/sys/devices/system/cpu/cpufreq/policy0"))
    p.add_argument("--zone",type=Path,default=Path("/sys/class/thermal/thermal_zone0"))
    p.add_argument("--gpu-device",default=str(Path("/sys/class/drm/renderD128/device").resolve()))
    p.add_argument("--gpu-index",type=int,default=0)
    p.add_argument("--runner",type=Path,default=ROOT/"build-micro/pm_microbench")
    p.add_argument("--out",type=Path,default=ROOT/("results/dvfs-"+time.strftime("%Y%m%d-%H%M%S")))
    p.add_argument("--start-temp",type=float,default=60)
    p.add_argument("--max-temp",type=float,default=75)
    p.add_argument("--cpu-only",action="store_true",help="Skip GPU verification and baseline")
    a=p.parse_args()
    if os.geteuid()!=0:
        p.error("Run with sudo; CPUFreq writes require root")
    if not (10<=a.seconds<=120 and 1<=a.repeats<=10 and 1<=a.threads<=64 and
            1<=a.iterations<=16384 and 16<=a.elements<=16777216 and a.elements%16==0 and
            0<a.start_temp<a.max_temp<=75 and len(set(a.freqs))==len(a.freqs)):
        p.error("Invalid workload, frequency list, duration (10..120s) or thermal limits")
    a.policy=a.policy.resolve()
    a.zone=a.zone.resolve()
    a.runner=a.runner.resolve()
    a.out=a.out.resolve()
    if not a.runner.is_file():
        p.error("Build pm_microbench first")
    if not a.cpu_only and not (Path(a.gpu_device)/"power/runtime_status").is_file():
        p.error("Cannot locate GPU runtime PM device")
    if read(a.zone/"type")!="cpu-thermal":
        p.error("Expected cpu-thermal sensor; specify --zone")
    # This lock protects cooperating invocations only; external controllers must be stopped.
    with open("/run/lock/pm-dvfs-profile.lock","w") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another DVFS profiling session is running")
        sweep(a)

def sweep(a):
    capfile=a.policy/"scaling_max_freq"
    govfile=a.policy/"scaling_governor"
    original_cap=int(read(capfile))
    original_min=int(read(a.policy/"scaling_min_freq"))
    original_gov=read(govfile)
    available={int(x) for x in read(a.policy/"scaling_available_frequencies").split()}
    if any(f not in available or f<original_min or f>original_cap for f in a.freqs):
        raise RuntimeError(f"Caps must be supported and within original min/max {original_min}/{original_cap}")
    if "performance" not in read(a.policy/"scaling_available_governors").split():
        raise RuntimeError("performance governor unavailable")
    a.out.mkdir(parents=True,exist_ok=False)
    snapshot=dict(policy=str(a.policy), governor=original_gov, min_khz=original_min,
                  max_khz=original_cap, related_cpus=read(a.policy/"related_cpus"),
                  cpu_caps_khz=a.freqs, cpu_governor="performance", gpu_governor=original_gov,
                  gpu_cpu_cap_khz=original_cap, threads=a.threads, iterations=a.iterations,
                  elements=a.elements, seconds=a.seconds, repeats=a.repeats,
                  sample_interval_s=.137, start_temp_c=a.start_temp, max_temp_c=a.max_temp,
                  gpu_index=a.gpu_index, gpu_device=a.gpu_device, cpu_only=a.cpu_only)
    (a.out/"original_settings.json").write_text(json.dumps(snapshot,indent=2)+"\n")
    print("Original settings:",json.dumps(snapshot),flush=True)
    print("Results:",a.out,flush=True)
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
        signal.signal(sig,interrupt)
    env=dict(os.environ, OMP_WAIT_POLICY="PASSIVE")
    common=["--elements",str(a.elements),"--iterations",str(a.iterations),
            "--threads",str(a.threads),"--seconds",str(a.seconds),"--gpu-index",str(a.gpu_index)]
    rows=[]
    # Preserve the original scaling_min_freq throughout. Never raise original max.
    try:
        if not a.cpu_only:
            cool(a.zone,a.start_temp)
            # Verification before CPUFreq changes, separate from telemetry.
            run_checked([str(a.runner),"--mode","verify",*common],env,a.zone,a.max_temp,a.out/"verify.stderr.log")
        with (a.out/"runs.csv").open("w") as file:
            writer=None
            for repeat in range(1,a.repeats+1):
                configs=[("cpu",f) for f in a.freqs]
                if not a.cpu_only:
                    configs.append(("gpu",original_cap))
                if repeat%2==0:
                    configs.reverse()
                for backend,cap in configs:
                    # Restore stock limits/governor between runs, including cooldown.
                    write(capfile,original_cap)
                    write(govfile,original_gov)
                    start_temp=cool(a.zone,a.start_temp)
                    if backend=="cpu":
                        write(capfile,cap)
                        write(govfile,"performance")
                    governor=read(govfile)
                    time.sleep(.5)
                    if read(capfile)!=str(cap) or read(a.policy/"scaling_min_freq")!=str(original_min):
                        raise RuntimeError("CPUFreq limits changed unexpectedly")
                    dest=a.out/f"r{repeat}-{backend}-{cap}"
                    print(f"=== repeat {repeat}: {backend} {cap} kHz ({governor}) ===",flush=True)
                    cmd=[sys.executable,str(ROOT/"scripts/run.py"),"--out",str(dest),
                         "--policy",str(a.policy),"--zone",str(a.zone),"--gpu-device",a.gpu_device,
                         "--interval","0.137","--",str(a.runner),"--mode",backend,*common]
                    run_checked(cmd,env,a.zone,a.max_temp,a.out/f"{dest.name}.stderr.log")
                    if read(govfile)!=governor or read(capfile)!=str(cap) or read(a.policy/"scaling_min_freq")!=str(original_min):
                        raise RuntimeError("CPUFreq settings changed during run")
                    row=parse_run(dest,backend,cap,a.threads,a.iterations,a.elements,repeat,governor,start_temp)
                    rows.append(row)
                    if writer is None:
                        writer=csv.DictWriter(file,fieldnames=list(row))
                        writer.writeheader()
                    writer.writerow(row)
                    file.flush()
                    print(f'P95 {row["p95_service_ms"]:.3f} ms, energy {row["approx_energy_mj_job"]:.3f} mJ, '
                          f'frequency match {row["freq_target_match_pct"]:.1f}%, valid={row["valid"]}',flush=True)
    finally:
        # Do not let a second Ctrl-C interrupt the restoration itself.
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
            signal.signal(sig,signal.SIG_IGN)
        failures=[]
        for path,value in ((capfile,original_cap),(govfile,original_gov)):
            try:
                write(path,value)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError("RESTORE FAILED: "+"; ".join(failures)+f"; see {a.out/'original_settings.json'}")
        print(f"Restored cap={original_cap}, governor={original_gov}; min unchanged",flush=True)
    summarize(rows,a.out,a.repeats)

if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Profiling interrupted",file=sys.stderr)
        sys.exit(130)
    except (OSError,RuntimeError,ValueError) as exc:
        print(f"profile_dvfs: {exc}",file=sys.stderr)
        sys.exit(1)
