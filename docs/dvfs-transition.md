
# CPUFreq transition characterization

Run with normal active cooling. No inference build or GPU is required.

```sh
sudo python3 scripts/profile_dvfs_transition.py
```

Defaults: 30 trials per directed pair, 1.5/2.0/2.4 GHz (180 measured
transitions), performance governor, 100 us requested polling interval,
100 ms source settling, 1 s per-transition timeout. Order is shuffled
with a reproducible seed. Each trial establishes and checks the source
frequency before the timed cap write. The setup/settle and trace capture
overhead are not counted in the measured ready latency.

The script uses a private tracefs instance at /sys/kernel/tracing/instances
with trace_clock=mono, power:cpu_frequency and (if available)
power:cpu_frequency_limits. It does not clear global tracing. If tracefs
is unavailable, check mount and kernel configuration; explicitly pass
--no-trace for sysfs-only timing. No fabricated zero for missing events.
Trace events are filtered by related CPUs, target frequency and measured
monotonic time interval; shared-policy per-CPU duplicate events are counted
and the first/last event are retained. Kernel event timestamps are not
necessarily physical clock/voltage settling timestamps.

Metrics, all relative to userspace immediately before writing the cap:
- write_ms: open/write/close of scaling_max_freq.
- policy_readback_ms: first observed target cap.
- freq_readback_ms: first observed target scaling_cur_freq.
- ready_ms: both cap and reported frequency matched in the same poll.
- kernel_limits_event_ms / kernel_frequency_event_ms: first matching kernel
  trace event (if observed). The latter is a driver/kernel notification,
  not a hardware scope measurement. Event and readback order need not be
  identical. Python polling can oversleep; these are observed latencies
  for this implementation, not pure hardware DVFS latency.

Artifacts: original_settings.json, transitions.csv (raw trial records),
trace.txt (raw per-trial trace), summary.csv (P50/P95/max for each directed
pair and valid/trace sample counts), transition_costs.csv (ready P95/max
for pairs with all trials valid; diagonal zero means no requested change).
Trace event loss is warned, and trace overflow invalidates a trial.
A missing trace event is left blank, not treated as zero. The ready cost
matrix is sysfs-based and remains distinct from the kernel event statistics.
Do not treat P95 or the observed maximum as a hard real-time bound; a future
C++ controller needs its own timing and deadline validation.

Optional loaded comparison (install stress-ng first):
```sh
sudo python3 scripts/profile_dvfs_transition.py --load-workers 3
```
This is a separate background-load experiment, not a CPU affinity guarantee.
The default has no injected load; the measurement process itself is active.
Compare idle and loaded distributions separately.

The script snapshots original governor/cap/min, only writes governor and
max cap, refuses unsupported values or increases above original cap,
uses the cooperating DVFS lock, restores settings on normal exit, errors,
SIGINT/SIGTERM/SIGHUP, and tears down its own trace instance and optional
stress-ng process. SIGKILL/power loss cannot be handled; restore manually
from original_settings.json if necessary. Kernel thermal protections remain
enabled; start <=60 C and abort at sampled >=75 C. Stop external frequency
controllers. Root-owned output files are expected.
