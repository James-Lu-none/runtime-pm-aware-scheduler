#!/usr/bin/env python3
"""Run a workload with backward-compatible legacy or multi-rate telemetry."""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import threading
import time
from contextlib import nullcontext

from profile_dvfs_transition import isolated_trace, trace_overrun, EVENT, FIELD

FIELDS = ["monotonic_s","temp_mc","freq_khz","cap_khz","gpu_state","core_v","core_a","core_w"]

def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""

def adc():
    try:
        text = subprocess.check_output(["vcgencmd","pmic_read_adc"],text=True,timeout=1)
    except (OSError,subprocess.SubprocessError):
        return {}
    return {m[0]:float(m[1]) for m in re.findall(
        r"^\s*(\w+)\s+[^\n=]*=\s*([+-]?[\d.]+)\s*[AV]\s*$",text,re.M)}

def next_due(previous, interval, now):
    # Skip missed ticks, never catch up with a burst of samples.
    return previous+max(1,math.floor((now-previous)/interval)+1)*interval

def timing_stats(times):
    gaps=sorted((b-a)*1000 for a,b in zip(times,times[1:]))
    return dict(samples=len(times),median_gap_ms=statistics.median(gaps) if gaps else None,
                p95_gap_ms=gaps[math.ceil(.95*len(gaps))-1] if gaps else None,
                max_gap_ms=max(gaps) if gaps else None)

def cpuidle_states(policy, root=Path("/sys/devices/system/cpu")):
    states=[]
    for cpu in read(policy/"related_cpus").split():
        for state in sorted((root/f"cpu{cpu}"/"cpuidle").glob("state*")):
            if (state/"time").is_file() and (state/"usage").is_file():
                states.append((int(cpu),state.name,read(state/"name"),state))
    return states

def trace_to_csv(text,path,cpus):
    count=0
    with path.open("w") as f:
        writer=csv.writer(f)
        writer.writerow(["monotonic_s","event","cpu_id","freq_khz","min_khz","max_khz"])
        for line in text.splitlines():
            m=EVENT.search(line)
            if not m:
                continue
            sec,frac,event,fields=m.groups()
            data=dict(FIELD.findall(fields))
            if "cpu_id" not in data or int(data["cpu_id"]) not in cpus:
                continue
            writer.writerow([sec+"."+frac,event,data["cpu_id"],data.get("state",""),
                             data.get("min",""),data.get("max","")])
            count+=1
    return count

