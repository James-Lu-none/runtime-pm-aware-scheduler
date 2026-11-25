#!/usr/bin/env python3
"""Measure directed CPUFreq transitions, with isolated tracefs and monotonic timing."""
import argparse
import csv
import fcntl
import itertools
import json
import math
import os
from pathlib import Path
import random
import re
import signal
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext

from profile_dvfs import ROOT, read, write, cool, interrupt, percentile

# tracefs mono clock shares CLOCK_MONOTONIC's time domain. Text output
# normally has microsecond resolution; avoid floating-point epoch arithmetic.
EVENT = re.compile(r"\s(\d+)\.(\d+):\s+(?:power:)?(cpu_frequency_limits|cpu_frequency):\s+(.*)$")
FIELD = re.compile(r"\b(\w+)=(\d+)\b")

def trace_events(text, start_ns, end_ns, cpus, target):
    matches = {"cpu_frequency": [], "cpu_frequency_limits": []}
    for line in text.splitlines():
        m = EVENT.search(line)
        if not m:
            continue
        sec, fraction, kind, fields = m.groups()
        ns = int(sec)*10**9 + int((fraction+"000000000")[:9])
        # Text trace timestamps may be truncated to microseconds.
        if ns < start_ns-999 or ns > end_ns:
            continue
        data = {k:int(v) for k,v in FIELD.findall(fields)}
        if data.get("cpu_id") not in cpus:
            continue
        if data.get("state" if kind=="cpu_frequency" else "max")==target:
            matches[kind].append(max(0,ns-start_ns)/1e6)
    return {k:sorted(v) for k,v in matches.items()}

def trace_overrun(text):
    m = re.search(r"entries-in-buffer/entries-written:\s*(\d+)/(\d+)",text)
    return bool(m and int(m[1])!=int(m[2]))

class Trace:
    def __init__(self, path):
        self.path=path
        self.events=[]
    def setup(self):
        (self.path/"tracing_on").write_text("0\n")
        (self.path/"current_tracer").write_text("nop\n")
        (self.path/"trace_clock").write_text("mono\n")
        if "[mono]" not in read(self.path/"trace_clock"):
            raise RuntimeError("Trace clock is not mono; cannot correlate timestamps")
        for name in ("cpu_frequency","cpu_frequency_limits"):
            enable=self.path/"events/power"/name/"enable"
            if enable.exists():
                enable.write_text("1\n")
                self.events.append(name)
        if "cpu_frequency" not in self.events:
            raise RuntimeError("power:cpu_frequency unavailable; use --no-trace only for sysfs timing")
    def start(self):
        (self.path/"tracing_on").write_text("0\n")
        (self.path/"trace").write_text("\n")
        (self.path/"tracing_on").write_text("1\n")
    def marker(self, text):
        (self.path/"trace_marker").write_text(text+"\n")
    def stop(self):
        (self.path/"tracing_on").write_text("0\n")
        return (self.path/"trace").read_text()

@contextmanager
def isolated_trace(root):
    parent=root/"instances"
    if not parent.is_dir():
        raise RuntimeError("tracefs instances unavailable; mount tracefs or use --no-trace")
    instance=parent/f"pm-dvfs-{os.getpid()}"
    instance.mkdir()
    trace=Trace(instance)
    try:
        trace.setup()
        yield trace
    finally:
        try:
            (instance/"tracing_on").write_text("0\n")
            for event in trace.events:
                (instance/"events/power"/event/"enable").write_text("0\n")
        finally:
            instance.rmdir()

def prepare(policy, source, timeout_s):
    # Slow stable readback is appropriate for setup, not for measured switching.
    write(policy/"scaling_max_freq",source)
    deadline=time.monotonic()+timeout_s
    stable=0
    while time.monotonic()<deadline:
        if int(read(policy/"scaling_cur_freq"))==source:
            stable+=1
            if stable>=3:
                return
        else:
            stable=0
        time.sleep(.005)
    raise RuntimeError(f"Source frequency {source} not observed before trial")

