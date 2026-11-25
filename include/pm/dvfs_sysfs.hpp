#pragma once
#include "pm/platform.hpp"
#include <chrono>
#include <thread>
#include <iostream>
namespace pm {
struct ReadyResult {
    double elapsed_ms=0, write_ms=0, cap_seen_ms=-1, freq_seen_ms=-1;
    bool late=false;
};
inline bool ready_late(double elapsed_ms,int timeout_ms) {
    return elapsed_ms>timeout_ms;
}
// Independent controller actuator. The external experiment driver owns
// governor selection and crash-safe best-effort restoration.
class DvfsActuator {
    std::string dir_;
    long initial_, min_;
    bool changed_=false;
    using Clock=std::chrono::steady_clock;
    void write_cap(long target) {
        std::ofstream f(dir_+"/scaling_max_freq");
        if(!(f<<target<<'\n') || !(f.flush()))
            throw std::runtime_error("DVFS cap write failed");
    }
    ReadyResult wait(long target,int timeout_ms,bool set) {
        auto start=Clock::now();
        auto elapsed=[&](){return std::chrono::duration<double,std::milli>(Clock::now()-start).count();};
        ReadyResult result;
        if(set) write_cap(target);
        result.write_ms=elapsed();
        const int recovery_ms=std::max(timeout_ms,500);
        while(true) {
            const auto observed_cap=cap();
            const double cap_at=elapsed();
            const auto observed_freq=freq();
            const double freq_at=elapsed();
            if(observed_cap==target && result.cap_seen_ms<0) result.cap_seen_ms=cap_at;
            if(observed_freq==target && result.freq_seen_ms<0) result.freq_seen_ms=freq_at;
            result.elapsed_ms=freq_at;
            if(freq_at>recovery_ms)
                throw std::runtime_error("DVFS recovery timeout for "+std::to_string(target));
            if(observed_cap==target && observed_freq==target) {
                result.late=ready_late(freq_at,timeout_ms);
                return result;
            }
            std::this_thread::sleep_for(std::chrono::microseconds(100));
        }
    }
public:
    explicit DvfsActuator(std::string dir):dir_(std::move(dir)),initial_(cap()),
        min_(read_long(dir_+"/scaling_min_freq")) {
        if(initial_<=0 || min_<=0 || governor()!="performance")
            throw std::runtime_error("DVFS requires valid CPUFreq policy and performance governor");
    }
    DvfsActuator(const DvfsActuator&)=delete;
    DvfsActuator& operator=(const DvfsActuator&)=delete;
    long cap() const {return read_long(dir_+"/scaling_max_freq");}
    long freq() const {return read_long(dir_+"/scaling_cur_freq");}
    std::string governor() const {return read_text(dir_+"/scaling_governor");}
    long initial() const {return initial_;}
    void validate() const {
        if(governor()!="performance" || read_long(dir_+"/scaling_min_freq")!=min_)
            throw std::runtime_error("External CPUFreq policy change");
    }
    ReadyResult set_ready(long target,int timeout_ms) {
        validate();
        if(target<min_ || target>initial_) throw std::runtime_error("Unsafe DVFS target");
        bool set=cap()!=target;
        if(set) changed_=true;
        return wait(target,timeout_ms,set);
    }
    void restore() {
        if(!changed_) return;
        // Restoring the cap is sufficient; don't require physical frequency
        // under thermal throttling. Do not overwrite an external governor.
        write_cap(initial_);
        auto end=Clock::now()+std::chrono::seconds(5);
        while(Clock::now()<end) {
            if(cap()==initial_) {changed_=false;return;}
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        throw std::runtime_error("DVFS cap restore timeout");
    }
    ~DvfsActuator() {
        if(changed_) try {restore();} catch(const std::exception& e) {
            std::cerr<<"DVFS restoration failed: "<<e.what()<<'\n';
        }
    }
};
}