class SysfsSampler:
    def __init__(self,a,out):
        self.a=a
        self.out=out
        self.lock=threading.Lock()
        self.stop=threading.Event()
        self.error=None
        self.latest={}
        self.times={k:[] for k in ("frequency","temperature","cpuidle")}
        self.states=cpuidle_states(Path(a.policy))
        self.worker=threading.Thread(target=self.run,name="sysfs-telemetry")

    def frequency(self):
        begin=time.monotonic()
        freq=read(Path(self.a.policy)/"scaling_cur_freq")
        cap=read(Path(self.a.policy)/"scaling_max_freq")
        end=time.monotonic()
        self.times["frequency"].append(end)
        with self.lock:
            self.latest["frequency"]=(end,freq,cap)
        return end,freq,cap,(end-begin)*1000

    def temperature(self):
        begin=time.monotonic()
        temp=read(Path(self.a.zone)/"temp")
        end=time.monotonic()
        self.times["temperature"].append(end)
        with self.lock:
            self.latest["temperature"]=(end,temp)
        return end,temp,(end-begin)*1000

    def snapshot(self):
        with self.lock:
            return self.latest["frequency"],self.latest["temperature"]

    def run(self):
        try:
            with (self.out/"frequency.csv").open("w") as ff, \
                 (self.out/"temperature.csv").open("w") as tf, \
                 (self.out/"cpuidle.csv").open("w") as idle:
                fw,tw,iw=csv.writer(ff),csv.writer(tf),csv.writer(idle)
                fw.writerow(["monotonic_s","freq_khz","cap_khz","read_ms"])
                tw.writerow(["monotonic_s","temp_mc","read_ms"])
                iw.writerow(["monotonic_s","cpu","state","name","time_us","usage"])
                steps={"frequency":self.a.freq_ms/1000,"temperature":self.a.temp_ms/1000,
                       "cpuidle":self.a.idle_ms/1000}
                due={k:time.monotonic() for k in steps}
                if not self.states:
                    del due["cpuidle"]
                while not self.stop.is_set():
                    now=time.monotonic()
                    for k in due:
                        if now<due[k]:
                            continue
                        if k=="frequency":
                            fw.writerow(self.frequency())
                        elif k=="temperature":
                            tw.writerow(self.temperature())
                        else:
                            self.times[k].append(time.monotonic())
                            for cpu,state,name,path in self.states:
                                t=read(path/"time")
                                usage=read(path/"usage")
                                iw.writerow([time.monotonic(),cpu,state,name,t,usage])
                        due[k]=next_due(due[k],steps[k],time.monotonic())
                    # Buffered output avoids flushing at 200 Hz.
                    self.stop.wait(max(0,min(due.values())-time.monotonic()))
        except Exception as exc:
            self.error=exc

    def __enter__(self):
        self.worker.start()
        # Wait for real initial readings instead of fabricating cached values.
        deadline=time.monotonic()+5
        try:
            while time.monotonic()<deadline:
                if self.error:
                    raise self.error
                with self.lock:
                    if "frequency" in self.latest and "temperature" in self.latest:
                        return self
                time.sleep(.001)
            raise RuntimeError("Initial sysfs telemetry unavailable")
        except BaseException:
            self.stop.set()
            self.worker.join()
            raise

    def __exit__(self,*args):
        self.stop.set()
        self.worker.join()

