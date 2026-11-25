#!/usr/bin/env python3
"""Compare CPU-only deadline-aware DVFS with fixed caps and native governors."""
import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import random
from pathlib import Path
import signal
import statistics
import sys
import time

from profile_dvfs import ROOT, read, write, cool, run_checked, interrupt, percentile

def rows(path):
    with path.open() as f:
        return list(csv.DictReader(f))

def save(path,data):
    if not data:
        return
    with path.open("w") as f:
        w=csv.DictWriter(f,fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)

def hint_spec(label):
    for prefix in ("hint_fixed_","hint_mixed_","hint_static_"):
        if label.startswith(prefix):
            return prefix.split("_")[1],int(label[len(prefix):])
    return None

def dynamic_policy(label):
    return label in ("adaptive","transition_blind","lookahead") or hint_spec(label) is not None

def make_replay(models,deadlines,jobs,period,seed):
    pairs=list(zip(models,deadlines))*(jobs//len(models))
    rng=random.Random(seed)
    rng.shuffle(pairs)
    notice=random.Random(seed ^ 0x5A17B3)
    return [dict(job=i,release_ms=i*period,model=model,deadline_ms=deadline,
                 notice_token=notice.randint(0,1000000))
            for i,(model,deadline) in enumerate(pairs)]

def sha256(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()

def latest(pattern,name):
    candidates=[p for p in (ROOT/"results").glob(pattern)
                if (p/name).is_file() and (p/"original_settings.json").is_file()]
    if not candidates:
        raise RuntimeError(f"No completed {pattern} profile; provide its directory explicitly")
    return max(candidates,key=lambda p:p.name)

def load_profiles(a):
    cpu=a.cpu_profile_dir or latest("cpu-inference-*","cpu_costs.csv")
    trans=a.transition_dir or latest("dvfs-transition-*","transition_costs.csv")
    cpu=cpu.resolve()
    trans=trans.resolve()
    cmeta=json.loads((cpu/"original_settings.json").read_text())
    tmeta=json.loads((trans/"original_settings.json").read_text())
    if cmeta["threads"]!=a.threads or cmeta["period_ms"]!=a.period_ms:
        raise RuntimeError("CPU profile threads/period mismatch")
    for meta in (cmeta,tmeta):
        if meta["policy"]!=str(a.policy) or meta["kernel"]!=os.uname().release:
            raise RuntimeError("Profile policy or kernel mismatch; reprofile")
    for model in set(a.models):
        for ext in ("param","bin"):
            p=a.model_dir/f"{model}.{ext}"
            if sha256(p)!=cmeta["model_sha256"].get(p.name):
                raise RuntimeError(f"Model hash mismatch: {p}")
    costs=rows(cpu/"cpu_costs.csv")
    by_model={m:{int(x["cap_khz"]) for x in costs if x["model"]==m} for m in set(a.models)}
    freqs=sorted(next(iter(by_model.values())))
    if len(freqs)<2 or any(set(freqs)!=v for v in by_model.values()):
        raise RuntimeError("All workload models need identical calibrated frequency sets")
    transitions={(int(x["from_khz"]),int(x["to_khz"])):x for x in rows(trans/"transition_costs.csv")}
    for f in freqs:
        for g in freqs:
            if f!=g and ((f,g) not in transitions or int(transitions[f,g]["samples"])<1):
                raise RuntimeError(f"Missing directed transition {f}->{g}")
    print("CPU costs:",cpu/"cpu_costs.csv",flush=True)
    print("Transition costs:",trans/"transition_costs.csv",flush=True)
    if tmeta.get("load_workers",0)!=0:
        print("Using transition profile with injected CPU load",flush=True)
    return cpu,trans,freqs

def analyze(dest,label,repeat,a,cap,replay=None):
    jobs=rows(dest/"jobs.csv")
    samples=rows(dest/"telemetry.csv")
    if len(jobs)!=a.jobs or any(
        int(j["job"])!=i or j["status"]!="ok" or j["backend"]!="cpu" or
        j["workload"]!=(replay[i]["model"] if replay else a.models[i%len(a.models)]) or int(j["threads"])!=a.threads or
        abs(float(j["release_ms"])-i*a.period_ms)>1e-6 or
        abs(float(j["deadline_ms"])-(replay[i]["deadline_ms"] if replay else a.deadlines[i%len(a.deadlines)]))>1e-6
        for i,j in enumerate(jobs)):
        raise RuntimeError(f"Incomplete or nonidentical workload: {dest}")
    if len(samples)<2 or any(not s[k] for s in samples for k in
                            ("monotonic_s","core_w","freq_khz","cap_khz","temp_mc")):
        raise RuntimeError(f"Missing telemetry: {dest}")
    t=[float(s["monotonic_s"]) for s in samples]
    w=[float(s["core_w"]) for s in samples]
    if any(not math.isfinite(v) for v in t+w) or any(v<0 for v in w) or any(
        y<=x for x,y in zip(t,t[1:])):
        raise RuntimeError(f"Invalid telemetry: {dest}")
    elapsed=t[-1]-t[0]
    horizon=(a.jobs*a.period_ms+a.startup_lead_ms)/1000
    energy=sum((w[i]+w[i-1])*.5*(t[i]-t[i-1]) for i in range(1,len(t)))
    response=[float(j["response_ms"]) for j in jobs]
    service=[float(j["service_ms"]) for j in jobs]
    changes=[j for j in jobs if int(j["from_khz"])!=int(j["target_khz"])]
    sw=[float(j["transition_ms"]) for j in changes]
    if any(not math.isfinite(v) or v<0 for v in response+service+sw):
        raise RuntimeError(f"Invalid job latency: {dest}")
    misses=sum(int(j["deadline_miss"]) for j in jobs)
    late_switches=sum(int(j["transition_late"]) for j in changes)
    pre_ready=sum(int(j["pre_release_ready"])==1 for j in changes)
    on_path=[float(j["transition_on_path_ms"]) for j in changes]
    on_time=len(jobs)-misses
    dynamic=dynamic_policy(label)
    hint=hint_spec(label)
    effective_leads=[]
    if hint:
        kind,bound=hint
        for i,j in enumerate(jobs):
            expected=bound*replay[i]["notice_token"]//1000000 if kind=="mixed" else bound
            requested=int(j["hint_requested_lead_ms"])
            due=float(j["hint_due_ms"])
            observed=float(j["hint_observed_ms"])
            available=float(j["hint_available_lead_ms"])
            release=float(j["release_ms"])
            if (requested!=expected or abs(due-(release-expected))>1e-4 or
                observed<due-1e-4 or abs(available-max(0,release-observed))>1e-4 or
                (int(j["from_khz"])!=int(j["target_khz"]) and
                 float(j["transition_start_ms"])<observed-1e-4)):
                raise RuntimeError(f"Hint timing/reveal violation: {dest} job {i}")
            effective_leads.append(available)
    if dynamic:
        valid_caps=all(int(j["cap_khz"])==int(j["target_khz"])==int(j["reported_freq_khz"]) for j in jobs)
    else:
        valid_caps=all(int(j["cap_khz"])==cap for j in jobs) and all(
            int(s["cap_khz"])==cap for s in samples)
    # Approximate telemetry window includes model initialization and trailing
    # idle; equal release horizon is enforced by runner --tail-idle.
    valid=valid_caps and .9<=elapsed/horizon<=1.1 and max(float(j["finish_ms"]) for j in jobs)<=1.1*horizon*1000
    telemetry=json.loads((dest/"telemetry_metadata.json").read_text())
    trace_valid=(not telemetry["trace_requested"]) or telemetry.get("trace_overrun") is False
    result=dict(policy=label,repeat=repeat,jobs=len(jobs),misses=misses,
        hint_kind=hint[0] if hint else "",hint_bound_ms=hint[1] if hint else -1,
        hint_median_available_ms=statistics.median(effective_leads) if effective_leads else -1,
        hint_late_observations=sum(float(j["hint_observed_ms"])>float(j["release_ms"])+.1 for j in jobs) if hint else 0,
        telemetry_mode=telemetry["mode"],pmic_samples=telemetry["pmic"]["samples"],
        pmic_median_gap_ms=telemetry["pmic"]["median_gap_ms"],
        pmic_p95_gap_ms=telemetry["pmic"]["p95_gap_ms"],
        trace_requested=int(telemetry["trace_requested"]),trace_valid=int(trace_valid),
        frequency_trace_rows=telemetry.get("trace_event_rows",0),
        miss_pct=100*misses/len(jobs),p95_service_ms=percentile(service,.95),
        p95_response_ms=percentile(response,.95),p99_response_ms=percentile(response,.99),
        max_response_ms=max(response),rail_w=energy/elapsed,
        approx_energy_mj_job=energy*1000/len(jobs),approx_energy_j=energy,
        sampled_s=elapsed,expected_horizon_s=horizon,coverage_ratio=elapsed/horizon,
        switches=len(changes),switch_p95_ms=percentile(sw,.95) if sw else 0.,
        late_switches=late_switches,pre_release_switches=pre_ready,
        on_path_switch_p95_ms=percentile(on_path,.95) if on_path else 0.,
        goodput_jobs_s=on_time/(a.jobs*a.period_ms/1000),
        energy_per_on_time_mj=energy*1000/on_time if on_time else None,
        switch_max_ms=max(sw) if sw else 0.,
        predicted_infeasible=sum(j["predicted_feasible"]=="0" for j in jobs) if dynamic else 0,
        max_temp_c=max(int(s["temp_mc"])/1000 for s in samples),
        valid=int(valid),run_dir=str(dest))
    # Keep deadline outcomes by workload/deadline; pooled latency alone hides
    # misses in tight jobs.
    groups={}
    for j in jobs:
        groups.setdefault((j["workload"],float(j["deadline_ms"])),[]).append(j)
    details=[]
    for (model,deadline),group in groups.items():
        times=[float(j["response_ms"]) for j in group]
        details.append(dict(policy=label,repeat=repeat,model=model,deadline_ms=deadline,
            jobs=len(group),misses=sum(int(j["deadline_miss"]) for j in group),
            p95_response_ms=percentile(times,.95),max_response_ms=max(times)))
    save(dest/"by_deadline.csv",details)
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cpu-profile-dir",type=Path)
    p.add_argument("--transition-dir",type=Path)
    p.add_argument("--models",nargs="+",choices=["small","medium","large"],
                   default=["small","medium","large","large"])
    p.add_argument("--deadlines",nargs="+",type=float,default=[18,45,68,100])
    p.add_argument("--threads",type=int,default=4)
    p.add_argument("--period-ms",type=int,default=500)
    p.add_argument("--jobs",type=int,default=60)
    p.add_argument("--repeats",type=int,default=1)
    p.add_argument("--native-governors",nargs="*",default=["original"])
    p.add_argument("--ablation",action="store_true",help="Include transition-blind selector")
    p.add_argument("--lead-ms",type=int,default=150,help="Look-ahead upscale lead")
    p.add_argument("--startup-lead-ms",type=int,default=200,help="Same initial release offset for all policies")
    p.add_argument("--lookahead-only",action="store_true",help="Only run V2 for a smoke test")
    p.add_argument("--hint-sweep",nargs="+",type=int,metavar="MS",
                   help="Randomized replay: compare native, oracle, and fixed hint leads")
    p.add_argument("--hint-mixed-ms",type=int,help="Add randomized 0..MS notice arm")
    p.add_argument("--hint-static-ms",type=int,help="Add static per-class frequency map with this notice lead")
    p.add_argument("--replay-seed",type=int,default=42)
    p.add_argument("--telemetry",choices=["legacy","multi"],default="multi")
    p.add_argument("--freq-ms",type=int,default=500)
    p.add_argument("--pmic-ms",type=int,default=137)
    p.add_argument("--temp-ms",type=int,default=500)
    p.add_argument("--idle-ms",type=int,default=1000)
    p.add_argument("--trace-buffer-kb",type=int,default=8192)
    p.add_argument("--no-frequency-trace",action="store_true")
    p.add_argument("--observer-check",action="store_true",
                   help="Compare legacy and multi telemetry using identical native governor workload")
    p.add_argument("--margin-ms",type=float,default=3)
    p.add_argument("--dwell-ms",type=float,default=250)
    p.add_argument("--min-saving",type=float,default=.05)
    p.add_argument("--use-max",action="store_true")
    p.add_argument("--thermal",action="store_true",help="Enable adaptive software thermal ceiling")
    p.add_argument("--thermal-low",type=float,default=55)
    p.add_argument("--thermal-high",type=float,default=60)
    p.add_argument("--start-temp",type=float,default=60)
    p.add_argument("--max-temp",type=float,default=75)
    p.add_argument("--policy",type=Path,default=Path("/sys/devices/system/cpu/cpufreq/policy0"))
    p.add_argument("--zone",type=Path,default=Path("/sys/class/thermal/thermal_zone0"))
    p.add_argument("--model-dir",type=Path,default=ROOT/"models/mobilenetv4")
    p.add_argument("--runner",type=Path,default=ROOT/"build/pm_runner")
    p.add_argument("--out",type=Path,default=ROOT/("results/cpu-dvfs-"+time.strftime("%Y%m%d-%H%M%S")))
    a=p.parse_args()
    if a.hint_sweep is not None:
        leads=a.hint_sweep+[v for v in (a.hint_mixed_ms,a.hint_static_ms) if v is not None]
        if (a.observer_check or a.lookahead_only or a.ablation or a.thermal or
            len(set(a.hint_sweep))!=len(a.hint_sweep) or
            any(v<0 or v>min(a.period_ms,a.startup_lead_ms) for v in leads)):
            p.error("Hint sweep needs distinct leads within period/startup, without other experiment modes or thermal")
    elif a.hint_mixed_ms is not None or a.hint_static_ms is not None:
        p.error("Mixed/static hints require --hint-sweep")
    if a.observer_check and a.lookahead_only:
        p.error("--observer-check and --lookahead-only are mutually exclusive")
    if not (1<=a.freq_ms<=10000 and 30<=a.pmic_ms<=10000 and
            50<=a.temp_ms<=10000 and 20<=a.idle_ms<=10000 and 128<=a.trace_buffer_kb<=32768):
        p.error("Invalid telemetry interval or trace buffer")
    if os.geteuid()!=0:
        p.error("Run with sudo")
    if not (len(a.models)==len(a.deadlines) and a.jobs>=20 and a.jobs%len(a.models)==0 and
            a.period_ms>=100 and all(math.isfinite(x) and 0<x<=a.period_ms for x in a.deadlines) and
            1<=a.threads<=4 and 1<=a.repeats<=10 and
            math.isfinite(a.margin_ms) and a.margin_ms>=0 and math.isfinite(a.dwell_ms) and
            a.dwell_ms>=0 and 0<=a.min_saving<1 and
            0<=a.lead_ms<=a.period_ms and 0<=a.startup_lead_ms<=1000 and
            0<a.thermal_low<a.thermal_high<=75 and 0<a.start_temp<a.max_temp<=75):
        p.error("Invalid workload/deadline cycle, timing, policy or thermal settings")
    for k in ("policy","zone","model_dir","runner","out"):
        setattr(a,k,getattr(a,k).resolve())
    if not a.runner.is_file() or read(a.zone/"type")!="cpu-thermal":
        p.error("Build runner and specify CPU thermal zone")
    cpu,trans,freqs=load_profiles(a)
    with open("/run/lock/pm-dvfs-profile.lock","w") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        sweep(a,cpu,trans,freqs)

def sweep(a,cpu,trans,freqs):
    capfile=a.policy/"scaling_max_freq"
    govfile=a.policy/"scaling_governor"
    minfile=a.policy/"scaling_min_freq"
    orig_cap=int(read(capfile))
    orig_min=int(read(minfile))
    orig_gov=read(govfile)
    governors=set(read(a.policy/"scaling_available_governors").split())
    available={int(x) for x in read(a.policy/"scaling_available_frequencies").split()}
    native=list(dict.fromkeys(orig_gov if g=="original" else g for g in a.native_governors))
    if orig_cap not in freqs or any(f not in available or not orig_min<=f<=orig_cap for f in freqs):
        raise RuntimeError("Original cap/min or supported frequencies do not match profile")
    if "performance" not in governors or any(g not in governors for g in native):
        raise RuntimeError(f"Governor unavailable: {sorted(governors)}")
    configs=[("fixed_low","performance",freqs[0]),("fixed_high","performance",freqs[-1])]
    configs.extend((g,g,orig_cap) for g in native if g!="performance")
    configs.append(("adaptive","performance",orig_cap))
    configs.append(("lookahead","performance",orig_cap))
    if a.ablation:
        configs.append(("transition_blind","performance",orig_cap))
    if a.lookahead_only:
        configs=[x for x in configs if x[0]=="lookahead"]
    if a.observer_check:
        configs=[("observer_legacy",orig_gov,orig_cap),("observer_multi",orig_gov,orig_cap)]
    if a.hint_sweep is not None:
        configs=[(g,g,orig_cap) for g in native]
        configs.append(("lookahead","performance",orig_cap))
        configs.extend((f"hint_fixed_{v}","performance",orig_cap) for v in a.hint_sweep)
        if a.hint_mixed_ms is not None:
            configs.append((f"hint_mixed_{a.hint_mixed_ms}","performance",orig_cap))
        if a.hint_static_ms is not None:
            configs.append((f"hint_static_{a.hint_static_ms}","performance",orig_cap))
    a.out.mkdir(parents=True,exist_ok=False)
    replay_paths={}
    replay_data={}
    if a.hint_sweep is not None:
        for repeat in range(1,a.repeats+1):
            replay=make_replay(a.models,a.deadlines,a.jobs,a.period_ms,a.replay_seed+repeat-1)
            path=a.out/f"replay-r{repeat}.csv"
            save(path,replay)
            replay_paths[repeat]=path
            replay_data[repeat]=replay
    meta=dict(policy=str(a.policy),governor=orig_gov,min_khz=orig_min,max_khz=orig_cap,
              cpu_profile=str(cpu),transition_profile=str(trans),configs=configs,
              cpu_costs_sha256=sha256(cpu/"cpu_costs.csv"),
              transition_costs_sha256=sha256(trans/"transition_costs.csv"),
              runner_sha256=sha256(a.runner),models=a.models,deadlines=a.deadlines,
              jobs=a.jobs,period_ms=a.period_ms,threads=a.threads,repeats=a.repeats,
              margin_ms=a.margin_ms,dwell_ms=a.dwell_ms,min_saving=a.min_saving,use_max=a.use_max,
              thermal=a.thermal,thermal_low=a.thermal_low,thermal_high=a.thermal_high,
              lead_ms=a.lead_ms,startup_lead_ms=a.startup_lead_ms,
              hint_sweep=a.hint_sweep,hint_mixed_ms=a.hint_mixed_ms,hint_static_ms=a.hint_static_ms,
              replay_seed=a.replay_seed,replay_sha256={str(k):sha256(v) for k,v in replay_paths.items()},
              telemetry=a.telemetry,observer_check=a.observer_check,
              freq_ms=a.freq_ms,pmic_ms=a.pmic_ms,temp_ms=a.temp_ms,idle_ms=a.idle_ms,
              trace_frequency=not a.no_frequency_trace,trace_buffer_kb=a.trace_buffer_kb,
              start_temp=a.start_temp,max_temp=a.max_temp,kernel=os.uname().release)
    (a.out/"original_settings.json").write_text(json.dumps(meta,indent=2)+"\n")
    print("Results:",a.out,flush=True)
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
        signal.signal(sig,interrupt)
    env=dict(os.environ,OMP_WAIT_POLICY="PASSIVE")
    results=[]
    try:
        for repeat in range(1,a.repeats+1):
            for label,gov,cap in (configs if repeat%2 else list(reversed(configs))):
                write(capfile,orig_cap)
                write(govfile,orig_gov)
                cool(a.zone,a.start_temp)
                write(capfile,cap)
                write(govfile,gov)
                if int(read(minfile))!=orig_min:
                    raise RuntimeError("CPUFreq minimum changed")
                dest=a.out/f"r{repeat}-{label}"
                dynamic=dynamic_policy(label)
                hint=hint_spec(label)
                telemetry=("legacy" if label=="observer_legacy" else "multi") if a.observer_check else a.telemetry
                collector=["--telemetry",telemetry,"--freq-ms",str(a.freq_ms),"--pmic-ms",str(a.pmic_ms),
                           "--temp-ms",str(a.temp_ms),"--idle-ms",str(a.idle_ms),
                           "--trace-buffer-kb",str(a.trace_buffer_kb)]
                if telemetry=="multi" and not a.no_frequency_trace:
                    collector.append("--trace-frequency")
                cmd=[sys.executable,str(ROOT/"scripts/run.py"),"--out",str(dest),
                     "--policy",str(a.policy),"--zone",str(a.zone),"--interval","0.137",
                     *collector,"--",str(a.runner),"--mode","hint" if hint else ("lookahead" if label=="lookahead" else ("dvfs" if dynamic else "cpu")),
                     "--gpu","0","--workload",",".join(a.models),"--model-dir",str(a.model_dir),
                     "--threads",str(a.threads),"--jobs",str(a.jobs),"--period-ms",str(a.period_ms),
                     "--deadlines-ms",",".join(map(str,a.deadlines)),"--tail-idle","1",
                     "--startup-lead-ms",str(a.startup_lead_ms),
                     "--cpufreq-policy",str(a.policy),"--thermal-zone",str(a.zone),
                     "--thermal","1" if dynamic and a.thermal else "0",
                     "--thermal-low",str(a.thermal_low),"--thermal-high",str(a.thermal_high)]
                if repeat in replay_paths:
                    cmd+=["--workload-trace",str(replay_paths[repeat])]
                if hint:
                    kind,bound=hint
                    cmd+=["--hint-lead-ms",str(bound),"--hint-mixed",str(int(kind=="mixed")),
                          "--hint-static",str(int(kind=="static"))]
                if dynamic:
                    cmd+=["--cpu-costs",str(cpu/"cpu_costs.csv"),"--transition-costs",str(trans/"transition_costs.csv"),
                          "--dvfs-margin-ms",str(a.margin_ms),"--dvfs-dwell-ms",str(a.dwell_ms),
                          "--dvfs-min-saving",str(a.min_saving),"--dvfs-use-max",str(int(a.use_max)),
                          "--dvfs-ignore-transition",str(int(label=="transition_blind")),
                          "--dvfs-lead-ms",str(a.lead_ms)]
                print(f"=== repeat {repeat}: {label} ===",flush=True)
                run_checked(cmd,env,a.zone,a.max_temp,a.out/f"{dest.name}.stderr.log")
                if int(read(capfile))!=cap or read(govfile)!=gov or int(read(minfile))!=orig_min:
                    raise RuntimeError("CPUFreq settings not restored by runner or external change")
                result=analyze(dest,label,repeat,a,cap,replay_data.get(repeat))
                results.append(result)
                save(a.out/"runs.csv",results)
                print(f'{label}: misses={result["misses"]}/{result["jobs"]}, '
                      f'energy/job={result["approx_energy_mj_job"]:.3f} mJ, '
                      f'switches={result["switches"]}, valid={result["valid"]}',flush=True)
    finally:
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
            signal.signal(sig,signal.SIG_IGN)
        errors=[]
        for path,value in ((capfile,orig_cap),(govfile,orig_gov)):
            try:
                write(path,value)
            except Exception as e:
                errors.append(str(e))
        if errors:
            raise RuntimeError("RESTORE FAILED: "+"; ".join(errors))
        print(f"Restored cap={orig_cap}, governor={orig_gov}; min unchanged",flush=True)
    summary=[]
    for label,_,_ in configs:
        group=[x for x in results if x["policy"]==label]
        if len(group)!=a.repeats or not all(x["valid"] for x in group):
            print(f"Excluded invalid/incomplete {label}",file=sys.stderr)
            continue
        x=dict(policy=label,repeats=len(group),jobs=sum(g["jobs"] for g in group),
               misses=sum(g["misses"] for g in group))
        x["miss_pct"]=100*x["misses"]/x["jobs"]
        for key in ("p95_response_ms","p99_response_ms","rail_w","approx_energy_mj_job","switches","switch_p95_ms","on_path_switch_p95_ms"):
            x[key]=statistics.median(g[key] for g in group)
        x["hint_kind"]=group[0]["hint_kind"]
        x["hint_bound_ms"]=group[0]["hint_bound_ms"]
        x["hint_median_available_ms"]=statistics.median(g["hint_median_available_ms"] for g in group)
        x["hint_late_observations"]=sum(g["hint_late_observations"] for g in group)
        x["telemetry_mode"]=group[0]["telemetry_mode"]
        x["pmic_median_gap_ms"]=statistics.median(g["pmic_median_gap_ms"] for g in group)
        x["trace_valid"]=int(all(g["trace_valid"] for g in group))
        x["late_switches"]=sum(g["late_switches"] for g in group)
        x["pre_release_switches"]=sum(g["pre_release_switches"] for g in group)
        x["total_switches"]=sum(g["switches"] for g in group)
        x["goodput_jobs_s"]=(x["jobs"]-x["misses"])/(x["jobs"]*a.period_ms/1000)
        x["energy_per_on_time_mj"]=1000*sum(g["approx_energy_j"] for g in group)/(x["jobs"]-x["misses"]) if x["jobs"]>x["misses"] else None
        summary.append(x)
    if not summary:
        raise RuntimeError("No valid configurations")
    save(a.out/"summary.csv",summary)
    print("\npolicy,misses,jobs,p95_response_ms,energy_mj_job,rail_w,switches,switch_p95_ms,on_path_switch_p95_ms,pre_release_switches,total_switches,late_switches,goodput,energy_per_on_time_mj,hint_median_available_ms")
    for x in summary:
        print(f'{x["policy"]},{x["misses"]},{x["jobs"]},{x["p95_response_ms"]:.3f},'
              f'{x["approx_energy_mj_job"]:.3f},{x["rail_w"]:.3f},{x["switches"]},{x["switch_p95_ms"]:.3f},'
              f'{x["on_path_switch_p95_ms"]:.3f},{x["pre_release_switches"]},{x["total_switches"]},'
              f'{x["late_switches"]},{x["goodput_jobs_s"]:.6f},{x["energy_per_on_time_mj"]},{x["hint_median_available_ms"]:.3f}')

if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted",file=sys.stderr)
        sys.exit(130)
    except (OSError,ValueError,RuntimeError,KeyError) as e:
        print(f"benchmark_cpu_dvfs: {e}",file=sys.stderr)
        sys.exit(1)
