#!/usr/bin/env python3
"""Verify and characterize each model on CPU and physical Vulkan GPU."""
import argparse
from pathlib import Path
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--models", nargs="+", choices=["small","medium","large"], default=["small","medium","large"])
p.add_argument("--model-dir", default="models/mobilenetv4")
p.add_argument("--runner", default="./build/pm_runner")
p.add_argument("--gpu-device", default=str(Path("/sys/class/drm/renderD128/device").resolve()))
p.add_argument("--gpu-index", type=int, default=0)
p.add_argument("--threads", type=int, default=2)
p.add_argument("--jobs", type=int, default=30)
p.add_argument("--period-ms", type=int, default=2000)
p.add_argument("--out", default="results/mnv4-"+time.strftime("%Y%m%d-%H%M%S"))
a=p.parse_args()
out=Path(a.out)
out.mkdir(parents=True,exist_ok=False)
for model in a.models:
    common=["--workload",model,"--model-dir",a.model_dir,"--gpu-index",str(a.gpu_index),
            "--gpu-device",a.gpu_device,"--threads",str(a.threads)]
    ref=Path(a.model_dir)/f"{model}.reference.txt"
    result=subprocess.check_output([a.runner,"--mode","gpu",*common,"--verify","1",
                                    "--reference",str(ref)],text=True)
    print(result,flush=True)
    (out/f"{model}-verify.txt").write_text(result)
    for backend in ("cpu","gpu"):
        dest=out/f"{model}-{backend}"
        subprocess.run([sys.executable,"scripts/run.py","--out",str(dest),"--gpu-device",a.gpu_device,
                        "--interval","0.137","--",a.runner,"--mode",backend,*common,
                        "--jobs",str(a.jobs),"--period-ms",str(a.period_ms),"--deadline-ms",str(a.period_ms)],check=True)
        print(f"=== {model} {backend} ===",flush=True)
        subprocess.run([sys.executable,"scripts/summarize.py",str(dest)],check=True)
