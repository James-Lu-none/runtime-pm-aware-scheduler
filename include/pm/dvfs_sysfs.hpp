#pragma once
#include "pm/platform.hpp"
#include <chrono>
#include <thread>
#include <iostream>
namespace pm {
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
    double wait(long target,int timeout_ms,bool set) {
        auto start=Clock::now();
        if(set) write_cap(target);
        auto end=start+std::chrono::milliseconds(timeout_ms);
        while(Clock::now()<=end) {
            if(cap()==target && freq()==target)
                return std::chrono::duration<double,std::milli>(Clock::now()-start).count();
            std::this_thread::sleep_for(std::chrono::microseconds(100));
        }
        throw std::runtime_error("DVFS ready timeout for "+std::to_string(target));
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
    double set_ready(long target,int timeout_ms) {
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
