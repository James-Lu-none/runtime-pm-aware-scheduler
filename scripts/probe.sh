#!/bin/sh
set -eu
uname -a
cat /etc/os-release
printf '\nCPU policies\n'
for p in /sys/devices/system/cpu/cpufreq/policy*; do
  [ -d "$p" ] || continue
  grep . "$p"/related_cpus "$p"/scaling_driver "$p"/scaling_available_governors "$p"/scaling_available_frequencies "$p"/scaling_max_freq || true
done
printf '\nDRM render nodes and PM paths\n'
for p in /sys/class/drm/renderD*; do
  [ -e "$p/device" ] || continue
  d=$(readlink -f "$p/device")
  echo "$p -> $d"
  readlink -f "$d/driver" || true
  grep . "$d"/power/runtime_status "$d"/power/control "$d"/power/autosuspend_delay_ms || true
done
grep . /sys/class/thermal/thermal_zone*/type /sys/class/thermal/thermal_zone*/temp || true
cat /sys/power/state || true
cat /sys/power/mem_sleep || true
command -v vulkaninfo >/dev/null && vulkaninfo --summary || true
command -v vcgencmd >/dev/null && vcgencmd pmic_read_adc || true
command -v trace-cmd >/dev/null && trace-cmd list -e | grep -E '^(rpm|power|thermal):' || true
