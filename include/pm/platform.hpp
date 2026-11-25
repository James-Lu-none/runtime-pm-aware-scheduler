#pragma once
#include "pm/policy.hpp"
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace pm {
inline std::string read_text(const std::string& path) {
    std::ifstream f(path); std::string s;
    if (!(f >> s)) return {};
    return s;
}
inline long read_long(const std::string& path) {
    auto s = read_text(path);
    try { size_t n; auto v = std::stol(s, &n); return n == s.size() ? v : -1; }
    catch (...) { return -1; }
}
inline State runtime_state(const std::string& device) {
    if (device.empty()) return State::unknown;
    auto s = read_text(device + "/power/runtime_status");
    if (s == "active") return State::active;
    if (s == "suspended") return State::suspended;
    return State::unknown;
}
inline double temperature(const std::string& zone) {
    auto t = read_long(zone + "/temp");
    return t < 0 ? std::numeric_limits<double>::quiet_NaN() : t / 1000.;
}
class CpuCap {
    std::string dir_;
    long original_;
    bool changed_ = false;
public:
    explicit CpuCap(std::string dir) : dir_(std::move(dir)), original_(current()) {}
    CpuCap(const CpuCap&) = delete;
    CpuCap& operator=(const CpuCap&) = delete;
    long current() const { return read_long(dir_ + "/scaling_max_freq"); }
    std::vector<long> frequencies() const {
        std::ifstream f(dir_ + "/scaling_available_frequencies");
        std::vector<long> v; long x;
        auto minimum = read_long(dir_ + "/scaling_min_freq");
        while (f >> x) if (x >= minimum && x <= original_) v.push_back(x);
        std::sort(v.begin(), v.end());
        v.erase(std::unique(v.begin(), v.end()), v.end());
        return v;
    }
    void set(long khz) {
        if (original_ <= 0 || khz <= 0 || khz > original_)
            throw std::runtime_error("Invalid CPU frequency cap");
        if (khz == current()) return;
        changed_ = true;
        std::ofstream f(dir_ + "/scaling_max_freq");
        if (!(f << khz << '\n') || !(f.flush()))
            throw std::runtime_error("CPU frequency cap write failed");
        if (current() != khz) throw std::runtime_error("CPU cap readback differs");
    }
    ~CpuCap() {
        if (changed_) {
            std::ofstream f(dir_ + "/scaling_max_freq");
            f << original_ << '\n';
        }
    }
};
}
