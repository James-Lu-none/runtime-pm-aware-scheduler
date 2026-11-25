
# Thermal-aware hint scheduling

Three matched arms replace the full lead sweep:

* thermal_off_N: dynamic hint, no software supervisor (native hardware thermal protections remain).
* thermal_static_N: static per-class selection under the same supervisor ceiling.
* thermal_dynamic_N: transition/slack-aware selection under that same supervisor.

All use the same balanced shuffled jobs and independent 0..N ms notice tokens
per repetition. Compare static versus dynamic for selector effect; compare
dynamic off versus dynamic on for supervisor effect. Native governor results
from the earlier sweep are historical context, not matched thermal baselines.

```sh
sudo apt install stress-ng
cmake --build build -j4
ctest --test-dir build --output-on-failure
sudo python3 scripts/benchmark_cpu_dvfs.py --thermal-compare --jobs 120 --repeats 1 \
  --thermal-low 48 --thermal-high 51 --start-temp 48 --preheat-temp 54
```

For each arm, cool to the same upper start threshold, set performance at the
original maximum, preheat with four stress-ng matrix workers until the
requested temperature, stop/reap all workers, then launch the normal
collector and inference runner. Preheat is separate from measured job energy.
Keep active cooling and hardware protection unchanged. No concurrent stress
load. Preheat logs include actual sensor temperatures; initialization may
cool the board before the first hint, so record first-hint temperature and
binding counts. This is a warm-start transient experiment, not a sustained
heating test. If no jobs are constrained, do not claim thermal validation;
inspect the preheat and temperature traces before changing thresholds.
Default thermal-compare invocation needs explicit suitable thresholds
(the legacy benchmark thermal defaults remain unchanged).

Supervisor evaluates at each revealed hint (~500 ms, not an independent
timer/interrupt), holds a soft frequency ceiling until the next decision,
and does not preempt an executing inference. EMA uses elapsed time with
tau=1 s; slope is the EMA secant over ~2 s with three consecutive rising
observations above the low threshold and a provisional 0.5 C/s threshold.
A long sample gap resets derivative history. Raw or EMA high+1 C forces the
minimum calibrated cap; at high or confirmed rising, step down after
2 s dwell; recover stepwise only when raw and EMA are below low and the
slope is nonpositive. This is a provisional conservative design, not an
empirically calibrated reproduction of any published algorithm.
Existing 75 C guard and wrapper's max-temp guard remain independent.
No deadline is dropped; if no candidate is feasible, choose best effort
under the ceiling and record infeasibility. Thermal restriction can increase
deadline misses; report the trade-off, not only energy.

The static selector uses the minimum-energy feasible frequency under the
current ceiling, without transition/slack/hysteresis. Both arms use exactly
the same supervisor algorithm, but actual temperature histories can differ.
Binding means the unconstrained choice for this job and current state exceeds
the ceiling; it is not a causal estimate of misses. Check thermal_binding_jobs,
thermal_binding_misses, thermal_limited_jobs, thermal_ceiling_changes,
thermal_rising_actions, temperature excess C*s and time above high.
Temperature integration uses sample interpolation over the measured collector
window; it is approximate. If the derivative never fires, no evidence of
the derivative branch's benefit is established. Same profiles as previous
500 ms/4-thread runs; warm execution costs may differ, so report residual
latency error. No online execution-cost learning.
