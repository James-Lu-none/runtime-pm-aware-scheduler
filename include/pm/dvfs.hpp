#pragma once
#include <algorithm>
#include <cmath>
#include <fstream>
#include <limits>
#include <map>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace pm {
struct CpuCost {
    long khz;
    double service_ms, energy_mj;
};
using CpuCosts = std::map<std::string,std::vector<CpuCost>>;
using SwitchCosts = std::map<std::pair<long,long>,double>;
struct DvfsDecision {
    long khz;
    double predicted_ms, switch_ms, energy_mj;
    bool feasible;
    const char* reason;
};
inline std::vector<std::string> split_csv(std::string line) {
    if (!line.empty() && line.back()=='\r') line.pop_back();
    std::vector<std::string> v;
    std::istringstream in(line);
    for(std::string s;std::getline(in,s,',');) v.push_back(s);
    return v;
}
inline double number(const std::string& s) {
    size_t n=0; auto x=std::stod(s,&n);
    if(n!=s.size() || !std::isfinite(x) || x<0) throw std::runtime_error("Invalid nonnegative number: "+s);
    return x;
}
inline long positive_int(const std::string& s) {
    size_t n=0; auto x=std::stol(s,&n);
    if(n!=s.size() || x<=0) throw std::runtime_error("Invalid positive integer: "+s);
    return x;
}
inline CpuCosts load_cpu_costs(const std::string& path,int threads,int period) {
    std::ifstream f(path);
    std::string line;
    if(!std::getline(f,line) || split_csv(line)!=split_csv("model,cap_khz,threads,period_ms,p95_service_ms,approx_energy_mj_job,rail_w,repeats"))
        throw std::runtime_error("Invalid CPU costs CSV: "+path);
    CpuCosts out;
    while(std::getline(f,line)) {
        auto a=split_csv(line);
        if(a.empty()) continue;
        if(a.size()!=8 || positive_int(a[2])!=threads || positive_int(a[3])!=period)
            throw std::runtime_error("CPU cost threads/period mismatch");
        CpuCost c{positive_int(a[1]),number(a[4]),number(a[5])};
        if(c.service_ms<=0 || c.energy_mj<=0 || positive_int(a[7])<1)
            throw std::runtime_error("Invalid CPU cost");
        auto& rows=out[a[0]];
        for(auto& v:rows) if(v.khz==c.khz) throw std::runtime_error("Duplicate CPU cost");
        rows.push_back(c);
    }
    if(out.empty()) throw std::runtime_error("Empty CPU costs");
    return out;
}
inline SwitchCosts load_switch_costs(const std::string& path,bool use_max) {
    std::ifstream f(path);
    std::string line;
    if(!std::getline(f,line) || split_csv(line)!=split_csv("from_khz,to_khz,samples,ready_p95_ms,ready_max_ms"))
        throw std::runtime_error("Invalid transition costs CSV: "+path);
    SwitchCosts out;
    while(std::getline(f,line)) {
        auto a=split_csv(line);
        if(a.empty()) continue;
        if(a.size()!=5) throw std::runtime_error("Invalid transition row");
        long from=positive_int(a[0]),to=positive_int(a[1]);
        double p95=number(a[3]),mx=number(a[4]);
        if(mx<p95 || (from==to && mx!=0) || (from!=to && (positive_int(a[2])<1 || p95<=0)))
            throw std::runtime_error("Invalid transition cost");
        if(!out.emplace(std::make_pair(from,to),use_max?mx:p95).second)
            throw std::runtime_error("Duplicate transition cost");
    }
    return out;
}
inline std::optional<double> switch_cost(const SwitchCosts& costs,long from,long to) {
    if(from==to) return 0.;
    auto it=costs.find({from,to});
    if(it==costs.end()) return std::nullopt;
    return it->second;
}
// Energy estimates include the calibrated release period; they are scores,
// not additive measurements of actual mixed-workload/transition energy.
// Hysteresis only retains an already-feasible frequency. Thermal constraints
// and deadline feasibility override dwell and savings thresholds.
inline DvfsDecision choose_dvfs(const std::vector<CpuCost>& costs,const SwitchCosts& switches,
                                long current,long ceiling,double slack,double margin,
                                double since_switch,double dwell,double min_saving,
                                bool ignore_transition=false,double pre_release_ms=0.) {
    std::vector<DvfsDecision> options;
    for(auto& c:costs) {
        if(c.khz>ceiling) continue;
        auto sw=switch_cost(switches,current,c.khz);
        if(!sw) continue; // no invented zero cost for an unprofiled transition
        double total=c.service_ms+(ignore_transition?0.:std::max(0.,*sw-pre_release_ms))+margin;
        options.push_back({c.khz,total,*sw,c.energy_mj,total<=slack,"energy"});
    }
    if(options.empty()) throw std::runtime_error("No calibrated frequency under thermal ceiling");
    auto faster=[](const auto& a,const auto& b) {
        return a.predicted_ms<b.predicted_ms || (a.predicted_ms==b.predicted_ms && a.energy_mj<b.energy_mj);
    };
    auto best=options.end(), stay=options.end();
    for(auto it=options.begin();it!=options.end();++it) {
        if(it->khz==current) stay=it;
        if(it->feasible && (best==options.end() || it->energy_mj<best->energy_mj ||
           (it->energy_mj==best->energy_mj && faster(*it,*best)))) best=it;
    }
    if(best==options.end()) {
        auto x=*std::min_element(options.begin(),options.end(),faster);
        x.reason="best_effort"; return x;
    }
    if(stay!=options.end() && stay->feasible && best->khz!=current &&
       (since_switch<dwell || (stay->energy_mj-best->energy_mj)/stay->energy_mj<min_saving)) {
        auto x=*stay; x.reason="hysteresis"; return x;
    }
    auto x=*best;
    if(x.khz==current) x.reason="stay";
    return x;
}
}
