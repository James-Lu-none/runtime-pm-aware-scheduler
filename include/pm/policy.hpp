#pragma once
#include <algorithm>
#include <cmath>
#include <limits>
#include <optional>
#include <string>
#include <vector>

namespace pm {
enum class Device { cpu, gpu };
enum class State { active, suspended, unknown };
enum class Mode { cpu, gpu, rr, eft, energy, adaptive };
inline const char* name(Device d) { return d == Device::cpu ? "cpu" : "gpu"; }
inline const char* name(State s) {
    return s == State::active ? "active" : s == State::suspended ? "suspended" : "unknown";
}
struct Cost {
    Device device;
    State state;
    long cap_khz;       // actual configured CPU policy cap; 0 = uncalibrated/default
    double p95_ms;
    double energy_mj;  // measured/estimated energy under declared calibration protocol
    double power_w;    // measured average over calibration window, not instantaneous
};
struct Decision {
    std::optional<Device> device;
    const char* reason;
};
inline bool known(double x) { return std::isfinite(x) && x >= 0.; }

// Serial, non-preemptive dispatch. Costs include end-to-end backend service time.
// Adaptive uses the observed state; EFT/energy intentionally ignore GPU cold state.
inline Decision select(Mode mode, State gpu_state, long cap, double slack_ms,
                       double budget_w, bool gpu_available,
                       const std::vector<Cost>& costs, unsigned long id) {
    if (mode == Mode::cpu) return {Device::cpu, "fixed"};
    if (mode == Mode::gpu) return {gpu_available ? std::optional<Device>(Device::gpu) : std::nullopt, "fixed"};
    if (mode == Mode::rr) return {gpu_available && id % 2 ? Device::gpu : Device::cpu, "round_robin"};
    double best = std::numeric_limits<double>::infinity();
    std::optional<Device> choice;
    for (const auto& c : costs) {
        if (c.cap_khz != cap || !known(c.p95_ms) || c.p95_ms <= 0.) continue;
        if (c.device == Device::gpu) {
            if (!gpu_available) continue;
            const auto expected = mode == Mode::adaptive ? gpu_state : State::active;
            if (c.state != expected) continue;
        } else if (c.state != State::active) continue;
        if (mode == Mode::adaptive && c.p95_ms > slack_ms) continue;
        if (mode == Mode::adaptive && budget_w > 0. &&
            (!known(c.power_w) || c.power_w > budget_w)) continue;
        const double score = mode == Mode::eft ? c.p95_ms : c.energy_mj;
        if (!known(score)) continue;
        if (score < best) { best = score; choice = c.device; }
    }
    return {choice, choice ? "profile" : "no_feasible_profile"};
}

// A supervisory cap controller. The stock thermal governor remains enabled.
// Step down above high, step up below low, hold within hysteresis band.
inline long thermal_step(long current, double temp, double low, double high,
                         const std::vector<long>& ascending) {
    if (ascending.empty() || !std::isfinite(temp)) return current;
    if (temp >= high) {
        auto i = std::lower_bound(ascending.begin(), ascending.end(), current);
        return i == ascending.begin() ? ascending.front() : *std::prev(i);
    }
    if (temp <= low) {
        auto i = std::upper_bound(ascending.begin(), ascending.end(), current);
        return i == ascending.end() ? ascending.back() : *i;
    }
    return current;
}
}
