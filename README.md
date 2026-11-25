# Runtime PM-aware Heterogeneous Scheduler

An experimental Linux application-level CPU/GPU dispatcher for Raspberry Pi 5
(BCM2712 / Cortex-A76 / VideoCore VII). Investigate whether measured runtime
power state, service latency and energy estimates improve task placement
under workload deadlines and average rail-power constraints.

## Status

Current direction: CPU-centric deadline-aware DVFS and power management.
The previous heterogeneous and V3D runtime PM experiments remain available.
For the next CPU-only inference sweep, see [CPU profiling](docs/cpu-inference.md).

Initial MVP: ncnn CPU/Vulkan backend, deterministic frame replay, serial
dispatch, six policies, state-aware profile lookup, in-tree thermal hysteresis,
PMIC telemetry, unit tests and experiment scripts. Hardware validation is
pending; no measured improvement or BPMP implementation is claimed.

## Build on Raspberry Pi OS 64-bit

    sudo apt update
    sudo apt install -y git cmake g++ make libvulkan-dev mesa-vulkan-drivers vulkan-tools python3 ffmpeg
    git clone --recurse-submodules https://github.com/James-Lu-none/runtime-pm-aware-scheduler.git
    cd runtime-pm-aware-scheduler
    git submodule update --init --recursive
    sh scripts/probe.sh
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j2
    ctest --test-dir build --output-on-failure

ncnn is pinned to 20250503 (305837fd4a722ebc47c5d72e72d8ec9ae970e932).
Use -DPM_SYSTEM_NCNN=ON to use an installed ncnn package, or
-DPM_VULKAN=OFF for a CPU-only vendored build. To test only the policy:

    cmake -S . -B build-test -DPM_BUILD_RUNNER=OFF
    cmake --build build-test
    ctest --test-dir build-test --output-on-failure

## Arithmetic intensity microbenchmark

Standalone FP32 CPU NEON versus Vulkan compute characterization, independent of
ncnn. See [build and measurement protocol](docs/arithmetic.md).

## MobileNetV4 workload

Three pretrained Conv variants, RGB FP32 inference, numerical parity checks and
workload-tagged profiles are supported by the new experimental path. See
[MobileNetV4 setup and benchmark](docs/mobilenetv4.md). Generated model weights
must be exported on a separate host or provided locally; hardware validation
of this path is pending. Existing pooling runs remain supported.

## Workload

The supplied network is a deterministic pooling/ReLU image-processing graph,
not a trained detector. It requires no weights: create its empty binary file.

    touch models/pool.bin
    ./build/pm_runner --mode cpu --jobs 100 --period-ms 100 > cpu.csv

CPU and GPU receive identical grayscale bytes and run the same FP32-configured
graph. Output checksums are a smoke check, not proof of elementwise equivalence.
For real video, record a stream to a fixed grayscale raw replay file:

    mkdir -p data
    ffmpeg -i rtsp://SERVER:8554/video -t 20 -vf scale=320:240 -pix_fmt gray -f rawvideo data/video.gray
    ./build/pm_runner --mode cpu --raw data/video.gray --width 320 --height 240 --jobs 100 --period-ms 100

Replay is paced by monotonic release times; --burst 10 --gap-ms 2000 adds
an extra gap after each 10 jobs. No live streaming server is required for MVP.
The default 100 ms period and deadline are examples; calibrate workload size
and timing. Use --param and --bin for compatible custom ncnn graphs with
input/output blob names input/output, one grayscale input channel.

## GPU / Runtime PM

Run vulkaninfo --summary and verify the selected GPU is VideoCore, not software
rendering. Use scripts/probe.sh to find the V3D render node and its actual
sysfs device directory; replace GPU_PATH below. Verify power/runtime_status
and actual transition traces. No runtime suspend support is assumed.

    GPU_PATH=/sys/devices/REPLACE_WITH_V3D_DEVICE
    python3 scripts/run.py --out results/gpu-warm --gpu-device "$GPU_PATH" -- ./build/pm_runner --mode gpu --gpu-device "$GPU_PATH" --jobs 100 --period-ms 100
    python3 scripts/run.py --out results/gpu-gaps --gpu-device "$GPU_PATH" -- ./build/pm_runner --mode gpu --gpu-device "$GPU_PATH" --jobs 30 --period-ms 3000 --deadline-ms 500
    python3 scripts/run.py --out results/cpu -- ./build/pm_runner --mode cpu --jobs 100 --period-ms 100
    python3 scripts/summarize.py results/cpu

