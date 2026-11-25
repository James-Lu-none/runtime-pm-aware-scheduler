#!/usr/bin/env python3
"""Export pretrained MobileNetV4 models at a common 224x224 FP32 input."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import tempfile

MODELS = {
    "small": "mobilenetv4_conv_small.e2400_r224_in1k",
    "medium": "mobilenetv4_conv_medium.e500_r224_in1k",
    "large": "mobilenetv4_conv_large.e500_r256_in1k",
}

def canonicalize(path):
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "7767517":
        raise RuntimeError("Invalid ncnn param")
    layers = [line.split() for line in lines[2:] if line.strip()]
    sources, produced, consumed = [], set(), set()
    for layer in layers:
        n, k = int(layer[2]), int(layer[3])
        inputs, outputs = layer[4:4+n], layer[4+n:4+n+k]
        if layer[0] == "Input":
            sources.extend(outputs)
        consumed.update(inputs)
        produced.update(outputs)
    sinks = produced - consumed
    if len(sources) != 1 or len(sinks) != 1:
        raise RuntimeError(f"Expected single input/output, got {sources}, {sinks}")
    mapping = {sources[0]: "input", next(iter(sinks)): "output"}
    if any(v in produced and v != k for k,v in mapping.items()):
        raise RuntimeError("Canonical blob name collision")
    for layer in layers:
        end = 4 + int(layer[2]) + int(layer[3])
        layer[4:end] = [mapping.get(x,x) for x in layer[4:end]]
    return "\n".join(lines[:2]+[" ".join(x) for x in layers])+"\n"

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("models/mobilenetv4"))
    p.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    p.add_argument("--pnnx", default="pnnx")
    a = p.parse_args()
    import torch
    import timm
    import numpy as np
    torch.set_num_threads(2)
    torch.manual_seed(0)
    a.out.mkdir(parents=True, exist_ok=True)
    mean = (.485,.456,.406)
    std = (.229,.224,.225)
    raw = (np.arange(224*224*3,dtype=np.int64)*17 % 256).astype(np.uint8).reshape(224,224,3)
    x = torch.from_numpy(raw).permute(2,0,1).unsqueeze(0).float()/255
    x = (x-torch.tensor(mean).reshape(1,3,1,1))/torch.tensor(std).reshape(1,3,1,1)
    for variant in a.models:
        name = MODELS[variant]
        print("Export", name, flush=True)
        model = timm.create_model(name, pretrained=True).eval()
        cfg = timm.data.resolve_model_data_config(model)
        if tuple(cfg["mean"]) != mean or tuple(cfg["std"]) != std:
            raise RuntimeError("Pretrained normalization differs from runner")
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            traced = torch.jit.trace(model, x)
            traced.save(str(temp/"model.pt"))
            subprocess.run([a.pnnx,"model.pt","inputshape=[1,3,224,224]","fp16=0"],
                           cwd=temp, check=True)
            param, binary = temp/"model.ncnn.param", temp/"model.ncnn.bin"
            if not binary.is_file() or binary.stat().st_size == 0:
                raise RuntimeError("Missing exported weights")
            out_param, out_bin = a.out/f"{variant}.param", a.out/f"{variant}.bin"
            out_param.write_text(canonicalize(param))
            out_bin.write_bytes(binary.read_bytes())
            with torch.inference_mode():
                logits=model(x).detach().cpu().numpy().reshape(-1)
            if logits.size != 1000 or not np.isfinite(logits).all():
                raise RuntimeError("Unexpected classifier output")
            np.savetxt(a.out/f"{variant}.reference.txt",logits,fmt="%.9g")
            manifest = {"model":name,"shape":[1,3,224,224],"mean":mean,"std":std,
                        "fp16":False,"torch":torch.__version__,"timm":timm.__version__,
                        "pnnx_package":metadata.version("pnnx") if a.pnnx=="pnnx" else a.pnnx,
                        "param_sha256":sha256(out_param),"bin_sha256":sha256(out_bin)}
            (a.out/f"{variant}.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print("Exported; verify numerical parity on target before benchmarking.")

if __name__ == "__main__":
    main()