def measure(policy, target, timeout_s, poll_s):
    capfile=policy/"scaling_max_freq"
    freqfile=policy/"scaling_cur_freq"
    # No slow stable-write helper in the measured path. This includes sysfs
    # open/write/close and subsequent userspace observation delays.
    start=time.monotonic_ns()
    capfile.write_text(str(target)+"\n")
    returned=time.monotonic_ns()
    deadline=start+int(timeout_s*1e9)
    cap_seen=freq_seen=None
    count=0
    last_cap=last_freq=None
    while True:
        last_cap=int(read(capfile))
        cap_at=time.monotonic_ns()
        last_freq=int(read(freqfile))
        freq_at=time.monotonic_ns()
        count+=1
        if cap_seen is None and last_cap==target:
            cap_seen=cap_at
        if freq_seen is None and last_freq==target:
            freq_seen=freq_at
        if last_cap==target and last_freq==target:
            break
        if freq_at>=deadline:
            break
        if poll_s:
            time.sleep(poll_s)
    done=time.monotonic_ns()
    def delta(t):
        return (t-start)/1e6 if t is not None else None
    return dict(start_ns=start,returned_ns=returned,done_ns=done,
                write_ms=delta(returned),policy_readback_ms=delta(cap_seen),
                freq_readback_ms=delta(freq_seen),ready_ms=delta(done),
                polls=count,last_cap_khz=last_cap,last_freq_khz=last_freq,
                valid=int(last_cap==target and last_freq==target and done<=deadline))

def aggregate(rows, freqs):
    out=[]
    metrics=("write_ms","policy_readback_ms","freq_readback_ms","ready_ms",
             "kernel_limits_event_ms","kernel_frequency_event_ms")
    for src,dst in itertools.permutations(freqs,2):
        group=[r for r in rows if r["from_khz"]==src and r["to_khz"]==dst]
        good=[r for r in group if r["valid"]]
        row=dict(from_khz=src,to_khz=dst,samples=len(group),valid_samples=len(good),
                 frequency_trace_samples=sum(r["kernel_frequency_event_ms"] is not None for r in good),
                 limits_trace_samples=sum(r["kernel_limits_event_ms"] is not None for r in good))
        for metric in metrics:
            vals=[r[metric] for r in good if r[metric] is not None]
            for name,value in (("p50",statistics.median(vals) if vals else None),
                               ("p95",percentile(vals,.95) if vals else None),
                               ("max",max(vals) if vals else None)):
                row[f"{metric}_{name}"]=value
        out.append(row)
    return out

