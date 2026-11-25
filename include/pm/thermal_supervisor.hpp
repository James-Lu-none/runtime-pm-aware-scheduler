#pragma once
#include "pm/dvfs.hpp"
#include <deque>
namespace pm {
// Sampled soft ceiling, evaluated at task hints, not a kernel thermal trip.
// Absolute hot readings override dwell; recovery is deliberately gradual.
class ThermalSupervisor {
    std::vector<long> freqs_;
    double low_, high_, tau_, slope_limit_, dwell_;
    size_t index_;
    double last_ = -1e30, changed_ = -1e30, ema_ = 0., slope_ = 0.;
    unsigned rising_ = 0;
    std::deque<std::pair<double,double>> history_;
    const char* reason_ = "initial";
public:
    ThermalSupervisor(std::vector<long> freqs,double low,double high,
                      double tau=1.,double slope_limit=.5,double dwell=2.)
        :freqs_(std::move(freqs)),low_(low),high_(high),tau_(tau),
         slope_limit_(slope_limit),dwell_(dwell),index_(freqs_.empty()?0:freqs_.size()-1) {
        if(freqs_.empty()||!std::is_sorted(freqs_.begin(),freqs_.end())||
           freqs_.front()<=0||!std::isfinite(low_)||!std::isfinite(high_)||
           !std::isfinite(tau_)||!std::isfinite(slope_limit_)||!std::isfinite(dwell_)||
           low_>=high_||tau_<=0||slope_limit_<=0||dwell_<0)
            throw std::runtime_error("Invalid thermal supervisor configuration");
    }
    long update(double t,double raw) {
        if(!std::isfinite(t)||!std::isfinite(raw)||raw<0||raw>=75||
           (!history_.empty()&&t<=last_))
            throw std::runtime_error("Invalid thermal supervisor sample");
        const bool first=history_.empty();
        const double dt=first?0.:t-last_;
        if(first || dt>2.0) {
            // Do not extrapolate a slope over missing observations.
            ema_=raw; slope_=0; rising_=0; history_.clear();
        } else {
            ema_+=(-std::expm1(-dt/tau_))*(raw-ema_);
        }
        last_=t;
        history_.emplace_back(t,ema_);
        while(history_.size()>2 && history_[1].first<=t-2.)
            history_.pop_front();
        const double span=t-history_.front().first;
        slope_=span>=1.5?(ema_-history_.front().second)/span:0.;
        rising_=(span>=1.5 && ema_>=low_ && slope_>=slope_limit_)?rising_+1:0;
        reason_="hold";
        size_t next=index_;
        if(raw>=high_+1. || ema_>=high_+1.) {
            next=0; reason_="hot";
        } else if(raw>=high_ || ema_>=high_) {
            if(index_>0 && t-changed_>=dwell_) {next=index_-1;reason_="high";}
        } else if(rising_>=3 && index_>0 && t-changed_>=dwell_) {
            next=index_-1; reason_="rising"; rising_=0;
        } else if(raw<=low_ && ema_<=low_ && slope_<=0. &&
                  index_+1<freqs_.size() && t-changed_>=dwell_) {
            next=index_+1; reason_="recover";
        }
        if(next!=index_) {index_=next;changed_=t;}
        else if(next==0 && (raw>=high_+1.||ema_>=high_+1.)) reason_="hold_hot";
        return ceiling();
    }
    long ceiling() const {return freqs_[index_];}
    double ema() const {return ema_;}
    double slope() const {return slope_;}
    const char* reason() const {return reason_;}
};
}
