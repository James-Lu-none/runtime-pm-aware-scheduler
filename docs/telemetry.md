
# Multi-rate telemetry and observer effects

The CPU DVFS evaluation defaults to multi-rate collection. Existing profiling
scripts and direct run.py calls retain legacy mode unless explicitly selected.
Benchmark defaults (validated low-observer protocol): frequency/cap 500 ms,
PMIC 137 ms, temperature 500 ms, cpuidle cumulative counters 1000 ms.
The old high-rate configuration remains available explicitly with
--freq-ms 5 --pmic-ms 30 --temp-ms 500 --idle-ms 100. CPUFreq kernel trace events
are enabled for the whole multi-rate evaluation by default. No GPU
monitoring is needed for CPU experiments. Polling is best-effort, not
real-time, and sysfs reads may block. The independent sysfs sampler
runs alongside the PMIC subprocess loop; missed sysfs ticks are skipped.
PMIC commands never overlap, and a slow ADC call stretches its cadence.

```sh
python3 tests/test_telemetry.py
sudo python3 scripts/benchmark_cpu_dvfs.py --observer-check --jobs 120 --repeats 3
sudo python3 scripts/benchmark_cpu_dvfs.py --lookahead-only
```

The observer check uses the original native governor for both arms, identical
models, inputs, deadlines and release period, and alternates arm order.
It compares 137 ms legacy versus the full multi-rate configuration
(including tracing). Inspect runs.csv and summary.csv for energy/job,
deadline misses, response latency and actual PMIC spacing. This measures
the combined observer effect, not the overhead of individual streams.
If it is material, reduce sampling pressure or turn off redundant frequency
polling by increasing --freq-ms while retaining trace events. Use
--no-frequency-trace to isolate tracing and --telemetry legacy to reproduce
the old sampling protocol. All policies within a comparison must share
one telemetry configuration; if overhead is material, recalibrate the
CPU profiles with the selected protocol before interpreting adaptive costs.

Files per run:
- telemetry.csv: original columns, plus ADC begin/end and cached frequency
  and temperature timestamps/ages in multi mode. Time is taken at the end
  of observation in multi mode (legacy remains at the start of ADC call).
  Power is integrated only on PMIC rows; repeated cached values are not new
  frequency or temperature samples.
- frequency.csv, temperature.csv: independent observations with read duration.
- cpuidle.csv: per-CPU/state cumulative time in microseconds and usage counts.
  Compute residency from differences over a time window, accounting for
  resets; no instantaneous idle state is inferred. Missing cpuidle sysfs
  produces a header-only CSV, recorded as zero discovered states.
- frequency_trace.txt and frequency_events.csv: isolated tracefs instance
  with mono clock, CPU frequency and optional limits events. CPU IDs are
  filtered to related_cpus; a shared policy may emit duplicate per-CPU
  events. The buffer is 8192 KiB per CPU by default, drained after the run,
  not continuously. Overflow is recorded/warned and invalidates trace-only
  analysis, not the independent job/PMIC results. Increase buffer size or
  shorten runs if needed. Kernel events report notifications, not measured
  physical clock transitions. Tracefs must be mounted and available.
- telemetry_metadata.json: requested rates, actual sample gap statistics,
  trace status, ADC call latency and Python process CPU time (excludes
  vcgencmd child CPU time and kernel overhead).

All timestamps use monotonic time. Job timestamps are relative to the runner
release origin. New jobs.csv rows include release_origin_monotonic_s and
clock_anchor_uncertainty_us. Add release_ms/1000, dispatch_ms/1000,
finish_ms/1000 or transition_start_ms/1000 to that origin to align jobs
and transitions with telemetry and trace events. Legacy results without
this anchor cannot be precisely aligned. Rebuild pm_runner for the new columns.
The uncertainty records half the bracketing interval of clock_gettime,
not all possible timestamp or ADC observation error. Multi-rate PMIC
reads do not establish the hardware ADC update rate, and VDD_CORE does not
represent whole-board energy. Missing readings must not be interpolated
silently. The current benchmark retains its original approximate energy
window and thermal safety polling; telemetry temperature sampling is
not the thermal guard. No thermal control algorithm is changed.