def save_csv(path, rows):
    if not rows:
        return
    with path.open("w") as file:
        writer=csv.DictWriter(file,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--freqs",nargs="+",type=int,default=[1500000,2000000,2400000])
    p.add_argument("--samples",type=int,default=30,help="Trials per directed pair")
    p.add_argument("--poll-us",type=int,default=100,help="Requested sleep between sysfs polls; 0 busy polls")
    p.add_argument("--timeout-ms",type=int,default=1000)
    p.add_argument("--settle-ms",type=int,default=100)
    p.add_argument("--seed",type=int,default=42)
    p.add_argument("--policy",type=Path,default=Path("/sys/devices/system/cpu/cpufreq/policy0"))
    p.add_argument("--zone",type=Path,default=Path("/sys/class/thermal/thermal_zone0"))
    p.add_argument("--tracefs",type=Path,default=Path("/sys/kernel/tracing"))
    p.add_argument("--no-trace",action="store_true",help="Explicitly collect sysfs-only timing")
    p.add_argument("--load-workers",type=int,default=0,choices=range(4),
                   help="Optional stress-ng matrixprod background CPU workers (0..3)")
    p.add_argument("--start-temp",type=float,default=60)
    p.add_argument("--max-temp",type=float,default=75)
    p.add_argument("--out",type=Path,default=ROOT/("results/dvfs-transition-"+time.strftime("%Y%m%d-%H%M%S")))
    a=p.parse_args()
    if os.geteuid()!=0:
        p.error("Run with sudo")
    if not (2<=len(a.freqs)<=10 and len(set(a.freqs))==len(a.freqs) and
            1<=a.samples<=1000 and 0<=a.poll_us<=10000 and 10<=a.timeout_ms<=5000 and
            0<=a.settle_ms<=1000 and 0<a.start_temp<a.max_temp<=75):
        p.error("Invalid frequencies, sample count, timing or thermal limits")
    for key in ("policy","zone","tracefs","out"):
        setattr(a,key,getattr(a,key).resolve())
    if read(a.zone/"type")!="cpu-thermal":
        p.error("Expected cpu-thermal zone")
    with open("/run/lock/pm-dvfs-profile.lock","w") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        sweep(a)

def sweep(a):
    capfile=a.policy/"scaling_max_freq"
    govfile=a.policy/"scaling_governor"
    minfile=a.policy/"scaling_min_freq"
    orig_cap=int(read(capfile))
    orig_min=int(read(minfile))
    orig_gov=read(govfile)
    cpus={int(x) for x in read(a.policy/"related_cpus").split()}
    available={int(x) for x in read(a.policy/"scaling_available_frequencies").split()}
    if any(f not in available or not orig_min<=f<=orig_cap for f in a.freqs):
        raise RuntimeError("Frequencies must be supported and within original CPUFreq limits")
    if "performance" not in read(a.policy/"scaling_available_governors").split():
        raise RuntimeError("performance governor unavailable")
    a.out.mkdir(parents=True,exist_ok=False)
    meta=dict(policy=str(a.policy),governor=orig_gov,min_khz=orig_min,max_khz=orig_cap,
              related_cpus=sorted(cpus),freqs=a.freqs,samples_per_pair=a.samples,
              poll_us=a.poll_us,timeout_ms=a.timeout_ms,settle_ms=a.settle_ms,seed=a.seed,
              load_workers=a.load_workers,trace=not a.no_trace,trace_clock="mono" if not a.no_trace else None,
              start_temp_c=a.start_temp,max_temp_c=a.max_temp,kernel=os.uname().release)
    (a.out/"original_settings.json").write_text(json.dumps(meta,indent=2)+"\n")
    print("Results:",a.out,flush=True)
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
        signal.signal(sig,interrupt)
    load=None
    rows=[]
    plan=list(itertools.permutations(a.freqs,2))*a.samples
    random.Random(a.seed).shuffle(plan)
    try:
        cool(a.zone,a.start_temp)
        write(govfile,"performance")
        ctx=nullcontext(None) if a.no_trace else isolated_trace(a.tracefs)
        with ctx as trace, (a.out/"trace.txt").open("w") as raw, (a.out/"transitions.csv").open("w") as file, (a.out/"load.log").open("w") as loadlog:
            if trace:
                meta["trace_events"]=trace.events
                (a.out/"original_settings.json").write_text(json.dumps(meta,indent=2)+"\n")
            if a.load_workers:
                load=subprocess.Popen(["stress-ng","--cpu",str(a.load_workers),
                                       "--cpu-method","matrixprod","--timeout","0s"],
                                      stdout=loadlog,stderr=loadlog,start_new_session=True)
                time.sleep(.5)
            writer=None
            for trial,(source,target) in enumerate(plan,1):
                if load and load.poll() is not None:
                    raise RuntimeError("stress-ng exited; see load.log")
                temp=int(read(a.zone/"temp"))/1000
                if temp>=a.max_temp:
                    raise RuntimeError("Thermal guard reached; stopped profiling")
                if read(govfile)!="performance" or int(read(minfile))!=orig_min:
                    raise RuntimeError("External CPUFreq setting change")
                prepare(a.policy,source,a.timeout_ms/1000)
                time.sleep(a.settle_ms/1000)
                if int(read(capfile))!=source or int(read(a.policy/"scaling_cur_freq"))!=source:
                    raise RuntimeError("Source frequency changed before trial")
                if trace:
                    trace.start()
                    trace.marker(f"BEGIN trial={trial} from={source} to={target}")
                result=measure(a.policy,target,a.timeout_ms/1000,a.poll_us/1e6)
                if trace:
                    # Allow late trace events to be recorded, without adding this
                    # delay to userspace ready_ms.
                    time.sleep(.005)
                    end_ns=time.monotonic_ns()
                    trace.marker(f"END trial={trial}")
                    log=trace.stop()
                    raw.write(f"\n### trial={trial} from={source} to={target}\n"+log)
                    raw.flush()
                    events=trace_events(log,result["start_ns"],end_ns,cpus,target)
                    if trace_overrun(log):
                        events={k:[] for k in events}
                        result["valid"]=0
                else:
                    events={"cpu_frequency":[],"cpu_frequency_limits":[]}
                # Verify the observed destination did not immediately revert.
                if int(read(capfile))!=target or int(read(a.policy/"scaling_cur_freq"))!=target:
                    result["valid"]=0
                row=dict(trial=trial,from_khz=source,to_khz=target,temp_c=temp,**result,
                         kernel_limits_event_ms=next(iter(events["cpu_frequency_limits"]),None),
                         kernel_frequency_event_ms=next(iter(events["cpu_frequency"]),None),
                         kernel_frequency_last_event_ms=events["cpu_frequency"][-1] if events["cpu_frequency"] else None,
                         kernel_frequency_event_count=len(events["cpu_frequency"]))
                rows.append(row)
                if writer is None:
                    writer=csv.DictWriter(file,fieldnames=list(row))
                    writer.writeheader()
                writer.writerow(row)
                file.flush()
                if not row["valid"]:
                    print(f"Invalid trial {trial}: {source}->{target}",file=sys.stderr)
                if trial%max(1,len(a.freqs)*(len(a.freqs)-1)*5)==0 or trial==len(plan):
                    print(f"Completed {trial}/{len(plan)} transitions",flush=True)
    finally:
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
            signal.signal(sig,signal.SIG_IGN)
        if load and load.poll() is None:
            try:
                os.killpg(load.pid,signal.SIGTERM)
                load.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(load.pid,signal.SIGKILL)
                load.wait()
            except ProcessLookupError:
                load.wait()
        errors=[]
        for path,value in ((capfile,orig_cap),(govfile,orig_gov)):
            try:
                write(path,value)
            except Exception as e:
                errors.append(str(e))
        if errors:
            raise RuntimeError("RESTORE FAILED: "+"; ".join(errors))
        print(f"Restored cap={orig_cap}, governor={orig_gov}; min unchanged",flush=True)
    summary=aggregate(rows,a.freqs)
    save_csv(a.out/"summary.csv",summary)
    # Only fully observed pairs enter the initial controller cost matrix.
    matrix=[dict(from_khz=r["from_khz"],to_khz=r["to_khz"],samples=r["valid_samples"],
                 ready_p95_ms=r["ready_ms_p95"],ready_max_ms=r["ready_ms_max"])
            for r in summary if r["valid_samples"]==a.samples]
    matrix.extend(dict(from_khz=f,to_khz=f,samples=0,ready_p95_ms=0,ready_max_ms=0) for f in a.freqs)
    save_csv(a.out/"transition_costs.csv",matrix)
    print("from_khz,to_khz,valid_samples,write_p95_ms,policy_p95_ms,freq_readback_p95_ms,kernel_event_p95_ms,ready_p95_ms")
    def fmt(x):
        return f"{x:.3f}" if x is not None else "NA"
    for row in summary:
        print(",".join([str(row[k]) for k in ("from_khz","to_khz","valid_samples")]+
                       [fmt(row[k]) for k in ("write_ms_p95","policy_readback_ms_p95",
                        "freq_readback_ms_p95","kernel_frequency_event_ms_p95","ready_ms_p95")]))
    if not a.no_trace and any(r["frequency_trace_samples"]<r["valid_samples"] for r in summary):
        print("WARNING: missing kernel frequency events; see summary.csv and trace.txt; sysfs timings remain separate",file=sys.stderr)
    if any(r["valid_samples"]!=a.samples for r in summary):
        raise RuntimeError("Some pairs have invalid trials; cost matrix is incomplete")

if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Profiling interrupted; partial raw trials retained",file=sys.stderr)
        sys.exit(130)
    except (OSError,RuntimeError,ValueError) as e:
        print(f"profile_dvfs_transition: {e}",file=sys.stderr)
        sys.exit(1)
