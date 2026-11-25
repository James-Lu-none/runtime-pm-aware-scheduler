# Experimental protocol

Hypothesis: device runtime state changes the latency/energy cost of offloading a
task. Measure rather than assume GPU wins. The runner is an application-level,
serial dispatcher, not a Linux CPU scheduler or DRM scheduler.

1. Record OS/kernel, firmware, Mesa, board cooling, ambient temperature (if
   available), governor and CPU policy. Use scripts/probe.sh. Confirm physical
   Vulkan device (not llvmpipe/lavapipe), and resolve its exact DRM device path.
2. Use the same input dimensions, model, precision, CPU threads, arrival trace
   and thermal initial conditions. Confirm CPU/GPU checksums and preferably
   elementwise outputs before drawing conclusions. GPU-enabled ncnn may fall
   back to CPU for some operators; inspect implementation/traces.
3. Run fixed CPU and GPU with long and short idle gaps. Observe actual runtime
   state and use kernel rpm/power tracepoints if present. An initialized Vulkan
   context may prevent suspension. Never equate inactivity with suspension.
4. Calibrate p95 service times by observed state and CPU cap. Do not transfer
   energy numbers across arrival patterns or caps. For cost estimates, use a
   repeated isolated calibration window, subtract matched idle baseline if
   estimating incremental energy, document accounting and sampling error.
   PMIC VDD_CORE is a rail, not board power or CPU-only/GPU-only power.
5. Compare CPU-only, GPU-enabled, RR, earliest finish time, energy-aware and
   adaptive. Fixed cap and thermal control are separate experimental factors.
   Keep original governor, fan and safety limits. Use >=3 repeats when possible.
6. Report throughput, response p95/p99, missed/rejected jobs, rail energy,
   actual runtime transitions, temperatures and overhead. A rejected frame
   counts against QoS; do not report energy improvement alone.
7. Use a fixed arrival trace (period, burst, gap). The initial runner replays
   frames sequentially against absolute release times and records backlog delay;
   it has no parallel device queues or live-network admission control yet.
   Capture remote frames with FFmpeg for deterministic replay.

## Thermal controller

The in-tree hysteresis controller changes only the CPU max-frequency cap, at
a bounded update cadence between jobs. It never raises the original cap and
does not alter thermal trips, fan, firmware or voltage. It is not a hard
real-time thermal controller. Test thermal-only with fixed backend first.
Adaptive with thermal enabled needs a profile at every cap. No feasible
profile is an explicit rejected job. Restore the original cap after crashes
or SIGKILL; graceful exits attempt restoration. Do not run other cap writers.

## Next milestones

- Online receiver and bounded independent arrival queue (FFmpeg/GStreamer).
- Parallel CPU/GPU queues; account for resource contention.
- Deadline-aware batching; measure actual autosuspend and resume latency.
- Kernel freq_qos extension with independent constraints and driver lifecycle.
- Compare Raspberry Pi firmware mailbox and Tegra BPMP MRQ; no BPMP claim on Pi.
- Validate V3D Runtime PM support before contemplating kernel backport.