The ncnn Vulkan context remains initialized; confirm this does not hold an
active PM reference. Unknown PM state is logged as unknown, never suspended.
Use --gpu-index to select the verified physical device.

## Calibrate and schedule

Profile CSV format:

    backend,state,cap_khz,p95_ms,energy_mj,power_w,workload

Legacy six-column profiles are accepted for pool only.

CPU uses state active (its reference category); GPU uses observed
active/suspended/unknown. Each profile belongs to one workload, CPU cap,
thread count, model and calibration protocol. No sample numbers are supplied.

Generate latency profiles from the actual job logs, using nan for energy/power
until separately measured. The state filter is strict; a missing state or
fewer than ten matching jobs fails calibration:

    python3 scripts/build_profile.py --row results/cpu/jobs.csv cpu active nan nan --row results/gpu-warm/jobs.csv gpu active nan nan > profile.csv
    ./build/pm_runner --mode eft --profile profile.csv --gpu-device "$GPU_PATH" --jobs 100

EFT ignores GPU resume state by design. For energy and adaptive modes, replace
nan fields with measured energy estimates; add a suspended GPU profile only
after observing genuine runtime suspend. Adaptive chooses the lowest predicted
energy among candidates satisfying remaining deadline and optional average
power budget (--power-w). Unknown state requires an explicit unknown profile
or CPU alternative. It rejects a job when no eligible profile exists.

Available modes: cpu, gpu, rr, eft, energy, adaptive. --gpu 0 disables GPU
initialization in mixed policies. Fixed backend modes do not require profiles.

## Thermal / DVFS

The CPU-only transition-aware controller is available in pm_runner --mode dvfs (reactive) and --mode lookahead (known periodic releases).
Run `sudo python3 scripts/benchmark_cpu_dvfs.py` after rebuilding and generating
CPU/transition profiles. See [controller evaluation](docs/cpu-dvfs-controller.md).

Measure directed CPUFreq transition costs with `sudo python3 scripts/profile_dvfs_transition.py`.
See [transition profiling](docs/dvfs-transition.md) for trace and readback semantics.

For the standalone arithmetic workload, run the automated CPUFreq sweep and
GPU baseline: `sudo python3 scripts/profile_dvfs.py`. See
[DVFS profiling protocol](docs/dvfs.md) for restoration, validation and Pareto CSVs.

Optional in-tree controller, no external thermal project dependency.
It uses Linux cpufreq scaling_max_freq and thermal zone temperature, not a
new governor. First verify policy and thermal-zone paths with probe.sh.

    sudo ./build/pm_runner --mode cpu --thermal 1 --thermal-low 65 --thermal-high 70 --thermal-dwell-ms 1000 --jobs 1000

The cap steps down/up using hysteresis and available frequencies bounded by
the original cap. It attempts to restore the original cap on graceful exit;
record the original value before privileged experiments, restore manually
after SIGKILL, and keep stock firmware/thermal protections enabled. Only one
controller should write the cap. Thermal and adaptive combined require
profiles for all reached caps.

## Architecture and limitations

- include/pm/policy.hpp: independent decision logic and thermal controller.
- include/pm/platform.hpp: Linux sysfs adapters and scoped cap management.
- src/backend.cpp: ncnn CPU/Vulkan execution and output checksum.
- src/main.cpp: deterministic paced replay and CSV event log.
- scripts/: platform probe, telemetry capture, calibration and analysis.
- docs/experiment.md: validation protocol, assumptions and next milestones.

Power estimates are calibration-window averages, not instantaneous hard
power caps. Onboard PMIC VDD_CORE readings cover a specific supply rail;
they are not total platform power. Runtime PM state reads are observations
and can race with transitions. Actual device state, frequency and wake
events need trace validation. No kernel modifications or live network queue
are implemented in this initial version.

References: [ncnn](https://github.com/Tencent/ncnn), [Linux Runtime PM](https://docs.kernel.org/power/runtime_pm.html),
[CPUFreq](https://docs.kernel.org/admin-guide/pm/cpufreq.html),
[Raspberry Pi documentation](https://www.raspberrypi.com/documentation/computers/os.html).
ncnn is BSD-3-Clause; its own license is retained in the submodule.
