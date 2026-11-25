
# Arithmetic-intensity characterization

Standalone FP32 microbenchmark independent of ncnn. CPU uses OpenMP with
explicit AArch64 NEON FMA; GPU uses Vulkan compute with four vec4 accumulators
per invocation, a pre-recorded command buffer and coherent mapped buffers.
Input and output each have N FP32 scalars. Both compute x = fma(x,a,b) K
times on every scalar; the GPU shader loads once and stores once. The loop
uses four independent vectors to reduce dependency stalls. This is synthetic
and does not establish peak GPU ALU utilization or real-world inference speed.

Nominal arithmetic intensity = (2*N*K)/(8*N) = K/4 FLOP/byte at the
logical input/output boundary. It is **not** measured DRAM traffic; caches,
register spilling and driver traffic can change actual arithmetic intensity.
The default 262144 elements give 1 MiB input and 1 MiB output. Verify all
outputs against the CPU reference before each iteration count. The GPU
uses a hardware Vulkan device, not llvmpipe. FP16 is deliberately not
enabled so the two devices perform the same FP32 computation.

## Build independently on Pi 5

```sh
sudo apt install -y libvulkan-dev glslang-tools
cmake -S . -B build-micro -DPM_BUILD_RUNNER=OFF -DPM_BUILD_MICROBENCH=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build-micro -j4
python3 scripts/benchmark_arithmetic.py
```

Defaults: 2 CPU threads, 5 seconds of timed saturated work (at least 10 jobs)
per backend/iteration count, 137 ms PMIC sampling, GPU index 0.
Check the reported device name is V3D. For a quick smoke check:

```sh
./build-micro/pm_microbench --mode verify --iterations 64
```

The summary CSV reports actual completed jobs per second and equivalent
FP32 GFLOP/s (2 FLOPs per FMA), P95 service latency, approximate core rail
power and sampled-window energy/job. Pipeline creation and three warmup
dispatches are excluded from service timing but may be included in the
telemetry window; energy is therefore only exploratory. Repeat with longer
runs (e.g. --seconds 20), vary sampling phase, and keep governor, cap,
temperature, element count and threads fixed for credible comparison.

The GPU command buffer is pre-recorded; timed service includes submit/fence
and reading three output elements, not a full device-to-host transfer.
CPU buffers are already allocated too. Both sides work on identical data.
Runs are saturated, not paced, and different backends complete different
numbers of jobs; compare throughput and energy/job, not raw total energy.
The CPU benchmark uses two threads by default; compare --threads 1 and 4
if needed. No FP16 or multi-in-flight tuning is included. This benchmark is
not a calibration profile for the MobileNet scheduler.

## Decision

A GPU crossover would justify exploring CPU/GPU dispatch with PM state.
If none occurs, retain this negative result and the measured V3D runtime PM
transitions. Shift emphasis to deadline-aware CPU frequency selection,
CPUIdle residency and device runtime PM; do not manufacture GPU speedup.
