
# Controlled CPU DVFS profiling

Reuse the standalone FP32 arithmetic benchmark. No rebuild is necessary if
build-micro/pm_microbench already exists.

```sh
sudo python3 scripts/profile_dvfs.py
```

Defaults: 1024 FMA iterations, 262144 elements, four CPU threads,
30 seconds per configuration, one repeat, 137 ms PMIC sampling.
CPU caps: 1500000, 2000000, 2400000 kHz, performance governor.
Only scaling_max_freq and scaling_governor are changed; scaling_min_freq is
left unchanged. CPU samples must match the target within 2% for at least 90%
of samples. scaling_cur_freq is the CPUFreq-reported value, not a direct
hardware clock measurement. GPU baseline uses the original CPU governor/cap,
since forcing high CPU clocks can distort offloading energy. It is a
comparison of practical configurations, not an isolated CPU-frequency effect
on GPU execution. The GPU baseline has one Vulkan queue; no FP16 changes.

The script requires root to change CPUFreq; all benchmark processes run with
the same privilege, and output files are root-owned. Stop any other frequency
controller first. The script checks allowed frequency points and never raises
the original cap. It verifies writes by reading back sysfs, logs original
settings before changes, and restores cap and governor on normal exit,
exceptions, SIGINT/SIGTERM/SIGHUP. SIGKILL, power loss and external changes
cannot be handled. Original min is never written. Restore manually using the
original_settings.json file if needed. Stock thermal protection remains on.
The script waits for CPU temperature <=60 C before each test and aborts
at >=75 C (software sampling every ~250 ms, not a hard thermal safety limit).

Optional longer profiling:
```sh
sudo python3 scripts/profile_dvfs.py --freqs 1500000 1600000 1700000 1800000 1900000 2000000 2100000 2200000 2300000 2400000 --repeats 3
```

Repeat ordering alternates forward/reverse; the default single sweep is
exploratory, not statistical confirmation. Results:
- original_settings.json: restoration values and experiment configuration
- rN-BACKEND-CAP/jobs.csv, telemetry.csv: existing raw telemetry and job log
- runs.csv: per-run P95 service time, throughput, GFLOP/s, sampled VDD_CORE
  average power and energy/job, reported CPU frequency min/median/max,
  frequency-match percentage, sampled max temperature and validity.
- summary.csv: medians of valid repeated runs (all repeats must be valid).
- pareto.csv: non-dominated configurations in P95 service latency and energy/job.

The power integration is over the telemetry window, not precisely the timed
job window. Runs with a window ratio outside 0.90..1.10, a changed cap or
insufficient CPU frequency match are excluded. Initialization/warmup and
sampling error can still affect energy, especially short runs. VDD_CORE is
not total board power. A Pareto flag is descriptive, not proof of statistical
significance or a deadline guarantee. GPU frequency/ALU occupancy are not
measured here. The saturated FMA result must not be reused as a MobileNetV4
calibration profile.

If GPU is dominated by a CPU cap, favor CPU DVFS for this workload; keep
runtime PM characterization as a separate system result. If not, retain both
devices as candidate configurations for a future deadline-aware policy.

CPUFreq QoS limit changes can be applied asynchronously by the kernel policy
workqueue. The script polls readback for up to five seconds and requires three
consecutive matching reads (50 ms apart), including on restoration. A persistent
mismatch still aborts the run; it is not silently accepted.
