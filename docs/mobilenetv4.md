
# MobileNetV4 CPU/GPU characterization

The three pretrained timm Conv variants are exported with the same 224x224 RGB
input and FP32 weights. The Large checkpoint was trained at 256px: 224px is
an intentional common-size performance experiment, not an accuracy benchmark.
The runner performs ImageNet mean/std normalization. Classification logits
are compared elementwise with PyTorch and the ncnn CPU backend before timing.
Do not proceed when verification fails; inspect conversion and precision.
Vulkan-enabled execution can include CPU fallback; confirm physical V3D and
inspect operators/traces. No GPU advantage is assumed.

## Export on a Linux workstation

```sh
python3 -m venv .venv-export
. .venv-export/bin/activate
python -m pip install --upgrade pip
python -m pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-export.txt
python scripts/export_mobilenetv4.py --models small medium large
```

Use Python 3.10–3.12 on the export host. Export requires network access to
download pretrained weights. Transfer the generated directory to the Pi
(e.g. rsync -av models/mobilenetv4/ user@rpi5:~/workspace/runtime-pm-aware-scheduler/models/mobilenetv4/).
Generated weights, reference logits and manifests are intentionally not
tracked in Git. Record the manifest and verify checksums when reproducing.
PNNX 20250430 is a nearby release to the existing ncnn pin; compatibility
must be verified, not assumed.

## Pi 5: one model first

```sh
git pull --ff-only
cmake --build build -j4
ctest --test-dir build --output-on-failure
python3 scripts/benchmark_mobilenetv4.py --models small --jobs 20 --period-ms 2000
```

Use an unprivileged user with render-node access. The script checks CPU/GPU
and PyTorch/CPU numerical parity, then collects fixed-backend measurements.
It uses 137 ms PMIC sampling to reduce phase-locking with periodic jobs.
Energy remains an approximation for the sampled core-rail window. Repeat
with varied sampling phase and matched conditions. Choose a period/deadline
greater than the observed service times for cold-state characterization.
Run medium and large after Small passes; these may take longer.

## Mixed requests

```sh
./build/pm_runner --mode rr --workload small,medium,large --jobs 30 --period-ms 2000 --deadline-ms 2000
```

This is a deterministic cycle of independent model requests, not a model
accuracy/quality adaptation policy. Every job runs the requested model.
The dispatcher remains serial; all required models are preloaded.
The workload column is part of the profile key (with backend, runtime state
and CPU cap). Build profiles from separate fixed-model calibration runs.
Existing untagged CSV profiles are accepted only for pool. Calibration must
also match input size, thread count, model hash, governor, and arrival pattern;
these conditions are not all enforced by the profile format.

For RGB video replay, prepare raw 224x224 rgb24 data using FFmpeg and pass
--raw PATH. Pool still accepts gray input and is unchanged as a microbenchmark.