def collect(a,cmd,out,trace,sampler,meta):
    pmic_times=[]
    adc_ms=[]
    extra=["adc_begin_s","adc_end_s","frequency_sample_s","temperature_sample_s",
           "frequency_age_ms","temperature_age_ms"] if sampler else []
    with (out/"jobs.csv").open("w") as jobs,(out/"telemetry.csv").open("w") as f:
        writer=csv.writer(f)
        writer.writerow(FIELDS+extra)
        if trace:
            trace.start()
            trace.marker("WORKLOAD_BEGIN")
        child=None
        interrupted=False
        def stop(signum,frame):
            nonlocal interrupted
            interrupted=True
            if child is not None and child.poll() is None:
                child.terminate()
        old={sig:signal.signal(sig,stop) for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
        try:
            child=subprocess.Popen(cmd,stdout=jobs)
            if interrupted:
                child.terminate()
            meta["process_start_s"]=time.monotonic()
            while child.poll() is None:
                if sampler and sampler.error:
                    raise sampler.error
                begin=time.monotonic()
                d=adc()
                adc_end=time.monotonic()
                v,i=d.get("VDD_CORE_V"),d.get("VDD_CORE_A")
                power=v*i if v is not None and i is not None else None
                gpu=read(Path(a.gpu_device)/"power/runtime_status") if a.gpu_device else "unknown"
                if sampler:
                    (ft,freq,cap),(tt,temp)=sampler.snapshot()
                    stamp=time.monotonic()
                    writer.writerow([stamp,temp,freq,cap,gpu,v,i,power,begin,adc_end,ft,tt,
                                     (stamp-ft)*1000,(stamp-tt)*1000])
                else:
                    stamp=begin
                    writer.writerow([stamp,read(Path(a.zone)/"temp"),
                        read(Path(a.policy)/"scaling_cur_freq"),
                        read(Path(a.policy)/"scaling_max_freq"),gpu,v,i,power])
                pmic_times.append(stamp)
                adc_ms.append((adc_end-begin)*1000)
                f.flush()
                interval=a.pmic_ms/1000 if sampler else a.interval
                time.sleep(max(0,interval-(time.monotonic()-begin)))
        finally:
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                code=child.wait()
                meta["process_end_observed_s"]=time.monotonic()
                (out/"exit_code.txt").write_text(str(code)+"\n")
            for sig,handler in old.items():
                signal.signal(sig,handler)
        if interrupted:
            code=130
    meta["pmic"]=timing_stats(pmic_times)
    meta["adc_call_median_ms"]=statistics.median(adc_ms) if adc_ms else None
    meta["adc_call_max_ms"]=max(adc_ms) if adc_ms else None
    return code

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",required=True)
    p.add_argument("--gpu-device",default="")
    p.add_argument("--zone",default="/sys/class/thermal/thermal_zone0")
    p.add_argument("--policy",default="/sys/devices/system/cpu/cpufreq/policy0")
    p.add_argument("--interval",type=float,default=.5,help="Legacy sampling interval in seconds")
    p.add_argument("--telemetry",choices=["legacy","multi"],default="legacy")
    p.add_argument("--freq-ms",type=int,default=5)
    p.add_argument("--pmic-ms",type=int,default=30)
    p.add_argument("--temp-ms",type=int,default=500)
    p.add_argument("--idle-ms",type=int,default=100)
    p.add_argument("--trace-frequency",action="store_true")
    p.add_argument("--tracefs",type=Path,default=Path("/sys/kernel/tracing"))
    p.add_argument("--trace-buffer-kb",type=int,default=8192,help="Per-CPU trace buffer size")
    p.add_argument("command",nargs=argparse.REMAINDER)
    a=p.parse_args()
    cmd=a.command[1:] if a.command[:1]==["--"] else a.command
    if not cmd or not .03<=a.interval<=10 or not (
        1<=a.freq_ms<=10000 and 30<=a.pmic_ms<=10000 and
        50<=a.temp_ms<=10000 and 20<=a.idle_ms<=10000 and 128<=a.trace_buffer_kb<=32768):
        p.error("Invalid command or telemetry interval")
    out=Path(a.out)
    out.mkdir(parents=True,exist_ok=False)
    (out/"command.txt").write_text(repr(cmd)+"\n")
    meta=dict(mode=a.telemetry,legacy_interval_s=a.interval,freq_ms=a.freq_ms,pmic_ms=a.pmic_ms,
              temp_ms=a.temp_ms,idle_ms=a.idle_ms,trace_requested=a.trace_frequency,
              trace_buffer_kb_per_cpu=a.trace_buffer_kb,clock="CLOCK_MONOTONIC")
    observer_start=time.process_time()
    wall_start=time.monotonic()
    ctx=isolated_trace(a.tracefs) if a.trace_frequency else nullcontext(None)
    sampler=SysfsSampler(a,out) if a.telemetry=="multi" else None
    try:
        with ctx as trace:
            if trace:
                (trace.path/"buffer_size_kb").write_text(str(a.trace_buffer_kb)+"\n")
                meta["trace_events"]=trace.events
            try:
                with (sampler if sampler else nullcontext()):
                    code=collect(a,cmd,out,trace,sampler,meta)
            finally:
                if trace:
                    trace.marker("WORKLOAD_END")
                    log=trace.stop()
                    (out/"frequency_trace.txt").write_text(log)
                    cpus={int(x) for x in read(Path(a.policy)/"related_cpus").split()}
                    meta["trace_event_rows"]=trace_to_csv(log,out/"frequency_events.csv",cpus)
                    meta["trace_overrun"]=trace_overrun(log)
                    if meta["trace_overrun"]:
                        print("WARNING: frequency trace buffer overflow; trace is incomplete",flush=True)
        if sampler:
            if sampler.error:
                raise sampler.error
            meta["streams"]={k:timing_stats(v) for k,v in sampler.times.items()}
            meta["cpuidle_states"]=len(sampler.states)
    finally:
        meta["observer_python_cpu_s"]=time.process_time()-observer_start
        meta["observer_wall_s"]=time.monotonic()-wall_start
        # Python process CPU time excludes vcgencmd children and kernel work.
        (out/"telemetry_metadata.json").write_text(json.dumps(meta,indent=2)+"\n")
    return code

if __name__=="__main__":
    raise SystemExit(main())
