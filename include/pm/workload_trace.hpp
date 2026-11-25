#pragma once
#include "pm/dvfs.hpp"
#include <set>
namespace pm {
// Trace is owned by the replay harness. Only a revealed job is supplied to
// the DVFS selector; the selector never receives the trace or random seed.
struct ReplayJob {
    std::string model;
    double deadline_ms;
    int notice_token; // uniform integer in [0, 1000000], independent of workload
};
inline int hint_lead_ms(int bound,int token,bool mixed) {
    if(bound<0 || bound>1000000 || token<0 || token>1000000)
        throw std::runtime_error("Invalid hint lead/token");
    return mixed?static_cast<int>((static_cast<long long>(bound)*token)/1000000):bound;
}
inline std::vector<ReplayJob> load_replay(std::istream& f,const std::vector<std::string>& models,
                                          int jobs,int period) {
    std::string line;
    if(!std::getline(f,line) || split_csv(line)!=split_csv("job,release_ms,model,deadline_ms,notice_token"))
        throw std::runtime_error("Invalid replay trace header");
    std::set<std::string> allowed(models.begin(),models.end());
    std::vector<ReplayJob> out;
    while(std::getline(f,line)) {
        auto a=split_csv(line);
        if(a.size()!=5 || number(a[0])!=out.size() ||
           number(a[1])!=static_cast<double>(out.size())*period ||
           !allowed.count(a[2]))
            throw std::runtime_error("Invalid replay job identity, release or model");
        double deadline=number(a[3]), token=number(a[4]);
        if(deadline<=0 || deadline>period || token>1000000 || std::floor(token)!=token)
            throw std::runtime_error("Invalid replay deadline or notice token");
        out.push_back({a[2],deadline,static_cast<int>(token)});
        if(out.size()>static_cast<size_t>(jobs)) throw std::runtime_error("Replay too long");
    }
    if(out.size()!=static_cast<size_t>(jobs)) throw std::runtime_error("Replay job count mismatch");
    return out;
}
}
