
# CPU-only paced inference profiling

The current experimental direction is deadline-aware CPU DVFS, not GPU
offloading. Prior CPU/Vulkan experiments remain available for reproducibility.

Reuse the existing pm_runner and exported MobileNetV4 models:
```sh
sudo python3 scripts/profile_cpu_inference.py
```

Defaults: Small, Medium, Large; four CPU threads; fixed performance-governor
caps of 1.5, 2.0, 2.4 GHz and the original native governor at its original cap;
60 jobs/model/configuration, 500 ms release period, 250 ms deadline. No GPU
context or GPU verification is initialized. CPU inference uses the already
validated model export; this script does not rerun numerical parity tests.

The arrival schedule, input sequence and thread count are identical for all
configurations of each model. This is a paced workload, including idle gaps.
A native governor baseline is not treated as a fixed-frequency cost. Optional
native governor comparisons (must be supported):
```sh
sudo python3 scripts/profile_cpu_inference.py --native-governors ondemand schedutil --repeats 3
```
Use --models large for a smaller initial sweep. Use --freqs with supported
kHz values within the original CPUFreq limits. An empty --native-governors
list disables the native baseline.

Output: original_settings.json (including model and runner SHA256), raw
jobs.csv/telemetry.csv per run, runs.csv, summary.csv (per-configuration
medians), pareto.csv (service latency and energy/job, separately per model),
cpu_costs.csv (fixed-frequency candidate costs, not an existing runner
profile). Deadline misses are counted, not silently dropped. No results
from GPU or different model sizes enter the same Pareto comparison.

The old runner's telemetry window is approximate: it includes initialization,
idle gaps and final process exit; it is not precisely aligned to the release
horizon. The script rejects grossly mismatched windows, missing telemetry,
incomplete runs and changed caps. Fixed-frequency runs require 90% of
reported frequency samples within 2% of target; native governor runs do
not. VDD_CORE energy/job is a rail-level, period-dependent estimate, not
isolated inference energy. Do not extrapolate it to other arrival periods.
P95 service time is not a hard deadline bound. One sweep is exploratory;
repeat and validate actual transition latency before adaptive control.

Existing asynchronous CPUFreq readback, thermal guard (60 C start / 75 C
abort), exclusive cooperating lock and restoration helpers are reused.
The original cap and governor are restored on normal exit, exceptions,
SIGINT/SIGTERM/SIGHUP; SIGKILL and power loss cannot be handled. The original
minimum frequency and kernel thermal protection are not changed. Stop
external frequency controllers before running. Output files are root-owned.
