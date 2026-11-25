#!/usr/bin/env python3
"""Run the workload with identical, low-rate PMIC telemetry for every policy."""
import argparse
import csv
import pathlib
import re
import subprocess
import time
import signal

def read(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return ""

def adc():
    try:
        text = subprocess.check_output(["vcgencmd", "pmic_read_adc"], text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return {}
    return {m[0]: float(m[1]) for m in re.findall(r"^\s*(\w+)\s+[^\n=]*=\s*([+-]?[\d.]+)\s*[AV]\s*$", text, re.M)}

p = argparse.ArgumentParser()
p.add_argument("--out", required=True)
p.add_argument("--gpu-device", default="")
p.add_argument("--zone", default="/sys/class/thermal/thermal_zone0")
p.add_argument("--policy", default="/sys/devices/system/cpu/cpufreq/policy0")
p.add_argument("--interval", type=float, default=0.5)
p.add_argument("command", nargs=argparse.REMAINDER)
a = p.parse_args()
cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
if not cmd or not 0.03 <= a.interval <= 10:
    p.error("provide runner command after -- and interval 0.03..10 s")
out = pathlib.Path(a.out)
out.mkdir(parents=True, exist_ok=False)
(out / "command.txt").write_text(repr(cmd) + "\n")
with (out / "jobs.csv").open("w") as jobs, (out / "telemetry.csv").open("w") as f:
    writer = csv.writer(f)
    writer.writerow(["monotonic_s","temp_mc","freq_khz","cap_khz","gpu_state","core_v","core_a","core_w"])
    process = subprocess.Popen(cmd, stdout=jobs)
    def stop(signum, frame):
        if process.poll() is None:
            process.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while process.poll() is None:
            begin = time.monotonic()
            d = adc()
            v, i = d.get("VDD_CORE_V"), d.get("VDD_CORE_A")
            writer.writerow([begin, read(a.zone+"/temp"),read(a.policy+"/scaling_cur_freq"),
                read(a.policy+"/scaling_max_freq"), read(a.gpu_device+"/power/runtime_status") if a.gpu_device else "unknown",
                v, i, v*i if v is not None and i is not None else None])
            f.flush()
            time.sleep(max(0,a.interval-(time.monotonic()-begin)))
    finally:
        if process.poll() is None:
            process.terminate()
        code = process.wait()
    (out/"exit_code.txt").write_text(str(code)+"\n")
raise SystemExit(code)
